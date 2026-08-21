"""PGN -> ordered mistake list -> ReviewSession.

We analyse every position along the mainline exactly once (results are cached in the
engine pool), then derive each move's before/after win% from consecutive positions:

    win_before(my move at P)  = best win% at P            (I am to move at P)
    win_after (my move at P)  = 100 - best win% at P+1    (opponent is to move at P+1)

Terminal positions (checkmate/stalemate/draw) are scored directly without the engine.
"""
from __future__ import annotations

import io
import hashlib
import json
from dataclasses import dataclass
from typing import Callable

import chess
import chess.pgn

from server import config
from server.core import critical
from server.core import engine
from server.core import fact_extraction
from server.core import game_identity
from server.core.evaluation import (
    aggregate_accuracy,
    classify,
    classify_speed,
    move_accuracy,
    thresholds_for_elo,
    thresholds_for_speed,
    time_control_clock,
    win_percent_from_score,
)
from server.core.session import MoveReview, ReviewSession


@dataclass
class _PosEval:
    """Evaluation of a single position, from the side-to-move's perspective."""

    win_stm: float  # win% for the side to move
    cp_stm: float  # signed centipawns for the side to move (mate -> +/-MATE_SCORE_CP)
    raw_cp: int | None  # centipawns for side to move; None when this is a mate score
    raw_mate: int | None  # signed mate distance for side to move; None for a cp score
    best_pv_uci: list[str]  # principal variation (empty if terminal)
    is_terminal: bool
    terminal_winner: chess.Color | None = None
    terminal_draw: bool = False


def _signed_cp(cp: int | None, mate: int | None) -> float:
    if mate is not None:
        return float(config.MATE_SCORE_CP) if mate > 0 else float(-config.MATE_SCORE_CP)
    return float(cp if cp is not None else 0)


def _evaluate_position(board: chess.Board, *, depth: int) -> _PosEval:
    """Evaluate `board` from the side-to-move's perspective, handling terminal cases."""
    outcome = board.outcome(claim_draw=True)
    if outcome is not None:
        if outcome.winner is None:  # draw of any kind
            return _PosEval(
                win_stm=50.0,
                cp_stm=0.0,
                raw_cp=0,
                raw_mate=None,
                best_pv_uci=[],
                is_terminal=True,
                terminal_draw=True,
            )
        # There is a winner; the side to move is the one who is checkmated -> losing.
        side_to_move_won = outcome.winner == board.turn
        win = 100.0 if side_to_move_won else 0.0
        cp = float(config.MATE_SCORE_CP) if side_to_move_won else float(-config.MATE_SCORE_CP)
        return _PosEval(
            win_stm=win,
            cp_stm=cp,
            raw_cp=None,
            raw_mate=None,
            best_pv_uci=[],
            is_terminal=True,
            terminal_winner=outcome.winner,
        )

    res = engine.analyse(board.fen(), depth=depth, multipv=1)
    best = res.best
    return _PosEval(
        win_stm=win_percent_from_score(best.cp, best.mate),
        cp_stm=_signed_cp(best.cp, best.mate),
        raw_cp=best.cp,
        raw_mate=best.mate,
        best_pv_uci=list(best.pv_uci),
        is_terminal=False,
    )


def _color_name(color: chess.Color) -> str:
    return "white" if color == chess.WHITE else "black"


def _score_for(pe: _PosEval, side_to_move: chess.Color, pov: chess.Color) -> dict:
    """A score with an explicit POV. Mate and cp are never collapsed into one number."""
    pov_name = _color_name(pov)
    if pe.terminal_draw:
        return {"type": "cp", "value": 0, "pov": pov_name}
    if pe.is_terminal and pe.terminal_winner is not None:
        return {
            "type": "mate",
            "value": 0,
            "pov": pov_name,
            "winning": pe.terminal_winner == pov,
        }
    sign = 1 if side_to_move == pov else -1
    if pe.raw_mate is not None:
        return {"type": "mate", "value": sign * pe.raw_mate, "pov": pov_name}
    return {"type": "cp", "value": sign * int(pe.raw_cp or 0), "pov": pov_name}


