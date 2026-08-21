"""Artifact-backed Retry and personal training primitives.

Stage 2 analysis is authoritative: known candidate moves are graded from its MultiPV output and
the played move is graded from its dedicated continuation. Only a move absent from both is sent to
Stockfish on demand. Every submission is appended to a local JSONL attempt log.
"""
from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import chess

from server import config
from server.core import lines
from server.core.storage import games


ATTEMPT_SCHEMA_VERSION = 1
_ATTEMPT_LOCK = threading.Lock()
_PRINCIPLES = {
    "allowed_mate": "Before committing, calculate every forcing check the opponent will have.",
    "missed_mate": "When the king is exposed, calculate checks before considering quiet moves.",
    "wrong_exchange_sequence": "Calculate an exchange through the final recapture before starting it.",
    "missed_opponent_threat": "After every opponent move, identify their checks, captures, and threats.",
    "hanging_piece": "Before moving, check whether the destination leaves the piece loose or overloaded.",
    "missed_capture": "Scan all legal captures before choosing a positional move.",
    "fork": "Look for forcing moves that attack two valuable targets at once.",
}


class TrainingPositionError(ValueError):
    """Raised when a requested game/critical-position pair is unavailable or inconsistent."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _attempt_path(data_dir: str | None = None) -> str:
    return os.path.join(data_dir or config.DATA_DIR, "history", "attempts.jsonl")


def _legacy_attempt_path(data_dir: str | None = None) -> str:
    return os.path.join(data_dir or config.DATA_DIR, "training", "attempts.jsonl")


def _find_critical(analysis: dict, critical_id: str) -> dict:
    position = next(
        (
            item
            for item in analysis.get("critical_positions", []) or []
            if item.get("critical_id") == critical_id
        ),
        None,
    )
    if position is None:
        raise TrainingPositionError("Unknown critical position.")
    return position


def load_position(game_id: str, critical_id: str, review_side: str | None = None) -> tuple[dict, dict]:
    """Load one critical position and its containing analysis artifact."""
    try:
        analysis = games.load_analysis(game_id, review_side)
    except games.GameNotFoundError as exc:
        raise TrainingPositionError(str(exc)) from exc
    if analysis.get("review_side") not in {"white", "black"}:
        raise TrainingPositionError("Training position has no concrete review side.")
    return analysis, _find_critical(analysis, critical_id)


def _category(position: dict) -> str:
    return str((position.get("facts") or {}).get("primary_category") or "uncategorized")


def _phase(position: dict) -> str:
    return str(
        (((position.get("facts") or {}).get("snapshots") or {}).get("before") or {})
        .get("phase", {})
        .get("name", "middlegame")
    )


def _metadata_for(game_id: str, data_dir: str | None = None) -> dict:
    if data_dir is not None:
        path = Path(data_dir) / "games" / game_id / "metadata.json"
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return {}
    try:
        return games.load_game(game_id)
    except games.GameNotFoundError:
        return {}


def list_training_positions(data_dir: str | None = None) -> list[dict]:
    """Return all Stage 2 critical positions as personal-puzzle candidates.

    Files are enumerated locally rather than through history so category/phase/facts come from the
    versioned analysis artifact and each item keeps its stable ``critical_id``.
    """
    root = Path(data_dir or config.DATA_DIR) / "games"
    if not root.is_dir():
        return []
    out: list[dict] = []
    for path in sorted(root.glob("*/analysis/*.json")):
        try:
            analysis = json.loads(path.read_text(encoding="utf-8"))
            game_id = str(analysis["game_id"])
            side = str(analysis["review_side"])
            if side not in {"white", "black"}:
                continue
            metadata = _metadata_for(game_id, data_dir)
            headers = analysis.get("headers") or metadata.get("headers") or {}
            thresholds = (((analysis.get("profile") or {}).get("critical") or {}).get("thresholds") or [])
            accept_swing = float(thresholds[0]) if thresholds else 5.0
            speed = ((analysis.get("profile") or {}).get("review") or {}).get("speed") or "unknown"
            for position in analysis.get("critical_positions", []) or []:
                critical_id = position.get("critical_id")
                fen = position.get("fen_before")
                played = position.get("played_move") or {}
                best = ((position.get("candidates") or [{}])[0].get("move") or {})
                if not critical_id or not fen or not played.get("uci") or not best.get("uci"):
                    continue
                out.append(
                    {
                        "key": f"{game_id}:{side}:{critical_id}",
                        "id": f"{game_id}:{side}:{critical_id}",
                        "source": "your_games",
                        "game_id": game_id,
                        "reviewed_side": side,
                        "critical_id": critical_id,
                        "ply": position.get("ply"),
                        "fen": fen,
                        "side_to_move": position.get("side") or side,
                        "win_drop": float(position.get("win_loss") or 0.0),
                        "accept_swing": accept_swing,
                        "played_uci": played.get("uci"),
                        "played_san": played.get("san"),
                        "best_uci": best.get("uci"),
                        "best_san": best.get("san"),
                        "classification": position.get("classification"),
                        "motifs": [
                            item
                            for item in [
                                _category(position),
                                *((position.get("facts") or {}).get("secondary_categories") or []),
                            ]
                            if item and item != "uncategorized"
                        ],
                        "category": _category(position),
                        "phase": _phase(position),
                        "white": headers.get("White"),
                        "black": headers.get("Black"),
                        "speed": speed,
                        "date": (headers.get("UTCDate") or headers.get("Date") or "").replace(".", "-") or None,
                        "game_url": metadata.get("source_url") or headers.get("Link") or headers.get("Site"),
                    }
                )
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            continue
    return out


def available_categories(data_dir: str | None = None) -> list[str]:
    return sorted({item["category"] for item in list_training_positions(data_dir) if item["category"] != "uncategorized"})


def _score_text(score: dict | None) -> str:
    if not score:
        return "unknown"
    value = int(score.get("value") or 0)
    if score.get("type") == "mate":
        return f"{'-' if value < 0 else ''}M{abs(value)}"
    return f"{value / 100:+.2f}"


def _explanation_for(game_id: str, review_side: str, critical_id: str) -> dict | None:
    try:
        artifact = games.load_explanations(game_id, review_side)
    except games.GameNotFoundError:
        return None
    return next(
        (item for item in artifact.get("positions", []) or [] if item.get("critical_id") == critical_id),
        None,
    )


def study_context(game_id: str, analysis: dict, position: dict) -> dict:
    facts = position.get("facts") or {}
    category = _category(position)
    motif = next((item for item in facts.get("motifs", []) or [] if item.get("name") == category), None)
    evidence = (motif or {}).get("evidence") or []
    explanation = _explanation_for(game_id, str(analysis["review_side"]), str(position["critical_id"]))
    return {
        "original_error_reason": " ".join(str(item) for item in evidence[:2])
        or (
            f"The game move lost {float(position.get('win_loss') or 0.0):.1f} percentage points "
            "of win chance."
        ),
        "transferable_principle": (
            (explanation or {}).get("transferable_principle")
            or _PRINCIPLES.get(category)
            or "Compare forcing checks, captures, and threats before committing to a move."
        ),
    }


def record_attempt(
    *,
    game_id: str,
    critical_id: str,
    selected_move: str | None,
    verdict: str,
    hints_used: int,
    solved: bool,
    source: str,
    category: str | None = None,
    phase: str | None = None,
    review_side: str | None = None,
    data_dir: str | None = None,
) -> dict:
    attempt = {
        "schema_version": ATTEMPT_SCHEMA_VERSION,
        "attempt_id": uuid.uuid4().hex,
        "game_id": game_id,
        "critical_id": critical_id,
        "reviewed_side": review_side,
        "attempted_at": _now_iso(),
        "selected_move": selected_move,
        "verdict": verdict,
        "hints_used": max(0, int(hints_used)),
        "solved": bool(solved),
        "source": source,
        "category": category,
        "phase": phase,
    }
    path = _attempt_path(data_dir)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with _ATTEMPT_LOCK:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(attempt, ensure_ascii=False, sort_keys=True) + "\n")
            handle.flush()
    return attempt


def load_attempts(
    *, game_id: str | None = None, critical_id: str | None = None, data_dir: str | None = None
) -> list[dict]:
    found: dict[str, dict] = {}
    for path in (_attempt_path(data_dir), _legacy_attempt_path(data_dir)):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                for line in handle:
                    try:
                        item = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if game_id and item.get("game_id") != game_id:
                        continue
                    if critical_id and item.get("critical_id") != critical_id:
                        continue
                    key = str(item.get("attempt_id") or f"{path}:{len(found)}")
                    found[key] = item
        except FileNotFoundError:
            pass
    return sorted(found.values(), key=lambda item: item.get("attempted_at", ""))


def _thresholds(analysis: dict) -> tuple[float, float, float]:
    raw = (((analysis.get("profile") or {}).get("critical") or {}).get("thresholds") or [])
    values = [float(item) for item in raw[:3]]
    while len(values) < 3:
        values.append((5.0, 10.0, 20.0)[len(values)])
    return values[0], values[1], values[2]


def _verdict(
    *,
    selected: str,
    best: str,
    played: str,
    gap: float | None,
    cached_candidate: bool,
    thresholds: tuple[float, float, float],
) -> str:
    if selected == best:
        return "best"
    if selected == played:
        return "same_as_game"
    if gap is None:
        return "unknown"
    inaccuracy_cutoff, mistake_cutoff, _blunder_cutoff = thresholds
    if gap <= min(2.0, inaccuracy_cutoff) or (cached_candidate and gap <= inaccuracy_cutoff):
        return "acceptable"
    if gap <= mistake_cutoff:
        return "inaccurate"
    return "bad"


def _feedback_message(verdict: str, position: dict) -> str:
    if verdict == "best":
        return "You found the Engine's first choice."
    if verdict == "acceptable":
        return "This is also a sound choice; matching the first Engine line was not required."
    if verdict == "inaccurate":
        return "The move is playable, but it gives away more than the accepted alternatives."
    if verdict == "same_as_game":
        category = _category(position).replace("_", " ")
        return f"You chose the game move again. Recheck all opponent checks, captures, and threats; the original issue was {category}."
    if verdict == "bad":
        return "The opponent has a forcing response and the move gives away substantial winning chances."
    return "This move was not in the saved lines and Stockfish could not finish the on-demand check."


def evaluate_attempt(
    *,
    game_id: str,
    critical_id: str,
    selected_move: str,
    review_side: str | None = None,
    hints_used: int = 0,
    source: str = "retry",
) -> dict:
    analysis, position = load_position(game_id, critical_id, review_side)
    board = chess.Board(str(position["fen_before"]))
    try:
        move = chess.Move.from_uci(selected_move)
    except ValueError as exc:
        raise TrainingPositionError("Move must be legal UCI.") from exc
    if move not in board.legal_moves:
        raise TrainingPositionError("Move is not legal in this training position.")
    selected_move = move.uci()
    selected_san = board.san(move)

    candidates = position.get("candidates") or []
    best_candidate = candidates[0] if candidates else {}
    best_move = (best_candidate.get("move") or {}).get("uci") or (
        ((position.get("best_line") or {}).get("uci") or [None])[0]
    )
    played_move = (position.get("played_move") or {}).get("uci")
    selected_candidate = next(
        (item for item in candidates if (item.get("move") or {}).get("uci") == selected_move), None
    )
    used_cached = selected_candidate is not None or selected_move == played_move
    gap: float | None
    evaluation: dict[str, Any]
    variation: dict
    opponent_reply: dict | None

    if selected_candidate is not None:
        gap = float(selected_candidate.get("win_gap_from_best") or 0.0)
        evaluation = {
            "score": selected_candidate.get("scores", {}).get("mover"),
            "label": _score_text(selected_candidate.get("scores", {}).get("mover")),
            "win_percent": selected_candidate.get("win_percent", {}).get("mover"),
        }
        variation = selected_candidate.get("line") or {"uci": [], "san": []}
        ucis = variation.get("uci") or []
        sans = variation.get("san") or []
        opponent_reply = (
            {"uci": ucis[1], "san": sans[1] if len(sans) > 1 else None} if len(ucis) > 1 else None
        )
    elif selected_move == played_move:
        top_win = float(best_candidate.get("win_percent", {}).get("mover") or 50.0)
        gap = float(position.get("win_loss") or 0.0)
        played_win = max(0.0, top_win - gap)
        score = position.get("played_move_scores", {}).get("mover")
        evaluation = {"score": score, "label": _score_text(score), "win_percent": played_win or None}
        variation = position.get("played_line") or {"uci": [], "san": []}
        opponent_reply = position.get("opponent_best_reply")
    else:
        try:
            depth = max(config.DEFAULT_DEPTH, int(position.get("deep_depth") or config.DEFAULT_DEPTH))
            result = lines.engine_line(str(position["fen_before"]), move=selected_move, depth=depth)
            checked = result.get("move")
        except Exception:  # noqa: BLE001 - return/persist unknown instead of losing the attempt
            checked = None
        if not checked:
            gap = None
            evaluation = {"score": None, "label": "unknown", "win_percent": None}
            variation = {"uci": [selected_move], "san": [selected_san]}
            opponent_reply = None
        else:
            gap = max(0.0, float(checked.get("win_swing") or 0.0))
            evaluation = {
                "score": None,
                "label": checked.get("eval_after") or "unknown",
                "win_percent": checked.get("win_after"),
            }
            reply_uci = checked.get("refutation_line_uci") or []
            reply_san = checked.get("refutation_line_san") or []
            variation = {
                "uci": [selected_move, *reply_uci],
                "san": [selected_san, *reply_san],
            }
            opponent_reply = (
                {"uci": reply_uci[0], "san": reply_san[0] if reply_san else None}
                if reply_uci
                else None
            )

    verdict = _verdict(
        selected=selected_move,
        best=str(best_move or ""),
        played=str(played_move or ""),
        gap=gap,
        cached_candidate=selected_candidate is not None,
        thresholds=_thresholds(analysis),
    )
    solved = verdict in {"best", "acceptable"}
    context = study_context(game_id, analysis, position)
    best_line = position.get("best_line") or {"uci": [], "san": []}
    best_san = (best_line.get("san") or [None])[0]
    shapes = [{"orig": selected_move[:2], "dest": selected_move[2:4], "brush": "blue"}]
    if best_move and best_move != selected_move:
        shapes.append({"orig": best_move[:2], "dest": best_move[2:4], "brush": "green"})
    if opponent_reply:
        uci = opponent_reply.get("uci")
        if uci:
            shapes.append({"orig": uci[:2], "dest": uci[2:4], "brush": "red"})

    attempt = record_attempt(
        game_id=game_id,
        critical_id=critical_id,
        selected_move=selected_move,
        verdict=verdict,
        hints_used=hints_used,
        solved=solved,
        source=source,
        category=_category(position),
        phase=_phase(position),
        review_side=str(analysis.get("review_side") or "") or None,
    )
    return {
        "attempt": attempt,
        "game_id": game_id,
        "critical_id": critical_id,
        "selected_move": {"uci": selected_move, "san": selected_san},
        "game_move": position.get("played_move"),
        "best_move": {"uci": best_move, "san": best_san},
        "verdict": verdict,
        "solved": solved,
        "hints_used": max(0, int(hints_used)),
        "used_cached_analysis": used_cached,
        "engine_evaluation": evaluation,
        "win_gap_from_best": round(gap, 1) if gap is not None else None,
        "opponent_best_reply": opponent_reply,
        "variation": {"uci": (variation.get("uci") or [])[:12], "san": (variation.get("san") or [])[:12]},
        "best_line": best_line,
        "problem_resolved": solved,
        "primary_category": _category(position),
        "phase": _phase(position),
        "message": _feedback_message(verdict, position),
        "shapes": shapes,
        **context,
    }


def hint_for(
    *, game_id: str, critical_id: str, level: int, review_side: str | None = None
) -> dict:
    _analysis, position = load_position(game_id, critical_id, review_side)
    level = max(1, min(4, int(level)))
    category = _category(position)
    best_line = position.get("best_line") or {"uci": [], "san": []}
    best_uci = (best_line.get("uci") or [None])[0]
    best_san = (best_line.get("san") or [None])[0]
    if level == 1:
        if category in {"allowed_mate", "missed_mate", "missed_opponent_threat"}:
            text = "Think: start with checks and the opponent's immediate threats."
        elif category in {"missed_capture", "hanging_piece", "wrong_exchange_sequence"}:
            text = "Think: list every capture and calculate the full exchange sequence."
        else:
            text = "Think: scan checks, captures, and threats before considering quiet moves."
        return {"level": 1, "kind": "think", "text": text, "shapes": []}
    if level == 2:
        board = chess.Board(str(position["fen_before"]))
        square = chess.parse_square(best_uci[:2]) if best_uci else None
        piece = board.piece_at(square) if square is not None else None
        file_index = chess.square_file(square) if square is not None else 3
        area = "queenside" if file_index <= 2 else "kingside" if file_index >= 5 else "center"
        piece_name = chess.piece_name(piece.piece_type) if piece else "forcing pieces"
        return {
            "level": 2,
            "kind": "area",
            "text": f"Area: focus on the {area}, especially what your {piece_name} can do there.",
            "shapes": [],
        }
    if level == 3:
        return {
            "level": 3,
            "kind": "first_move",
            "text": f"First move: {best_san or best_uci}.",
            "shapes": (
                [{"orig": best_uci[:2], "dest": best_uci[2:4], "brush": "green"}]
                if best_uci
                else []
            ),
        }
    return {
        "level": 4,
        "kind": "show_line",
        "text": "Show line: " + " ".join(best_line.get("san") or []),
        "shapes": (
            [{"orig": best_uci[:2], "dest": best_uci[2:4], "brush": "green"}]
            if best_uci
            else []
        ),
        "line": best_line,
    }


def reveal_solution(
    *,
    game_id: str,
    critical_id: str,
    review_side: str | None = None,
    hints_used: int = 0,
    source: str = "puzzle",
) -> dict:
    """Return post-attempt study data and record a non-solved give-up attempt."""
    analysis, position = load_position(game_id, critical_id, review_side)
    best_line = position.get("best_line") or {"uci": [], "san": []}
    best_uci = (best_line.get("uci") or [None])[0]
    best_san = (best_line.get("san") or [None])[0]
    attempt = record_attempt(
        game_id=game_id,
        critical_id=critical_id,
        selected_move=None,
        verdict="unknown",
        hints_used=hints_used,
        solved=False,
        source=source,
        category=_category(position),
        phase=_phase(position),
        review_side=str(analysis.get("review_side") or "") or None,
    )
    return {
        "attempt": attempt,
        "solution_uci": list(best_line.get("uci") or []),
        "solution_san": list(best_line.get("san") or []),
        "best_move": {"uci": best_uci, "san": best_san},
        "game_move": position.get("played_move"),
        "best_line": best_line,
        "primary_category": _category(position),
        "phase": _phase(position),
        **study_context(game_id, analysis, position),
    }
