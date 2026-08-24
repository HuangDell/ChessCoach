from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from server.core.agent.models import LearningObservation
from server.core.learning.estimates import (
    AGGREGATION_POLICY_VERSION,
    RECENT_WINDOW_DAYS,
    EstimateStore,
    aggregate_observations,
    estimate_skill,
    rank_estimates,
)
from server.core.learning.observations import ObservationStore


NOW = datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc)
SKILL = "calculation.candidate_moves"


def _observation(
    index: int,
    outcome: str,
    *,
    game: str | None = None,
    position: str | None = None,
    puzzle: str | None = None,
    occurred_at: str = "2026-08-20T12:00:00Z",
    severity: float | None = None,
) -> LearningObservation:
    attempt_id = f"attempt-{index}"
    if game is not None:
        critical = position or f"position-{index}"
        source_type = "training_attempt"
        evidence_type = "attempt_outcome"
        dedupe_key = f"attempt:{attempt_id}:{SKILL}:{outcome}"
        ownership = {
            "game_id": game,
            "review_side": "white",
            "critical_id": critical,
            "attempt_id": attempt_id,
        }
    else:
        puzzle_id = puzzle or f"puzzle-{index}"
        source_type = "puzzle_attempt"
        evidence_type = "puzzle_theme"
        dedupe_key = f"attempt:{attempt_id}:{SKILL}:{outcome}"
        ownership = {"puzzle_id": puzzle_id, "attempt_id": attempt_id}
    observation_id = "obs-" + hashlib.sha256(dedupe_key.encode()).hexdigest()[:32]
    return LearningObservation(
        observation_id=observation_id,
        dedupe_key=dedupe_key,
        skill_id=SKILL,
        outcome=outcome,
        source_type=source_type,
        evidence_type=evidence_type,
        severity=severity,
        evidence_refs=[f"attempt:{attempt_id}"],
        occurred_at=occurred_at,
        **ownership,
    )


class SkillAggregationTests(unittest.TestCase):
    def test_confidence_and_promotion_policy_table(self) -> None:
        cases = (
            ([], "insufficient", "unknown"),
            ([_observation(1, "failure", game="g1")], "insufficient", "watch"),
            (
                [
                    _observation(1, "failure", game="g1"),
                    _observation(2, "failure", game="g2"),
                ],
                "emerging",
                "weakness",
            ),
            (
                [
                    _observation(1, "failure", game="g1", occurred_at="2026-01-01T00:00:00Z", severity=10),
                    _observation(2, "failure", game="g2", occurred_at="2026-01-02T00:00:00Z", severity=10),
                ],
                "emerging",
                "weakness",
            ),
            (
                [
                    _observation(1, "success", game="g1"),
                    _observation(2, "success", game="g2"),
                    _observation(3, "success", game="g2", position="position-3"),
                ],
                "established",
                "strength",
            ),
        )
        for observations, confidence, status in cases:
            with self.subTest(confidence=confidence, status=status):
                estimate = estimate_skill(SKILL, observations, now=NOW)
                self.assertEqual(confidence, estimate.confidence_level)
                self.assertEqual(status, estimate.status)

    def test_repeated_attempts_on_one_position_do_not_create_confidence(self) -> None:
        observations = [
            _observation(index, "failure", game="g1", position="same")
            for index in range(1, 4)
        ]
        estimate = estimate_skill(SKILL, observations, now=NOW)
        self.assertEqual(3, estimate.evidence_count)
        self.assertEqual(1, estimate.distinct_games)
        self.assertEqual(1, estimate.distinct_positions)
        self.assertEqual("insufficient", estimate.confidence_level)
        self.assertEqual("watch", estimate.status)

    def test_failure_rate_and_independent_position_are_both_required(self) -> None:
        observations = [
            _observation(1, "failure", game="g1", occurred_at="2026-01-01T00:00:00Z"),
            _observation(2, "failure", game="g2", occurred_at="2026-01-02T00:00:00Z"),
            _observation(3, "success", game="g3", occurred_at="2026-01-03T00:00:00Z"),
            _observation(4, "success", game="g4", occurred_at="2026-01-04T00:00:00Z"),
        ]
        estimate = estimate_skill(SKILL, observations, now=NOW)
        self.assertEqual("watch", estimate.status)
        self.assertEqual(0, estimate.recent_failure_count)

    def test_recent_snapshot_can_show_improvement_without_rewriting_lifetime(self) -> None:
        old_failures = [
            _observation(1, "failure", game="g1", occurred_at="2026-01-01T00:00:00Z", severity=12),
            _observation(2, "failure", game="g2", occurred_at="2026-01-02T00:00:00Z", severity=12),
        ]
        recent_successes = [
            _observation(3, "success", game="g3", occurred_at="2026-08-10T00:00:00Z"),
            _observation(4, "success", game="g4", occurred_at="2026-08-11T00:00:00Z"),
            _observation(5, "success", game="g5", occurred_at="2026-08-12T00:00:00Z"),
        ]
        snapshots = aggregate_observations(old_failures + recent_successes, now=NOW)[SKILL]
        self.assertEqual("weakness", snapshots["lifetime"].status)
        self.assertEqual("strength", snapshots["recent"].status)
        self.assertEqual(5, snapshots["lifetime"].evidence_count)
        self.assertEqual(3, snapshots["recent"].evidence_count)

    def test_examples_are_recent_unique_verified_and_bounded(self) -> None:
        observations = [
            _observation(index, "failure", game=f"game-{index}", severity=float(index))
            for index in range(1, 6)
        ]
        estimate = estimate_skill(SKILL, observations, now=NOW)
        self.assertEqual(3, len(estimate.examples))
        self.assertTrue(all(item.kind == "critical_position" for item in estimate.examples))
        self.assertEqual(3, len({item.game_id for item in estimate.examples}))

    def test_deterministic_rank_uses_relevance_then_confidence_and_signals(self) -> None:
        snapshots = aggregate_observations(
            [
                _observation(1, "failure", game="g1"),
                _observation(2, "failure", game="g2"),
            ],
            now=NOW,
        )
        estimates = [item["lifetime"] for item in snapshots.values()]
        ranked = rank_estimates(
            estimates,
            focus_skill_id="opening.development",
            relevant_skill_ids=[SKILL],
        )
        self.assertEqual("opening.development", ranked[0].skill_id)
        self.assertEqual(SKILL, ranked[1].skill_id)


class EstimateStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.observations = ObservationStore(self.root)
        self.store = EstimateStore(self.root, now=lambda: NOW)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_envelope_contains_versioned_lifetime_and_recent_snapshots(self) -> None:
        self.observations.append_many(
            [_observation(1, "failure", game="g1")]
        )
        envelope = self.store.rebuild()
        self.assertEqual(AGGREGATION_POLICY_VERSION, envelope["aggregation_policy_version"])
        self.assertEqual(RECENT_WINDOW_DAYS, envelope["recent_window_days"])
        self.assertTrue(envelope["observations_digest"])
        row = next(item for item in envelope["skills"] if item["skill_id"] == SKILL)
        self.assertEqual(1, row["lifetime"].evidence_count)
        self.assertEqual(1, row["recent"].evidence_count)

    def test_corrupt_or_incompatible_cache_rebuilds_deterministically(self) -> None:
        self.observations.append_many(
            [
                _observation(1, "failure", game="g1"),
                _observation(2, "failure", game="g2"),
            ]
        )
        expected = [item.model_dump(mode="json") for item in self.store.ensure_current()]
        self.store.path.write_text("{broken", encoding="utf-8")
        rebuilt = [item.model_dump(mode="json") for item in self.store.ensure_current()]
        self.assertEqual(expected, rebuilt)

        envelope = json.loads(self.store.path.read_text(encoding="utf-8"))
        envelope["aggregation_policy_version"] += 1
        self.store.path.write_text(json.dumps(envelope), encoding="utf-8")
        rebuilt_version = [item.model_dump(mode="json") for item in self.store.ensure_current()]
        self.assertEqual(expected, rebuilt_version)

    def test_observation_digest_detects_stale_compatible_cache(self) -> None:
        self.observations.append_many([_observation(1, "failure", game="g1")])
        self.store.rebuild()
        self.observations.append_many([_observation(2, "failure", game="g2")])
        estimate = next(item for item in self.store.ensure_current() if item.skill_id == SKILL)
        self.assertEqual(2, estimate.evidence_count)
        self.assertEqual("weakness", estimate.status)

    def test_semantically_tampered_snapshots_are_atomically_rebuilt(self) -> None:
        self.observations.append_many([_observation(1, "failure", game="g1")])
        self.store.rebuild()
        canonical = json.loads(self.store.path.read_text(encoding="utf-8"))
        row_index = next(
            index
            for index, row in enumerate(canonical["skills"])
            if row["skill_id"] == SKILL
        )
        cases = (
            ("lifetime status", "lifetime", {"status": "weakness"}),
            ("recent confidence", "recent", {"confidence_level": "established"}),
            (
                "lifetime examples",
                "lifetime",
                {
                    "examples": [
                        {
                            "kind": "critical_position",
                            "game_id": "fabricated-game",
                            "review_side": "white",
                            "critical_id": "fabricated-position",
                        }
                    ]
                },
            ),
            (
                "recent counts",
                "recent",
                {"evidence_count": 2, "failure_count": 2},
            ),
            ("lifetime nested schema", "lifetime", {"schema_version": 2}),
            ("recent nested taxonomy", "recent", {"taxonomy_version": 2}),
        )
        for label, window, changes in cases:
            with self.subTest(label=label):
                tampered = json.loads(json.dumps(canonical))
                tampered["skills"][row_index][window].update(changes)
                self.store.path.write_text(json.dumps(tampered), encoding="utf-8")

                estimates = self.store.ensure_current(window=window)

                actual = next(item for item in estimates if item.skill_id == SKILL)
                expected = canonical["skills"][row_index][window]
                self.assertEqual(expected, actual.model_dump(mode="json", exclude_none=True))
                self.assertEqual(
                    canonical,
                    json.loads(self.store.path.read_text(encoding="utf-8")),
                )

    def test_load_envelope_rebuilds_semantically_tampered_cache(self) -> None:
        self.observations.append_many([_observation(1, "failure", game="g1")])
        self.store.rebuild()
        canonical = json.loads(self.store.path.read_text(encoding="utf-8"))
        tampered = json.loads(json.dumps(canonical))
        row = next(item for item in tampered["skills"] if item["skill_id"] == SKILL)
        row["lifetime"]["status"] = "weakness"
        self.store.path.write_text(json.dumps(tampered), encoding="utf-8")

        loaded = self.store.load_envelope()

        restored = next(item for item in loaded["skills"] if item["skill_id"] == SKILL)
        self.assertEqual("watch", restored["lifetime"].status)
        self.assertEqual(
            canonical,
            json.loads(self.store.path.read_text(encoding="utf-8")),
        )

    def test_missing_nested_versions_rebuild_the_whole_cache(self) -> None:
        self.observations.append_many([_observation(1, "failure", game="g1")])
        self.store.rebuild()
        canonical = json.loads(self.store.path.read_text(encoding="utf-8"))
        cases = (("schema_version", "lifetime"), ("taxonomy_version", "recent"))
        for field, window in cases:
            with self.subTest(field=field, window=window):
                tampered = json.loads(json.dumps(canonical))
                row = next(item for item in tampered["skills"] if item["skill_id"] == SKILL)
                del row[window][field]
                self.store.path.write_text(json.dumps(tampered), encoding="utf-8")

                self.store.ensure_current()

                self.assertEqual(
                    canonical,
                    json.loads(self.store.path.read_text(encoding="utf-8")),
                )

    def test_current_semantic_cache_is_not_rewritten(self) -> None:
        self.observations.append_many([_observation(1, "failure", game="g1")])
        self.store.rebuild()

        with patch("server.core.learning.estimates._atomic_json") as atomic_json:
            estimate = next(
                item for item in self.store.ensure_current() if item.skill_id == SKILL
            )

        self.assertEqual("watch", estimate.status)
        atomic_json.assert_not_called()

    def test_delete_game_rebuilds_examples_and_counts(self) -> None:
        self.observations.append_many(
            [
                _observation(1, "failure", game="deleted"),
                _observation(2, "failure", game="kept"),
                _observation(3, "success", game="kept-2"),
            ]
        )
        self.store.rebuild()
        estimates = self.store.delete_game("deleted")
        estimate = next(item for item in estimates if item.skill_id == SKILL)
        self.assertEqual(2, estimate.evidence_count)
        self.assertFalse(any(item.game_id == "deleted" for item in estimate.examples))
        self.assertFalse(any(item.game_id == "deleted" for item in self.observations.load()))


if __name__ == "__main__":
    unittest.main()
