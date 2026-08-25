"""Deterministic, artifact-backed candidate retrieval for personalized training."""
from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any

import chess

from server.core import training
from server.core.agent.models import (
    GetTrainingCandidatesInput,
    GetTrainingCandidatesResult,
    MemoryQuery,
    MoveReference,
    PositionReference,
    SkillEstimate,
    StartTrainingActionResult,
    TrainingCandidate,
)
from server.core.learning import memory, taxonomy


MAX_TRAINING_CANDIDATES = 10
MAX_TRAINING_POSITIONS = 5
RECENT_SUCCESS_DAYS = 30


class TrainingActionUnavailableError(ValueError):
    """Raised when a draft reference no longer matches its owning analysis artifact."""


EstimateLoader = Callable[[str], Iterable[SkillEstimate]]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _attempt_outcome(attempt: dict[str, Any]) -> str:
    if attempt.get("gave_up"):
        return "give_up"
    if attempt.get("solved"):
        return "success"
    verdict = str(attempt.get("verdict") or "").strip()
    return verdict or "failure"


def _move(board: chess.Board, uci: Any, san: Any) -> MoveReference | None:
    try:
        parsed = chess.Move.from_uci(str(uci or ""))
    except ValueError:
        return None
    if parsed not in board.legal_moves:
        return None
    canonical_san = board.san(parsed)
    if san not in (None, canonical_san):
        return None
    return MoveReference(uci=parsed.uci(), san=canonical_san)


def _candidate_from_position(
    raw: dict[str, Any],
    *,
    attempts: Sequence[dict[str, Any]],
    estimate: SkillEstimate | None,
) -> TrainingCandidate | None:
    game_id = str(raw.get("game_id") or "").strip()
    review_side = str(raw.get("reviewed_side") or "").strip()
    critical_id = str(raw.get("critical_id") or "").strip()
    category = str(raw.get("category") or "").strip()
    mapping = taxonomy.map_attempt_category(category)
    if (
        not game_id
        or review_side not in {"white", "black"}
        or not critical_id
        or mapping is None
    ):
        return None
    try:
        board = chess.Board(str(raw.get("fen") or ""))
        ply = int(raw["ply"])
    except (KeyError, TypeError, ValueError):
        return None
    if not board.is_valid() or ply < 0:
        return None
    played = _move(board, raw.get("played_uci"), raw.get("played_san"))
    best = _move(board, raw.get("best_uci"), raw.get("best_san"))
    if played is None or best is None:
        return None
    candidate_moves: list[MoveReference] = []
    for item in raw.get("candidate_moves") or []:
        if not isinstance(item, dict):
            continue
        candidate = _move(board, item.get("uci"), item.get("san"))
        if candidate is not None and candidate.uci not in {move.uci for move in candidate_moves}:
            candidate_moves.append(candidate)
        if len(candidate_moves) == 3:
            break
    if best.uci not in {move.uci for move in candidate_moves}:
        candidate_moves.insert(0, best)
        candidate_moves = candidate_moves[:3]

    owned_attempts = sorted(
        (
            attempt
            for attempt in attempts
            if str(attempt.get("game_id") or "") == game_id
            and str(attempt.get("critical_id") or "") == critical_id
            and str(attempt.get("reviewed_side") or attempt.get("review_side") or "")
            in {"", review_side}
        ),
        key=lambda attempt: str(attempt.get("attempted_at") or ""),
    )
    latest = owned_attempts[-1] if owned_attempts else None
    evidence_refs = [
        f"analysis:{game_id}:{review_side}:{critical_id}:facts.primary_category"
    ]
    if estimate is not None:
        for evidence_ref in memory.evidence_refs_for_estimate(estimate):
            if evidence_ref not in evidence_refs:
                evidence_refs.append(evidence_ref)
    classification = str(raw.get("classification") or "").strip().casefold()
    difficulty = {
        "blunder": "easier",
        "mistake": "standard",
        "inaccuracy": "harder",
    }.get(classification, "standard")
    return TrainingCandidate(
        reference=PositionReference(
            game_id=game_id,
            review_side=review_side,
            critical_id=critical_id,
            ply=ply,
            fen=board.fen(),
        ),
        skill_ids=[mapping.skill_id],
        source_artifact=f"analysis:{game_id}:{review_side}",
        category=category,
        phase=str(raw.get("phase") or "middlegame"),
        difficulty=difficulty,
        played_move=played,
        best_move=best,
        candidate_moves=candidate_moves,
        attempted=bool(owned_attempts),
        last_outcome=_attempt_outcome(latest) if latest is not None else None,
        last_attempted_at=(
            str(latest["attempted_at"])
            if latest is not None and latest.get("attempted_at")
            else None
        ),
        attempt_count=len(owned_attempts),
        estimate_confidence=estimate.confidence_level if estimate is not None else None,
        evidence_refs=evidence_refs,
    )


