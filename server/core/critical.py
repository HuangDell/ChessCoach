"""Deterministic critical-position selection from the Stage 1 engine sweep."""
from __future__ import annotations

from server import config


def _importance(move: dict) -> float:
    """Rank learning value, led by win-chance loss rather than saturated cp swings."""
    win_loss = max(0.0, float(move.get("win_percent_loss") or 0.0))
    cp_loss = max(0.0, float(move.get("centipawn_loss") or 0.0))
    before = float((move.get("win_percent_before") or {}).get("mover", 50.0))
    flags = set(move.get("signals") or [])

    score = win_loss * 10.0 + min(cp_loss, 400.0) / 20.0
    score += 80.0 if "missed_mate" in flags else 0.0
    score += 80.0 if "allowed_mate" in flags else 0.0
    score += 35.0 if "missed_win" in flags else 0.0
    score += 25.0 if "winning_to_equal" in flags or "equal_to_losing" in flags else 0.0
    # Once the game is almost certainly lost, large cp oscillations are usually poor teaching
    # candidates unless they change a forced-mate fact.
    if before < 10.0 and not flags.intersection({"missed_mate", "allowed_mate"}):
        score *= 0.25
    return round(score, 3)


def _is_meaningful(move: dict, inaccuracy_cutoff: float) -> bool:
    flags = set(move.get("signals") or [])
    if flags:
        return True
    win_loss = float(move.get("win_percent_loss") or 0.0)
    cp_loss = float(move.get("centipawn_loss") or 0.0)
    return win_loss >= max(2.0, inaccuracy_cutoff * 0.5) or cp_loss >= 75.0


def _merge_forced_followups(candidates: list[dict], mistake_cutoff: float) -> list[dict]:
    """Keep the first root error when the next reviewed move is an already-lost follow-up."""
    kept: list[dict] = []
    for candidate in sorted(candidates, key=lambda item: int(item["ply"])):
        if kept:
            previous = kept[-1]
            ply_gap = int(candidate["ply"]) - int(previous["ply"])
            previous_loss = float(previous.get("win_percent_loss") or 0.0)
            before = float((candidate.get("win_percent_before") or {}).get("mover", 50.0))
            if 0 < ply_gap <= 4 and previous_loss >= mistake_cutoff and before <= 25.0:
                continue
        kept.append(candidate)
    return kept


def select_critical_moves(
    moves: list[dict],
    review_side: str,
    thresholds: tuple[float, float, float],
) -> list[dict]:
    """Return up to the configured maximum Stage 1 move records, highest priority first."""
    reviewed = [move for move in moves if move.get("side") == review_side]
    meaningful = [move for move in reviewed if _is_meaningful(move, thresholds[0])]

    # If the game has fewer than the target number of clear errors, add only moves with a real
    # measurable loss. This keeps the usual output near 3-8 without inventing mistakes in clean play.
    if len(meaningful) < config.CRITICAL_MIN:
        known = {int(move["ply"]) for move in meaningful}
        fallbacks = [
            move
            for move in reviewed
            if int(move["ply"]) not in known
            and (
                float(move.get("win_percent_loss") or 0.0) >= 1.0
                or float(move.get("centipawn_loss") or 0.0) >= 30.0
            )
        ]
        fallbacks.sort(key=_importance, reverse=True)
        meaningful.extend(fallbacks[: max(0, config.CRITICAL_MIN - len(meaningful))])

    merged = _merge_forced_followups(meaningful, thresholds[1])
    ranked = sorted(merged, key=lambda move: (-_importance(move), int(move["ply"])))
    selected = ranked[: config.CRITICAL_MAX]
    for priority, move in enumerate(selected, start=1):
        move["critical_priority"] = priority
        move["critical_score"] = _importance(move)
    return selected