def _line_score(cp: int | None, mate: int | None, side_to_move: chess.Color, pov: chess.Color) -> dict:
    sign = 1 if side_to_move == pov else -1
    if mate is not None:
        return {"type": "mate", "value": sign * mate, "pov": _color_name(pov)}
    return {"type": "cp", "value": sign * int(cp or 0), "pov": _color_name(pov)}


def _win_for(pe: _PosEval, side_to_move: chess.Color, pov: chess.Color) -> float:
    return pe.win_stm if side_to_move == pov else 100.0 - pe.win_stm


def _scores_for_position(
    pe: _PosEval,
    side_to_move: chess.Color,
    mover: chess.Color,
    review_color: chess.Color,
) -> dict:
    return {
        "white": _score_for(pe, side_to_move, chess.WHITE),
        "mover": _score_for(pe, side_to_move, mover),
        "review_side": _score_for(pe, side_to_move, review_color),
    }


def _win_povs(
    pe: _PosEval,
    side_to_move: chess.Color,
    mover: chess.Color,
    review_color: chess.Color,
) -> dict:
    white = _win_for(pe, side_to_move, chess.WHITE)
    return {
        "white": round(white, 1),
        "black": round(100.0 - white, 1),
        "mover": round(_win_for(pe, side_to_move, mover), 1),
        "review_side": round(_win_for(pe, side_to_move, review_color), 1),
    }


def _is_winning_mate(score: dict) -> bool:
    if score.get("type") != "mate":
        return False
    if "winning" in score:
        return bool(score["winning"])
    return int(score.get("value") or 0) > 0


def _is_losing_mate(score: dict) -> bool:
    if score.get("type") != "mate":
        return False
    if "winning" in score:
        return not bool(score["winning"])
    return int(score.get("value") or 0) < 0


def _pv_to_san(board: chess.Board, pv_uci: list[str], *, max_plies: int = 12) -> list[str]:
    """Convert a UCI principal variation to SAN by replaying on a copy of `board`."""
    b = board.copy(stack=False)
    sans: list[str] = []
    for uci in pv_uci[:max_plies]:
        try:
            move = chess.Move.from_uci(uci)
            sans.append(b.san(move))
            b.push(move)
        except (ValueError, AssertionError):
            break
    return sans


def resolve_player(headers: dict[str, str], player: str) -> str:
    """Resolve player='white'|'black'|'auto' to a concrete color."""
    p = (player or "auto").lower()
    if p in ("white", "black"):
        return p
    # auto: match any of my handles (CHESS_USERNAME + CHESS_ALIASES) against the PGN headers.
    mine = {config.USERNAME.lower().strip()} | {a for _, a in config.USERNAME_ALIASES}
    mine.discard("")
    if headers.get("White", "").lower().strip() in mine:
        return "white"
    if headers.get("Black", "").lower().strip() in mine:
        return "black"
    return "white"


# Lichess ratings run noticeably higher than chess.com / FIDE for the same player, so we pull
# them down to a common scale before mapping Elo -> thresholds. Rough and time-control-dependent;
# tune to taste. (chess.com is taken as the baseline at offset 0.)
_ELO_OFFSETS = {"lichess": -200, "chesscom": 0}

# Named sensitivity presets -> a representative normalized Elo.
_SENSITIVITY_ELO = {"casual": 1000.0, "default": 1500.0, "strong": 2000.0, "master": 2400.0}


def _detect_platform(headers: dict[str, str]) -> str | None:
    blob = " ".join(headers.get(k, "") for k in ("Site", "Link", "Event")).lower()
    if "lichess" in blob:
        return "lichess"
    if "chess.com" in blob or "chesscom" in blob:
        return "chesscom"
    return None


