"""Shared solve orchestration for the puzzle trainer.

The web routes (`routes_puzzles.py`) and the MCP tools (`mcp_server.py`) both drive puzzles through
this one module, so "everything the board does, a tool can do" — they mutate the *same*
`puzzle_session` singleton and apply Glicko exactly the same way. The routes wrap the returned dicts
in `JSONResponse`; the MCP tools return them straight to Claude Code. Keep the logic here HTTP-free
(plain dicts, no FastAPI) so both callers stay thin.

`score_attempt` is the single source of truth for the "does this attempt move the rating?" rule (the
RD-gate + no-hints rule); `apply_solver_moves` runs the per-ply validation loop for a curated
(Lichess) puzzle; `apply_mistake_move` does the engine-threshold check for a "from your games"
position. All best-effort — a puzzle bug must never break the board.
"""
from __future__ import annotations

from typing import Optional

import chess

from .. import config
from . import lines
from .learning.workflows import LearningProjectionError, finalize_puzzle_attempt
from . import puzzle_mistakes
from . import puzzle_rating
from . import puzzle_session
from . import puzzles as puzzles_mod
from . import training


def discard_deleted_personal_puzzle(exc: training.TrainingGameDeletedError) -> dict:
    """Clear a stale game-backed puzzle and return an HTTP-agnostic actionable error."""
    puzzle_session.clear_current()
    return {"error": {"code": exc.code, "message": str(exc)}}


def to_uci(fen: str, token: str) -> Optional[str]:
    """Normalise a move `token` (UCI like 'g1f3' or SAN like 'Nf3') to UCI, given the board `fen`.

    Returns the UCI string when the move is legal in that position, else None (illegal/unparseable).
    Lets the MCP tool accept either notation the way `get_engine_line` already does.
    """
    token = (token or "").strip()
    if not token:
        return None
    try:
        board = chess.Board(fen)
    except ValueError:
        return None
    # Try UCI first (the puzzle solution is stored in UCI, so this is the common path).
    try:
        mv = chess.Move.from_uci(token)
        if mv in board.legal_moves:
            return mv.uci()
    except ValueError:
        pass
    # Fall back to SAN ("Nf3", "exd5", "O-O", "e8=Q").
    try:
        return board.parse_san(token).uci()
    except ValueError:
        return None


def score_attempt(
    prog,
    *,
    score: float,
    hinted_or_given: bool,
    outcome: str | None = None,
    source: str = "lichess",
    selected_move: str | None = None,
) -> dict:
    """Apply one terminal outcome to the Glicko state at most once. Returns a rating summary.

    The single rule for whether an attempt is *rated*: the puzzle's RD is established
    (`< PUZZLE_MAX_RD`), no hints were used, and it wasn't a hint/give-up reveal. The durable
    attempt source is committed before rating state, and `prog.scored` guards both effects.
    """
    with puzzle_session.transition():
        with prog.finalize_lock:
            return _score_attempt_locked(
                prog,
                score=score,
                hinted_or_given=hinted_or_given,
                outcome=outcome,
                source=source,
                selected_move=selected_move,
            )


def _score_attempt_locked(
    prog,
    *,
    score: float,
    hinted_or_given: bool,
    outcome: str | None,
    source: str,
    selected_move: str | None,
) -> dict:
    if prog.scored:
        return {}

    puzzle = prog.puzzle
    resolved_outcome = outcome or ("success" if score >= 1.0 else "failure")
    projection_error: LearningProjectionError | None = None
    try:
        persisted = finalize_puzzle_attempt(
            prog,
            outcome=resolved_outcome,
            source=source,
            selected_move=selected_move,
        )
    except LearningProjectionError as exc:
        if exc.operation != "puzzle_attempt_sync":
            # No durable source means there must be no rating side effect. The caller may retry.
            raise
        projection_error = exc
        prog.learning_sync_error = str(exc)

    if projection_error is None and persisted is None:
        return {}

    state = puzzle_rating.load_state()
    rd_ok = float(puzzle.get("rd", 999)) < config.PUZZLE_MAX_RD
    rated = rd_ok and prog.hints_used == 0 and not hinted_or_given
    summary = puzzle_rating.record_result(
        state,
        puzzle_id=puzzle.get("id", ""),
        puzzle_rating=float(puzzle.get("rating", 1500)),
        puzzle_rd=float(puzzle.get("rd", 60)),
        themes=puzzle.get("themes", []),
        score=score,
        rated=rated,
    )
    puzzle_rating.save_state(state)
    prog.scored = True
    if projection_error is not None:
        # The attempt can be backfilled, while this served session cannot rate it twice.
        raise projection_error
    return summary


def apply_solver_moves(prog, tokens: list[str]) -> dict:
    """Serialize one session's move and terminal transitions."""
    with puzzle_session.transition():
        with prog.finalize_lock:
            return _apply_solver_moves_locked(prog, tokens)


