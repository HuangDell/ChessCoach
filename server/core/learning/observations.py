"""Canonical, rebuildable learning observations.

Analysis and attempt artifacts remain the source of truth.  This module projects
them into a strictly validated JSONL index whose rows are deterministic and
deduplicated by a stable event key.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import chess
from pydantic import ValidationError

from server import config
from server.core.agent.models import LearningObservation
from server.core.learning import taxonomy
from server.core.storage.coordination import coordinated_learning_source_mutation


OBSERVATION_SCHEMA_VERSION = 1
_OBSERVATION_LOCK = threading.RLock()
_UTC_EPOCH = "1970-01-01T00:00:00Z"


class LearningStorageError(RuntimeError):
    """The canonical learning store could not be read or durably updated."""


class LearningConsistencyError(LearningStorageError):
    """Two persisted records claim the same identity with different content."""


def _default_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso_utc(value: str | datetime | None, *, fallback: str | None = None) -> str:
    if value is None:
        if fallback is None:
            raise LearningConsistencyError("Learning evidence has no UTC occurrence time.")
        value = fallback
    if isinstance(value, datetime):
        parsed = value
    else:
        raw = str(value).strip()
        if not raw:
            raise LearningConsistencyError("Learning evidence has an empty occurrence time.")
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError as exc:
            raise LearningConsistencyError(
                "Learning evidence occurrence time must be ISO-8601 UTC."
            ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise LearningConsistencyError("Learning evidence occurrence time must be UTC.")
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _stable_id(prefix: str, value: str, *, length: int = 32) -> str:
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]
    return f"{prefix}-{digest}"


def _observation_id(dedupe_key: str) -> str:
    return _stable_id("obs", dedupe_key)


def _legacy_attempt_id(attempt: Mapping[str, Any]) -> str:
    payload = {key: value for key, value in attempt.items() if key != "attempt_id"}
    return _stable_id("legacy", _canonical_json(payload))


def _model_dump(observation: LearningObservation) -> dict[str, Any]:
    return observation.model_dump(mode="json", exclude_none=True)


def _atomic_jsonl(path: Path, observations: Iterable[LearningObservation]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            for observation in observations:
                handle.write(_canonical_json(_model_dump(observation)) + "\n")
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


def _supported_evidence(skill_id: str, evidence_type: str) -> bool:
    helper = getattr(taxonomy, "supports_evidence_type", None)
    if helper is not None:
        return bool(helper(skill_id, evidence_type))
    definition = taxonomy.get_skill_definition(skill_id)
    return bool(definition and evidence_type in definition.supported_evidence_types)


def _expected_dedupe_key(observation: LearningObservation) -> str:
    if observation.source_type == "game_fact":
        return ":".join(
            (
                "game_fact",
                str(observation.game_id),
                str(observation.review_side),
                str(observation.critical_id),
                observation.skill_id,
                observation.outcome,
            )
        )
    return ":".join(
        (
            "attempt",
            str(observation.attempt_id),
            observation.skill_id,
            observation.outcome,
        )
    )


def _validate_observation(value: LearningObservation | Mapping[str, Any]) -> LearningObservation:
    try:
        observation = LearningObservation.model_validate(value)
    except ValidationError as exc:
        raise LearningConsistencyError("Invalid learning observation payload.") from exc
    if observation.schema_version != OBSERVATION_SCHEMA_VERSION:
        raise LearningConsistencyError("Unsupported learning observation schema version.")
    if observation.taxonomy_version != taxonomy.TAXONOMY_VERSION:
        raise LearningConsistencyError("Unsupported learning observation taxonomy version.")
    if taxonomy.get_skill_definition(observation.skill_id) is None:
        raise LearningConsistencyError(f"Unknown canonical skill: {observation.skill_id}.")
    if not _supported_evidence(observation.skill_id, observation.evidence_type):
        raise LearningConsistencyError(
            f"Skill {observation.skill_id} does not support {observation.evidence_type} evidence."
        )
    normalized_time = _iso_utc(observation.occurred_at)
    if normalized_time != observation.occurred_at:
        observation = observation.model_copy(update={"occurred_at": normalized_time})

    has_game = observation.game_id is not None
    has_position = observation.review_side is not None and observation.critical_id is not None
    has_puzzle = observation.puzzle_id is not None
    if has_game != has_position:
        raise LearningConsistencyError(
            "Game-backed observations require game, review-side, and critical-position ownership."
        )
    if has_game and has_puzzle:
        raise LearningConsistencyError("An observation cannot claim game and puzzle ownership.")
    if observation.source_type == "game_fact":
        if not has_game or observation.attempt_id is not None:
            raise LearningConsistencyError("Game-fact evidence requires only game ownership.")
    else:
        if observation.attempt_id is None:
            raise LearningConsistencyError("Attempt evidence requires an attempt id.")
        if observation.source_type in {"retry_attempt", "training_attempt"} and not has_game:
            raise LearningConsistencyError("Retry/training evidence requires a verified game position.")
        if observation.source_type == "puzzle_attempt" and (has_game or not has_puzzle):
            raise LearningConsistencyError("External puzzle evidence requires puzzle ownership.")

    expected_key = _expected_dedupe_key(observation)
    if observation.dedupe_key != expected_key:
        raise LearningConsistencyError("Observation dedupe key does not match its ownership fields.")
    if observation.observation_id != _observation_id(expected_key):
        raise LearningConsistencyError("Observation id is not the stable hash of its dedupe key.")
    return observation


def _evidence_fields(mapping: Any) -> tuple[str, str, tuple[str, ...]]:
    if isinstance(mapping, Mapping):
        skill_id = mapping.get("skill_id")
        evidence_type = mapping.get("evidence_type")
        refs = mapping.get("evidence_refs") or ()
    else:
        skill_id = getattr(mapping, "skill_id", None)
        evidence_type = getattr(mapping, "evidence_type", None)
        refs = getattr(mapping, "evidence_refs", ()) or ()
    if not skill_id or not evidence_type:
        raise LearningConsistencyError("Taxonomy mapping omitted skill or evidence type.")
    return str(skill_id), str(evidence_type), tuple(str(item) for item in refs)


def _attempt_outcome(attempt: Mapping[str, Any]) -> str | None:
    verdict = str(attempt.get("verdict") or "").strip().lower()
    hints = max(0, int(attempt.get("hints_used") or 0))
    explicit = str(attempt.get("outcome") or "").strip().lower()
    if explicit in {"success", "partial", "failure"}:
        return "partial" if explicit == "success" and hints else explicit
    gave_up = bool(attempt.get("gave_up")) or (
        attempt.get("selected_move") is None
        and not bool(attempt.get("solved"))
        and verdict in {"", "unknown", "give_up", "gave_up"}
    )
    if gave_up or verdict in {"same_as_game", "bad", "give_up", "gave_up", "failure"}:
        return "failure"
    if verdict == "unknown":
        return None
    if verdict == "inaccurate":
        return "partial"
    if verdict in {"best", "acceptable", "success"} or bool(attempt.get("solved")):
        return "success" if hints == 0 else "partial"
    if verdict in {"partial", "hinted"}:
        return "partial"
    return None


def _mapping_refs(
    mapping_refs: Iterable[str], *, owner_ref: str, prefix: str
) -> list[str]:
    refs = [owner_ref]
    refs.extend(
        ref if ref.startswith(("analysis:", "attempt:", "puzzle:")) else f"{prefix}.{ref}"
        for ref in mapping_refs
        if ref
    )
    return list(dict.fromkeys(refs))


class ObservationStore:
    """Single-process, lock-protected canonical observation storage."""

    def __init__(
        self,
        data_dir: str | os.PathLike[str] | None = None,
        *,
        now: Callable[[], datetime] = _default_now,
    ) -> None:
        self.data_dir = Path(data_dir or config.DATA_DIR)
        self.path = self.data_dir / "learning" / "observations.jsonl"
        self._now = now

    def load(self) -> list[LearningObservation]:
        with _OBSERVATION_LOCK:
            try:
                lines = self.path.read_text(encoding="utf-8").splitlines()
            except FileNotFoundError:
                return []
            except OSError as exc:
                raise LearningStorageError(f"Could not read {self.path}.") from exc
            observations: list[LearningObservation] = []
            by_key: dict[str, LearningObservation] = {}
            for line_number, line in enumerate(lines, start=1):
                if not line.strip():
                    continue
                try:
                    raw = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise LearningConsistencyError(
                        f"Invalid observations JSON on line {line_number}."
                    ) from exc
                observation = _validate_observation(raw)
                existing = by_key.get(observation.dedupe_key)
                if existing is not None:
                    if _model_dump(existing) != _model_dump(observation):
                        raise LearningConsistencyError(
                            f"Conflicting observation key: {observation.dedupe_key}."
                        )
                    continue
                by_key[observation.dedupe_key] = observation
                observations.append(observation)
            return sorted(observations, key=lambda item: (item.occurred_at, item.dedupe_key))

    def replace(self, observations: Iterable[LearningObservation | Mapping[str, Any]]) -> list[LearningObservation]:
        ordered = self._ordered_observations(observations)
        with _OBSERVATION_LOCK:
            _atomic_jsonl(self.path, ordered)
        return ordered

    @staticmethod
    def _ordered_observations(
        observations: Iterable[LearningObservation | Mapping[str, Any]],
    ) -> list[LearningObservation]:
        validated = [_validate_observation(item) for item in observations]
        by_key: dict[str, LearningObservation] = {}
        for observation in validated:
            existing = by_key.get(observation.dedupe_key)
            if existing is not None and _model_dump(existing) != _model_dump(observation):
                raise LearningConsistencyError(
                    f"Conflicting observation key: {observation.dedupe_key}."
                )
            by_key[observation.dedupe_key] = observation
        return sorted(by_key.values(), key=lambda item: (item.occurred_at, item.dedupe_key))

    def append_many(
        self, observations: Iterable[LearningObservation | Mapping[str, Any]]
    ) -> list[LearningObservation]:
        incoming = [_validate_observation(item) for item in observations]
        if not incoming:
            return []
        with _OBSERVATION_LOCK:
            current = self.load()
            by_key = {item.dedupe_key: item for item in current}
            appended: list[LearningObservation] = []
            for observation in incoming:
                existing = by_key.get(observation.dedupe_key)
                if existing is not None:
                    if _model_dump(existing) != _model_dump(observation):
                        raise LearningConsistencyError(
                            f"Conflicting observation key: {observation.dedupe_key}."
                        )
                    continue
                by_key[observation.dedupe_key] = observation
                appended.append(observation)
            if appended:
                _atomic_jsonl(
                    self.path,
                    sorted(by_key.values(), key=lambda item: (item.occurred_at, item.dedupe_key)),
                )
            return appended

    def _analysis_observations(
        self,
        analysis: Mapping[str, Any],
        *,
        occurred_at: str | datetime | None = None,
        history_record: Mapping[str, Any] | None = None,
        artifact_path: Path | None = None,
    ) -> list[LearningObservation]:
        game_id = str(analysis.get("game_id") or "").strip()
        review_side = str(analysis.get("review_side") or "").strip()
        if not game_id or review_side not in {"white", "black"}:
            raise LearningConsistencyError("Analysis artifact has invalid game ownership.")
        fallback = None
        for candidate in (
            occurred_at,
            analysis.get("generated_at"),
            analysis.get("analyzed_at"),
            (history_record or {}).get("analyzed_at"),
        ):
            if candidate:
                fallback = candidate
                break
        if fallback is None and artifact_path is not None:
            try:
                fallback = datetime.fromtimestamp(artifact_path.stat().st_mtime, timezone.utc)
            except OSError:
                fallback = None
        timestamp = _iso_utc(fallback, fallback=_UTC_EPOCH)
        observations: list[LearningObservation] = []
        seen_positions: set[str] = set()
        for position in analysis.get("critical_positions") or []:
            if not isinstance(position, Mapping):
                raise LearningConsistencyError("Analysis critical position must be an object.")
            critical_id = str(position.get("critical_id") or "").strip()
            facts = position.get("facts") or {}
            if not critical_id or not isinstance(facts, Mapping):
                raise LearningConsistencyError("Analysis critical position has invalid facts ownership.")
            if critical_id in seen_positions:
                raise LearningConsistencyError("Analysis contains duplicate critical-position ids.")
            seen_positions.add(critical_id)
            fact_critical = facts.get("critical_id")
            if fact_critical is not None and str(fact_critical) != critical_id:
                raise LearningConsistencyError("Facts critical id does not match its owner.")
            mappings = taxonomy.map_analysis_position(
                analysis, position, history_record=history_record
            )
            owner_ref = f"analysis:{game_id}:{review_side}:{critical_id}"
            for mapping in mappings:
                skill_id, evidence_type, mapping_refs = _evidence_fields(mapping)
                outcome = "failure"
                dedupe_key = ":".join(
                    ("game_fact", game_id, review_side, critical_id, skill_id, outcome)
                )
                payload = {
                    "schema_version": OBSERVATION_SCHEMA_VERSION,
                    "taxonomy_version": taxonomy.TAXONOMY_VERSION,
                    "observation_id": _observation_id(dedupe_key),
                    "dedupe_key": dedupe_key,
                    "skill_id": skill_id,
                    "evidence_type": evidence_type,
                    "outcome": outcome,
                    "source_type": "game_fact",
                    "game_id": game_id,
                    "review_side": review_side,
                    "critical_id": critical_id,
                    "severity": max(0.0, float(position.get("win_loss") or 0.0)),
                    "evidence_refs": _mapping_refs(
                        mapping_refs, owner_ref=owner_ref, prefix=f"{owner_ref}:facts"
                    ),
                    "occurred_at": timestamp,
                }
                observations.append(_validate_observation(payload))
        return observations

    def ingest_analysis(
        self,
        analysis: Mapping[str, Any],
        *,
        occurred_at: str | datetime | None = None,
        history_record: Mapping[str, Any] | None = None,
        sync_estimates: bool = True,
    ) -> list[LearningObservation]:
        observations = self._analysis_observations(
            analysis, occurred_at=occurred_at, history_record=history_record
        )
        appended = self.append_many(observations)
        if sync_estimates:
            self._sync_estimates()
        return appended

    def _analysis_candidates(
        self, game_id: str, review_side: str | None
    ) -> list[tuple[dict[str, Any], Path]]:
        game_dir = self.data_dir / "games" / game_id
        paths: list[Path] = []
        if review_side in {"white", "black"}:
            paths.append(game_dir / "analysis" / f"{review_side}.json")
        else:
            paths.extend(game_dir / "analysis" / f"{side}.json" for side in ("white", "black"))
            paths.append(game_dir / "analysis.json")
        candidates: dict[str, tuple[dict[str, Any], Path]] = {}
        for path in paths:
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                continue
            except (OSError, json.JSONDecodeError) as exc:
                raise LearningConsistencyError(f"Invalid owning analysis artifact: {path}.") from exc
            if not isinstance(raw, dict) or raw.get("game_id") != game_id:
                raise LearningConsistencyError("Attempt owner analysis identity mismatch.")
            side = str(raw.get("review_side") or "")
            if side not in {"white", "black"} or (review_side and side != review_side):
                raise LearningConsistencyError("Attempt owner review side mismatch.")
            candidates.setdefault(side, (raw, path))
        return list(candidates.values())

    def _attempt_observations(
        self,
        attempt: Mapping[str, Any],
        *,
        analysis: Mapping[str, Any] | None = None,
    ) -> list[LearningObservation]:
        outcome = _attempt_outcome(attempt)
        if outcome is None:
            return []
        game_id = str(attempt.get("game_id") or "").strip()
        critical_id = str(attempt.get("critical_id") or "").strip()
        review_side_raw = attempt.get("review_side", attempt.get("reviewed_side"))
        review_side = str(review_side_raw or "").strip() or None
        if not game_id or not critical_id or (review_side and review_side not in {"white", "black"}):
            raise LearningConsistencyError("Attempt has invalid game-position ownership.")

        candidates: list[Mapping[str, Any]]
        if analysis is not None:
            candidates = [analysis]
        else:
            candidates = [item for item, _path in self._analysis_candidates(game_id, review_side)]
        owners: list[tuple[Mapping[str, Any], Mapping[str, Any]]] = []
        for artifact in candidates:
            if artifact.get("game_id") != game_id:
                raise LearningConsistencyError("Attempt owner analysis identity mismatch.")
            side = str(artifact.get("review_side") or "")
            if side not in {"white", "black"} or (review_side and side != review_side):
                continue
            found = [
                item
                for item in artifact.get("critical_positions") or []
                if isinstance(item, Mapping) and item.get("critical_id") == critical_id
            ]
            if len(found) > 1:
                raise LearningConsistencyError("Attempt owner analysis has duplicate critical ids.")
            if found:
                owners.append((artifact, found[0]))
        if len(owners) != 1:
            raise LearningConsistencyError(
                "Attempt review side and critical position cannot be uniquely verified."
            )
        owner_analysis, position = owners[0]
        review_side = str(owner_analysis["review_side"])
        facts = position.get("facts") or {}
        if not isinstance(facts, Mapping):
            raise LearningConsistencyError("Attempt owner has no verified facts.")
        category = str(facts.get("primary_category") or "").strip()
        supplied_category = str(attempt.get("category") or "").strip()
        if supplied_category and supplied_category != category:
            raise LearningConsistencyError("Attempt category does not match owning analysis facts.")
        mapping = taxonomy.map_attempt_category(category)
        if mapping is None:
            return []
        skill_id, evidence_type, mapping_refs = _evidence_fields(mapping)
        raw_attempt_id = str(attempt.get("attempt_id") or "").strip()
        attempt_id = raw_attempt_id or _legacy_attempt_id(attempt)
        source = str(attempt.get("source") or "training").strip().lower()
        if source == "retry":
            source_type = "retry_attempt"
        elif source in {"puzzle", "personal_puzzle", "your_games"}:
            # Personal puzzles are still owned by their source game position.  The dedicated
            # puzzle_attempt source is reserved for externally identified puzzle artifacts.
            source_type = "training_attempt"
        else:
            source_type = "training_attempt"
        dedupe_key = ":".join(("attempt", attempt_id, skill_id, outcome))
        owner_ref = f"attempt:{attempt_id}"
        timestamp = _iso_utc(
            attempt.get("attempted_at", attempt.get("occurred_at")), fallback=_UTC_EPOCH
        )
        severity_raw = attempt.get("win_gap_from_best", attempt.get("severity"))
        payload = {
            "schema_version": OBSERVATION_SCHEMA_VERSION,
            "taxonomy_version": taxonomy.TAXONOMY_VERSION,
            "observation_id": _observation_id(dedupe_key),
            "dedupe_key": dedupe_key,
            "skill_id": skill_id,
            "evidence_type": evidence_type,
            "outcome": outcome,
            "source_type": source_type,
            "game_id": game_id,
            "review_side": review_side,
            "critical_id": critical_id,
            "attempt_id": attempt_id,
            "severity": max(0.0, float(severity_raw)) if severity_raw is not None else None,
            "evidence_refs": _mapping_refs(
                mapping_refs,
                owner_ref=owner_ref,
                prefix=f"analysis:{game_id}:{review_side}:{critical_id}:facts",
            ),
            "occurred_at": timestamp,
        }
        return [_validate_observation(payload)]

    def ingest_attempt(
        self,
        attempt: Mapping[str, Any],
        *,
        analysis: Mapping[str, Any] | None = None,
        sync_estimates: bool = True,
    ) -> list[LearningObservation]:
        observations = self._attempt_observations(attempt, analysis=analysis)
        appended = self.append_many(observations)
        if sync_estimates:
            self._sync_estimates()
        return appended

    def _puzzle_attempt_observations(
        self, attempt: Mapping[str, Any]
    ) -> list[LearningObservation]:
        if attempt.get("game_id"):
            return self._attempt_observations(attempt)
        outcome = _attempt_outcome(attempt)
        if outcome is None:
            return []
        attempt_id = str(attempt.get("attempt_id") or "").strip()
        puzzle_id = str(attempt.get("puzzle_id", attempt.get("id")) or "").strip()
        themes = attempt.get("verified_themes")
        if themes is None and attempt.get("themes_verified") is True:
            themes = attempt.get("themes")
        if not attempt_id or not puzzle_id or not isinstance(themes, list) or not themes:
            # Old puzzle history has neither a stable attempt id nor verified themes and is not
            # safe to reverse-project into long-term evidence.
            return []
        fen = str(attempt.get("fen") or "").strip()
        if not fen:
            raise LearningConsistencyError("External puzzle evidence requires a FEN owner.")
        try:
            board = chess.Board(fen)
        except ValueError as exc:
            raise LearningConsistencyError(
                "External puzzle evidence has an invalid FEN owner."
            ) from exc
        if not board.is_valid():
            raise LearningConsistencyError("External puzzle evidence has an invalid FEN owner.")
        mappings = taxonomy.map_lichess_themes(themes)
        timestamp = _iso_utc(
            attempt.get("attempted_at", attempt.get("occurred_at", attempt.get("date"))),
            fallback=_UTC_EPOCH,
        )
        observations: list[LearningObservation] = []
        for mapping in mappings:
            skill_id, evidence_type, mapping_refs = _evidence_fields(mapping)
            dedupe_key = ":".join(("attempt", attempt_id, skill_id, outcome))
            owner_ref = f"puzzle:{puzzle_id}:attempt:{attempt_id}"
            payload = {
                "schema_version": OBSERVATION_SCHEMA_VERSION,
                "taxonomy_version": taxonomy.TAXONOMY_VERSION,
                "observation_id": _observation_id(dedupe_key),
                "dedupe_key": dedupe_key,
                "skill_id": skill_id,
                "evidence_type": evidence_type,
                "outcome": outcome,
                "source_type": "puzzle_attempt",
                "puzzle_id": puzzle_id,
                "attempt_id": attempt_id,
                "severity": None,
                "evidence_refs": _mapping_refs(
                    mapping_refs, owner_ref=owner_ref, prefix=f"puzzle:{puzzle_id}:themes"
                ),
                "occurred_at": timestamp,
            }
            observations.append(_validate_observation(payload))
        return observations

    def ingest_puzzle_attempt(
        self, attempt: Mapping[str, Any], *, sync_estimates: bool = True
    ) -> list[LearningObservation]:
        observations = self._puzzle_attempt_observations(attempt)
        appended = self.append_many(observations)
        if sync_estimates:
            self._sync_estimates()
        return appended

    def _history_records(self) -> dict[tuple[str, str], dict[str, Any]]:
        path = self.data_dir / "history" / "games.jsonl"
        records: dict[tuple[str, str], dict[str, Any]] = {}
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return records
        except OSError as exc:
            raise LearningStorageError(f"Could not read {path}.") from exc
        for line in lines:
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(item, dict):
                continue
            game_id = str(item.get("game_id") or "")
            side = str(item.get("reviewed_side", item.get("review_side")) or "")
            if game_id and side in {"white", "black"}:
                key = (game_id, side)
                previous = records.get(key)
                if previous is None or str(item.get("analyzed_at") or "") >= str(
                    previous.get("analyzed_at") or ""
                ):
                    records[key] = item
        return records

    def _analysis_artifacts(self) -> list[tuple[dict[str, Any], Path]]:
        games_root = self.data_dir / "games"
        if not games_root.is_dir():
            return []
        artifacts: list[tuple[dict[str, Any], Path]] = []
        for game_dir in sorted(path for path in games_root.iterdir() if path.is_dir()):
            seen_sides: set[str] = set()
            side_paths = [game_dir / "analysis" / f"{side}.json" for side in ("white", "black")]
            for path in side_paths:
                if not path.is_file():
                    continue
                artifact = self._read_owned_analysis(path, expected_game_id=game_dir.name)
                expected_side = path.stem
                if artifact.get("review_side") != expected_side:
                    raise LearningConsistencyError(f"Analysis side path mismatch: {path}.")
                seen_sides.add(expected_side)
                artifacts.append((artifact, path))
            root_path = game_dir / "analysis.json"
            if root_path.is_file():
                artifact = self._read_owned_analysis(root_path, expected_game_id=game_dir.name)
                side = str(artifact.get("review_side") or "")
                if side not in seen_sides:
                    artifacts.append((artifact, root_path))
        return artifacts

    @staticmethod
    def _read_owned_analysis(path: Path, *, expected_game_id: str) -> dict[str, Any]:
        try:
            artifact = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise LearningConsistencyError(f"Invalid analysis artifact: {path}.") from exc
        if not isinstance(artifact, dict) or artifact.get("game_id") != expected_game_id:
            raise LearningConsistencyError(f"Analysis artifact ownership mismatch: {path}.")
        if artifact.get("review_side") not in {"white", "black"}:
            raise LearningConsistencyError(f"Analysis artifact review side is invalid: {path}.")
        return artifact

    def _attempt_artifacts(self) -> list[dict[str, Any]]:
        paths = (
            self.data_dir / "history" / "attempts.jsonl",
            self.data_dir / "training" / "attempts.jsonl",
        )
        attempts: dict[str, dict[str, Any]] = {}
        for path in paths:
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise LearningStorageError(f"Could not read {path}.") from exc
            for line in lines:
                if not line.strip():
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(item, dict):
                    continue
                attempt_id = str(item.get("attempt_id") or "").strip() or _legacy_attempt_id(item)
                normalized = dict(item)
                normalized["attempt_id"] = attempt_id
                existing = attempts.get(attempt_id)
                if existing is not None and _canonical_json(existing) != _canonical_json(normalized):
                    raise LearningConsistencyError(f"Conflicting attempt id: {attempt_id}.")
                attempts[attempt_id] = normalized
        return sorted(
            attempts.values(), key=lambda item: (str(item.get("attempted_at") or ""), item["attempt_id"])
        )

    def _puzzle_attempt_artifacts(self) -> list[dict[str, Any]]:
        path = self.data_dir / "history" / "puzzle_attempts.jsonl"
        attempts: list[dict[str, Any]] = []
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            lines = []
        except OSError as exc:
            raise LearningStorageError(f"Could not read {path}.") from exc
        for line in lines:
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                attempts.append(item)

        # Legacy puzzle state is inspected only for forward-compatible, fully verified rows.
        # Current legacy rows lack themes and attempt ids, so they deliberately produce nothing.
        state_path = self.data_dir / "puzzles" / "state.json"
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
            if isinstance(state, dict):
                attempts.extend(item for item in state.get("history") or [] if isinstance(item, dict))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            pass
        return attempts

    @staticmethod
    def _attempt_owner_from_sources(
        attempt: Mapping[str, Any],
        analysis_by_owner: Mapping[tuple[str, str], Mapping[str, Any]],
    ) -> Mapping[str, Any] | None:
        """Resolve an attempt only when its current owning position is unambiguous."""

        game_id = str(attempt.get("game_id") or "").strip()
        critical_id = str(attempt.get("critical_id") or "").strip()
        raw_side = attempt.get("review_side", attempt.get("reviewed_side"))
        review_side = str(raw_side or "").strip()
        if not game_id or not critical_id:
            return None
        if review_side not in {"", "white", "black"}:
            raise LearningConsistencyError("Attempt has invalid game-position ownership.")

        if review_side:
            candidates = [analysis_by_owner.get((game_id, review_side))]
        else:
            candidates = [
                artifact
                for (owner_game_id, _owner_side), artifact in analysis_by_owner.items()
                if owner_game_id == game_id
            ]

        owners: list[Mapping[str, Any]] = []
        for artifact in candidates:
            if artifact is None:
                continue
            found = [
                item
                for item in artifact.get("critical_positions") or []
                if isinstance(item, Mapping) and item.get("critical_id") == critical_id
            ]
            if len(found) > 1:
                raise LearningConsistencyError("Attempt owner analysis has duplicate critical ids.")
            if found:
                owners.append(artifact)
        return owners[0] if len(owners) == 1 else None

    def _collect_backfill(
        self, *, expected_analysis: Mapping[str, Any] | None = None
    ) -> list[LearningObservation]:
        history = self._history_records()
        analysis_by_owner: dict[tuple[str, str], Mapping[str, Any]] = {}
        observations: list[LearningObservation] = []
        artifacts = self._analysis_artifacts()
        if expected_analysis is not None:
            expected_game_id = str(expected_analysis.get("game_id") or "").strip()
            expected_side = str(expected_analysis.get("review_side") or "").strip()
            if not expected_game_id or expected_side not in {"white", "black"}:
                raise LearningConsistencyError("Analysis artifact has invalid game ownership.")
            persisted = next(
                (
                    artifact
                    for artifact, _path in artifacts
                    if artifact.get("game_id") == expected_game_id
                    and artifact.get("review_side") == expected_side
                ),
                None,
            )
            if persisted is None:
                raise LearningConsistencyError(
                    "Authoritative analysis artifact is not persisted for reconciliation."
                )
            if _canonical_json(persisted) != _canonical_json(expected_analysis):
                raise LearningConsistencyError(
                    "Persisted analysis artifact differs from the reconciliation payload."
                )

        for artifact, path in artifacts:
            owner = (str(artifact["game_id"]), str(artifact["review_side"]))
            analysis_by_owner[owner] = artifact
            observations.extend(
                self._analysis_observations(
                    artifact,
                    history_record=history.get(owner),
                    artifact_path=path,
                )
            )
        for attempt in self._attempt_artifacts():
            analysis = self._attempt_owner_from_sources(attempt, analysis_by_owner)
            if analysis is None:
                # Re-analysis can remove a critical position, and old data can lack a side.
                # Such rows are not evidence until ownership is uniquely verifiable again.
                continue
            reconciled_attempt = dict(attempt)
            reconciled_attempt.pop("category", None)
            observations.extend(
                self._attempt_observations(reconciled_attempt, analysis=analysis)
            )
        for attempt in self._puzzle_attempt_artifacts():
            if attempt.get("game_id"):
                analysis = self._attempt_owner_from_sources(attempt, analysis_by_owner)
                if analysis is None:
                    continue
                reconciled_attempt = dict(attempt)
                reconciled_attempt.pop("category", None)
                observations.extend(
                    self._attempt_observations(reconciled_attempt, analysis=analysis)
                )
            else:
                observations.extend(self._puzzle_attempt_observations(attempt))
        return observations

    def _reconcile_sources(
        self,
        *,
        expected_analysis: Mapping[str, Any] | None = None,
        sync_estimates: bool,
    ) -> list[LearningObservation]:
        with coordinated_learning_source_mutation():
            if sync_estimates:
                # EstimateStore reads observations while holding this lock.  Taking the same
                # package lock first preserves one lock order and lets both files reflect the
                # exact same source snapshot.
                from server.core.learning import estimates as estimates_module

                with estimates_module._ESTIMATE_LOCK:  # noqa: SLF001 - package transaction boundary
                    with _OBSERVATION_LOCK:
                        ordered = self._ordered_observations(
                            self._collect_backfill(expected_analysis=expected_analysis)
                        )
                        _atomic_jsonl(self.path, ordered)
                        estimates_module.EstimateStore(self.data_dir, now=self._now).rebuild(
                            observations=ordered
                        )
                        return ordered
            with _OBSERVATION_LOCK:
                ordered = self._ordered_observations(
                    self._collect_backfill(expected_analysis=expected_analysis)
                )
                _atomic_jsonl(self.path, ordered)
                return ordered

    def backfill(self, *, sync_estimates: bool = True) -> list[LearningObservation]:
        with coordinated_learning_source_mutation():
            appended = self.append_many(self._collect_backfill())
            if sync_estimates:
                self._sync_estimates()
            return appended

    def rebuild(self, *, sync_estimates: bool = True) -> list[LearningObservation]:
        return self._reconcile_sources(sync_estimates=sync_estimates)

    def reconcile_analysis(
        self,
        analysis: Mapping[str, Any],
        *,
        sync_estimates: bool = True,
    ) -> list[LearningObservation]:
        """Rebuild projections after an authoritative analysis source was replaced."""

        return self._reconcile_sources(
            expected_analysis=analysis,
            sync_estimates=sync_estimates,
        )

    def delete_game(self, game_id: str, *, sync_estimates: bool = True) -> int:
        game_id = str(game_id or "").strip()
        if not game_id:
            raise ValueError("game_id must not be empty")
        with coordinated_learning_source_mutation():
            with _OBSERVATION_LOCK:
                observations = self.load()
                kept = [item for item in observations if item.game_id != game_id]
                removed = len(observations) - len(kept)
                if removed:
                    _atomic_jsonl(self.path, kept)
            if sync_estimates:
                self._sync_estimates()
            return removed

    def _sync_estimates(self) -> None:
        from server.core.learning.estimates import EstimateStore

        EstimateStore(self.data_dir, now=self._now).rebuild()


def ingest_analysis(
    analysis: Mapping[str, Any], *, data_dir: str | os.PathLike[str] | None = None, **kwargs: Any
) -> list[LearningObservation]:
    return ObservationStore(data_dir).ingest_analysis(analysis, **kwargs)


def ingest_attempt(
    attempt: Mapping[str, Any], *, data_dir: str | os.PathLike[str] | None = None, **kwargs: Any
) -> list[LearningObservation]:
    return ObservationStore(data_dir).ingest_attempt(attempt, **kwargs)


def ingest_puzzle_attempt(
    attempt: Mapping[str, Any], *, data_dir: str | os.PathLike[str] | None = None, **kwargs: Any
) -> list[LearningObservation]:
    return ObservationStore(data_dir).ingest_puzzle_attempt(attempt, **kwargs)


def backfill(
    *, data_dir: str | os.PathLike[str] | None = None, **kwargs: Any
) -> list[LearningObservation]:
    return ObservationStore(data_dir).backfill(**kwargs)


def delete_game(
    game_id: str, *, data_dir: str | os.PathLike[str] | None = None, **kwargs: Any
) -> int:
    return ObservationStore(data_dir).delete_game(game_id, **kwargs)


def rebuild(
    *, data_dir: str | os.PathLike[str] | None = None, **kwargs: Any
) -> list[LearningObservation]:
    return ObservationStore(data_dir).rebuild(**kwargs)


__all__ = [
    "LearningConsistencyError",
    "LearningStorageError",
    "OBSERVATION_SCHEMA_VERSION",
    "ObservationStore",
    "backfill",
    "delete_game",
    "ingest_analysis",
    "ingest_attempt",
    "ingest_puzzle_attempt",
    "rebuild",
]