def _resolve_review_elo(
    headers: dict[str, str], me: str, elo: int | None, sensitivity: str | None
) -> tuple[float | None, str | None]:
    """Resolve the normalized review Elo + where it came from.

    Priority: explicit `elo` (taken as already-normalized) > named `sensitivity` > the user's
    configured skill (`config.PLAYER_ELO`, the Settings "Skill level") > the PGN's WhiteElo/BlackElo
    for the reviewed side (normalized by detected platform) > None (default).
    """
    if elo is not None:
        return float(elo), "explicit"
    if sensitivity and sensitivity.lower() in _SENSITIVITY_ELO:
        return _SENSITIVITY_ELO[sensitivity.lower()], f"sensitivity:{sensitivity.lower()}"
    if config.PLAYER_ELO is not None:
        return float(config.PLAYER_ELO), "settings"
    raw = headers.get("WhiteElo" if me == "white" else "BlackElo", "").strip()
    if raw.isdigit():
        platform = _detect_platform(headers)
        return float(int(raw) + _ELO_OFFSETS.get(platform, 0)), (platform or "pgn")
    return None, None


def _depth_for_elo(elo: float | None) -> int:
    """Deepen the sweep for stronger players so small win%-drop cutoffs aren't just noise."""
    base = config.SWEEP_DEPTH
    if elo is None:
        return base
    if elo >= 2300:
        return max(base, 20)
    if elo >= 1900:
        return max(base, 18)
    return base


def _depths_for_preset(scan_depth: int) -> tuple[int, int]:
    """Translate the user-facing speed preset into reproducible scan/deep depths."""
    preset = config.ANALYSIS_PRESET
    if preset == "fast":
        scan = max(10, scan_depth - 3)
        return scan, max(scan, config.DEEP_ANALYSIS_DEPTH - 4)
    if preset == "deep":
        scan = scan_depth + 2
        return scan, max(scan, config.DEEP_ANALYSIS_DEPTH + 4)
    return scan_depth, max(scan_depth, config.DEEP_ANALYSIS_DEPTH)


def _make_analysis_profile(
    *,
    scan_depth: int,
    deep_depth: int,
    thresholds: tuple[float, float, float],
    review_elo: float | None,
    speed: str,
) -> dict:
    profile = {
        "version": config.ANALYSIS_PROFILE_VERSION,
        "preset": config.ANALYSIS_PRESET,
        "scan": {"depth": scan_depth, "multipv": 1},
        "deep": {"depth": deep_depth, "multipv": config.DEEP_ANALYSIS_MULTIPV},
        "critical": {
            "minimum_target": config.CRITICAL_MIN,
            "maximum": config.CRITICAL_MAX,
            "thresholds": list(thresholds),
        },
        "facts": {
            "version": fact_extraction.FACTS_VERSION,
            "line_plies": config.FACT_LINE_PLIES,
        },
        "review": {"elo": review_elo, "speed": speed},
    }
    encoded = json.dumps(profile, sort_keys=True, separators=(",", ":")).encode("utf-8")
    profile["id"] = f"{config.ANALYSIS_PROFILE_VERSION}-{hashlib.sha256(encoded).hexdigest()[:12]}"
    return profile


def analysis_profile_for_headers(headers: dict[str, str], player: str) -> dict:
    """Current whole-game cache profile for a PGN and concrete/auto review side."""
    me = resolve_player(headers, player)
    review_elo, _elo_source = _resolve_review_elo(headers, me, None, None)
    speed = classify_speed(headers.get("TimeControl"), headers.get("Event"))
    thresholds = thresholds_for_speed(thresholds_for_elo(review_elo), speed)
    scan_depth, deep_depth = _depths_for_preset(_depth_for_elo(review_elo))
    return _make_analysis_profile(
        scan_depth=scan_depth,
        deep_depth=deep_depth,
        thresholds=thresholds,
        review_elo=review_elo,
        speed=speed,
    )