def _resolve_focus(request: GetTrainingCandidatesInput) -> list[str] | None:
    resolved: list[str] = []
    for value in [*request.skill_ids, *request.categories]:
        skill_id = taxonomy.resolve_skill_id(value)
        if skill_id is None:
            return None
        if skill_id not in resolved:
            resolved.append(skill_id)
    return resolved


def _load_estimates(
    request: GetTrainingCandidatesInput,
    focus: Sequence[str],
    *,
    estimate_loader: EstimateLoader | None,
) -> list[SkillEstimate]:
    if focus:
        estimates: list[SkillEstimate] = []
        for skill_id in focus:
            estimates.extend(
                memory.retrieve_estimates(
                    MemoryQuery(
                        activity="training_planning",
                        focus_skill_id=skill_id,
                        window=request.window,
                        limit=1,
                    ),
                    personalization_enabled=True,
                    estimate_loader=estimate_loader,
                )
            )
        return list({item.skill_id: item for item in estimates}.values())
    return [
        estimate
        for estimate in memory.retrieve_estimates(
            MemoryQuery(
                activity="training_planning",
                window=request.window,
                limit=5,
            ),
            personalization_enabled=True,
            estimate_loader=estimate_loader,
        )
        if estimate.status == "weakness"
    ]


def _diverse_prefix(candidates: list[TrainingCandidate], limit: int) -> list[TrainingCandidate]:
    remaining = list(candidates)
    selected: list[TrainingCandidate] = []
    seen_games: set[str] = set()
    seen_phases: set[str] = set()
    while remaining and len(selected) < limit:
        index = max(
            range(len(remaining)),
            key=lambda offset: (
                int(str(remaining[offset].reference.game_id) not in seen_games),
                int(str(remaining[offset].phase) not in seen_phases),
                -offset,
            ),
        )
        candidate = remaining.pop(index)
        selected.append(candidate)
        seen_games.add(str(candidate.reference.game_id))
        seen_phases.add(str(candidate.phase))
    return selected


def get_training_candidates(
    request: GetTrainingCandidatesInput,
    *,
    current_game_id: str | None = None,
    data_dir: str | None = None,
    estimate_loader: EstimateLoader | None = None,
    now: datetime | None = None,
) -> GetTrainingCandidatesResult:
    """Return a bounded shortlist sourced only from canonical weakness and Stage 2 artifacts."""

    focus = _resolve_focus(request)
    if focus is None:
        return GetTrainingCandidatesResult()
    estimates = _load_estimates(request, focus, estimate_loader=estimate_loader)
    estimate_by_skill = {estimate.skill_id: estimate for estimate in estimates}
    allowed_skills = set(focus or estimate_by_skill)
    if not allowed_skills:
        return GetTrainingCandidatesResult()

    all_attempts = training.load_attempts(data_dir=data_dir)
    current_time = (now or _utc_now()).astimezone(timezone.utc)
    practice_cutoff = current_time - timedelta(days=request.recent_practice_days)
    success_cutoff = current_time - timedelta(days=RECENT_SUCCESS_DAYS)
    candidates: list[TrainingCandidate] = []
    owner_seen: set[tuple[str | None, str | None, str | None]] = set()
    fen_seen: set[str] = set()
    for raw in training.list_training_positions(data_dir):
        if request.exclude_current_game and raw.get("game_id") == current_game_id:
            continue
        mapping = taxonomy.map_attempt_category(str(raw.get("category") or ""))
        if mapping is None or mapping.skill_id not in allowed_skills:
            continue
        candidate = _candidate_from_position(
            raw,
            attempts=all_attempts,
            estimate=estimate_by_skill.get(mapping.skill_id),
        )
        if candidate is None:
            continue
        last_attempted = _timestamp(candidate.last_attempted_at)
        if (
            request.exclude_recently_practiced
            and last_attempted is not None
            and last_attempted >= practice_cutoff
        ):
            continue
        if (
            request.exclude_recently_solved
            and candidate.last_outcome == "success"
            and last_attempted is not None
            and last_attempted >= success_cutoff
        ):
            continue
        owner = (
            candidate.reference.game_id,
            candidate.reference.review_side,
            candidate.reference.critical_id,
        )
        fen = str(candidate.reference.fen)
        if owner in owner_seen or fen in fen_seen:
            continue
        owner_seen.add(owner)
        fen_seen.add(fen)
        candidates.append(candidate)

    confidence_rank = {"established": 0, "emerging": 1, "insufficient": 2, None: 3}
    difficulty_rank = {"standard": 0, "harder": 1, "easier": 2}
    candidates.sort(
        key=lambda candidate: (
            0 if set(candidate.skill_ids).intersection(focus) else 1,
            confidence_rank[candidate.estimate_confidence],
            -len(candidate.candidate_moves),
            0 if not candidate.attempted else 1,
            candidate.last_attempted_at or "",
            difficulty_rank.get(candidate.difficulty, 3),
            str(candidate.reference.game_id),
            str(candidate.reference.review_side),
            int(candidate.reference.ply or 0),
        )
    )
    return GetTrainingCandidatesResult(
        candidates=_diverse_prefix(candidates, min(request.limit, MAX_TRAINING_CANDIDATES))
    )


