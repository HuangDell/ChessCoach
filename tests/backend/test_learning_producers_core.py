from __future__ import annotations

import json
import tempfile
import threading
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from server import config
from server.core import history, puzzle_flow, puzzle_session, puzzle_storm, training
from server.core.learning import (
    LearningProjectionError,
    ObservationStore,
    delete_game_learning,
    get_learning_status,
    initialize_learning,
    is_learning_available,
    sync_analysis_artifact,
)
from server.core.learning.workflows import finalize_puzzle_attempt
from server.core.learning.estimates import EstimateStore
from server.core.storage import (
    GameMutationSupersededError,
    game_mutation_generation,
    superseding_game_deletion,
)
from server.web import jobs
from tests.backend.fixtures import (
    CRITICAL_ID,
    GAME_ID,
    analysis_artifact,
    history_record,
    store_analysis_fixture,
)


class _TemporaryDataCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="chesscoach-learning-producer-")
        self.old_data_dir = config.DATA_DIR
        config.DATA_DIR = self.temporary.name
        puzzle_session.clear_current()
        status = initialize_learning(self.temporary.name)
        self.assertTrue(status["available"], status.get("error"))

    def tearDown(self) -> None:
        puzzle_session.clear_current()
        puzzle_storm.clear()
        try:
            # Producer failures deliberately make process-local health sticky.
            # Exercise the public full-rebuild path before releasing this fixture
            # so later test modules cannot inherit its degraded state.
            status = initialize_learning(self.temporary.name)
            self.assertTrue(status["available"], status.get("error"))
        finally:
            config.DATA_DIR = self.old_data_dir
            self.temporary.cleanup()