def analyze_game(
    pgn: str,
    player: str = "auto",
    *,
    depth: int | None = None,
    elo: int | None = None,
    sensitivity: str | None = None,
    on_progress: Callable[[dict], None] | None = None,
) -> ReviewSession:
    """Analyse a PGN and build a ReviewSession for `player`'s mistakes.

    Mistake thresholds adapt to skill: pass `elo` (normalized scale) or a named `sensitivity`
    ("casual"/"default"/"strong"/"master"), else the reviewed side's Elo is read from the PGN
    (normalized for the detected platform). Stronger -> smaller win%-drop cutoffs + deeper sweep.

    `on_progress(event)` reports scanning, critical selection, deep analysis and finalization.
    Best-effort: exceptions in the callback are swallowed so a reporter cannot break a review.
    """
    game = chess.pgn.read_game(io.StringIO(pgn))
    if game is None:
        raise ValueError("Could not parse a game from the provided PGN.")

    headers = dict(game.headers)
    me = resolve_player(headers, player)
    my_turn = chess.WHITE if me == "white" else chess.BLACK

    review_elo, elo_source = _resolve_review_elo(headers, me, elo, sensitivity)
    speed = classify_speed(headers.get("TimeControl"), headers.get("Event"))
    # Cutoffs adapt to BOTH skill (Elo) and mode: faster time controls are more forgiving,
    # slower ones stricter, with blitz as the unchanged anchor.
    thresholds = thresholds_for_speed(thresholds_for_elo(review_elo), speed)
    if depth is None:
        depth, deep_depth = _depths_for_preset(_depth_for_elo(review_elo))
    else:
        deep_depth = max(depth, config.DEEP_ANALYSIS_DEPTH)
    analysis_profile = _make_analysis_profile(
        scan_depth=depth,
        deep_depth=deep_depth,
        thresholds=thresholds,
        review_elo=review_elo,
        speed=speed,
    )

    # Replay the mainline, collecting (board_before, move) pairs plus each move's remaining
    # clock from [%clk] comments (None when the PGN has no clocks). We still ignore NAGs and
    # variations by only following the first variation (== the mainline).
    board = game.board()
    steps: list[tuple[chess.Board, chess.Move]] = []
    clocks: list[float | None] = []  # remaining seconds for the side that just moved, per ply
    node = game
    while node.variations:
        node = node.variations[0]
        # Keep the move stack so claimable repetition draws are detectable by board.outcome().
        steps.append((board.copy(stack=True), node.move))
        clocks.append(node.clock())
        board.push(node.move)
    final_board = board

    # Time control base/increment, used to turn remaining-clock readings into time *spent* per
    # move. None when the PGN has no sudden-death clock (correspondence, "-", etc.).
    tc = time_control_clock(headers.get("TimeControl"))
    tc_base, tc_increment = tc if tc else (None, 0.0)

    # Evaluate every position once: the position before each move, plus the final one. This is
    # the slow part of the sweep (one fixed-depth engine call per ply ⇒ roughly linear time), so
    # we report progress here for the web board's progress bar.
    pos_evals: list[_PosEval] = []
    total_positions = len(steps) + 1

    def _report(
        phase: str,
        done: int,
        total: int,
        *,
        critical_done: int = 0,
        critical_total: int = 0,
    ) -> None:
        if on_progress is None:
            return
        try:
            on_progress(
                {
                    "phase": phase,
                    "done": done,
                    "total": total,
                    "critical_done": critical_done,
                    "critical_total": critical_total,
                }
            )
        except Exception:  # pragma: no cover - a broken reporter must never break a review
            pass

    _report("scanning", 0, total_positions)
    for before, _move in steps:
        pos_evals.append(_evaluate_position(before, depth=depth))
        _report("scanning", len(pos_evals), total_positions)
    pos_evals.append(_evaluate_position(final_board, depth=depth))
    _report("scanning", len(pos_evals), total_positions)

    all_my_moves: list[MoveReview] = []
    stage_moves: list[dict] = []
    white_accs: list[float] = []
    black_accs: list[float] = []

    for i, (before, move) in enumerate(steps):
        mover_is_white = before.turn == chess.WHITE
        mover = before.turn
        eval_at = pos_evals[i]
        eval_next = pos_evals[i + 1]
        after_board = before.copy(stack=True)
        after_board.push(move)

        # From the mover's perspective.
        win_before = eval_at.win_stm
        win_after = 100.0 - eval_next.win_stm
        cp_before = eval_at.cp_stm
        cp_after = -eval_next.cp_stm
        acc = move_accuracy(win_before, win_after)

        best_uci = eval_at.best_pv_uci[0] if eval_at.best_pv_uci else move.uci()
        is_best = move.uci() == best_uci
        classification = classify(win_before, win_after, is_best=is_best, thresholds=thresholds)
        best_line_san = _pv_to_san(before, eval_at.best_pv_uci)
        best_move_san = best_line_san[0] if best_line_san else before.san(move)

        scores_before = _scores_for_position(eval_at, before.turn, mover, my_turn)
        scores_after = _scores_for_position(eval_next, after_board.turn, mover, my_turn)
        wins_before = _win_povs(eval_at, before.turn, mover, my_turn)
        wins_after = _win_povs(eval_next, after_board.turn, mover, my_turn)
        cp_loss: float | None = None
        if eval_at.raw_cp is not None and eval_next.raw_cp is not None:
            cp_loss = round(max(0.0, float(eval_at.raw_cp + eval_next.raw_cp)), 1)

        signals: list[str] = []
        before_mover_score = scores_before["mover"]
        after_mover_score = scores_after["mover"]
        if _is_winning_mate(before_mover_score) and not _is_winning_mate(after_mover_score):
            signals.append("missed_mate")
        if not _is_losing_mate(before_mover_score) and _is_losing_mate(after_mover_score):
            signals.append("allowed_mate")
        if win_before >= 75.0 and win_after < 60.0:
            signals.append("missed_win")
        if win_before >= 65.0 and win_after < 60.0:
            signals.append("winning_to_equal")
        if 40.0 <= win_before <= 60.0 and win_after < 35.0:
            signals.append("equal_to_losing")

        stage_moves.append(
            {
                "ply": i + 1,
                "move_number": before.fullmove_number,
                "side": _color_name(mover),
                "fen_before": before.fen(),
                "fen_after": after_board.fen(),
                "played_move": {"uci": move.uci(), "san": before.san(move)},
                "eval_before": scores_before["white"],
                "eval_after": scores_after["white"],
                "scores": {"before": scores_before, "after": scores_after},
                "win_percent_before": wins_before,
                "win_percent_after": wins_after,
                "win_percent_loss": round(max(0.0, win_before - win_after), 1),
                "centipawn_loss": cp_loss,
                "classification": classification,
                "best_move": {"uci": best_uci, "san": best_move_san},
                "best_pv": {
                    "uci": eval_at.best_pv_uci[:12],
                    "san": best_line_san,
                },
                "signals": signals,
                "terminal_after": eval_next.is_terminal,
                "clock_seconds": clocks[i],
            }
        )

        if mover_is_white:
            white_accs.append(acc)
        else:
            black_accs.append(acc)

        if before.turn != my_turn:
            continue  # only build full reviews for my moves

        # Engine-grounded explanation for flagged moves. Uses only data already computed
        # in the sweep (eval_next is the cached eval of the position after the played move),
        # so this adds no engine calls and no LLM calls.
        comment = ""
        if classification in ("inaccuracy", "mistake", "blunder"):
            followup_san = _pv_to_san(after_board, eval_next.best_pv_uci, max_plies=6)
            comment = _mistake_comment(
                round(win_before, 1),
                round(win_after, 1),
                best_move_san,
                best_line_san,
                followup_san,
            )

        # Time spent on this move = clock_before - clock_after + increment. clock_before is my
        # remaining time after my *previous* move (two plies back), or the base for my first move.
        # Needs [%clk] data; left None otherwise. Clamp to >= 0 to absorb clock-reading noise.
        seconds_spent: float | None = None
        if clocks[i] is not None:
            prev_clock = clocks[i - 2] if i >= 2 else tc_base
            if prev_clock is not None:
                spent = prev_clock - clocks[i] + tc_increment
                seconds_spent = round(spent, 1) if spent >= 0 else None

        review = MoveReview(
            ply=i + 1,
            move_number=before.fullmove_number,
            color="white" if mover_is_white else "black",
            move_san=before.san(move),
            move_uci=move.uci(),
            fen_before=before.fen(),
            fen_after=_fen_after(before, move),
            eval_before=round(cp_before, 1),
            eval_after=round(cp_after, 1),
            win_before=round(win_before, 1),
            win_after=round(win_after, 1),
            win_swing=round(win_before - win_after, 1),
            classification=classification,
            best_move_san=best_move_san,
            best_line_uci=eval_at.best_pv_uci[:12],
            best_line_san=best_line_san,
            accuracy=round(acc, 1),
            comment=comment,
            clock_after=clocks[i],
            opp_clock=clocks[i - 1] if i >= 1 else None,
            seconds_spent=seconds_spent,
        )
        all_my_moves.append(review)

    mistakes = [
        m for m in all_my_moves if m.classification in ("inaccuracy", "mistake", "blunder")
    ]

    _report("selecting_critical", 0, len(stage_moves))
    selected = critical.select_critical_moves(stage_moves, me, thresholds)
    _report("selecting_critical", len(stage_moves), len(stage_moves))

    deep_depth = int(analysis_profile["deep"]["depth"])
    deep_total = len(selected)
    critical_positions: list[dict] = []
    _report("deep_analysis", 0, deep_total, critical_done=0, critical_total=deep_total)
    for done, stage_move in enumerate(selected, start=1):
        step_index = int(stage_move["ply"]) - 1
        before, played = steps[step_index]
        critical_positions.append(
            _deep_critical(
                stage_move,
                before,
                played,
                review_color=my_turn,
                depth=deep_depth,
                multipv=config.DEEP_ANALYSIS_MULTIPV,
            )
        )
        _report(
            "deep_analysis",
            done,
            deep_total,
            critical_done=done,
            critical_total=deep_total,
        )

    _report(
        "extracting_facts",
        0,
        deep_total,
        critical_done=0,
        critical_total=deep_total,
    )
    for done, position in enumerate(critical_positions, start=1):
        position["facts"] = fact_extraction.extract_facts(
            position, line_plies=config.FACT_LINE_PLIES
        )
        _report(
            "extracting_facts",
            done,
            deep_total,
            critical_done=done,
            critical_total=deep_total,
        )
    timeline = _build_timeline(steps, pos_evals, final_board, all_my_moves, mistakes, my_turn)
    initial_fen = game.board().fen()
    game_id = game_identity.game_id_for_moves(
        [move.uci() for _before, move in steps],
        game_identity.setup_fen(headers, initial_fen),
    )
    engine_info = engine.info()
    engine_analysis = {
        "schema_version": 2,
        "game_id": game_id,
        "review_side": me,
        "profile": analysis_profile,
        "cache_key": f"{game_id}:{me}:{analysis_profile['id']}",
        "engine": {
            "name": engine_info["name"],
            "options": engine_info["options"],
        },
        "headers": headers,
        "result": headers.get("Result", "*"),
        "summary": {
            "positions_scanned": len(pos_evals),
            "plies": len(stage_moves),
            "critical_positions": len(critical_positions),
            "fact_positions": sum(1 for position in critical_positions if position.get("facts")),
            "reviewed_moves": len(all_my_moves),
            "classifications": {
                label: sum(1 for move in all_my_moves if move.classification == label)
                for label in ("best", "good", "inaccuracy", "mistake", "blunder")
            },
        },
        "moves": stage_moves,
        "critical_positions": critical_positions,
    }

    session = ReviewSession(
        pgn=pgn,
        player=me,
        headers=headers,
        result=headers.get("Result", "*"),
        speed=speed,
        accuracy_white=round(aggregate_accuracy(white_accs), 1),
        accuracy_black=round(aggregate_accuracy(black_accs), 1),
        all_moves=all_my_moves,
        mistakes=mistakes,
        current_index=0,
        timeline=timeline,
        review_elo=review_elo,
        elo_source=elo_source,
        thresholds=list(thresholds),
        sweep_depth=depth,
        engine_analysis=engine_analysis,
    )
    return session


