"""Single-position Engine analysis shared by Web and Core callers."""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
import hashlib
import json
import os
import threading
from typing import Optional

import chess

from server import config
from server.core import engine
from server.core import tablebase
from server.core.evaluation import classify
from server.core.game_analysis import _signed_cp


_PIECE_VALUES = {
    chess.PAWN: 1,
    chess.KNIGHT: 3,
    chess.BISHOP: 3,
    chess.ROOK: 5,
    chess.QUEEN: 9,
}


@dataclass(frozen=True)
class StructuredEngineLine:
    """One raw Engine line without collapsing mate scores into display centipawns."""

    cp: int | None
    mate: int | None
    pv_uci: tuple[str, ...]
    win_percent: float


@dataclass(frozen=True)
class StructuredAnalysis:
    """Cache-aware, framework-neutral result for bounded domain adapters."""

    fen: str
    depth: int
    multipv: int
    lines: tuple[StructuredEngineLine, ...]
    engine_name: str
    engine_version: str
    cache_key: str
    cache_hit: bool
    engine_call_count: int


_LIVE_ANALYSIS_LIMIT = 32
_live_analysis_lock = threading.Lock()
_live_analyses: OrderedDict[str, StructuredAnalysis] = OrderedDict()


def remember_live_analysis(result: StructuredAnalysis) -> StructuredAnalysis:
    with _live_analysis_lock:
        _live_analyses[result.cache_key] = result
        _live_analyses.move_to_end(result.cache_key)
        while len(_live_analyses) > _LIVE_ANALYSIS_LIMIT:
            _live_analyses.popitem(last=False)
    return result


def get_live_analysis(cache_key: str) -> StructuredAnalysis | None:
    """Return a recent server-produced analysis without starting Stockfish."""

    with _live_analysis_lock:
        result = _live_analyses.get(cache_key)
        if result is not None:
            _live_analyses.move_to_end(cache_key)
        return result


def _engine_identity(raw_name: str) -> tuple[str, str]:
    """Keep the complete UCI name as version provenance without inventing a version."""
    cleaned = raw_name.strip() or "Stockfish"
    if cleaned.casefold().startswith("stockfish"):
        return "Stockfish", cleaned
    return cleaned, cleaned


def _engine_cache_metadata(
    fen: str,
    *,
    depth: int,
    multipv: int,
    engine_info: dict,
) -> tuple[str, bool]:
    identity = {
        "schema_version": 1,
        "fen": fen,
        "engine": str(engine_info.get("name") or "unknown"),
        "options": dict(engine_info.get("options") or {}),
        "depth": depth,
        "multipv": multipv,
    }
    encoded = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()
    path = os.path.join(config.DATA_DIR, "engine-cache", f"{digest}.json")
    return digest, bool(config.ENGINE_CACHE_ENABLED and os.path.isfile(path))