def _apply_solver_moves_locked(prog, tokens: list[str]) -> dict:
    """Run a sequence of the solver's moves against a curated (Lichess) puzzle, mutating `prog`.

    Auto-plays the forced opponent reply between the solver's moves, exactly like the board's per-ply
    `/api/puzzle/move` loop, so a caller can submit the whole line at once (the terminal use) or one
    move at a time. Stops at the first wrong move (which fails the puzzle) or on completion.

    Returns `{steps: [...], correct, is_complete, failed, rating, opponent_replies}` where `steps`
    is the per-move verdict list and `opponent_replies` is the SAN of the forced replies played, for
    narration.
    """
    steps: list[dict] = []
    opponent_replies: list[str] = []
    rating_summary: Optional[dict] = None
    is_complete = False
    all_correct = True

    for token in tokens:
        if prog.learning_sync_error:
            raise LearningProjectionError(
                prog.learning_sync_error,
                operation="puzzle_attempt_sync",
                attempt_id=prog.attempt_id,
            )
        if prog.finished:
            break
        fen = puzzles_mod.position_fen(prog.puzzle, prog.ply_index)
        uci = to_uci(fen, token)
        if uci is None:
            steps.append({"move": token, "correct": False, "error": "Illegal or unparseable move."})
            all_correct = False
            prog.attempts += 1
            if not prog.scored:
                prog.failed = True
                rating_summary = score_attempt(
                    prog,
                    score=0.0,
                    hinted_or_given=False,
                    outcome="failure",
                    selected_move=token,
                )
            break

        result = puzzles_mod.validate_step(prog.puzzle, prog.ply_index, uci)
        prog.tried.append({
            "uci": uci,
            "fen_before": fen,
            "correct": result["correct"],
            "ply_index": prog.ply_index,
        })

        if not result["correct"]:
            steps.append({"move": uci, "correct": False})
            all_correct = False
            prog.attempts += 1
            if not prog.scored:
                prog.failed = True
                rating_summary = score_attempt(
                    prog,
                    score=0.0,
                    hinted_or_given=False,
                    outcome="failure",
                    selected_move=uci,
                )
            break

        if result["is_complete"]:
            is_complete = True
            steps.append({"move": uci, "correct": True, "is_complete": True})
            if not prog.scored:
                solved_clean = not prog.failed and prog.hints_used == 0
                rating_summary = score_attempt(
                    prog,
                    score=1.0 if solved_clean else 0.0,
                    hinted_or_given=prog.hints_used > 0,
                    outcome="success" if solved_clean else "partial",
                    selected_move=uci,
                )
            prog.finished = True
            break

        # Correct with more to come: record the forced reply (SAN) and skip past it.
        reply_uci = result.get("opponent_reply_uci")
        reply_san = _reply_san(prog.puzzle, prog.ply_index + 1, reply_uci)
        if reply_san:
            opponent_replies.append(reply_san)
        steps.append({
            "move": uci,
            "correct": True,
            "opponent_reply_uci": reply_uci,
            "opponent_reply_san": reply_san,
        })
        prog.ply_index += 2

    return {
        "steps": steps,
        "correct": all_correct,
        "is_complete": is_complete,
        "failed": prog.failed,
        "rating": rating_summary,
        "opponent_replies": opponent_replies,
    }


def give_up(prog) -> dict:
    """Finalize a curated puzzle give-up once and return the remaining verified line."""
    with puzzle_session.transition():
        with prog.finalize_lock:
            return _give_up_locked(prog)


def _give_up_locked(prog) -> dict:
    if prog.learning_sync_error:
        raise LearningProjectionError(
            prog.learning_sync_error,
            operation="puzzle_attempt_sync",
            attempt_id=prog.attempt_id,
        )
    if not prog.scored:
        prog.failed = True
        score_attempt(
            prog,
            score=0.0,
            hinted_or_given=True,
            outcome="failure",
            source="lichess",
        )
    prog.finished = True
    moves = prog.puzzle.get("moves", [])
    return {
        "solution_uci": moves[prog.ply_index :],
        "solution_san": puzzles_mod.solution_san(prog.puzzle),
    }


def give_up_mistake(prog) -> dict:
    """Finalize a personal-puzzle give-up through the game-backed training attempt boundary."""
    with puzzle_session.transition():
        with prog.finalize_lock:
            return _give_up_mistake_locked(prog)


