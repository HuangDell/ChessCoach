"""Deterministic bounded review prioritization over saved analysis artifacts.

This module does not call Stockfish or infer new chess facts.  It exposes comparable features
from the Stage 2 shortlist so an Agent can choose teaching order without scanning the game or
hiding the largest recorded error.
"""
from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from typing import Any, cast

import chess

from server.core.agent.models import (
    PositionReference,
    ReviewPrioritizationContext,
    ReviewPriorityCandidate,
    ReviewSide,
)


class PrioritizationError(ValueError):
    """Raised when an analysis artifact cannot produce an owned shortlist."""


_CLASSIFICATION_RANK = {
    "blunder": 3,
    "mistake": 2,
    "inaccuracy": 1,
}
_CRITICALITY_SCORE = {
    "forced_mate": 1.0,
    "only_move": 0.85,
    "critical": 0.65,
}


def _category(position: Mapping[str, Any]) -> str | None:
    value = str(((position.get("facts") or {}).get("primary_category") or "")).strip()
    return value or None


def _fact_confidence(position: Mapping[str, Any]) -> float:
    """Rate completeness of existing deterministic facts, never chess correctness itself."""
    facts = position.get("facts") or {}
    if not isinstance(facts, Mapping) or not facts:
        return 0.0
    confidence = 0.35
    if facts.get("facts_version") and str(facts.get("method") or "").startswith("deterministic-"):
        confidence += 0.25
    snapshots = facts.get("snapshots") or {}
    if isinstance(snapshots, Mapping) and snapshots.get("before"):
        confidence += 0.15
    line_results = [facts.get("played_line_result") or {}, facts.get("best_line_result") or {}]
    if all(isinstance(item, Mapping) and item.get("line_was_legal") is True for item in line_results):
        confidence += 0.15
    if facts.get("primary_category") and facts.get("classification_evidence"):
        confidence += 0.10
    return round(min(confidence, 1.0), 2)


def _phase(position: Mapping[str, Any]) -> str | None:
    raw = (
        (((position.get("facts") or {}).get("snapshots") or {}).get("before") or {})
        .get("phase", {})
        .get("name")
    )
    value = str(raw or "").strip()
    return value or None


def _training_available(position: Mapping[str, Any]) -> bool:
    """Mirror the minimum artifact guarantees needed by the existing Retry boundary."""
    try:
        board = chess.Board(str(position["fen_before"]))
        played = chess.Move.from_uci(str((position.get("played_move") or {})["uci"]))
        candidates = position.get("candidates") or []
        best = chess.Move.from_uci(str((candidates[0].get("move") or {})["uci"]))
    except (IndexError, KeyError, TypeError, ValueError):
        return False
    return played in board.legal_moves and best in board.legal_moves


def _recurrence(
    category: str | None,
    evidence: Mapping[str, int | Mapping[str, Any]],
) -> tuple[int, list[str]]:
    if category is None or category not in evidence:
        return 0, []
    raw = evidence[category]
    if isinstance(raw, Mapping):
        count = max(0, int(raw.get("count") or raw.get("evidence_count") or 0))
        refs = [str(item).strip() for item in raw.get("evidence_refs", []) or []]
        return count, [item for item in refs if item]
    return max(0, int(raw)), []


def _goal_relevance(
    *,
    category: str | None,
    phase: str | None,
    classification: str,
    criticality: str,
    user_goal: str | None,
    focus_categories: Collection[str],
) -> float:
    focus = {str(item).strip().casefold() for item in focus_categories if str(item).strip()}
    normalized_category = (category or "").casefold()
    if normalized_category and normalized_category in focus:
        return 1.0
    goal = (user_goal or "").strip().casefold().replace("_", " ")
    if not goal:
        return 0.0
    attributes = [normalized_category, (phase or "").casefold(), classification, criticality]
    if any(value and value.replace("_", " ") in goal for value in attributes):
        return 1.0
    category_tokens = [token for token in normalized_category.split("_") if len(token) >= 4]
    return 0.5 if any(token in goal for token in category_tokens) else 0.0


def _position_reference(
    position: Mapping[str, Any],
    *,
    game_id: str,
    review_side: ReviewSide,
) -> PositionReference:
    try:
        critical_id = str(position["critical_id"]).strip()
        ply = int(position["ply"])
        fen = chess.Board(str(position["fen_before"])).fen()
    except (KeyError, TypeError, ValueError) as exc:
        raise PrioritizationError("Critical position has an invalid reference.") from exc
    if not critical_id:
        raise PrioritizationError("Critical position has an empty critical_id.")
    side = position.get("side")
    if side is not None and str(side) != review_side:
        raise PrioritizationError("Critical position belongs to another review side.")
    return PositionReference(
        game_id=game_id,
        review_side=review_side,
        critical_id=critical_id,
        ply=ply,
        fen=fen,
    )


