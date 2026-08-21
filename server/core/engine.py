"""Stockfish engine pool with reproducible, cached, fixed-depth analysis.

Design notes:
- Engine processes are *reused*, never spawned per call (a bounded pool guarded by a
  lock). Spawning Stockfish per request is the main performance trap.
- Analysis is at fixed depth so results are reproducible, and cached by
  (fen, depth, multipv) so repeat calls are free and deterministic.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
from dataclasses import dataclass, field
from queue import Queue

import chess
import chess.engine

from server import config
from server.core.evaluation import win_percent_from_score


@dataclass
class EngineLine:
    """One principal variation from the engine, side-to-move relative."""

    cp: int | None  # centipawns (None if mate)
    mate: int | None  # mate-in-N (None if cp)
    pv_uci: list[str] = field(default_factory=list)

    @property
    def win_percent(self) -> float:
        return win_percent_from_score(self.cp, self.mate)


@dataclass
class AnalysisResult:
    """Result of analysing one FEN. lines[0] is the best line."""

    fen: str
    depth: int
    lines: list[EngineLine]

    @property
    def best(self) -> EngineLine:
        return self.lines[0]


class _EnginePool:
    """A tiny bounded pool of reusable SimpleEngine processes."""

    def __init__(self) -> None:
        self._pool: Queue[chess.engine.SimpleEngine] = Queue()
        self._lock = threading.Lock()
        self._started = False
        self._cache: dict[tuple[str, str, int, int], AnalysisResult] = {}
        self._engine_name = "unknown"

    def _spawn_one(self) -> chess.engine.SimpleEngine:
        """Start and configure one Stockfish process."""
        try:
            eng = chess.engine.SimpleEngine.popen_uci(config.STOCKFISH_PATH)
        except FileNotFoundError as exc:
            raise RuntimeError(config.stockfish_install_hint()) from exc
        eng.configure({"Threads": config.ENGINE_THREADS, "Hash": config.ENGINE_HASH_MB})
        name = str(eng.id.get("name") or "Stockfish").strip()
        if name:
            self._engine_name = name
        return eng

    def info(self) -> dict:
        self._ensure_started()
        return {
            "name": self._engine_name,
            "path": os.path.realpath(config.STOCKFISH_PATH),
            "options": {
                "Threads": config.ENGINE_THREADS,
                "Hash": config.ENGINE_HASH_MB,
            },
        }

    def _disk_key(self, fen: str, depth: int, multipv: int) -> tuple[str, dict]:
        identity = {
            "schema_version": 1,
            "fen": fen,
            "engine": self._engine_name,
            "options": {
                "Threads": config.ENGINE_THREADS,
                "Hash": config.ENGINE_HASH_MB,
            },
            "depth": depth,
            "multipv": multipv,
        }
        encoded = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest(), identity

    def _load_disk(self, fen: str, depth: int, multipv: int) -> AnalysisResult | None:
        if not config.ENGINE_CACHE_ENABLED:
            return None
        digest, identity = self._disk_key(fen, depth, multipv)
        path = os.path.join(config.DATA_DIR, "engine-cache", f"{digest}.json")
        try:
            with open(path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
            if payload.get("key") != identity:
                return None
            result = payload["result"]
            lines = [
                EngineLine(
                    cp=line.get("cp"),
                    mate=line.get("mate"),
                    pv_uci=list(line.get("pv_uci") or []),
                )
                for line in result["lines"]
            ]
            return AnalysisResult(fen=fen, depth=depth, lines=lines) if lines else None
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None

    def _store_disk(self, result: AnalysisResult, multipv: int) -> None:
        if not config.ENGINE_CACHE_ENABLED:
            return
        digest, identity = self._disk_key(result.fen, result.depth, multipv)
        directory = os.path.join(config.DATA_DIR, "engine-cache")
        path = os.path.join(directory, f"{digest}.json")
        payload = {
            "key": identity,
            "result": {
                "fen": result.fen,
                "depth": result.depth,
                "lines": [
                    {"cp": line.cp, "mate": line.mate, "pv_uci": line.pv_uci}
                    for line in result.lines
                ],
            },
        }
        try:
            os.makedirs(directory, exist_ok=True)
            tmp = f"{path}.{threading.get_ident()}.tmp"
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, separators=(",", ":"))
            os.replace(tmp, path)
        except OSError:
            pass

    def _ensure_started(self) -> None:
        if self._started:
            return
        with self._lock:
            if self._started:
                return
            for _ in range(max(1, config.ENGINE_POOL_SIZE)):
                self._pool.put(self._spawn_one())
            self._started = True

    def analyse(
        self,
        fen: str,
        *,
        depth: int = config.DEFAULT_DEPTH,
        multipv: int = 1,
    ) -> AnalysisResult:
        multipv = max(1, multipv)
        # Normalise FEN for stable cache keys (en passant / move counters matter to
        # the engine, so keep the full FEN as-is).
        self._ensure_started()
        key = (self._engine_name, fen, depth, multipv)
        cached = self._cache.get(key)
        if cached is not None:
            return cached

        cached = self._load_disk(fen, depth, multipv)
        if cached is not None:
            self._cache[key] = cached
            return cached

        board = chess.Board(fen)
        # A pooled engine whose process has died (crash, OOM, a stray `pkill stockfish`)
        # raises EngineTerminatedError on use. If we just put it back, it poisons every
        # later call. So on an engine-level failure we discard the broken process, spawn a
        # fresh one, and retry once — the pool self-heals instead of getting stuck.
        # Retry enough times to cycle past every engine in the pool, in case more than one
        # process died at once (e.g. the whole pool was killed).
        last_exc: chess.engine.EngineError | None = None
        for attempt in range(max(1, config.ENGINE_POOL_SIZE) + 1):
            eng = self._pool.get()
            try:
                infos = eng.analyse(
                    board, chess.engine.Limit(depth=depth), multipv=multipv
                )
            except chess.engine.EngineError as exc:
                last_exc = exc
                try:
                    eng.quit()  # best-effort; the process is likely already gone
                except chess.engine.EngineError:
                    pass
                self._pool.put(self._spawn_one())  # replace, keep the pool size constant
                continue
            try:
                if isinstance(infos, dict):  # multipv=1 may return a single dict
                    infos = [infos]
                lines: list[EngineLine] = []
                for info in infos:
                    score = info["score"].pov(board.turn)  # side-to-move relative
                    lines.append(
                        EngineLine(
                            cp=score.score(),  # None if mate
                            mate=score.mate(),  # None if cp
                            pv_uci=[m.uci() for m in info.get("pv", [])],
                        )
                    )
                result = AnalysisResult(fen=fen, depth=depth, lines=lines)
            finally:
                self._pool.put(eng)
            self._cache[key] = result
            self._store_disk(result, multipv)
            return result

        raise RuntimeError(f"Stockfish engine failed: {last_exc}") from last_exc

    def shutdown(self) -> None:
        with self._lock:
            while not self._pool.empty():
                eng = self._pool.get()
                try:
                    eng.quit()
                except chess.engine.EngineError:
                    pass
            self._started = False


# Process-wide singleton pool.
_POOL = _EnginePool()


def analyse(fen: str, *, depth: int = config.DEFAULT_DEPTH, multipv: int = 1) -> AnalysisResult:
    """Analyse a FEN at fixed depth. Cached and reproducible."""
    return _POOL.analyse(fen, depth=depth, multipv=multipv)


def info() -> dict:
    """Engine identity and relevant UCI options used in persistent cache keys."""
    return _POOL.info()


def shutdown() -> None:
    """Quit all engine processes. Call on server shutdown."""
    _POOL.shutdown()


def restart() -> None:
    """Quit engines and drop cached evals so the next analyse() respawns with the current
    config.STOCKFISH_PATH. Used when the engine path is changed at runtime via Settings."""
    with _POOL._lock:
        _POOL._cache.clear()
    _POOL.shutdown()