class LearningLifecycleProducerTests(_TemporaryDataCase):
    def test_projection_failure_stays_unavailable_until_full_initialize_succeeds(self) -> None:
        with patch("server.core.learning.workflows.ObservationStore.rebuild"):
            initialized = initialize_learning(self.temporary.name)

        self.assertTrue(initialized["available"])

        with patch(
            "server.core.learning.workflows.ObservationStore.reconcile_analysis",
            side_effect=OSError("observation write failed"),
        ):
            with self.assertRaises(LearningProjectionError):
                sync_analysis_artifact({}, data_dir=self.temporary.name)

        degraded = get_learning_status()
        self.assertFalse(degraded["available"])
        self.assertEqual("analysis_sync", degraded["operation"])
        self.assertIn("observation write failed", degraded["error"])

        with patch(
            "server.core.learning.workflows.ObservationStore.delete_game", return_value=0
        ):
            delete_game_learning("unrelated-game", data_dir=self.temporary.name)

        still_degraded = get_learning_status()
        self.assertFalse(still_degraded["available"])
        self.assertEqual(degraded["operation"], still_degraded["operation"])
        self.assertEqual(degraded["error"], still_degraded["error"])
        self.assertFalse(is_learning_available())

        with patch("server.core.learning.workflows.ObservationStore.rebuild"):
            recovered = initialize_learning(self.temporary.name)

        self.assertTrue(recovered["available"])
        self.assertIsNone(recovered["error"])
        self.assertTrue(is_learning_available())

    def test_startup_reconciliation_is_idempotent_and_failure_only_disables_learning(self) -> None:
        store_analysis_fixture(self.temporary.name)

        first = initialize_learning(self.temporary.name)
        second = initialize_learning(self.temporary.name)

        self.assertTrue(first["available"])
        self.assertTrue(second["available"])
        self.assertTrue(is_learning_available())
        self.assertEqual(
            len(ObservationStore(self.temporary.name).load()),
            len({item.dedupe_key for item in ObservationStore(self.temporary.name).load()}),
        )

        with patch(
            "server.core.learning.workflows.ObservationStore.rebuild",
            side_effect=OSError("read-only learning directory"),
        ):
            degraded = initialize_learning(self.temporary.name)

        self.assertFalse(degraded["available"])
        self.assertFalse(is_learning_available())
        self.assertIn("read-only learning directory", get_learning_status()["error"])

        recovered = initialize_learning(self.temporary.name)
        self.assertTrue(recovered["available"])
        self.assertTrue(is_learning_available())

    def test_startup_recovers_stale_rows_and_skips_unverifiable_legacy_attempts(self) -> None:
        artifact = store_analysis_fixture(self.temporary.name)
        self.assertTrue(initialize_learning(self.temporary.name)["available"])

        changed = json.loads(json.dumps(artifact))
        changed["generated_at"] = "2026-08-24T12:00:00Z"
        changed["critical_positions"][0]["win_loss"] = 27.0
        game_dir = Path(self.temporary.name) / "games" / GAME_ID
        content = json.dumps(changed, sort_keys=True)
        (game_dir / "analysis.json").write_text(content, encoding="utf-8")
        (game_dir / "analysis" / "white.json").write_text(content, encoding="utf-8")

        attempt_path = Path(self.temporary.name) / "history" / "attempts.jsonl"
        attempt_path.parent.mkdir(parents=True, exist_ok=True)
        attempt_path.write_text(
            json.dumps(
                {
                    "attempt_id": "legacy-orphan",
                    "game_id": GAME_ID,
                    "verdict": "bad",
                    "attempted_at": "2026-08-24T12:01:00Z",
                }
            )
            + "\n",
            encoding="utf-8",
        )

        recovered = initialize_learning(self.temporary.name)

        self.assertTrue(recovered["available"], recovered.get("error"))
        candidate = next(
            item
            for item in ObservationStore(self.temporary.name).load()
            if item.skill_id == "calculation.candidate_moves"
        )
        self.assertEqual(27.0, candidate.severity)
        self.assertFalse(
            any(
                item.attempt_id == "legacy-orphan"
                for item in ObservationStore(self.temporary.name).load()
            )
        )

    def test_persisted_analysis_survives_learning_projection_failure(self) -> None:
        artifact = analysis_artifact()
        session = SimpleNamespace(player="white", engine_analysis=artifact)
        with (
            patch("server.web.jobs.history.record_game"),
            patch(
                "server.web.jobs.sync_analysis_artifact",
                side_effect=LearningProjectionError("learning unavailable", operation="analysis_sync"),
            ),
        ):
            error = jobs._persist_artifact(GAME_ID, session)

        stored = Path(self.temporary.name) / "games" / GAME_ID / "analysis" / "white.json"
        self.assertTrue(stored.is_file())
        self.assertIn("learning unavailable", error)
        self.assertIn("generated_at", json.loads(stored.read_text(encoding="utf-8")))

    def test_cache_hit_and_batch_keep_ready_with_learning_sync_error(self) -> None:
        cached = SimpleNamespace(player="white", engine_analysis=analysis_artifact())
        with (
            patch("server.web.jobs.analysis_cache.load", return_value=cached),
            patch("server.web.jobs.session_mod.set_session"),
            patch("server.web.jobs._persist_artifact", return_value="canonical write failed"),
        ):
            cached_status = jobs.start("[Event \"cached\"]\n\n*", player="white", game_id=GAME_ID)

        self.assertEqual("ready", cached_status["status"])
        self.assertEqual("canonical write failed", cached_status["learning_sync_error"])

        with jobs._lock:
            token, _job_id = jobs._new_job_locked(game_id=None, total_games=1)
        with (
            patch("server.web.jobs.game_identity.game_id_from_pgn", return_value=GAME_ID),
            patch("server.web.jobs.analysis_cache.load", return_value=cached),
            patch("server.web.jobs._persist_artifact", return_value="canonical write failed"),
            patch("server.web.jobs.session_mod.set_session"),
        ):
            jobs._run_batch(["game"], ["white"], None, None, token)

        batch_status = jobs.status()
        self.assertEqual("ready", batch_status["status"])
        self.assertIn(GAME_ID, batch_status["learning_sync_error"])

    def test_concurrent_persist_finishes_before_delete_and_cannot_resurrect_game(self) -> None:
        artifact = store_analysis_fixture(self.temporary.name)
        record = {**history_record(1), "game_id": GAME_ID, "reviewed_side": "white"}
        history.append_record(record, self.temporary.name)
        attempt_path = Path(self.temporary.name) / "history" / "attempts.jsonl"
        attempt_path.parent.mkdir(parents=True, exist_ok=True)
        attempt_path.write_text(
            json.dumps(
                {
                    "attempt_id": "attempt-race",
                    "game_id": GAME_ID,
                    "critical_id": CRITICAL_ID,
                    "review_side": "white",
                    "verdict": "bad",
                    "source": "training",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        ObservationStore(self.temporary.name).ingest_analysis(artifact)

        generation = game_mutation_generation(GAME_ID)
        producer_inside = threading.Barrier(2)
        release_producer = threading.Event()
        delete_started = threading.Event()
        producer_errors: list[BaseException] = []
        delete_errors: list[BaseException] = []
        real_store_analysis = jobs.store_analysis

        def blocking_store(game_id: str, review_side: str, payload: dict) -> None:
            real_store_analysis(game_id, review_side, payload)
            producer_inside.wait(timeout=5)
            if not release_producer.wait(timeout=5):
                raise TimeoutError("test did not release persistence transaction")

        def persist() -> None:
            try:
                jobs._persist_artifact(
                    GAME_ID,
                    SimpleNamespace(player="white", engine_analysis=artifact),
                    expected_generation=generation,
                )
            except BaseException as exc:  # pragma: no cover - asserted below
                producer_errors.append(exc)

        def delete() -> None:
            try:
                delete_started.set()
                with superseding_game_deletion(GAME_ID):
                    delete_game_learning(GAME_ID)
                    history.delete_game_data(GAME_ID)
            except BaseException as exc:  # pragma: no cover - asserted below
                delete_errors.append(exc)

        with (
            patch("server.web.jobs.store_analysis", side_effect=blocking_store),
            patch(
                "server.web.jobs.history.record_game",
                side_effect=lambda _sess: history.append_record(record, self.temporary.name),
            ),
        ):
            producer = threading.Thread(target=persist)
            producer.start()
            producer_inside.wait(timeout=5)
            remover = threading.Thread(target=delete)
            remover.start()
            self.assertTrue(delete_started.wait(timeout=5))
            release_producer.set()
            producer.join(timeout=5)
            remover.join(timeout=5)

        self.assertFalse(producer.is_alive())
        self.assertFalse(remover.is_alive())
        self.assertEqual([], producer_errors)
        self.assertEqual([], delete_errors)

        with self.assertRaises(GameMutationSupersededError):
            jobs._persist_artifact(
                GAME_ID,
                SimpleNamespace(player="white", engine_analysis=analysis_artifact()),
                expected_generation=generation,
            )

        self.assertFalse((Path(self.temporary.name) / "games" / GAME_ID).exists())
        self.assertFalse(any(row.get("game_id") == GAME_ID for row in history.load_records()))
        self.assertFalse(
            any(
                row.get("game_id") == GAME_ID
                for row in training.load_attempts(data_dir=self.temporary.name)
            )
        )
        self.assertFalse(
            any(item.game_id == GAME_ID for item in ObservationStore(self.temporary.name).load())
        )
        self.assertFalse(
            any(
                example.game_id == GAME_ID
                for estimate in EstimateStore(self.temporary.name).ensure_current()
                for example in estimate.examples
            )
        )


class TrainingProducerTests(_TemporaryDataCase):
    def setUp(self) -> None:
        super().setUp()
        store_analysis_fixture(self.temporary.name)

    def _delete_game(self) -> None:
        with superseding_game_deletion(GAME_ID):
            delete_game_learning(GAME_ID, data_dir=self.temporary.name)
            history.delete_game_data(GAME_ID, self.temporary.name)

    def test_invalid_side_and_critical_are_not_misreported_as_deleted(self) -> None:
        for critical_id, review_side in (
            ("missing-critical", "white"),
            (CRITICAL_ID, "invalid-side"),
        ):
            with self.subTest(critical_id=critical_id, review_side=review_side):
                with self.assertRaises(training.TrainingPositionError) as caught:
                    training.load_position(GAME_ID, critical_id, review_side)
                self.assertNotIsInstance(caught.exception, training.TrainingGameDeletedError)

    def _assert_game_has_no_durable_training_state(self) -> None:
        self.assertFalse((Path(self.temporary.name) / "games" / GAME_ID).exists())
        self.assertFalse(
            any(
                attempt.get("game_id") == GAME_ID
                for attempt in training.load_attempts(data_dir=self.temporary.name)
            )
        )
        self.assertFalse(
            any(item.game_id == GAME_ID for item in ObservationStore(self.temporary.name).load())
        )
        self.assertFalse(
            any(
                example.game_id == GAME_ID
                for estimate in EstimateStore(self.temporary.name).ensure_current()
                for example in estimate.examples
            )
        )

    def _seed_attempt_for_deletion(self) -> Path:
        attempt_path = Path(self.temporary.name) / "history" / "attempts.jsonl"
        attempt_path.parent.mkdir(parents=True, exist_ok=True)
        attempt_path.write_text(
            json.dumps({"attempt_id": "target", "game_id": GAME_ID}, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return attempt_path

    def _run_delete_while_other_game_appends(
        self, *, fail_delete_after_replacement: bool
    ) -> tuple[list[dict], list[BaseException], list[BaseException]]:
        attempt_path = self._seed_attempt_for_deletion()
        other_game_id = "fedcba9876543210fedc"
        delete_has_snapshot = threading.Barrier(2)
        release_delete = threading.Event()
        append_is_waiting = threading.Barrier(2)
        delete_errors: list[BaseException] = []
        append_errors: list[BaseException] = []
        real_read = history._read_attempt_rows_for_deletion
        real_atomic = history._atomic_jsonl
        real_coordination = training.coordinated_attempt_log_mutation

        def pause_delete(path: str) -> list[dict] | None:
            rows = real_read(path)
            if path == str(attempt_path):
                delete_has_snapshot.wait(timeout=5)
                if not release_delete.wait(timeout=5):
                    raise TimeoutError("test did not release deletion transaction")
            return rows

        def maybe_fail_delete(path: str, records: list[dict]) -> None:
            real_atomic(path, records)
            if fail_delete_after_replacement and path == str(attempt_path):
                raise OSError("attempt replacement failed after commit")

        @contextmanager
        def observe_append_wait():
            append_is_waiting.wait(timeout=5)
            with real_coordination():
                yield

        def delete() -> None:
            try:
                history.delete_game_data(GAME_ID, self.temporary.name)
            except BaseException as exc:  # pragma: no cover - asserted below
                delete_errors.append(exc)

        def append() -> None:
            try:
                training.record_attempt(
                    game_id=other_game_id,
                    critical_id="ply-9",
                    selected_move="a2a3",
                    verdict="best",
                    hints_used=0,
                    solved=True,
                    source="retry",
                    attempt_id="other-game-attempt",
                    data_dir=self.temporary.name,
                )
            except BaseException as exc:  # pragma: no cover - asserted below
                append_errors.append(exc)

        with (
            patch("server.core.history._read_attempt_rows_for_deletion", side_effect=pause_delete),
            patch("server.core.history._atomic_jsonl", side_effect=maybe_fail_delete),
            patch(
                "server.core.training.coordinated_attempt_log_mutation",
                new=observe_append_wait,
            ),
        ):
            remover = threading.Thread(target=delete)
            remover.start()
            delete_has_snapshot.wait(timeout=5)

            writer = threading.Thread(target=append)
            writer.start()
            append_is_waiting.wait(timeout=5)
            self.assertTrue(writer.is_alive())

            release_delete.set()
            remover.join(timeout=5)
            writer.join(timeout=5)

        self.assertFalse(remover.is_alive())
        self.assertFalse(writer.is_alive())
        rows = [json.loads(line) for line in attempt_path.read_text(encoding="utf-8").splitlines()]
        return rows, delete_errors, append_errors

    def test_delete_does_not_lose_other_game_attempt_appended_during_rewrite(self) -> None:
        rows, delete_errors, append_errors = self._run_delete_while_other_game_appends(
            fail_delete_after_replacement=False
        )

        self.assertEqual([], delete_errors)
        self.assertEqual([], append_errors)
        self.assertFalse(any(row.get("game_id") == GAME_ID for row in rows))
        self.assertEqual(
            ["other-game-attempt"],
            [row.get("attempt_id") for row in rows if row.get("game_id") != GAME_ID],
        )

    def test_delete_rollback_releases_attempt_lock_before_other_game_append(self) -> None:
        rows, delete_errors, append_errors = self._run_delete_while_other_game_appends(
            fail_delete_after_replacement=True
        )

        self.assertEqual(1, len(delete_errors))
        self.assertIsInstance(delete_errors[0], history.GameDeletionError)
        self.assertEqual([], append_errors)
        self.assertEqual(1, sum(row.get("game_id") == GAME_ID for row in rows))
        self.assertEqual(1, sum(row.get("attempt_id") == "other-game-attempt" for row in rows))

    def test_attempt_outcomes_project_success_partial_failure_and_skip_unknown(self) -> None:
        training.evaluate_attempt(
            game_id=GAME_ID,
            critical_id=CRITICAL_ID,
            selected_move="d1d3",
            review_side="white",
        )
        training.evaluate_attempt(
            game_id=GAME_ID,
            critical_id=CRITICAL_ID,
            selected_move="d1a4",
            review_side="white",
        )
        training.evaluate_attempt(
            game_id=GAME_ID,
            critical_id=CRITICAL_ID,
            selected_move="h1h2",
            review_side="white",
        )
        with patch("server.core.training.lines.engine_line", side_effect=TimeoutError("timeout")):
            unknown = training.evaluate_attempt(
                game_id=GAME_ID,
                critical_id=CRITICAL_ID,
                selected_move="h1g1",
                review_side="white",
            )
        training.reveal_solution(
            game_id=GAME_ID,
            critical_id=CRITICAL_ID,
            review_side="white",
            hints_used=1,
        )

        observations = [
            item
            for item in ObservationStore(self.temporary.name).load()
            if item.source_type in {"retry_attempt", "training_attempt"}
        ]
        self.assertEqual("unknown", unknown["verdict"])
        self.assertEqual(
            {"success", "partial", "failure"},
            {item.outcome for item in observations},
        )
        self.assertEqual(4, len(observations))
        self.assertEqual(5, len(training.load_attempts(data_dir=self.temporary.name)))
        self.assertTrue(training.load_attempts(data_dir=self.temporary.name)[-1]["gave_up"])

    def test_projection_failure_preserves_attempt_and_raises_typed_error(self) -> None:
        with patch(
            "server.core.training.project_training_attempt",
            side_effect=LearningProjectionError(
                "estimate write failed", operation="attempt_sync", attempt_id="source-id"
            ),
        ):
            with self.assertRaises(LearningProjectionError):
                training.evaluate_attempt(
                    game_id=GAME_ID,
                    critical_id=CRITICAL_ID,
                    selected_move="d1d3",
                    review_side="white",
                )

        attempts = training.load_attempts(data_dir=self.temporary.name)
        self.assertEqual(1, len(attempts))
        self.assertEqual("best", attempts[0]["verdict"])

    def test_evaluate_attempt_cannot_persist_after_delete_completes_during_engine_work(self) -> None:
        engine_entered = threading.Barrier(2)
        release_engine = threading.Event()
        errors: list[BaseException] = []

        def blocking_engine_line(*_args, **_kwargs) -> dict:
            engine_entered.wait(timeout=5)
            if not release_engine.wait(timeout=5):
                raise TimeoutError("test did not release Engine result")
            return {
                "move": {
                    "uci": "h1g1",
                    "win_swing": 0.0,
                    "eval_after": "+0.00",
                    "win_after": 50.0,
                    "refutation_line_uci": [],
                    "refutation_line_san": [],
                }
            }

        def evaluate() -> None:
            try:
                training.evaluate_attempt(
                    game_id=GAME_ID,
                    critical_id=CRITICAL_ID,
                    selected_move="h1g1",
                    review_side="white",
                )
            except BaseException as exc:  # pragma: no cover - asserted below
                errors.append(exc)

        with patch("server.core.training.lines.engine_line", side_effect=blocking_engine_line):
            worker = threading.Thread(target=evaluate)
            worker.start()
            engine_entered.wait(timeout=5)
            self._delete_game()
            release_engine.set()
            worker.join(timeout=5)

        self.assertFalse(worker.is_alive())
        self.assertEqual(1, len(errors))
        self.assertIsInstance(errors[0], training.TrainingGameDeletedError)
        self.assertIn("Reload your training positions", str(errors[0]))
        self._assert_game_has_no_durable_training_state()

    def test_reveal_solution_cannot_persist_after_delete_completes_before_finalizer(self) -> None:
        finalizer_entered = threading.Barrier(2)
        release_finalizer = threading.Event()
        errors: list[BaseException] = []
        real_finalize = training.finalize_attempt

        def blocking_finalize(**kwargs) -> dict:
            finalizer_entered.wait(timeout=5)
            if not release_finalizer.wait(timeout=5):
                raise TimeoutError("test did not release training finalizer")
            return real_finalize(**kwargs)

        def reveal() -> None:
            try:
                training.reveal_solution(
                    game_id=GAME_ID,
                    critical_id=CRITICAL_ID,
                    review_side="white",
                    hints_used=1,
                )
            except BaseException as exc:  # pragma: no cover - asserted below
                errors.append(exc)

        with patch("server.core.training.finalize_attempt", side_effect=blocking_finalize):
            worker = threading.Thread(target=reveal)
            worker.start()
            finalizer_entered.wait(timeout=5)
            self._delete_game()
            release_finalizer.set()
            worker.join(timeout=5)

        self.assertFalse(worker.is_alive())
        self.assertEqual(1, len(errors))
        self.assertIsInstance(errors[0], training.TrainingGameDeletedError)
        self._assert_game_has_no_durable_training_state()

    def test_core_personal_puzzle_move_uses_training_finalizer_and_retry_guard(self) -> None:
        puzzle = training.list_training_positions(self.temporary.name)[0]
        progress = puzzle_session.set_current(puzzle)
        with (
            patch("server.core.puzzle_flow.puzzle_rating.load_state", return_value={}),
            patch("server.core.puzzle_flow.puzzle_rating.save_state"),
            patch("server.core.puzzle_flow.puzzle_mistakes.record_practice_result"),
        ):
            solved = puzzle_flow.apply_mistake_move(progress, "d1d3")
            duplicate = puzzle_flow.apply_mistake_move(progress, "d1d3")

        self.assertTrue(solved["is_complete"])
        self.assertIn("already finished", duplicate["error"])
        attempts = training.load_attempts(data_dir=self.temporary.name)
        self.assertEqual(1, len(attempts))
        observations = [
            item
            for item in ObservationStore(self.temporary.name).load()
            if item.source_type == "training_attempt"
        ]
        self.assertEqual(1, len(observations))
        self.assertEqual("success", observations[0].outcome)

        failed_progress = puzzle_session.set_current(puzzle)
        with patch(
            "server.core.training.project_training_attempt",
            side_effect=LearningProjectionError(
                "projection unavailable", operation="attempt_sync", attempt_id="attempt"
            ),
        ):
            with self.assertRaises(LearningProjectionError):
                puzzle_flow.apply_mistake_move(failed_progress, "d1d3")
            with self.assertRaises(LearningProjectionError):
                puzzle_flow.apply_mistake_move(failed_progress, "d1d3")

        self.assertTrue(failed_progress.finished)
        self.assertTrue(failed_progress.scored)
        self.assertIsNotNone(failed_progress.learning_sync_error)
        self.assertEqual(2, len(training.load_attempts(data_dir=self.temporary.name)))


class PuzzleProducerTests(_TemporaryDataCase):
    @staticmethod
    def _puzzle() -> dict:
        return {
            "id": "curated-1",
            "fen": "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
            "solve_fen": "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1",
            "moves": ["e2e4", "e7e5"],
            "themes": ["fork", "opening"],
            "rating": 1200,
            "rd": 50,
        }

    @staticmethod
    def _run_concurrently(*targets) -> tuple[list[object], list[Exception]]:
        barrier = threading.Barrier(len(targets) + 1)
        results: list[object] = []
        errors: list[Exception] = []

        def run(target) -> None:
            barrier.wait()
            try:
                results.append(target())
            except Exception as exc:  # pragma: no cover - asserted by each caller
                errors.append(exc)

        threads = [threading.Thread(target=run, args=(target,)) for target in targets]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=5)
        if any(thread.is_alive() for thread in threads):
            raise AssertionError("Concurrent puzzle finalization did not complete.")
        return results, errors

    def _assert_storm_replacement_waits_for_submit(self, replacement: str) -> None:
        old_progress = puzzle_session.set_current(self._puzzle())
        old_run = puzzle_storm.StormRun(1200, 1, 100.0, 180)
        puzzle_storm._RUN = old_run
        new_puzzle = {**self._puzzle(), "id": f"curated-{replacement}"}

        lock_entered = threading.Barrier(2)
        release_submit = threading.Event()
        replacement_started = threading.Event()
        replacement_reached_serve = threading.Event()
        validated_ids: list[str] = []
        results: dict[str, dict] = {}
        errors: list[BaseException] = []
        real_finalize_lock = old_progress.finalize_lock

        class _PauseFirstAcquire:
            def __init__(self) -> None:
                self._guard = threading.Lock()
                self._paused = False

            def __enter__(self):
                with self._guard:
                    pause = not self._paused
                    self._paused = True
                if pause:
                    lock_entered.wait(timeout=5)
                    if not release_submit.wait(timeout=5):
                        raise TimeoutError("test did not release Storm submit")
                real_finalize_lock.acquire()
                return self

            def __exit__(self, exc_type, exc, traceback) -> None:
                real_finalize_lock.release()

        old_progress.finalize_lock = _PauseFirstAcquire()

        def validate(puzzle: dict, _ply_index: int, _uci: str) -> dict:
            validated_ids.append(puzzle["id"])
            return {"correct": True, "is_complete": True}

        def serve(*_args, **_kwargs) -> dict:
            replacement_reached_serve.set()
            return new_puzzle

        def submit() -> None:
            try:
                results["submit"] = puzzle_storm.submit_move({}, "e7e5", now=101.0)
            except BaseException as exc:  # pragma: no cover - asserted below
                errors.append(exc)

        def replace() -> None:
            replacement_started.set()
            try:
                if replacement == "next":
                    results["replace"] = puzzle_storm.next_puzzle({}, now=101.0)
                else:
                    results["replace"] = puzzle_storm.start({}, now=101.0)
            except BaseException as exc:  # pragma: no cover - asserted below
                errors.append(exc)

        with (
            patch("server.core.puzzle_storm.puzzles_mod.validate_step", side_effect=validate),
            patch("server.core.puzzle_storm.puzzles_mod.next_puzzle", side_effect=serve),
            patch("server.core.puzzle_storm.puzzle_rating.save_state"),
        ):
            submit_thread = threading.Thread(target=submit)
            submit_thread.start()
            lock_entered.wait(timeout=5)

            replace_thread = threading.Thread(target=replace)
            replace_thread.start()
            self.assertTrue(replacement_started.wait(timeout=5))
            # Before the fix, next_puzzle could reach _serve and replace _CURRENT while submit
            # waited on the old progress lock. Give that invalid interleaving a deterministic gate.
            replacement_reached_serve.wait(timeout=0.25)
            release_submit.set()

            submit_thread.join(timeout=5)
            replace_thread.join(timeout=5)

        self.assertFalse(submit_thread.is_alive())
        self.assertFalse(replace_thread.is_alive())
        self.assertEqual([], errors)
        self.assertEqual(["curated-1"], validated_ids)
        self.assertTrue(results["submit"]["solved"])
        self.assertTrue(old_progress.finished)
        self.assertTrue(old_progress.scored)
        self.assertEqual(1, old_run.score)

        current = puzzle_session.get_current()
        self.assertIsNotNone(current)
        self.assertEqual(new_puzzle["id"], current.id)
        self.assertFalse(current.finished)
        self.assertFalse(current.scored)
        attempts = [
            json.loads(line)
            for line in (
                Path(self.temporary.name) / "history" / "puzzle_attempts.jsonl"
            ).read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(1, len(attempts))
        self.assertEqual("curated-1", attempts[0]["puzzle_id"])

    def test_curated_terminal_attempt_is_persisted_and_projected_once(self) -> None:
        progress = puzzle_session.set_current(self._puzzle())
        with (
            patch("server.core.puzzle_flow.puzzle_rating.load_state", return_value={}),
            patch("server.core.puzzle_flow.puzzle_rating.record_result", return_value={"rating": 1500}),
            patch("server.core.puzzle_flow.puzzle_rating.save_state"),
        ):
            result = puzzle_flow.apply_solver_moves(progress, ["e7e5"])
            duplicate = puzzle_flow.apply_solver_moves(progress, ["e7e5"])

        rows = (
            Path(self.temporary.name) / "history" / "puzzle_attempts.jsonl"
        ).read_text(encoding="utf-8").splitlines()
        attempts = [json.loads(row) for row in rows]
        self.assertTrue(result["is_complete"])
        self.assertEqual([], duplicate["steps"])
        self.assertTrue(progress.scored)
        self.assertEqual(1, len(attempts))
        self.assertEqual("success", attempts[0]["outcome"])
        self.assertEqual(["fork", "opening"], attempts[0]["verified_themes"])
        puzzle_observations = [
            item
            for item in ObservationStore(self.temporary.name).load()
            if item.source_type == "puzzle_attempt"
        ]
        self.assertEqual(1, len(puzzle_observations))
        self.assertEqual("success", puzzle_observations[0].outcome)

    def test_curated_attempt_persistence_precedes_rating_and_failure_is_retryable(self) -> None:
        progress = puzzle_session.set_current(self._puzzle())
        with (
            patch("server.core.puzzle_flow.puzzle_rating.load_state", return_value={}) as load_state,
            patch(
                "server.core.puzzle_flow.puzzle_rating.record_result", return_value={"rating": 1500}
            ) as record_result,
            patch("server.core.puzzle_flow.puzzle_rating.save_state") as save_state,
        ):
            with patch(
                "server.core.learning.workflows._persist_puzzle_attempt",
                side_effect=OSError("attempt directory is read-only"),
            ):
                with self.assertRaises(LearningProjectionError) as raised:
                    puzzle_flow.apply_solver_moves(progress, ["e7e5"])

            self.assertEqual("puzzle_attempt_persist", raised.exception.operation)
            load_state.assert_not_called()
            record_result.assert_not_called()
            save_state.assert_not_called()
            self.assertFalse(progress.scored)
            self.assertFalse(progress.finished)
            self.assertIsNone(progress.learning_sync_error)

            recovered = puzzle_flow.apply_solver_moves(progress, ["e7e5"])

        self.assertTrue(recovered["is_complete"])
        self.assertTrue(progress.scored)
        self.assertTrue(progress.finished)
        load_state.assert_called_once_with()
        record_result.assert_called_once()
        save_state.assert_called_once()
        rows = (
            Path(self.temporary.name) / "history" / "puzzle_attempts.jsonl"
        ).read_text(encoding="utf-8").splitlines()
        self.assertEqual(1, len(rows))

    def test_projection_failure_keeps_source_and_rates_only_once(self) -> None:
        progress = puzzle_session.set_current(self._puzzle())
        with (
            patch(
                "server.core.learning.workflows.ObservationStore.ingest_puzzle_attempt",
                side_effect=OSError("observation write failed"),
            ),
            patch("server.core.puzzle_flow.puzzle_rating.load_state", return_value={}),
            patch(
                "server.core.puzzle_flow.puzzle_rating.record_result", return_value={"rating": 1500}
            ) as record_result,
            patch("server.core.puzzle_flow.puzzle_rating.save_state") as save_state,
        ):
            with self.assertRaises(LearningProjectionError) as first:
                puzzle_flow.apply_solver_moves(progress, ["e7e5"])
            with self.assertRaises(LearningProjectionError) as duplicate:
                puzzle_flow.apply_solver_moves(progress, ["e7e5"])

        self.assertEqual("puzzle_attempt_sync", first.exception.operation)
        self.assertEqual("puzzle_attempt_sync", duplicate.exception.operation)
        self.assertTrue(progress.scored)
        self.assertIsNotNone(progress.learning_sync_error)
        record_result.assert_called_once()
        save_state.assert_called_once()
        rows = (
            Path(self.temporary.name) / "history" / "puzzle_attempts.jsonl"
        ).read_text(encoding="utf-8").splitlines()
        self.assertEqual(1, len(rows))

    def test_puzzle_finalizer_dedupes_by_session_attempt_id(self) -> None:
        progress = puzzle_session.set_current(self._puzzle())
        first = finalize_puzzle_attempt(progress, outcome="failure", source="storm")
        second = finalize_puzzle_attempt(progress, outcome="failure", source="storm")

        self.assertIsNotNone(first)
        self.assertIsNone(second)
        rows = (
            Path(self.temporary.name) / "history" / "puzzle_attempts.jsonl"
        ).read_text(encoding="utf-8").splitlines()
        self.assertEqual(1, len(rows))

    def test_curated_hint_and_giveup_map_to_partial_and_failure(self) -> None:
        hinted = puzzle_session.set_current(self._puzzle())
        hinted.hints_used = 1
        with (
            patch("server.core.puzzle_flow.puzzle_rating.load_state", return_value={}),
            patch("server.core.puzzle_flow.puzzle_rating.record_result", return_value={}),
            patch("server.core.puzzle_flow.puzzle_rating.save_state"),
        ):
            puzzle_flow.apply_solver_moves(hinted, ["e7e5"])
            abandoned_puzzle = {**self._puzzle(), "id": "curated-giveup"}
            abandoned = puzzle_session.set_current(abandoned_puzzle)
            puzzle_flow.give_up(abandoned)

        attempts = [
            json.loads(line)
            for line in (
                Path(self.temporary.name) / "history" / "puzzle_attempts.jsonl"
            ).read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(["partial", "failure"], [item["outcome"] for item in attempts])
        self.assertEqual([1, 0], [item["hints_used"] for item in attempts])

    def test_concurrent_curated_giveups_finalize_once(self) -> None:
        progress = puzzle_session.set_current(self._puzzle())
        with (
            patch("server.core.puzzle_flow.puzzle_rating.load_state", return_value={}),
            patch(
                "server.core.puzzle_flow.puzzle_rating.record_result", return_value={"rating": 1500}
            ) as record_result,
            patch("server.core.puzzle_flow.puzzle_rating.save_state"),
        ):
            results, errors = self._run_concurrently(
                lambda: puzzle_flow.give_up(progress),
                lambda: puzzle_flow.give_up(progress),
            )

        self.assertEqual([], errors)
        self.assertEqual(2, len(results))
        self.assertTrue(progress.finished)
        record_result.assert_called_once()
        rows = (
            Path(self.temporary.name) / "history" / "puzzle_attempts.jsonl"
        ).read_text(encoding="utf-8").splitlines()
        self.assertEqual(1, len(rows))

    def test_concurrent_curated_giveup_and_terminal_move_finalize_once(self) -> None:
        progress = puzzle_session.set_current(self._puzzle())
        with (
            patch("server.core.puzzle_flow.puzzle_rating.load_state", return_value={}),
            patch(
                "server.core.puzzle_flow.puzzle_rating.record_result", return_value={"rating": 1500}
            ) as record_result,
            patch("server.core.puzzle_flow.puzzle_rating.save_state"),
        ):
            results, errors = self._run_concurrently(
                lambda: puzzle_flow.give_up(progress),
                lambda: puzzle_flow.apply_solver_moves(progress, ["e7e5"]),
            )

        self.assertEqual([], errors)
        self.assertEqual(2, len(results))
        self.assertTrue(progress.finished)
        record_result.assert_called_once()
        rows = (
            Path(self.temporary.name) / "history" / "puzzle_attempts.jsonl"
        ).read_text(encoding="utf-8").splitlines()
        self.assertEqual(1, len(rows))

    def test_storm_terminal_uses_same_puzzle_attempt_history(self) -> None:
        progress = puzzle_session.set_current(self._puzzle())
        puzzle_storm._RUN = puzzle_storm.StormRun(1200, 1, 100.0, 180)
        with patch(
            "server.core.puzzle_storm.puzzles_mod.validate_step",
            return_value={"correct": True, "is_complete": True},
        ):
            result = puzzle_storm.submit_move({}, "e7e5", now=101.0)

        attempts = [
            json.loads(line)
            for line in (
                Path(self.temporary.name) / "history" / "puzzle_attempts.jsonl"
            ).read_text(encoding="utf-8").splitlines()
        ]
        self.assertTrue(result["puzzle_done"])
        self.assertTrue(progress.scored)
        self.assertEqual("storm", attempts[0]["source"])
        self.assertEqual("success", attempts[0]["outcome"])

    def test_storm_next_does_not_replace_an_active_puzzle(self) -> None:
        progress = puzzle_session.set_current(self._puzzle())
        puzzle_storm._RUN = puzzle_storm.StormRun(1200, 1, 100.0, 180)

        with patch("server.core.puzzle_storm.puzzles_mod.next_puzzle") as select_puzzle:
            result = puzzle_storm.next_puzzle({}, now=101.0)

        self.assertIn("still active", result["error"])
        self.assertIs(progress, puzzle_session.get_current())
        select_puzzle.assert_not_called()

    def test_storm_finish_persistence_failure_keeps_run_retryable(self) -> None:
        progress = puzzle_session.set_current(self._puzzle())
        run = puzzle_storm.StormRun(1200, 1, 100.0, 180)
        puzzle_storm._RUN = run
        with (
            patch(
                "server.core.puzzle_storm.finalize_puzzle_attempt",
                side_effect=LearningProjectionError(
                    "attempt write failed",
                    operation="puzzle_attempt_persist",
                    attempt_id=progress.attempt_id,
                ),
            ),
            patch("server.core.puzzle_storm.puzzle_rating.save_state") as save_state,
        ):
            with self.assertRaises(LearningProjectionError):
                puzzle_storm.end({}, now=101.0)

        self.assertFalse(progress.scored)
        self.assertFalse(run.ended)
        self.assertIs(puzzle_session.get_current(), progress)
        self.assertIs(puzzle_storm.get_run(), run)
        save_state.assert_not_called()

    def test_concurrent_storm_end_and_terminal_move_finalize_once(self) -> None:
        progress = puzzle_session.set_current(self._puzzle())
        puzzle_storm._RUN = puzzle_storm.StormRun(1200, 1, 100.0, 180)
        state: dict = {}
        with (
            patch(
                "server.core.puzzle_storm.puzzles_mod.validate_step",
                return_value={"correct": True, "is_complete": True},
            ),
            patch("server.core.puzzle_storm.puzzle_rating.save_state") as save_state,
        ):
            results, errors = self._run_concurrently(
                lambda: puzzle_storm.end(state, now=101.0),
                lambda: puzzle_storm.submit_move(state, "e7e5", now=101.0),
            )

        self.assertEqual([], errors)
        self.assertEqual(2, len(results))
        self.assertTrue(progress.scored)
        self.assertIsNone(puzzle_session.get_current())
        self.assertIsNone(puzzle_storm.get_run())
        save_state.assert_called_once()
        rows = (
            Path(self.temporary.name) / "history" / "puzzle_attempts.jsonl"
        ).read_text(encoding="utf-8").splitlines()
        self.assertEqual(1, len(rows))

    def test_storm_next_cannot_replace_progress_selected_by_submit(self) -> None:
        self._assert_storm_replacement_waits_for_submit("next")

    def test_storm_start_waits_for_submit_without_duplicate_attempt(self) -> None:
        self._assert_storm_replacement_waits_for_submit("start")

    def test_concurrent_puzzle_finalization_produces_one_attempt(self) -> None:
        progress = puzzle_session.set_current(self._puzzle())
        barrier = threading.Barrier(3)
        results: list[dict | None] = []
        errors: list[Exception] = []

        def finalize() -> None:
            barrier.wait()
            try:
                results.append(
                    finalize_puzzle_attempt(progress, outcome="failure", source="lichess")
                )
            except Exception as exc:  # pragma: no cover - asserted empty below
                errors.append(exc)

        threads = [threading.Thread(target=finalize) for _ in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join()

        self.assertEqual([], errors)
        self.assertEqual(1, sum(item is not None for item in results))
        rows = (
            Path(self.temporary.name) / "history" / "puzzle_attempts.jsonl"
        ).read_text(encoding="utf-8").splitlines()
        self.assertEqual(1, len(rows))


if __name__ == "__main__":
    unittest.main()