def _line_win_percent(
    cp: int | None,
    mate: int | None,
    side_to_move: chess.Color,
    pov: chess.Color,
) -> float:
    win = win_percent_from_score(cp, mate)
    return round(win if side_to_move == pov else 100.0 - win, 1)


def _candidate(
    line: engine.EngineLine,
    board: chess.Board,
    review_color: chess.Color,
    rank: int,
) -> dict | None:
    if not line.pv_uci:
        return None
    try:
        first = chess.Move.from_uci(line.pv_uci[0])
        move_san = board.san(first)
    except (ValueError, AssertionError):
        return None
    mover = board.turn
    san_line = _pv_to_san(board, line.pv_uci)
    scores = {
        "white": _line_score(line.cp, line.mate, board.turn, chess.WHITE),
        "mover": _line_score(line.cp, line.mate, board.turn, mover),
        "review_side": _line_score(line.cp, line.mate, board.turn, review_color),
    }
    white_win = _line_win_percent(line.cp, line.mate, board.turn, chess.WHITE)
    return {
        "rank": rank,
        "move": {"uci": first.uci(), "san": move_san},
        "eval": scores["white"],
        "scores": scores,
        "win_percent": {
            "white": white_win,
            "black": round(100.0 - white_win, 1),
            "mover": _line_win_percent(line.cp, line.mate, board.turn, mover),
            "review_side": _line_win_percent(line.cp, line.mate, board.turn, review_color),
        },
        "line": {"uci": line.pv_uci[:12], "san": san_line},
    }