def build_review_prioritization(
    analysis: Mapping[str, Any],
    *,
    recurrence_evidence: Mapping[str, int | Mapping[str, Any]] | None = None,
    user_goal: str | None = None,
    focus_categories: Collection[str] = (),
    training_references: Collection[str] | None = None,
    limit: int = 8,
    max_selection: int = 3,
) -> ReviewPrioritizationContext:
    """Build a deterministic 1-8 item subset of the saved Stage 2 positions.

    Normal analyzed games already contain 3-8 positions.  Clean/short games may legitimately have
    fewer than three; no fake position is added.  When a legacy or imported artifact contains more
    than eight, the result is capped while always retaining the position with the largest win loss.
    """
    game_id = str(analysis.get("game_id") or "").strip()
    raw_side = str(analysis.get("review_side") or "").strip()
    if not game_id or raw_side not in {"white", "black"}:
        raise PrioritizationError("Analysis has invalid review ownership metadata.")
    review_side = cast(ReviewSide, raw_side)
    raw_positions = analysis.get("critical_positions") or []
    if not isinstance(raw_positions, Sequence) or isinstance(raw_positions, (str, bytes)):
        raise PrioritizationError("Analysis critical_positions must be a list.")
    if not raw_positions:
        raise PrioritizationError("Analysis has no critical positions to prioritize.")
    bounded_limit = max(1, min(8, int(limit)))
    recurrence_evidence = recurrence_evidence or {}
    available_refs = set(training_references) if training_references is not None else None

    prepared: list[tuple[ReviewPriorityCandidate, float, int, int]] = []
    identities: set[str] = set()
    for index, raw in enumerate(raw_positions):
        if not isinstance(raw, Mapping):
            raise PrioritizationError("Analysis contains an invalid critical position.")
        reference = _position_reference(raw, game_id=game_id, review_side=review_side)
        assert reference.critical_id is not None
        if reference.critical_id in identities:
            raise PrioritizationError("Analysis contains duplicate critical positions.")
        identities.add(reference.critical_id)
        classification = str(raw.get("classification") or "unclassified")
        criticality_name = str(raw.get("criticality") or "critical")
        severity = round(max(0.0, float(raw.get("win_loss") or 0.0)), 1)
        category = _category(raw)
        phase = _phase(raw)
        recurrence_count, recurrence_refs = _recurrence(category, recurrence_evidence)
        training_key = f"{game_id}:{review_side}:{reference.critical_id}"
        can_train = (
            training_key in available_refs
            if available_refs is not None
            else _training_available(raw)
        )
        review_ref = f"review:{game_id}:{review_side}:{reference.critical_id}"
        candidate = ReviewPriorityCandidate(
            reference=reference,
            classification=classification,
            category=category,
            severity=severity,
            criticality=_CRITICALITY_SCORE.get(criticality_name, 0.4),
            fact_confidence=_fact_confidence(raw),
            recurrence_evidence=recurrence_count,
            training_available=can_train,
            user_goal_relevance=_goal_relevance(
                category=category,
                phase=phase,
                classification=classification,
                criticality=criticality_name,
                user_goal=user_goal,
                focus_categories=focus_categories,
            ),
            largest_error=False,
            evidence_refs=[review_ref, *recurrence_refs],
        )
        prepared.append(
            (
                candidate,
                float(raw.get("critical_score") or 0.0),
                index,
                int(reference.ply or 0),
            )
        )

    largest_index = max(
        range(len(prepared)),
        key=lambda item: (
            prepared[item][0].severity,
            _CLASSIFICATION_RANK.get(prepared[item][0].classification, 0),
            prepared[item][1],
            -prepared[item][3],
        ),
    )
    largest = prepared[largest_index][0].model_copy(update={"largest_error": True})
    remaining = [item for index, item in enumerate(prepared) if index != largest_index]
    remaining.sort(
        key=lambda item: (
            -item[0].user_goal_relevance,
            -item[0].severity,
            -item[0].criticality,
            -item[0].fact_confidence,
            -item[0].recurrence_evidence,
            -int(item[0].training_available),
            item[2],
            item[3],
            item[0].reference.critical_id or "",
        )
    )
    selected = [largest, *(item[0] for item in remaining[: bounded_limit - 1])]
    return ReviewPrioritizationContext(
        game_id=game_id,
        review_side=review_side,
        candidates=selected,
        max_selection=max(1, min(3, int(max_selection))),
    )


__all__ = ["PrioritizationError", "build_review_prioritization"]
