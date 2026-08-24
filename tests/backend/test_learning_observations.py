from __future__ import annotations

import hashlib
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.backend.fixtures import analysis_artifact

from server.core.learning.estimates import EstimateStore
from server.core.learning.observations import (
    LearningConsistencyError,
    ObservationStore,
)


NOW = "2026-08-20T12:00:00Z"
START_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"


def _store_analysis(root: Path, artifact: dict, *, side: bool = True, root_copy: bool = True) -> None:
    game_dir = root / "games" / artifact["game_id"]
    if side:
        side_dir = game_dir / "analysis"
        side_dir.mkdir(parents=True, exist_ok=True)
        (side_dir / f"{artifact['review_side']}.json").write_text(
            json.dumps(artifact), encoding="utf-8"
        )
    if root_copy:
        game_dir.mkdir(parents=True, exist_ok=True)
        (game_dir / "analysis.json").write_text(json.dumps(artifact), encoding="utf-8")


def _attempt(artifact: dict, attempt_id: str | None, verdict: str, **overrides: object) -> dict:
    item = {
        "attempt_id": attempt_id,
        "game_id": artifact["game_id"],
        "critical_id": artifact["critical_positions"][0]["critical_id"],
        "reviewed_side": artifact["review_side"],
        "attempted_at": NOW,
        "selected_move": "h1h2",
        "verdict": verdict,
        "hints_used": 0,
        "solved": verdict in {"best", "acceptable"},
        "source": "retry",
        "category": artifact["critical_positions"][0]["facts"]["primary_category"],
    }
    item.update(overrides)
    if attempt_id is None:
        item.pop("attempt_id")
    return item


class ObservationStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.artifact = analysis_artifact()
        self.store = ObservationStore(self.root)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_analysis_ingest_is_strict_idempotent_and_conflicts_fail(self) -> None:
        first = self.store.ingest_analysis(
            self.artifact, occurred_at=NOW, sync_estimates=False
        )
        self.assertGreaterEqual(len(first), 1)
        self.assertEqual(
            [],
            self.store.ingest_analysis(
                self.artifact, occurred_at=NOW, sync_estimates=False
            ),
        )
        self.assertTrue(all(item.observation_id.startswith("obs-") for item in first))
        self.assertTrue(all(item.game_id == self.artifact["game_id"] for item in first))

        conflict = first[0].model_dump(mode="json", exclude_none=True)
        conflict["severity"] = float(conflict.get("severity") or 0) + 1
        with self.assertRaisesRegex(LearningConsistencyError, "Conflicting observation key"):
            self.store.append_many([conflict])

        invalid = first[0].model_dump(mode="json", exclude_none=True)
        invalid["dedupe_key"] += ":forged"
        with self.assertRaisesRegex(LearningConsistencyError, "dedupe key"):
            self.store.append_many([invalid])

    def test_authoritative_reanalysis_replaces_changed_and_removed_fact_projections(self) -> None:
        original = json.loads(json.dumps(self.artifact))
        original["generated_at"] = NOW
        _store_analysis(self.root, original)
        self.store.reconcile_analysis(original, sync_estimates=True)

        skill_id = "calculation.candidate_moves"
        initial = next(item for item in self.store.load() if item.skill_id == skill_id)
        self.assertEqual(84.0, initial.severity)

        changed = json.loads(json.dumps(original))
        changed["generated_at"] = "2026-08-20T13:00:00Z"
        changed["critical_positions"][0]["win_loss"] = 31.0
        _store_analysis(self.root, changed)
        reconciled = self.store.reconcile_analysis(changed, sync_estimates=True)
        replacement = next(item for item in reconciled if item.skill_id == skill_id)
        self.assertEqual(initial.dedupe_key, replacement.dedupe_key)
        self.assertEqual(31.0, replacement.severity)
        estimate = next(
            item
            for item in EstimateStore(self.root).ensure_current()
            if item.skill_id == skill_id
        )
        self.assertEqual(31.0, estimate.cumulative_loss)

        removed = json.loads(json.dumps(changed))
        facts = removed["critical_positions"][0]["facts"]
        facts["primary_category"] = None
        facts["secondary_categories"] = []
        facts["motifs"] = []
        removed["critical_positions"][0]["signals"] = []
        removed["critical_positions"][0]["classification"] = "good"
        _store_analysis(self.root, removed)
        final = self.store.reconcile_analysis(removed, sync_estimates=True)
        self.assertNotIn(skill_id, {item.skill_id for item in final})

    def test_reconciliation_remaps_linked_attempt_and_ignores_removed_owner(self) -> None:
        original = json.loads(json.dumps(self.artifact))
        original["generated_at"] = NOW
        _store_analysis(self.root, original)
        attempt = _attempt(original, "attempt-remap", "bad")
        history_dir = self.root / "history"
        history_dir.mkdir(parents=True)
        (history_dir / "attempts.jsonl").write_text(
            json.dumps(attempt) + "\n", encoding="utf-8"
        )
        self.store.rebuild(sync_estimates=False)
        self.assertEqual(
            ["calculation.candidate_moves"],
            [item.skill_id for item in self.store.load() if item.attempt_id == "attempt-remap"],
        )

        changed = json.loads(json.dumps(original))
        facts = changed["critical_positions"][0]["facts"]
        facts["primary_category"] = "fork"
        facts["secondary_categories"] = []
        facts["motifs"] = []
        changed["critical_positions"][0]["classification"] = "mistake"
        changed["critical_positions"][0]["signals"] = []
        _store_analysis(self.root, changed)
        self.store.reconcile_analysis(changed, sync_estimates=False)
        self.assertEqual(
            ["tactics.fork_detection"],
            [item.skill_id for item in self.store.load() if item.attempt_id == "attempt-remap"],
        )

        orphaned = json.loads(json.dumps(changed))
        orphaned["critical_positions"] = []
        _store_analysis(self.root, orphaned)
        self.store.reconcile_analysis(orphaned, sync_estimates=False)
        self.assertFalse(any(item.attempt_id == "attempt-remap" for item in self.store.load()))

    def test_rebuild_holds_lock_across_source_collection_and_replacement(self) -> None:
        artifact = json.loads(json.dumps(self.artifact))
        artifact["generated_at"] = NOW
        _store_analysis(self.root, artifact)
        attempt = _attempt(artifact, "attempt-concurrent-rebuild", "bad")
        history_dir = self.root / "history"
        history_dir.mkdir(parents=True)
        (history_dir / "attempts.jsonl").write_text(
            json.dumps(attempt) + "\n", encoding="utf-8"
        )

        collecting = threading.Event()
        release = threading.Event()
        errors: list[BaseException] = []
        original_collect = self.store._collect_backfill

        def blocked_collect(*, expected_analysis=None):
            collecting.set()
            if not release.wait(timeout=5):
                raise TimeoutError("test did not release source collection")
            return original_collect(expected_analysis=expected_analysis)

        def rebuild() -> None:
            try:
                self.store.rebuild(sync_estimates=True)
            except BaseException as exc:  # pragma: no cover - asserted below
                errors.append(exc)

        def append() -> None:
            try:
                self.store.ingest_attempt(
                    attempt, analysis=artifact, sync_estimates=True
                )
            except BaseException as exc:  # pragma: no cover - asserted below
                errors.append(exc)

        with patch.object(self.store, "_collect_backfill", side_effect=blocked_collect):
            rebuild_thread = threading.Thread(target=rebuild)
            rebuild_thread.start()
            self.assertTrue(collecting.wait(timeout=5))
            append_thread = threading.Thread(target=append)
            append_thread.start()
            self.assertTrue(append_thread.is_alive())
            release.set()
            rebuild_thread.join(timeout=5)
            append_thread.join(timeout=5)

        self.assertFalse(rebuild_thread.is_alive())
        self.assertFalse(append_thread.is_alive())
        self.assertEqual([], errors)
        self.assertEqual(
            1,
            len(
                [
                    item
                    for item in self.store.load()
                    if item.attempt_id == "attempt-concurrent-rebuild"
                ]
            ),
        )
        estimate = next(
            item
            for item in EstimateStore(self.root).ensure_current()
            if item.skill_id == "calculation.candidate_moves"
        )
        self.assertEqual(2, estimate.evidence_count)

    def test_concurrent_attempt_projection_does_not_lose_rows(self) -> None:
        count = 16
        barrier = threading.Barrier(count)
        errors: list[BaseException] = []

        def write(index: int) -> None:
            try:
                barrier.wait()
                self.store.ingest_attempt(
                    _attempt(self.artifact, f"attempt-{index}", "bad"),
                    analysis=self.artifact,
                    sync_estimates=False,
                )
            except BaseException as exc:  # pragma: no cover - surfaced by assertion
                errors.append(exc)

        threads = [threading.Thread(target=write, args=(index,)) for index in range(count)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        self.assertEqual([], errors)
        rows = self.store.load()
        self.assertEqual(count, len(rows))
        self.assertEqual(count, len({item.attempt_id for item in rows}))

    def test_attempt_outcome_mapping_table(self) -> None:
        cases = (
            ("best", 0, "h1h2", "success"),
            ("acceptable", 1, "h1h2", "partial"),
            ("inaccurate", 0, "h1h2", "partial"),
            ("same_as_game", 0, "h1h2", "failure"),
            ("bad", 0, "h1h2", "failure"),
            ("unknown", 0, "h1h2", None),
            ("unknown", 4, None, "failure"),
        )
        for index, (verdict, hints, selected, expected) in enumerate(cases):
            with self.subTest(verdict=verdict, hints=hints, selected=selected):
                rows = self.store.ingest_attempt(
                    _attempt(
                        self.artifact,
                        f"outcome-{index}",
                        verdict,
                        hints_used=hints,
                        selected_move=selected,
                        solved=verdict in {"best", "acceptable"},
                    ),
                    analysis=self.artifact,
                    sync_estimates=False,
                )
                self.assertEqual([] if expected is None else [expected], [row.outcome for row in rows])

    def test_attempt_requires_unique_analysis_owner_and_matching_category(self) -> None:
        attempt = _attempt(self.artifact, "owner-1", "bad")
        bad_category = dict(attempt, category="fork")
        with self.assertRaisesRegex(LearningConsistencyError, "category"):
            self.store.ingest_attempt(
                bad_category, analysis=self.artifact, sync_estimates=False
            )

        other = json.loads(json.dumps(self.artifact))
        other["review_side"] = "black"
        _store_analysis(self.root, self.artifact, side=True, root_copy=False)
        _store_analysis(self.root, other, side=True, root_copy=False)
        no_side = dict(attempt)
        no_side.pop("reviewed_side")
        with self.assertRaisesRegex(LearningConsistencyError, "uniquely verified"):
            self.store.ingest_attempt(no_side, sync_estimates=False)

    def test_backfill_prefers_side_artifacts_and_is_interruption_compatible(self) -> None:
        _store_analysis(self.root, self.artifact)
        history_dir = self.root / "history"
        history_dir.mkdir(parents=True)
        history = {
            "game_id": self.artifact["game_id"],
            "reviewed_side": "white",
            "analyzed_at": NOW,
        }
        (history_dir / "games.jsonl").write_text(json.dumps(history) + "\n", encoding="utf-8")
        legacy = _attempt(self.artifact, None, "bad")
        content = json.dumps(legacy, sort_keys=True) + "\n"
        (history_dir / "attempts.jsonl").write_text(content, encoding="utf-8")
        training_dir = self.root / "training"
        training_dir.mkdir()
        (training_dir / "attempts.jsonl").write_text(content, encoding="utf-8")

        first = self.store.backfill(sync_estimates=False)
        self.assertGreater(len(first), 1)
        self.assertEqual([], self.store.backfill(sync_estimates=False))
        attempts = [item for item in self.store.load() if item.attempt_id]
        self.assertEqual(1, len(attempts))
        expected_hash = hashlib.sha256(
            json.dumps(legacy, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()[:32]
        self.assertEqual(f"legacy-{expected_hash}", attempts[0].attempt_id)

        # An abandoned temp file from an interrupted pre-replace write is ignored; the next
        # backfill creates a unique temp path and preserves the canonical rows.
        abandoned = self.root / "learning" / ".observations.jsonl.interrupted.tmp"
        abandoned.write_text("partial", encoding="utf-8")
        self.assertEqual([], self.store.backfill(sync_estimates=False))
        self.assertEqual(len(first), len(self.store.load()))

    def test_backfill_does_not_let_stale_history_override_analysis_result(self) -> None:
        artifact = json.loads(json.dumps(self.artifact))
        artifact["result"] = "1-0"
        artifact["headers"]["Result"] = "1-0"
        critical = artifact["critical_positions"][0]
        critical["signals"] = ["missed_win"]
        critical["facts"]["snapshots"]["before"]["phase"] = {"name": "endgame"}
        _store_analysis(self.root, artifact)

        history_dir = self.root / "history"
        history_dir.mkdir(parents=True)
        stale_history = {
            "game_id": artifact["game_id"],
            "reviewed_side": artifact["review_side"],
            "analyzed_at": NOW,
            "player_result": "loss",
        }
        (history_dir / "games.jsonl").write_text(
            json.dumps(stale_history) + "\n", encoding="utf-8"
        )

        self.store.backfill(sync_estimates=False)
        self.assertNotIn("endgame.conversion", {item.skill_id for item in self.store.load()})

    def test_root_analysis_is_used_only_for_a_missing_side(self) -> None:
        white = self.artifact
        black = json.loads(json.dumps(self.artifact))
        black["review_side"] = "black"
        black["critical_positions"][0]["side"] = "black"
        _store_analysis(self.root, white, side=True, root_copy=False)
        game_dir = self.root / "games" / white["game_id"]
        (game_dir / "analysis.json").write_text(json.dumps(black), encoding="utf-8")
        self.store.backfill(sync_estimates=False)
        owners = {(item.review_side, item.critical_id) for item in self.store.load()}
        self.assertEqual({("white", "ply-1"), ("black", "ply-1")}, owners)

    def test_old_puzzle_history_without_attempt_id_and_themes_is_ignored(self) -> None:
        history_dir = self.root / "history"
        history_dir.mkdir()
        (history_dir / "puzzle_attempts.jsonl").write_text(
            json.dumps({"puzzle_id": "old-p1", "verdict": "bad", "date": NOW}) + "\n",
            encoding="utf-8",
        )
        puzzle_dir = self.root / "puzzles"
        puzzle_dir.mkdir()
        (puzzle_dir / "state.json").write_text(
            json.dumps({"history": [{"id": "p1", "result": 0, "date": NOW}]}),
            encoding="utf-8",
        )
        self.assertEqual([], self.store.backfill(sync_estimates=False))

    def test_verified_external_puzzle_attempt_is_deduped(self) -> None:
        attempt = {
            "attempt_id": "puzzle-attempt-1",
            "puzzle_id": "lichess-1",
            "fen": START_FEN,
            "verified_themes": ["fork", "advancedPawn"],
            "verdict": "bad",
            "selected_move": "e2e4",
            "attempted_at": NOW,
        }
        rows = self.store.ingest_puzzle_attempt(attempt, sync_estimates=False)
        self.assertEqual(["tactics.fork_detection"], [item.skill_id for item in rows])
        self.assertEqual("puzzle_theme", rows[0].evidence_type)
        self.assertEqual([], self.store.ingest_puzzle_attempt(attempt, sync_estimates=False))

    def test_external_puzzle_attempt_requires_a_valid_fen_owner(self) -> None:
        attempt = {
            "attempt_id": "puzzle-attempt-fen",
            "puzzle_id": "lichess-fen",
            "verified_themes": ["fork"],
            "verdict": "bad",
            "selected_move": "e2e4",
            "attempted_at": NOW,
        }
        with self.assertRaisesRegex(LearningConsistencyError, "requires a FEN owner"):
            self.store.ingest_puzzle_attempt(attempt, sync_estimates=False)

        for fen in ("not-a-fen", "8/8/8/8/8/8/8/8 w - - 0 1"):
            with self.subTest(fen=fen):
                with self.assertRaisesRegex(LearningConsistencyError, "invalid FEN owner"):
                    self.store.ingest_puzzle_attempt(
                        dict(attempt, fen=fen), sync_estimates=False
                    )

        self.assertEqual([], self.store.load())

    def test_delete_game_and_rebuild_remove_unowned_rows(self) -> None:
        _store_analysis(self.root, self.artifact)
        self.store.backfill(sync_estimates=False)
        self.store.ingest_puzzle_attempt(
            {
                "attempt_id": "external-1",
                "puzzle_id": "p1",
                "fen": START_FEN,
                "verified_themes": ["fork"],
                "verdict": "best",
                "selected_move": "e2e4",
                "attempted_at": NOW,
            },
            sync_estimates=False,
        )
        removed = self.store.delete_game(self.artifact["game_id"], sync_estimates=False)
        self.assertGreater(removed, 0)
        self.assertEqual({"p1"}, {item.puzzle_id for item in self.store.load()})

        # Rebuild replaces the index from source artifacts, including restoring the game and
        # dropping the external row because no puzzle-attempt source file owns it.
        rebuilt = self.store.rebuild(sync_estimates=False)
        self.assertTrue(any(item.game_id == self.artifact["game_id"] for item in rebuilt))
        self.assertFalse(any(item.puzzle_id == "p1" for item in rebuilt))

    def test_corrupt_observation_file_fails_closed(self) -> None:
        self.store.path.parent.mkdir(parents=True)
        self.store.path.write_text('{"not":"complete"}\n', encoding="utf-8")
        with self.assertRaises(LearningConsistencyError):
            self.store.load()


if __name__ == "__main__":
    unittest.main()