def _deep_critical(
    stage_move: dict,
    before: chess.Board,
    played: chess.Move,
    *,
    review_color: chess.Color,
    depth: int,
    multipv: int,
) -> dict:
    """Deep MultiPV analysis plus a separate best-response search after the played move."""
    result = engine.analyse(before.fen(), depth=depth, multipv=multipv)
    candidates = [
        item
        for rank, line in enumerate(result.lines, start=1)
        if (item := _candidate(line, before, review_color, rank)) is not None
    ]
    top_win = candidates[0]["win_percent"]["mover"] if candidates else 50.0
    top_score = candidates[0]["scores"]["mover"] if candidates else None
    for candidate in candidates:
        candidate["win_gap_from_best"] = round(
            max(0.0, top_win - candidate["win_percent"]["mover"]), 1
        )
        score = candidate["scores"]["mover"]
        candidate["centipawn_gap_from_best"] = (
            max(0, int(top_score["value"]) - int(score["value"]))
            if top_score
            and top_score.get("type") == "cp"
            and score.get("type") == "cp"
            else None
        )

    after = before.copy(stack=True)
    played_san = before.san(played)
    after.push(played)
    if after.is_game_over(claim_draw=True):
        played_eval = _evaluate_position(after, depth=depth)
    else:
        after_line = engine.analyse(after.fen(), depth=depth, multipv=1).best
        played_eval = _PosEval(
            win_stm=win_percent_from_score(after_line.cp, after_line.mate),
            cp_stm=_signed_cp(after_line.cp, after_line.mate),
            raw_cp=after_line.cp,
            raw_mate=after_line.mate,
            best_pv_uci=list(after_line.pv_uci),
            is_terminal=False,
        )

    played_scores = _scores_for_position(played_eval, after.turn, before.turn, review_color)
    response_san = _pv_to_san(after, played_eval.best_pv_uci)
    played_line_uci = [played.uci(), *played_eval.best_pv_uci[:11]]
    played_line_san = [played_san, *response_san]
    played_rank = next(
        (candidate["rank"] for candidate in candidates if candidate["move"]["uci"] == played.uci()),
        None,
    )

    second_gap = candidates[1]["win_gap_from_best"] if len(candidates) > 1 else 100.0
    forced_mate = bool(top_score and _is_winning_mate(top_score))
    if forced_mate:
        criticality = "forced_mate"
    elif len(list(before.legal_moves)) == 1 or second_gap >= 12.0:
        criticality = "only_move"
    else:
        criticality = "critical"

    return {
        "critical_id": f"ply-{stage_move['ply']}",
        "priority": stage_move.get("critical_priority"),
        "critical_score": stage_move.get("critical_score"),
        "ply": stage_move["ply"],
        "move_number": stage_move["move_number"],
        "side": stage_move["side"],
        "fen_before": before.fen(),
        "fen_after": after.fen(),
        "played_move": {"uci": played.uci(), "san": played_san},
        "classification": stage_move["classification"],
        "eval_before": stage_move["eval_before"],
        "eval_after": stage_move["eval_after"],
        "scores": stage_move["scores"],
        "win_loss": stage_move["win_percent_loss"],
        "centipawn_loss": stage_move["centipawn_loss"],
        "signals": stage_move["signals"],
        "played_move_rank": played_rank,
        "played_move_in_multipv": played_rank is not None,
        "played_move_eval": played_scores["white"],
        "played_move_scores": played_scores,
        "opponent_best_reply": (
            {"uci": played_eval.best_pv_uci[0], "san": response_san[0]}
            if played_eval.best_pv_uci and response_san
            else None
        ),
        "played_line": {"uci": played_line_uci, "san": played_line_san},
        "best_line": candidates[0]["line"] if candidates else {"uci": [], "san": []},
        "candidates": candidates,
        "criticality": criticality,
        "forced_mate": forced_mate,
        "deep_depth": depth,
        "multipv": multipv,
    }


