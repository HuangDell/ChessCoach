"""Single-user background orchestration for two-stage full-game analysis."""
from __future__ import annotations

import threading
import time

from server import config
from server.core import analysis_cache
from server.core import game_identity
from server.core import history
from server.core import session as session_mod
from server.core.game_analysis import analyze_game as _analyze_game
from server.core.storage import store_analysis

_lock = threading.Lock()
_state: dict = {
    "status": "idle",
    "phase": "completed",
    "error": None,
    "game_id": None,
    "job_id": None,
    "token": 0,
    "done": 0,
    "total": 0,
    "critical_done": 0,
    "critical_total": 0,
    "eta_seconds": None,
    "total_games": 1,
    "done_games": 0,
    "current_game": 1,
}
_records: dict[str, dict] = {}
_MAX_RECORDS = 50


def _remember_locked() -> None:
    job_id = _state.get("job_id")
    if job_id:
        _records[job_id] = dict(_state)
    while len(_records) > _MAX_RECORDS:
        _records.pop(next(iter(_records)))


def _update_locked(**changes) -> None:
    _state.update(changes)
    _remember_locked()


def _new_job_locked(*, game_id: str | None, total_games: int) -> tuple[int, str]:
    previous_id = _state.get("job_id")
    if previous_id and _state.get("status") == "pending":
        previous = dict(_state)
        previous.update(status="error", phase="cancelled", error="Superseded by a newer analysis.")
        _records[previous_id] = previous
    token = int(_state.get("token") or 0) + 1
    job_id = f"analysis-{token}"
    _state.update(
        status="pending",
        phase="queued",
        error=None,
        game_id=game_id,
        job_id=job_id,
        token=token,
        done=0,
        total=0,
        critical_done=0,
        critical_total=0,
        eta_seconds=None,
        total_games=total_games,
        done_games=0,
        current_game=1,
    )
    _remember_locked()
    return token, job_id


def status() -> dict:
    with _lock:
        return dict(_state)


def job_status(job_id: str) -> dict | None:
    with _lock:
        record = _records.get(job_id)
        return dict(record) if record is not None else None


def _persist_artifact(game_id: str, sess) -> None:
    if not sess.engine_analysis:
        return
    try:
        store_analysis(game_id, sess.player, sess.engine_analysis)
    except (OSError, ValueError):
        pass


def _progress_reporter(token: int):
    phase_clock = {"phase": None, "started": time.monotonic()}

    def report(event: dict) -> None:
        phase = str(event.get("phase") or "scanning")
        done = int(event.get("done") or 0)
        total = int(event.get("total") or 0)
        now = time.monotonic()
        if phase != phase_clock["phase"]:
            phase_clock.update(phase=phase, started=now)
        elapsed = now - float(phase_clock["started"])
        eta = (elapsed / done) * (total - done) if done >= 2 and total > done else None
        with _lock:
            if token == _state["token"]:
                _update_locked(
                    phase=phase,
                    done=done,
                    total=total,
                    critical_done=int(event.get("critical_done") or 0),
                    critical_total=int(event.get("critical_total") or 0),
                    eta_seconds=eta,
                )

    return report


def _run(pgn: str, player: str, game_id: str, token: int) -> None:
    try:
        sess = _analyze_game(pgn, player=player, on_progress=_progress_reporter(token))
    except Exception as exc:
        with _lock:
            if token == _state["token"]:
                _update_locked(status="error", phase="failed", error=str(exc), eta_seconds=None)
        return

    with _lock:
        if token != _state["token"]:
            return
        session_mod.set_session(sess)
    analysis_cache.store(sess)
    _persist_artifact(game_id, sess)
    if config.HISTORY_ENABLED:
        try:
            history.record_game(sess)
        except Exception:
            pass
    with _lock:
        if token == _state["token"]:
            _update_locked(
                status="ready",
                phase="completed",
                error=None,
                done_games=1,
                eta_seconds=None,
            )


def start(pgn: str, player: str = "auto", *, game_id: str | None = None) -> dict:
    """Start one analysis, or synchronously restore the exact game/side/profile cache."""
    cached = analysis_cache.load(pgn, player)
    resolved_game_id = game_id or game_identity.game_id_from_pgn(pgn)
    with _lock:
        token, _job_id = _new_job_locked(game_id=resolved_game_id, total_games=1)
        if cached is not None:
            session_mod.set_session(cached)
            _persist_artifact(resolved_game_id, cached)
            if config.HISTORY_ENABLED:
                try:
                    history.record_game(cached)
                except Exception:
                    pass
            _update_locked(status="ready", phase="completed", done_games=1)
            return dict(_state)
    threading.Thread(
        target=_run,
        args=(pgn, player, resolved_game_id, token),
        name="chess-analyze",
        daemon=True,
    ).start()
    return status()


def _run_batch(
    games: list[str],
    sides: list[str],
    self_handle: str | None,
    platform: str | None,
    token: int,
) -> None:
    if self_handle and config.HISTORY_ENABLED:
        try:
            history.ensure_self_alias(self_handle, platform)
        except Exception:
            pass

    first_set = False
    completed = 0
    for i, (pgn, side) in enumerate(zip(games, sides)):
        game_id = game_identity.game_id_from_pgn(pgn)
        with _lock:
            if token != _state["token"]:
                return
            _update_locked(
                current_game=i + 1,
                game_id=game_id,
                phase="queued",
                done=0,
                total=0,
                critical_done=0,
                critical_total=0,
                eta_seconds=None,
            )

        sess = analysis_cache.load(pgn, side)
        if sess is None:
            try:
                sess = _analyze_game(pgn, player=side, on_progress=_progress_reporter(token))
            except Exception as exc:
                with _lock:
                    if token == _state["token"]:
                        _update_locked(error=str(exc), phase="failed")
                continue
            analysis_cache.store(sess)
        _persist_artifact(game_id, sess)

        with _lock:
            if token != _state["token"]:
                return
            if not first_set:
                session_mod.set_session(sess)
                first_set = True
        if config.HISTORY_ENABLED:
            try:
                history.record_game(sess)
            except Exception:
                pass
        completed += 1
        with _lock:
            if token == _state["token"]:
                _update_locked(done_games=completed)

    with _lock:
        if token == _state["token"]:
            if first_set:
                _update_locked(status="ready", phase="completed", eta_seconds=None)
            else:
                _update_locked(
                    status="error",
                    phase="failed",
                    error=_state.get("error") or "No game could be analyzed.",
                    eta_seconds=None,
                )


def start_batch(
    games: list[str],
    sides: list[str],
    *,
    self_handle: str | None = None,
    platform: str | None = None,
) -> dict:
    with _lock:
        token, _job_id = _new_job_locked(game_id=None, total_games=len(games))
    threading.Thread(
        target=_run_batch,
        args=(games, sides, self_handle, platform, token),
        name="chess-analyze-batch",
        daemon=True,
    ).start()
    return status()
