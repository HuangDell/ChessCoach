"""Deterministic SkillEstimate aggregation and rebuildable cache storage."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import ValidationError

from server import config
from server.core.agent.models import ChessReference, LearningObservation, SkillEstimate
from server.core.learning import taxonomy
from server.core.learning.observations import (
    LearningConsistencyError,
    LearningStorageError,
    ObservationStore,
)


ESTIMATES_SCHEMA_VERSION = 1
AGGREGATION_POLICY_VERSION = 1
RECENT_WINDOW_DAYS = 30
MAX_EXAMPLES = 3

MIN_CONFIDENCE_EVIDENCE = 2
MIN_CONFIDENCE_POSITIONS = 2
ESTABLISHED_EVIDENCE = 3
MIN_WEAKNESS_FAILURES = 2
MIN_WEAKNESS_POSITIONS = 2
WEAKNESS_FAILURE_RATE = 0.60
WEAKNESS_RATE_MIN_EVIDENCE = 3
WEAKNESS_RECENT_FAILURES = 2
WEAKNESS_CUMULATIVE_LOSS = 20.0
MIN_STRENGTH_SUCCESSES = 3
MIN_STRENGTH_POSITIONS = 2
STRENGTH_SUCCESS_RATE = 0.70

Window = Literal["lifetime", "recent"]
_ESTIMATE_LOCK = threading.RLock()


def _default_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError as exc:
            raise LearningConsistencyError("Estimate evidence time must be ISO-8601 UTC.") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise LearningConsistencyError("Estimate evidence time must be UTC.")
    return parsed.astimezone(timezone.utc)


def _iso_utc(value: datetime) -> str:
    return _as_utc(value).isoformat(timespec="seconds").replace("+00:00", "Z")


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _observation_digest(observations: Sequence[LearningObservation]) -> str:
    payload = [item.model_dump(mode="json", exclude_none=True) for item in observations]
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _position_key(observation: LearningObservation) -> str:
    if observation.game_id is not None:
        return ":".join(
            (
                "game",
                observation.game_id,
                str(observation.review_side),
                str(observation.critical_id),
            )
        )
    if observation.puzzle_id is not None:
        return f"puzzle:{observation.puzzle_id}"
    # Strict observation validation currently prevents this fallback, but keeping it stable
    # makes old migrated observations conservative rather than merging unrelated attempts.
    return f"attempt:{observation.attempt_id or observation.observation_id}"


def _reference(observation: LearningObservation) -> ChessReference:
    if observation.puzzle_id is not None:
        return ChessReference(kind="puzzle", puzzle_id=observation.puzzle_id)
    if observation.game_id is not None and observation.critical_id is not None:
        return ChessReference(
            kind="critical_position",
            game_id=observation.game_id,
            review_side=observation.review_side,
            critical_id=observation.critical_id,
        )
    if observation.game_id is not None:
        return ChessReference(kind="game", game_id=observation.game_id)
    raise LearningConsistencyError("Observation cannot produce a verified chess reference.")


def _confidence(evidence_count: int, distinct_positions: int) -> str:
    if evidence_count < MIN_CONFIDENCE_EVIDENCE or distinct_positions < MIN_CONFIDENCE_POSITIONS:
        return "insufficient"
    if evidence_count >= ESTABLISHED_EVIDENCE:
        return "established"
    return "emerging"


def _status(
    observations: Sequence[LearningObservation], *, recent_failure_count: int
) -> str:
    evidence_count = len(observations)
    if evidence_count == 0:
        return "unknown"
    failures = [item for item in observations if item.outcome == "failure"]
    successes = [item for item in observations if item.outcome == "success"]
    failure_positions = {_position_key(item) for item in failures}
    success_positions = {_position_key(item) for item in successes}
    cumulative_loss = sum(float(item.severity or 0.0) for item in failures)
    failure_rate = len(failures) / evidence_count
    success_rate = len(successes) / evidence_count
    weakness_signal = (
        (failure_rate >= WEAKNESS_FAILURE_RATE and evidence_count >= WEAKNESS_RATE_MIN_EVIDENCE)
        or recent_failure_count >= WEAKNESS_RECENT_FAILURES
        or cumulative_loss >= WEAKNESS_CUMULATIVE_LOSS
    )
    is_weakness = (
        len(failures) >= MIN_WEAKNESS_FAILURES
        and len(failure_positions) >= MIN_WEAKNESS_POSITIONS
        and weakness_signal
    )
    if is_weakness:
        return "weakness"
    is_strength = (
        len(successes) >= MIN_STRENGTH_SUCCESSES
        and len(success_positions) >= MIN_STRENGTH_POSITIONS
        and success_rate >= STRENGTH_SUCCESS_RATE
        and recent_failure_count == 0
    )
    if is_strength:
        return "strength"
    if failures:
        return "watch"
    return "unknown"


def _examples(
    observations: Sequence[LearningObservation], *, status: str
) -> list[ChessReference]:
    preferred_outcome = {
        "weakness": "failure",
        "watch": "failure",
        "strength": "success",
    }.get(status)
    preferred = [item for item in observations if item.outcome == preferred_outcome]
    candidates = preferred or list(observations)
    candidates.sort(
        key=lambda item: (
            _as_utc(item.occurred_at),
            float(item.severity or 0.0),
            item.observation_id,
        ),
        reverse=True,
    )
    examples: list[ChessReference] = []
    seen: set[str] = set()
    for observation in candidates:
        position = _position_key(observation)
        if position in seen:
            continue
        seen.add(position)
        examples.append(_reference(observation))
        if len(examples) == MAX_EXAMPLES:
            break
    return examples


def estimate_skill(
    skill_id: str,
    observations: Iterable[LearningObservation],
    *,
    now: datetime,
) -> SkillEstimate:
    now = _as_utc(now)
    relevant = sorted(
        (item for item in observations if item.skill_id == skill_id),
        key=lambda item: (_as_utc(item.occurred_at), item.observation_id),
    )
    cutoff = now - timedelta(days=RECENT_WINDOW_DAYS)
    recent_failures = sum(
        1
        for item in relevant
        if item.outcome == "failure" and cutoff <= _as_utc(item.occurred_at) <= now
    )
    status = _status(relevant, recent_failure_count=recent_failures)
    success_count = sum(item.outcome == "success" for item in relevant)
    partial_count = sum(item.outcome == "partial" for item in relevant)
    failure_count = sum(item.outcome == "failure" for item in relevant)
    positions = {_position_key(item) for item in relevant}
    games = {item.game_id for item in relevant if item.game_id is not None}
    return SkillEstimate(
        schema_version=ESTIMATES_SCHEMA_VERSION,
        taxonomy_version=taxonomy.TAXONOMY_VERSION,
        skill_id=skill_id,
        evidence_count=len(relevant),
        distinct_games=len(games),
        distinct_positions=len(positions),
        success_count=success_count,
        partial_count=partial_count,
        failure_count=failure_count,
        cumulative_loss=round(
            sum(float(item.severity or 0.0) for item in relevant if item.outcome == "failure"),
            3,
        ),
        recent_failure_count=recent_failures,
        last_seen=relevant[-1].occurred_at if relevant else None,
        confidence_level=_confidence(len(relevant), len(positions)),
        status=status,
        examples=_examples(relevant, status=status),
    )


def aggregate_observations(
    observations: Iterable[LearningObservation],
    *,
    now: datetime | None = None,
) -> dict[str, dict[str, SkillEstimate]]:
    current = _as_utc(now or _default_now())
    all_observations = sorted(
        list(observations), key=lambda item: (_as_utc(item.occurred_at), item.dedupe_key)
    )
    cutoff = current - timedelta(days=RECENT_WINDOW_DAYS)
    recent = [
        item for item in all_observations if cutoff <= _as_utc(item.occurred_at) <= current
    ]
    snapshots: dict[str, dict[str, SkillEstimate]] = {}
    for definition in taxonomy.SKILL_DEFINITIONS:
        skill_id = definition.skill_id
        snapshots[skill_id] = {
            "lifetime": estimate_skill(skill_id, all_observations, now=current),
            "recent": estimate_skill(skill_id, recent, now=current),
        }
    return snapshots


_CONFIDENCE_RANK = {"established": 0, "emerging": 1, "insufficient": 2}


def rank_estimates(
    estimates: Iterable[SkillEstimate],
    *,
    focus_skill_id: str | None = None,
    relevant_skill_ids: Iterable[str] = (),
) -> list[SkillEstimate]:
    relevant = set(relevant_skill_ids)

    def key(estimate: SkillEstimate) -> tuple[Any, ...]:
        exact = 0 if focus_skill_id and estimate.skill_id == focus_skill_id else 1
        mapped = 0 if estimate.skill_id in relevant else 1
        recurrence = estimate.recent_failure_count
        success_rate = (
            estimate.success_count / estimate.evidence_count if estimate.evidence_count else 0.0
        )
        magnitude = (
            estimate.cumulative_loss
            if estimate.status in {"weakness", "watch"}
            else success_rate * 100.0
        )
        return (
            exact,
            mapped,
            _CONFIDENCE_RANK[estimate.confidence_level],
            -recurrence,
            -magnitude,
            estimate.skill_id,
        )

    return sorted(estimates, key=key)


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError as exc:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise LearningStorageError(f"Could not atomically update {path}.") from exc


class EstimateStore:
    """Versioned cache of lifetime and recent deterministic estimates."""

    def __init__(
        self,
        data_dir: str | os.PathLike[str] | None = None,
        *,
        now: Callable[[], datetime] = _default_now,
    ) -> None:
        self.data_dir = Path(data_dir or config.DATA_DIR)
        self.path = self.data_dir / "learning" / "estimates.json"
        self._now = now

    def _validate_envelope(
        self,
        raw: Any,
        *,
        expected_digest: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raise LearningConsistencyError("Estimates cache must be a JSON object.")
        expected_versions = {
            "schema_version": ESTIMATES_SCHEMA_VERSION,
            "taxonomy_version": taxonomy.TAXONOMY_VERSION,
            "aggregation_policy_version": AGGREGATION_POLICY_VERSION,
            "recent_window_days": RECENT_WINDOW_DAYS,
        }
        if any(raw.get(key) != value for key, value in expected_versions.items()):
            raise LearningConsistencyError("Estimates cache version is incompatible.")
        _as_utc(str(raw.get("generated_at") or ""))
        digest = str(raw.get("observations_digest") or "")
        if not digest or (expected_digest is not None and digest != expected_digest):
            raise LearningConsistencyError("Estimates cache does not match canonical observations.")
        skills = raw.get("skills")
        if not isinstance(skills, list):
            raise LearningConsistencyError("Estimates cache has no skill snapshots.")
        expected_skill_ids = [definition.skill_id for definition in taxonomy.SKILL_DEFINITIONS]
        if [str(item.get("skill_id") or "") for item in skills if isinstance(item, dict)] != expected_skill_ids:
            raise LearningConsistencyError("Estimates cache skill set is incomplete or unordered.")
        validated_skills: list[dict[str, Any]] = []
        for item in skills:
            if not isinstance(item, dict):
                raise LearningConsistencyError("Estimate skill snapshot must be an object.")
            skill_id = str(item.get("skill_id") or "")
            for window in ("lifetime", "recent"):
                snapshot = item.get(window)
                if not isinstance(snapshot, dict):
                    raise LearningConsistencyError("Estimate snapshot must be an object.")
                if snapshot.get("schema_version") != ESTIMATES_SCHEMA_VERSION:
                    raise LearningConsistencyError(
                        "Estimate snapshot schema version mismatch."
                    )
                if snapshot.get("taxonomy_version") != taxonomy.TAXONOMY_VERSION:
                    raise LearningConsistencyError(
                        "Estimate snapshot taxonomy version mismatch."
                    )
            try:
                lifetime = SkillEstimate.model_validate(item.get("lifetime"))
                recent = SkillEstimate.model_validate(item.get("recent"))
            except ValidationError as exc:
                raise LearningConsistencyError("Invalid SkillEstimate cache snapshot.") from exc
            if lifetime.skill_id != skill_id or recent.skill_id != skill_id:
                raise LearningConsistencyError("Estimate snapshot skill ownership mismatch.")
            if (
                lifetime.schema_version != ESTIMATES_SCHEMA_VERSION
                or recent.schema_version != ESTIMATES_SCHEMA_VERSION
            ):
                raise LearningConsistencyError("Estimate snapshot schema version mismatch.")
            if (
                lifetime.taxonomy_version != taxonomy.TAXONOMY_VERSION
                or recent.taxonomy_version != taxonomy.TAXONOMY_VERSION
            ):
                raise LearningConsistencyError("Estimate snapshot taxonomy version mismatch.")
            validated_skills.append(
                {"skill_id": skill_id, "lifetime": lifetime, "recent": recent}
            )
        return {**raw, "skills": validated_skills}

    @staticmethod
    def _validate_snapshot_semantics(
        envelope: dict[str, Any],
        observations: Sequence[LearningObservation],
        *,
        now: datetime,
    ) -> dict[str, Any]:
        expected = aggregate_observations(observations, now=now)
        for row in envelope["skills"]:
            skill_id = row["skill_id"]
            for window in ("lifetime", "recent"):
                actual_dump = row[window].model_dump(mode="json", exclude_none=True)
                expected_dump = expected[skill_id][window].model_dump(
                    mode="json", exclude_none=True
                )
                if actual_dump != expected_dump:
                    raise LearningConsistencyError(
                        f"Estimate {window} snapshot does not match canonical observations."
                    )
        return envelope

    def _load_current_envelope(self) -> dict[str, Any]:
        observations = ObservationStore(self.data_dir).load()
        digest = _observation_digest(observations)
        now = _as_utc(self._now())
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            envelope = self._validate_envelope(raw, expected_digest=digest)
            return self._validate_snapshot_semantics(
                envelope,
                observations,
                now=now,
            )
        except (FileNotFoundError, OSError, json.JSONDecodeError, LearningStorageError):
            return self.rebuild(observations=observations)

    def load_envelope(self) -> dict[str, Any]:
        with _ESTIMATE_LOCK:
            return self._load_current_envelope()

    def load(self, *, window: Window = "lifetime") -> list[SkillEstimate]:
        return self.ensure_current(window=window)

    def rebuild(
        self, *, observations: Iterable[LearningObservation] | None = None
    ) -> dict[str, Any]:
        with _ESTIMATE_LOCK:
            source = list(observations) if observations is not None else ObservationStore(self.data_dir).load()
            source.sort(key=lambda item: (item.occurred_at, item.dedupe_key))
            now = _as_utc(self._now())
            snapshots = aggregate_observations(source, now=now)
            envelope: dict[str, Any] = {
                "schema_version": ESTIMATES_SCHEMA_VERSION,
                "taxonomy_version": taxonomy.TAXONOMY_VERSION,
                "aggregation_policy_version": AGGREGATION_POLICY_VERSION,
                "recent_window_days": RECENT_WINDOW_DAYS,
                "generated_at": _iso_utc(now),
                "observations_digest": _observation_digest(source),
                "skills": [
                    {
                        "skill_id": definition.skill_id,
                        "lifetime": snapshots[definition.skill_id]["lifetime"].model_dump(
                            mode="json", exclude_none=True
                        ),
                        "recent": snapshots[definition.skill_id]["recent"].model_dump(
                            mode="json", exclude_none=True
                        ),
                    }
                    for definition in taxonomy.SKILL_DEFINITIONS
                ],
            }
            _atomic_json(self.path, envelope)
            return self._validate_envelope(envelope, expected_digest=_observation_digest(source))

    def ensure_current(self, *, window: Window = "lifetime") -> list[SkillEstimate]:
        if window not in {"lifetime", "recent"}:
            raise ValueError("window must be lifetime or recent")
        with _ESTIMATE_LOCK:
            envelope = self._load_current_envelope()
            return [item[window] for item in envelope["skills"]]

    def delete_game(self, game_id: str, *, window: Window = "lifetime") -> list[SkillEstimate]:
        ObservationStore(self.data_dir, now=self._now).delete_game(
            game_id, sync_estimates=False
        )
        self.rebuild()
        return self.ensure_current(window=window)


def rebuild(
    *,
    data_dir: str | os.PathLike[str] | None = None,
    observations: Iterable[LearningObservation] | None = None,
    now: Callable[[], datetime] = _default_now,
) -> dict[str, Any]:
    return EstimateStore(data_dir, now=now).rebuild(observations=observations)


def ensure_current(
    *,
    data_dir: str | os.PathLike[str] | None = None,
    window: Window = "lifetime",
    now: Callable[[], datetime] = _default_now,
) -> list[SkillEstimate]:
    return EstimateStore(data_dir, now=now).ensure_current(window=window)


__all__ = [
    "AGGREGATION_POLICY_VERSION",
    "ESTIMATES_SCHEMA_VERSION",
    "EstimateStore",
    "MAX_EXAMPLES",
    "RECENT_WINDOW_DAYS",
    "aggregate_observations",
    "ensure_current",
    "estimate_skill",
    "rank_estimates",
    "rebuild",
]