def _give_up_mistake_locked(prog) -> dict:
    """Finalize a selected personal puzzle while both session locks are held."""
    if prog.learning_sync_error:
        raise LearningProjectionError(
            prog.learning_sync_error,
            operation="attempt_sync",
            attempt_id=prog.attempt_id,
        )
    if prog.finished:
        return {"error": "Puzzle is already finished."}
    puzzle = prog.puzzle
    if puzzle.get("critical_id") and puzzle.get("game_id"):
        try:
            result = training.reveal_solution(
                game_id=puzzle["game_id"],
                critical_id=puzzle["critical_id"],
                review_side=puzzle.get("reviewed_side"),
                hints_used=prog.hints_used,
                source="puzzle",
            )
        except LearningProjectionError as exc:
            prog.finished = True
            prog.scored = True
            prog.learning_sync_error = str(exc)
            raise
    else:
        best_uci = puzzle.get("best_uci")
        best_san = puzzle.get("best_san")
        result = {
            "solution_uci": [best_uci] if best_uci else [],
            "solution_san": [best_san] if best_san else [],
        }
    try:
        if not prog.scored:
            prog.failed = True
            state = puzzle_rating.load_state()
            puzzle_mistakes.record_practice_result(state, puzzle["key"], solved=False)
            puzzle_rating.save_state(state)
    finally:
        prog.scored = True
        prog.finished = True
    return result


def apply_mistake_move(prog, token: str) -> dict:
    """Serialize one personal-puzzle move and its durable attempt projection."""
    with puzzle_session.transition():
        with prog.finalize_lock:
            return _apply_mistake_move_locked(prog, token)


def _apply_mistake_move_locked(prog, token: str) -> dict:
    """Validate one move for a 'from your games' mistake puzzle (eval-threshold, multi-solution).

    Accepts any move whose win% drop is under the game's inaccuracy threshold. UNRATED — never
    touches Glicko; records a spaced-repetition success/miss instead. Mirrors the board's
    `_mistake_move`.
    """
    puzzle = prog.puzzle
    if prog.learning_sync_error:
        raise LearningProjectionError(
            prog.learning_sync_error,
            operation="attempt_sync",
            attempt_id=prog.attempt_id,
        )
    if prog.finished:
        return {"error": "Puzzle is already finished.", "is_complete": True}
    if puzzle.get("critical_id") and puzzle.get("game_id"):
        try:
            feedback = training.evaluate_attempt(
                game_id=puzzle["game_id"],
                critical_id=puzzle["critical_id"],
                selected_move=token,
                review_side=puzzle.get("reviewed_side"),
                hints_used=prog.hints_used,
                source="puzzle",
            )
        except LearningProjectionError as exc:
            prog.finished = True
            prog.scored = True
            prog.learning_sync_error = str(exc)
            raise
        solved = bool(feedback["solved"])
        prog.tried.append(
            {"uci": token, "fen_before": puzzle["fen"], "correct": solved, "ply_index": 1}
        )
        if solved:
            prog.finished = True
            if not prog.scored:
                state = puzzle_rating.load_state()
                puzzle_mistakes.record_practice_result(state, puzzle["key"], solved=True)
                puzzle_rating.save_state(state)
                prog.scored = True
        else:
            prog.attempts += 1
            prog.failed = True
        return {
            **feedback,
            "correct": solved,
            "is_complete": solved,
            "can_retry": not solved,
            "source": "your_games",
            "refutation_san": (feedback.get("variation") or {}).get("san", [])[1:],
            "refutation_uci": (feedback.get("variation") or {}).get("uci", [])[1:],
        }

    uci = to_uci(puzzle["fen"], token)
    if uci is None:
        return {"correct": False, "is_complete": False, "can_retry": True,
                "source": "your_games", "error": "Illegal or unparseable move."}

    result = lines.engine_line(puzzle["fen"], move=uci)
    move = result.get("move")
    if not move:
        return {"correct": False, "is_complete": False, "can_retry": True,
                "source": "your_games", "error": "Illegal move."}
    swing = float(move.get("win_swing", 99.0))
    accept = float(puzzle.get("accept_swing", 5.0))
    solved = swing < accept
    prog.tried.append({"uci": uci, "fen_before": puzzle["fen"], "correct": solved, "ply_index": 1})

    if solved:
        prog.finished = True
        if not prog.scored:
            state = puzzle_rating.load_state()
            puzzle_mistakes.record_practice_result(state, puzzle["key"], solved=True)
            puzzle_rating.save_state(state)
            prog.scored = True
        return {
            "correct": True, "is_complete": True, "source": "your_games",
            "win_swing": round(swing, 1), "better_move_san": move.get("better_move_san"),
            "is_engine_best": move.get("is_engine_best"),
        }

    prog.attempts += 1
    prog.failed = True
    return {
        "correct": False, "is_complete": False, "can_retry": True, "source": "your_games",
        "win_swing": round(swing, 1),
        "refutation_san": move.get("refutation_line_san", []),
        "refutation_uci": move.get("refutation_line_uci", []),
    }


def _reply_san(puzzle: dict, ply_index: int, reply_uci: Optional[str]) -> Optional[str]:
    """SAN of the forced opponent reply at `ply_index`, for narration. Best-effort -> None."""
    if not reply_uci:
        return None
    try:
        board = chess.Board(puzzle["fen"])
        for m in puzzle.get("moves", [])[:ply_index]:
            board.push_uci(m)
        return board.san(chess.Move.from_uci(reply_uci))
    except (ValueError, KeyError):
        return None