def _win_white(pe: "_PosEval", turn: chess.Color) -> float:
    """Win% from White's perspective, given whose move it is at that position."""
    return pe.win_stm if turn == chess.WHITE else 100.0 - pe.win_stm


def _build_timeline(
    steps: list[tuple[chess.Board, chess.Move]],
    pos_evals: list["_PosEval"],
    final_board: chess.Board,
    all_my_moves: list[MoveReview],
    mistakes: list[MoveReview],
    my_turn: chess.Color,
) -> list[dict]:
    """One entry per position (node 0..N). Each non-final node carries its OUTGOING move,
    the engine's best move there, and (for the player's moves) the classification."""
    cls_by_ply = {m.ply: m.classification for m in all_my_moves}
    mistake_index_by_ply = {m.ply: i for i, m in enumerate(mistakes)}

    nodes: list[dict] = []
    for k in range(len(steps) + 1):
        is_final = k == len(steps)
        board = final_board if is_final else steps[k][0]
        turn = board.turn
        node: dict = {
            "node": k,
            "fen": board.fen(),
            "win_white": round(_win_white(pos_evals[k], turn), 1),
            "color": "white" if turn == chess.WHITE else "black",
            "move_number": board.fullmove_number,
        }
        if not is_final:
            before, move = steps[k]
            eval_at = pos_evals[k]
            best_uci = eval_at.best_pv_uci[0] if eval_at.best_pv_uci else None
            ply = k + 1
            node.update(
                {
                    "ply": ply,
                    "move_san": before.san(move),
                    "move_uci": move.uci(),
                    "best_uci": best_uci,
                    "best_san": before.san(chess.Move.from_uci(best_uci)) if best_uci else None,
                    "is_my_move": before.turn == my_turn,
                    "classification": cls_by_ply.get(ply),
                    "mistake_index": mistake_index_by_ply.get(ply),
                }
            )
        nodes.append(node)
    return nodes


def _mistake_comment(
    win_before: float,
    win_after: float,
    best_move_san: str,
    best_line_san: list[str],
    followup_san: list[str],
) -> str:
    """Concrete written explanation of a mistake, stitched from engine data we already have.

    Kept terse and free of the verdict/swing (the header already states those) — this block is
    just the engine substance: the win-chance swing, the better move + its line, and the
    refutation the played move runs into.
    """
    parts = [f"Win chance {win_before}% → {win_after}%."]
    if best_move_san:
        cont = " ".join(best_line_san[1:5])  # best_move_san is best_line_san[0]; don't repeat it
        parts.append(f"Better was {best_move_san}" + (f", then {cont}." if cont else "."))
    if followup_san:
        parts.append(f"Played line: {' '.join(followup_san)}.")
    return " ".join(parts)


def _fen_after(before: chess.Board, move: chess.Move) -> str:
    b = before.copy(stack=False)
    b.push(move)
    return b.fen()