def structured_analysis(
    fen: str,
    *,
    depth: int = config.DEFAULT_DEPTH,
    multipv: int = 1,
) -> StructuredAnalysis:
    """Return raw cp/mate lines while reusing the process-wide Engine cache.

    ``engine_call_count`` counts UCI analysis requests that were not already present in the
    persistent position cache. The Engine's in-memory cache is intentionally private; with disk
    caching disabled a same-process memory hit is conservatively reported as an Engine call.
    """
    board = chess.Board(fen)
    multipv = max(1, int(multipv))
    depth = max(1, int(depth))
    if board.is_game_over(claim_draw=True):
        version = str(getattr(chess, "__version__", "unknown"))
        terminal_key = hashlib.sha256(
            json.dumps(
                {
                    "schema_version": 1,
                    "fen": fen,
                    "engine": "python-chess",
                    "version": version,
                    "depth": depth,
                    "multipv": multipv,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        return StructuredAnalysis(
            fen=fen,
            depth=depth,
            multipv=multipv,
            lines=(),
            engine_name="python-chess",
            engine_version=version,
            cache_key=terminal_key,
            cache_hit=False,
            engine_call_count=0,
        )
    info = engine.info()
    cache_key, cache_hit = _engine_cache_metadata(
        fen,
        depth=depth,
        multipv=multipv,
        engine_info=info,
    )
    raw_name = str(info.get("name") or "Stockfish")
    engine_name, engine_version = _engine_identity(raw_name)

    result = engine.analyse(fen, depth=depth, multipv=multipv)
    return StructuredAnalysis(
        fen=fen,
        depth=depth,
        multipv=multipv,
        lines=tuple(
            StructuredEngineLine(
                cp=line.cp,
                mate=line.mate,
                pv_uci=tuple(line.pv_uci),
                win_percent=line.win_percent,
            )
            for line in result.lines
        ),
        engine_name=engine_name,
        engine_version=engine_version,
        cache_key=cache_key,
        cache_hit=cache_hit,
        engine_call_count=0 if cache_hit else 1,
    )


def material_balance(board: chess.Board, color: chess.Color) -> int:
    """Net material in pawn-points from `color`'s perspective (+ = `color` is ahead)."""
    total = 0
    for piece_type, value in _PIECE_VALUES.items():
        total += value * len(board.pieces(piece_type, color))
        total -= value * len(board.pieces(piece_type, not color))
    return total


def eval_str(cp: int | None, mate: int | None) -> str:
    """Human-readable eval from the side-to-move perspective."""
    if mate is not None:
        return f"#{mate}" if mate > 0 else f"#-{abs(mate)}"
    pawns = (cp or 0) / 100.0
    return f"{pawns:+.2f}"


def eval_str_from_signed_cp(cp: int) -> str:
    """Like eval_str but takes a signed cp that may be a mate-equivalent magnitude."""
    if abs(cp) >= config.MATE_SCORE_CP:
        return "#" if cp > 0 else "#-"
    return f"{cp / 100.0:+.2f}"


def pv_to_san(board: chess.Board, pv_uci: list[str], max_plies: int = 12) -> list[str]:
    b = board.copy(stack=False)
    out: list[str] = []
    for uci in pv_uci[:max_plies]:
        try:
            mv = chess.Move.from_uci(uci)
            out.append(b.san(mv))
            b.push(mv)
        except (ValueError, AssertionError):
            break
    return out


def parse_move(board: chess.Board, move: str) -> chess.Move:
    """Accept either UCI (e2e4) or SAN (e4, Nf3) for the move argument."""
    try:
        mv = chess.Move.from_uci(move)
        if mv in board.legal_moves:
            return mv
    except ValueError:
        pass
    return board.parse_san(move)  # raises ValueError if illegal/ambiguous


def _settle_leaf(after_board: chess.Board, pv_uci: list[str], depth: int) -> chess.Board:
    """Play `pv_uci` out from `after_board`, then keep following best play until the position is
    QUIET, so material is counted at a settled point — not mid-exchange.

    The engine's PV ends at its search horizon, which can fall in the middle of a trade (e.g. the
    line stops on `BxN` before the recapture `…PxB`). Counting material there reports a phantom
    swing. We treat a leaf as non-quiet when the side to move is in check, or the last move was a
    capture whose destination the side to move can recapture on, and resolve it by continuing the
    engine's own best line (capped, so a perpetual sequence can't loop forever).
    """
    def push_pv(board: chess.Board, ucis: list[str]) -> bool:
        """Push a PV; return whether the LAST pushed move was a capture."""
        last_capture = False
        for u in ucis:
            try:
                mv = chess.Move.from_uci(u)
            except ValueError:
                break
            if mv not in board.legal_moves:
                break
            last_capture = board.is_capture(mv)
            board.push(mv)
        return last_capture

    leaf = after_board.copy(stack=False)
    last_capture = push_pv(leaf, pv_uci)
    for _ in range(3):  # cap: at most a few extensions to settle the trade
        pending_recapture = (
            last_capture
            and bool(leaf.move_stack)
            and bool(leaf.attackers(leaf.turn, leaf.peek().to_square))
        )
        if not (leaf.is_check() or pending_recapture):
            break  # quiet
        cont = engine.analyse(leaf.fen(), depth=depth, multipv=1).best
        if not cont.pv_uci:
            break
        last_capture = push_pv(leaf, cont.pv_uci)
    return leaf


def engine_line(
    fen: str,
    move: Optional[str] = None,
    depth: int = config.DEFAULT_DEPTH,
    multipv: int = 1,
    settle_material: bool = False,
    probe_tablebase: bool = False,
) -> dict:
    """Evaluate a position (optionally after a candidate move) and return engine lines.

    Without `move`, returns the best move and principal variation for `fen`. With `move`
    (UCI like "g1f3" or SAN like "Nf3"), also returns how that move is classified and the
    engine's refutation / expected continuation after it.

    `settle_material` (off by default to keep the interactive board snappy) adds net-material
    fields to the `move` dict, counted at a QUIESCENT leaf (see `_settle_leaf`) so a caller can
    tell "loses a piece" from "loses tempo" without inferring it from the SAN line. It costs a few
    extra engine calls when a trade is unresolved, so only the chat path enables it.

    `probe_tablebase` (also off by default — it does network I/O) attaches the EXACT tablebase
    result to the position (`result["tablebase"]`) and, when a `move` is given, to the move
    (`result["move"]["tablebase"]`, normalised to the MOVER's perspective) for <=7-man endgames.
    Only the chat / coach path enables it; the interactive board never pays the network cost.
    """
    board = chess.Board(fen)
    base = engine.analyse(fen, depth=depth, multipv=max(1, multipv))
    best = base.best
    best_line_san = pv_to_san(board, best.pv_uci)

    result: dict = {
        "fen": fen,
        "side_to_move": "white" if board.turn == chess.WHITE else "black",
        "depth": depth,
        "eval": eval_str(best.cp, best.mate),
        "eval_cp": round(_signed_cp(best.cp, best.mate)),
        "win_percent": round(best.win_percent, 1),
        "best_san": best_line_san[0] if best_line_san else None,
        "line_san": best_line_san,
        "line_uci": best.pv_uci[:12],
        "shapes": [],  # board annotations, populated below when a candidate move is supplied
    }

    # Exact endgame result for the position the side to move faces (<=7 men). Trumps the eval.
    if probe_tablebase and tablebase.count_men(board) <= tablebase.PIECE_LIMIT:
        tb = tablebase.probe(fen)
        if tb:
            result["tablebase"] = tb

    if multipv > 1:
        result["lines"] = [
            {
                "eval": eval_str(ln.cp, ln.mate),
                "win_percent": round(ln.win_percent, 1),
                "line_san": pv_to_san(board, ln.pv_uci),
                "line_uci": ln.pv_uci[:12],
            }
            for ln in base.lines
        ]

    if move:
        try:
            mv = parse_move(board, move)
        except ValueError as exc:
            result["error"] = f"Illegal or unparseable move '{move}': {exc}"
            return result

        move_san = board.san(mv)
        win_before = best.win_percent  # best available for the mover
        mover = board.turn
        material_before = material_balance(board, mover) if settle_material else None
        # Net material once the engine's expected continuation plays out, from the mover's
        # perspective. Lets callers say "this loses a piece" vs "this only loses tempo" instead
        # of inferring it from the SAN line. Filled (quiescently) from the refutation PV below;
        # None for a move that ends the game (mate/stalemate) or when settle_material is off.
        material_after_line: int | None = None
        # Exact tablebase verdict on the resulting position, normalised back to the MOVER's
        # perspective (the after-position has the opponent to move, so we flip it). None for a
        # terminal/over-7-men position or when probing is off.
        move_tablebase: dict | None = None
        after_board = board.copy(stack=False)
        after_board.push(mv)

        if after_board.is_game_over(claim_draw=True):
            outcome = after_board.outcome(claim_draw=True)
            if outcome and outcome.winner is None:
                win_after = 50.0
            elif outcome and outcome.winner == board.turn:
                win_after = 100.0  # the mover delivered mate
            else:
                win_after = 0.0
            refutation_san: list[str] = []
            refutation_uci: list[str] = []
            after_eval_cp = 0 if (outcome and outcome.winner is None) else (
                config.MATE_SCORE_CP if (outcome and outcome.winner == board.turn) else -config.MATE_SCORE_CP
            )
        else:
            after = engine.analyse(after_board.fen(), depth=depth, multipv=1).best
            win_after = 100.0 - after.win_percent  # back to the mover's perspective
            refutation_uci = after.pv_uci[:12]
            refutation_san = pv_to_san(after_board, after.pv_uci)
            after_eval_cp = -round(_signed_cp(after.cp, after.mate))
            # Walk the refutation line to a QUIESCENT leaf (resolving any trade left hanging at
            # the engine's horizon), then count material from the mover's side.
            if settle_material:
                leaf = _settle_leaf(after_board, after.pv_uci, depth)
                material_after_line = material_balance(leaf, mover)
            if probe_tablebase and tablebase.count_men(after_board) <= tablebase.PIECE_LIMIT:
                move_tablebase = tablebase.flip(tablebase.probe(after_board.fen()))

        # Board annotation (Phase 7): draw the punishing reply as a red arrow so the board
        # shows *why* the move is bad, not just the prose.
        if refutation_uci:
            fr = refutation_uci[0]
            result["shapes"] = [{"orig": fr[:2], "dest": fr[2:4], "brush": "red"}]

        is_best = best.pv_uci and mv.uci() == best.pv_uci[0]
        result["move"] = {
            "move_san": move_san,
            "move_uci": mv.uci(),
            "classification": classify(win_before, win_after, is_best=bool(is_best)),
            "win_before": round(win_before, 1),
            "win_after": round(win_after, 1),
            "win_swing": round(win_before - win_after, 1),
            "eval_after_cp": after_eval_cp,
            "eval_after": eval_str_from_signed_cp(after_eval_cp),
            "is_engine_best": bool(is_best),
            "better_move_san": result["best_san"],
            "refutation_line_san": refutation_san,
            "refutation_line_uci": refutation_uci,
            "material_before": material_before,
            "material_after_line": material_after_line,
            "material_delta": (
                material_after_line - material_before
                if material_after_line is not None
                else None
            ),
            "tablebase": move_tablebase,
        }

    return result