def validate_training_action(
    position_references: Sequence[PositionReference],
    objective_skill_ids: Sequence[str],
    *,
    source: str | None,
) -> StartTrainingActionResult:
    """Reload and normalize every position immediately before entering Puzzles."""

    if source != "agent_training_draft":
        raise TrainingActionUnavailableError("The training action has an invalid source.")
    if not 1 <= len(position_references) <= MAX_TRAINING_POSITIONS:
        raise TrainingActionUnavailableError("The training action has no usable positions.")
    canonical_skills: list[str] = []
    for supplied in objective_skill_ids:
        canonical = taxonomy.resolve_skill_id(supplied)
        if canonical is None or canonical != supplied or canonical in canonical_skills:
            raise TrainingActionUnavailableError("The training action has invalid objective skills.")
        canonical_skills.append(canonical)
    if not canonical_skills:
        raise TrainingActionUnavailableError("The training action has no objective skill.")

    normalized: list[PositionReference] = []
    supported_skills: set[str] = set()
    owners: set[tuple[str, str, str]] = set()
    fens: set[str] = set()
    for supplied in position_references:
        if not all(
            (
                supplied.game_id,
                supplied.review_side,
                supplied.critical_id,
                supplied.fen,
                supplied.ply is not None,
            )
        ):
            raise TrainingActionUnavailableError(
                "A training position is missing its artifact identity."
            )
        try:
            analysis, position = training.load_position(
                str(supplied.game_id),
                str(supplied.critical_id),
                str(supplied.review_side),
            )
            board = chess.Board(str(position["fen_before"]))
            ply = int(position["ply"])
            played = _move(
                board,
                (position.get("played_move") or {}).get("uci"),
                (position.get("played_move") or {}).get("san"),
            )
            first = (position.get("candidates") or [])[0]
            best = _move(
                board,
                (first.get("move") or {}).get("uci"),
                (first.get("move") or {}).get("san"),
            )
        except (IndexError, KeyError, TypeError, ValueError, training.TrainingPositionError) as exc:
            raise TrainingActionUnavailableError(
                "A source game or training position is no longer available."
            ) from exc
        if (
            analysis.get("game_id") != supplied.game_id
            or analysis.get("review_side") != supplied.review_side
            or board.fen() != supplied.fen
            or ply != supplied.ply
            or played is None
            or best is None
        ):
            raise TrainingActionUnavailableError(
                "A source training position changed after the draft was created."
            )
        facts = position.get("facts") or {}
        mapping = taxonomy.map_attempt_category(str(facts.get("primary_category") or ""))
        if mapping is None:
            raise TrainingActionUnavailableError(
                "A source position no longer has trainable taxonomy evidence."
            )
        supported_skills.add(mapping.skill_id)
        owner = (str(supplied.game_id), str(supplied.review_side), str(supplied.critical_id))
        if owner in owners or board.fen() in fens:
            raise TrainingActionUnavailableError("The training action contains duplicate positions.")
        owners.add(owner)
        fens.add(board.fen())
        normalized.append(
            PositionReference(
                game_id=supplied.game_id,
                review_side=supplied.review_side,
                critical_id=supplied.critical_id,
                ply=ply,
                fen=board.fen(),
            )
        )
    if not set(canonical_skills).issubset(supported_skills):
        raise TrainingActionUnavailableError(
            "The selected positions no longer support every training objective."
        )
    return StartTrainingActionResult(
        position_references=normalized,
        objective_skill_ids=canonical_skills,
        source="agent_training_draft",
    )


__all__ = [
    "MAX_TRAINING_CANDIDATES",
    "MAX_TRAINING_POSITIONS",
    "TrainingActionUnavailableError",
    "get_training_candidates",
    "validate_training_action",
]
