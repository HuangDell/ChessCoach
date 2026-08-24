from __future__ import annotations

import asyncio
import json
import tempfile
import threading
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import httpx

from server import config
from server.core import app_liveness, history, lifecycle, puzzle_session, training
from server.core.learning import (
    LearningProjectionError,
    ObservationStore,
    get_learning_status,
    initialize_learning,
    sync_analysis_artifact,
)
from server.core.learning.estimates import EstimateStore
from server.web.app import create_app
from server.web import routes_puzzles
from tests.backend.fixtures import (
    CRITICAL_ID,
    GAME_ID,
    analysis_artifact,
    history_record,
    store_analysis_fixture,
)


OTHER_GAME_ID = "abcdef0123456789abcd"


async def _run_inline(function, *args, **kwargs):
    return function(*args, **kwargs)


class _ProducerApiCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="chesscoach-learning-api-")
        self.old_data_dir = config.DATA_DIR
        self.old_app_mode = config.APP_MODE
        self.old_session_ttl = config.SESSION_TTL_SECONDS
        self.old_puzzles_enabled = config.PUZZLES_ENABLED
        config.DATA_DIR = self.temporary.name
        config.APP_MODE = False
        config.SESSION_TTL_SECONDS = 0
        config.PUZZLES_ENABLED = True
        puzzle_session.clear_current()
        app_liveness.stop()
        lifecycle.stop_watchdog()
        self.app = create_app()

    def tearDown(self) -> None:
        puzzle_session.clear_current()
        app_liveness.stop()
        lifecycle.stop_watchdog()
        try:
            initialize_learning(self.temporary.name)
        finally:
            config.DATA_DIR = self.old_data_dir
            config.APP_MODE = self.old_app_mode
            config.SESSION_TTL_SECONDS = self.old_session_ttl
            config.PUZZLES_ENABLED = self.old_puzzles_enabled
            self.temporary.cleanup()

    def request(self, method: str, path: str, **kwargs) -> httpx.Response:
        async def send() -> httpx.Response:
            transport = httpx.ASGITransport(app=self.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                return await client.request(method, path, **kwargs)

        with patch("fastapi.routing.run_in_threadpool", new=_run_inline):
            return asyncio.run(send())


class LearningProducerApiTests(_ProducerApiCase):
    @staticmethod
    def _curated_puzzle(puzzle_id: str = "api-puzzle") -> dict:
        return {
            "id": puzzle_id,
            "fen": "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
            "solve_fen": "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1",
            "moves": ["e2e4", "e7e5"],
            "themes": ["fork"],
            "rating": 1200,
            "rd": 50,
        }

    @staticmethod
    def _response_json(response) -> dict:
        return json.loads(response.body.decode("utf-8"))

    def _store_other_analysis(self) -> dict:
        artifact = analysis_artifact()
        artifact["game_id"] = OTHER_GAME_ID
        artifact["cache_key"] = f"{OTHER_GAME_ID}:white:test-profile"
        game_dir = Path(self.temporary.name) / "games" / OTHER_GAME_ID
        side_dir = game_dir / "analysis"
        side_dir.mkdir(parents=True, exist_ok=True)
        content = json.dumps(artifact, sort_keys=True)
        (game_dir / "analysis.json").write_text(content, encoding="utf-8")
        (side_dir / "white.json").write_text(content, encoding="utf-8")
        return artifact

    def _run_delete_while_other_game_reconciles(
        self, *, fail_delete: bool
    ) -> tuple[httpx.Response, list[BaseException]]:
        from server.core.learning import observations as observations_module

        other_artifact = self._store_other_analysis()
        self.assertTrue(initialize_learning(self.temporary.name)["available"])
        delete_paused = threading.Barrier(2)
        reconcile_waiting = threading.Barrier(2)
        release_delete = threading.Event()
        responses: list[httpx.Response] = []
        errors: list[BaseException] = []
        real_delete = history.delete_game_data
        real_source_coordination = (
            observations_module.coordinated_learning_source_mutation
        )

        def paused_delete(game_id: str, data_dir: str | None = None) -> dict:
            delete_paused.wait(timeout=5)
            if not release_delete.wait(timeout=5):
                raise TimeoutError("test did not release game deletion")
            if fail_delete:
                raise history.GameDeletionError("forced source deletion failure")
            return real_delete(game_id, data_dir)

        @contextmanager
        def observe_reconcile_wait():
            if threading.current_thread().name == "other-game-reconcile":
                reconcile_waiting.wait(timeout=5)
            with real_source_coordination():
                yield

        def delete() -> None:
            try:
                responses.append(self.request("DELETE", f"/api/games/{GAME_ID}"))
            except BaseException as exc:  # pragma: no cover - surfaced below
                errors.append(exc)

        def reconcile() -> None:
            try:
                sync_analysis_artifact(other_artifact, data_dir=self.temporary.name)
            except BaseException as exc:  # pragma: no cover - surfaced below
                errors.append(exc)

        with (
            patch(
                "server.web.routes_history.history.delete_game_data",
                side_effect=paused_delete,
            ),
            patch.object(
                observations_module,
                "coordinated_learning_source_mutation",
                new=observe_reconcile_wait,
            ),
        ):
            delete_thread = threading.Thread(target=delete, name="game-delete")
            delete_thread.start()
            delete_paused.wait(timeout=5)

            reconcile_thread = threading.Thread(
                target=reconcile, name="other-game-reconcile"
            )
            reconcile_thread.start()
            reconcile_waiting.wait(timeout=5)
            self.assertTrue(reconcile_thread.is_alive())

            release_delete.set()
            delete_thread.join(timeout=5)
            reconcile_thread.join(timeout=5)

        self.assertFalse(delete_thread.is_alive())
        self.assertFalse(reconcile_thread.is_alive())
        self.assertEqual([], errors)
        self.assertEqual(1, len(responses))
        return responses[0], errors

    def _learning_models(self) -> tuple[list[dict], list[dict]]:
        observations = [
            item.model_dump(mode="json", exclude_none=True)
            for item in ObservationStore(self.temporary.name).load()
        ]
        estimates = [
            item.model_dump(mode="json", exclude_none=True)
            for item in EstimateStore(self.temporary.name).ensure_current()
        ]
        return observations, estimates

    def _prepare_deletion_rollback_fixture(self) -> tuple[dict[Path, bytes], list[dict], list[dict]]:
        artifact = store_analysis_fixture(self.temporary.name)
        target = {**history_record(1), "game_id": GAME_ID, "player_id": "student"}
        alice = {**history_record(2), "player_id": "alice", "player_name": "Alice"}
        bob = {**history_record(3), "player_id": "bob", "player_name": "Bob"}
        history.append_record(target, self.temporary.name)
        history.append_record(alice, self.temporary.name)
        history.append_record(bob, self.temporary.name)

        history_attempts = [
            {
                "attempt_id": "history-target",
                "game_id": GAME_ID,
                "critical_id": CRITICAL_ID,
                "review_side": "white",
                "verdict": "bad",
                "source": "training",
            },
            {
                "attempt_id": "history-other",
                "game_id": alice["game_id"],
                "selected_move": "a2a3",
            },
        ]
        training_attempts = [
            {
                "attempt_id": "training-target",
                "game_id": GAME_ID,
                "critical_id": CRITICAL_ID,
                "review_side": "white",
                "verdict": "bad",
                "source": "training",
            },
            {
                "attempt_id": "training-other",
                "game_id": bob["game_id"],
                "selected_move": "a2a3",
            },
        ]
        for path, rows in (
            (Path(self.temporary.name) / "history" / "attempts.jsonl", history_attempts),
            (Path(self.temporary.name) / "training" / "attempts.jsonl", training_attempts),
        ):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
                encoding="utf-8",
            )

        ObservationStore(self.temporary.name).rebuild(sync_estimates=True)
        for player_id in ("student", "alice", "bob"):
            history.write_profile(player_id, self.temporary.name)
        orphan = Path(self.temporary.name) / "profiles" / "orphan.json"
        orphan.write_text('{"preserve":"exact bytes"}\n', encoding="utf-8")

        paths = [
            Path(self.temporary.name) / "history" / "games.jsonl",
            Path(self.temporary.name) / "history" / "attempts.jsonl",
            Path(self.temporary.name) / "training" / "attempts.jsonl",
            *(Path(self.temporary.name) / "profiles").glob("*.json"),
        ]
        snapshots = {path: path.read_bytes() for path in paths}
        self.assertEqual(artifact["game_id"], GAME_ID)
        return snapshots, *self._learning_models()

    def test_training_projection_failure_returns_typed_error_and_keeps_source(self) -> None:
        store_analysis_fixture(self.temporary.name)
        with patch(
            "server.core.training.project_training_attempt",
            side_effect=LearningProjectionError(
                "observations are read-only", operation="attempt_sync", attempt_id="attempt"
            ),
        ):
            response = self.request(
                "POST",
                "/api/training/attempt",
                json={
                    "game_id": GAME_ID,
                    "critical_id": CRITICAL_ID,
                    "selected_move": "d1d3",
                    "review_side": "white",
                },
            )

        self.assertEqual(500, response.status_code)
        self.assertEqual("learning_storage_error", response.json()["error"]["code"])
        attempts = training.load_attempts(data_dir=self.temporary.name)
        self.assertEqual(1, len(attempts))
        self.assertEqual("best", attempts[0]["verdict"])

    def test_deleted_training_source_returns_typed_conflict(self) -> None:
        with patch(
            "server.core.training.evaluate_attempt",
            side_effect=training.TrainingGameDeletedError(
                "This game was deleted. Reload your training positions before trying again."
            ),
        ):
            response = self.request(
                "POST",
                "/api/training/attempt",
                json={
                    "game_id": GAME_ID,
                    "critical_id": CRITICAL_ID,
                    "selected_move": "d1d3",
                    "review_side": "white",
                },
            )

        self.assertEqual(409, response.status_code)
        self.assertEqual("training_game_deleted", response.json()["error"]["code"])
        self.assertIn("Reload your training positions", response.json()["error"]["message"])

    def test_curated_puzzle_api_finalizes_session_once(self) -> None:
        puzzle_session.set_current(
            {
                "id": "api-puzzle",
                "fen": "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
                "solve_fen": "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1",
                "moves": ["e2e4", "e7e5"],
                "themes": ["fork", "master"],
                "rating": 1200,
                "rd": 50,
            }
        )
        with (
            patch("server.core.puzzle_flow.puzzle_rating.load_state", return_value={}),
            patch("server.core.puzzle_flow.puzzle_rating.record_result", return_value={"rating": 1500}),
            patch("server.core.puzzle_flow.puzzle_rating.save_state"),
        ):
            first = self.request(
                "POST", "/api/puzzle/move", json={"id": "api-puzzle", "uci": "e7e5"}
            )
            second = self.request(
                "POST", "/api/puzzle/move", json={"id": "api-puzzle", "uci": "e7e5"}
            )

        self.assertEqual(200, first.status_code)
        self.assertTrue(first.json()["is_complete"])
        self.assertEqual(409, second.status_code)
        history_path = Path(self.temporary.name) / "history" / "puzzle_attempts.jsonl"
        self.assertEqual(1, len(history_path.read_text(encoding="utf-8").splitlines()))
        observations = ObservationStore(self.temporary.name).load()
        self.assertEqual(["tactics.fork_detection"], [item.skill_id for item in observations])

    def _assert_hint_precedes_terminal(self, terminal: str) -> None:
        progress = puzzle_session.set_current(self._curated_puzzle(f"hint-{terminal}"))
        hint_selected = threading.Event()
        release_hint = threading.Event()
        terminal_started = threading.Event()
        results: dict[str, object] = {}
        errors: list[BaseException] = []
        real_get_current = puzzle_session.get_current

        def paused_get_current():
            selected = real_get_current()
            if threading.current_thread().name == "puzzle-hint":
                hint_selected.set()
                if not release_hint.wait(timeout=5):
                    raise TimeoutError("test did not release puzzle hint")
            return selected

        def hint() -> None:
            try:
                results["hint"] = routes_puzzles.puzzle_hint(
                    routes_puzzles.PuzzleIdBody(id=progress.id)
                )
            except BaseException as exc:  # pragma: no cover - asserted below
                errors.append(exc)

        def finalize() -> None:
            terminal_started.set()
            try:
                if terminal == "move":
                    results["terminal"] = routes_puzzles.puzzle_move(
                        routes_puzzles.MoveBody(id=progress.id, uci="e7e5")
                    )
                else:
                    results["terminal"] = routes_puzzles.puzzle_giveup(
                        routes_puzzles.PuzzleIdBody(id=progress.id)
                    )
            except BaseException as exc:  # pragma: no cover - asserted below
                errors.append(exc)

        with (
            patch.object(puzzle_session, "get_current", side_effect=paused_get_current),
            patch("server.core.puzzle_flow.puzzle_rating.load_state", return_value={}),
            patch(
                "server.core.puzzle_flow.puzzle_rating.record_result",
                return_value={"rating": 1500},
            ) as record_result,
            patch("server.core.puzzle_flow.puzzle_rating.save_state"),
        ):
            hint_thread = threading.Thread(target=hint, name="puzzle-hint")
            hint_thread.start()
            self.assertTrue(hint_selected.wait(timeout=5))
            terminal_thread = threading.Thread(target=finalize, name="puzzle-terminal")
            terminal_thread.start()
            self.assertTrue(terminal_started.wait(timeout=5))
            release_hint.set()
            hint_thread.join(timeout=5)
            terminal_thread.join(timeout=5)

        self.assertFalse(hint_thread.is_alive())
        self.assertFalse(terminal_thread.is_alive())
        self.assertEqual([], errors)
        self.assertEqual(200, results["hint"].status_code)
        self.assertEqual(200, results["terminal"].status_code)
        self.assertTrue(progress.finished)
        self.assertTrue(progress.scored)
        self.assertEqual(1, progress.hints_used)
        self.assertFalse(record_result.call_args.kwargs["rated"])
        rows = [
            json.loads(line)
            for line in (
                Path(self.temporary.name) / "history" / "puzzle_attempts.jsonl"
            ).read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(1, len(rows))
        self.assertEqual(1, rows[0]["hints_used"])
        self.assertEqual("partial" if terminal == "move" else "failure", rows[0]["outcome"])

    def test_hint_and_terminal_move_persist_one_unrated_hinted_attempt(self) -> None:
        self._assert_hint_precedes_terminal("move")

    def test_hint_and_giveup_persist_one_hinted_failure(self) -> None:
        self._assert_hint_precedes_terminal("giveup")

    def _assert_replacement_waits_for_terminal(self, terminal: str) -> None:
        old_progress = puzzle_session.set_current(self._curated_puzzle(f"old-{terminal}"))
        new_puzzle = self._curated_puzzle(f"new-{terminal}")
        terminal_selected = threading.Event()
        release_terminal = threading.Event()
        replacement_started = threading.Event()
        selector_reached = threading.Event()
        results: dict[str, object] = {}
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
                    terminal_selected.set()
                    if not release_terminal.wait(timeout=5):
                        raise TimeoutError("test did not release puzzle terminal transition")
                real_finalize_lock.acquire()
                return self

            def __exit__(self, exc_type, exc, traceback) -> None:
                real_finalize_lock.release()

        old_progress.finalize_lock = _PauseFirstAcquire()

        def finalize() -> None:
            try:
                if terminal == "move":
                    results["terminal"] = routes_puzzles.puzzle_move(
                        routes_puzzles.MoveBody(id=old_progress.id, uci="e7e5")
                    )
                else:
                    results["terminal"] = routes_puzzles.puzzle_giveup(
                        routes_puzzles.PuzzleIdBody(id=old_progress.id)
                    )
            except BaseException as exc:  # pragma: no cover - asserted below
                errors.append(exc)

        def replace() -> None:
            replacement_started.set()
            try:
                results["replace"] = routes_puzzles.puzzle_next()
            except BaseException as exc:  # pragma: no cover - asserted below
                errors.append(exc)

        def select(*_args, **_kwargs) -> dict:
            selector_reached.set()
            return new_puzzle

        state = {"rating": 1200.0, "rd": 60.0, "seen_ids": [], "user_seed": 1}
        with (
            patch("server.web.routes_puzzles.puzzle_rating.load_state", return_value=state),
            patch("server.web.routes_puzzles.puzzle_rating.save_state"),
            patch("server.web.routes_puzzles.puzzles_mod.next_puzzle", side_effect=select),
            patch("server.core.puzzle_flow.puzzle_rating.load_state", return_value=state),
            patch(
                "server.core.puzzle_flow.puzzle_rating.record_result",
                return_value={"rating": 1500},
            ) as record_result,
            patch("server.core.puzzle_flow.puzzle_rating.save_state"),
            patch.object(config, "PUZZLE_MISTAKE_INTERLEAVE", False),
        ):
            terminal_thread = threading.Thread(target=finalize, name="puzzle-terminal")
            terminal_thread.start()
            self.assertTrue(terminal_selected.wait(timeout=5))
            replace_thread = threading.Thread(target=replace, name="puzzle-replacement")
            replace_thread.start()
            self.assertTrue(replacement_started.wait(timeout=5))
            self.assertFalse(selector_reached.wait(timeout=0.25))
            release_terminal.set()
            terminal_thread.join(timeout=5)
            replace_thread.join(timeout=5)

        self.assertFalse(terminal_thread.is_alive())
        self.assertFalse(replace_thread.is_alive())
        self.assertEqual([], errors)
        self.assertEqual(200, results["terminal"].status_code)
        self.assertEqual(200, results["replace"].status_code)
        self.assertTrue(old_progress.finished)
        self.assertTrue(old_progress.scored)
        record_result.assert_called_once()
        current = puzzle_session.get_current()
        self.assertIsNotNone(current)
        self.assertEqual(new_puzzle["id"], current.id)
        self.assertFalse(current.finished)
        rows = [
            json.loads(line)
            for line in (
                Path(self.temporary.name) / "history" / "puzzle_attempts.jsonl"
            ).read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(1, len(rows))
        self.assertEqual(old_progress.id, rows[0]["puzzle_id"])

    def test_next_waits_for_terminal_move_before_replacing_current(self) -> None:
        self._assert_replacement_waits_for_terminal("move")

    def test_next_waits_for_giveup_before_replacing_current(self) -> None:
        self._assert_replacement_waits_for_terminal("giveup")

    def test_personal_puzzle_giveup_persists_and_projects_once(self) -> None:
        store_analysis_fixture(self.temporary.name)
        puzzle = training.list_training_positions(self.temporary.name)[0]
        puzzle_session.set_current(puzzle)
        with (
            patch("server.web.routes_puzzles.puzzle_rating.load_state", return_value={}),
            patch("server.web.routes_puzzles.puzzle_rating.save_state"),
            patch("server.web.routes_puzzles.puzzle_mistakes.record_practice_result"),
        ):
            first = self.request(
                "POST", "/api/puzzle/giveup", json={"id": puzzle["id"]}
            )
            second = self.request(
                "POST", "/api/puzzle/giveup", json={"id": puzzle["id"]}
            )

        self.assertEqual(200, first.status_code)
        self.assertEqual(409, second.status_code)
        attempts = training.load_attempts(data_dir=self.temporary.name)
        self.assertEqual(1, len(attempts))
        self.assertTrue(attempts[0]["gave_up"])
        observations = [
            item
            for item in ObservationStore(self.temporary.name).load()
            if item.source_type == "training_attempt"
        ]
        self.assertEqual(1, len(observations))
        self.assertEqual("failure", observations[0].outcome)

    def test_personal_giveup_projection_error_cannot_retry_into_success(self) -> None:
        store_analysis_fixture(self.temporary.name)
        puzzle = training.list_training_positions(self.temporary.name)[0]
        progress = puzzle_session.set_current(puzzle)
        with patch(
            "server.core.training.project_training_attempt",
            side_effect=LearningProjectionError(
                "learning unavailable", operation="attempt_sync", attempt_id="attempt"
            ),
        ):
            first = self.request(
                "POST", "/api/puzzle/giveup", json={"id": puzzle["id"]}
            )
            second = self.request(
                "POST", "/api/puzzle/giveup", json={"id": puzzle["id"]}
            )

        self.assertEqual(500, first.status_code)
        self.assertEqual(500, second.status_code)
        self.assertEqual("learning_storage_error", second.json()["error"]["code"])
        self.assertTrue(progress.finished)
        self.assertTrue(progress.scored)
        self.assertEqual(1, len(training.load_attempts(data_dir=self.temporary.name)))

    def _assert_personal_puzzle_delete_conflict(self, terminal: str) -> None:
        store_analysis_fixture(self.temporary.name)
        puzzle = training.list_training_positions(self.temporary.name)[0]
        progress = puzzle_session.set_current(puzzle)
        work_started = threading.Barrier(2)
        release_work = threading.Event()
        responses: list[httpx.Response] = []
        errors: list[BaseException] = []

        def submit() -> None:
            try:
                if terminal == "move":
                    responses.append(
                        self.request(
                            "POST",
                            "/api/puzzle/move",
                            json={"id": puzzle["id"], "uci": "h1g1"},
                        )
                    )
                else:
                    responses.append(
                        self.request(
                            "POST", "/api/puzzle/giveup", json={"id": puzzle["id"]}
                        )
                    )
            except BaseException as exc:  # pragma: no cover - asserted below
                errors.append(exc)

        if terminal == "move":
            def pause_work(*_args, **_kwargs) -> dict:
                work_started.wait(timeout=5)
                if not release_work.wait(timeout=5):
                    raise TimeoutError("test did not release personal-puzzle Engine check")
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

            patcher = patch("server.core.training.lines.engine_line", side_effect=pause_work)
        else:
            real_finalize = training.finalize_attempt

            def pause_work(**kwargs) -> dict:
                work_started.wait(timeout=5)
                if not release_work.wait(timeout=5):
                    raise TimeoutError("test did not release personal-puzzle give-up")
                return real_finalize(**kwargs)

            patcher = patch("server.core.training.finalize_attempt", side_effect=pause_work)

        with patcher:
            worker = threading.Thread(target=submit, name=f"personal-puzzle-{terminal}")
            worker.start()
            work_started.wait(timeout=5)
            deleted = self.request("DELETE", f"/api/games/{GAME_ID}")
            release_work.set()
            worker.join(timeout=5)

        self.assertFalse(worker.is_alive())
        self.assertEqual([], errors)
        self.assertEqual(200, deleted.status_code)
        self.assertEqual(1, len(responses))
        response = responses[0]
        self.assertEqual(409, response.status_code)
        self.assertEqual(
            {
                "error": {
                    "code": "training_game_deleted",
                    "message": (
                        "This game was deleted while the training attempt was being checked. "
                        "Reload your training positions before trying again."
                    ),
                }
            },
            response.json(),
        )
        self.assertNotIn("can_retry", response.json())
        self.assertIsNone(puzzle_session.get_current())
        self.assertFalse(progress.finished)
        self.assertFalse(progress.scored)
        self.assertEqual([], progress.tried)
        self.assertEqual([], training.load_attempts(data_dir=self.temporary.name))
        self.assertFalse(
            any(
                item.source_type == "training_attempt"
                for item in ObservationStore(self.temporary.name).load()
            )
        )
        self.assertFalse((Path(self.temporary.name) / "puzzles" / "state.json").exists())

    def test_personal_puzzle_move_returns_deleted_source_conflict(self) -> None:
        self._assert_personal_puzzle_delete_conflict("move")

    def test_personal_puzzle_giveup_returns_deleted_source_conflict(self) -> None:
        self._assert_personal_puzzle_delete_conflict("giveup")

    def test_predeleted_personal_puzzle_operations_clear_stale_session(self) -> None:
        operations = {
            "move": ("POST", "/api/puzzle/move", {"uci": "d1d3"}),
            "giveup": ("POST", "/api/puzzle/giveup", {}),
            "hint": ("POST", "/api/puzzle/hint", {}),
        }

        for operation, (method, path, payload) in operations.items():
            with self.subTest(operation=operation):
                store_analysis_fixture(self.temporary.name)
                puzzle = training.list_training_positions(self.temporary.name)[0]
                puzzle_session.set_current(puzzle)

                deleted = self.request("DELETE", f"/api/games/{GAME_ID}")
                response = self.request(
                    method,
                    path,
                    json={"id": puzzle["id"], **payload},
                )

                self.assertEqual(200, deleted.status_code)
                self.assertEqual(409, response.status_code)
                self.assertEqual("training_game_deleted", response.json()["error"]["code"])
                self.assertIn("Reload your training positions", response.json()["error"]["message"])
                self.assertNotIn("can_retry", response.json())
                self.assertIsNone(puzzle_session.get_current())
                self.assertEqual([], training.load_attempts(data_dir=self.temporary.name))
                self.assertFalse(
                    any(
                        item.source_type == "training_attempt"
                        for item in ObservationStore(self.temporary.name).load()
                    )
                )
                self.assertFalse(
                    (Path(self.temporary.name) / "puzzles" / "state.json").exists()
                )

    def test_corrupt_analysis_is_not_misreported_as_deleted(self) -> None:
        store_analysis_fixture(self.temporary.name)
        puzzle = training.list_training_positions(self.temporary.name)[0]
        puzzle_session.set_current(puzzle)
        analysis_path = (
            Path(self.temporary.name) / "games" / GAME_ID / "analysis" / "white.json"
        )
        analysis_path.write_text("{not-json", encoding="utf-8")

        response = self.request(
            "POST",
            "/api/puzzle/hint",
            json={"id": puzzle["id"]},
        )

        self.assertEqual(400, response.status_code)
        self.assertNotIsInstance(response.json().get("error"), dict)
        self.assertIsNotNone(puzzle_session.get_current())

    def test_delete_removes_learning_examples_before_game_artifact(self) -> None:
        artifact = store_analysis_fixture(self.temporary.name)
        ObservationStore(self.temporary.name).ingest_analysis(artifact)

        response = self.request("DELETE", f"/api/games/{GAME_ID}")

        self.assertEqual(200, response.status_code)
        self.assertGreater(response.json()["learning_observations_removed"], 0)
        self.assertFalse((Path(self.temporary.name) / "games" / GAME_ID).exists())
        self.assertFalse(any(item.game_id == GAME_ID for item in ObservationStore(self.temporary.name).load()))
        estimates = EstimateStore(self.temporary.name).ensure_current()
        self.assertFalse(
            any(
                example.game_id == GAME_ID
                for estimate in estimates
                for example in estimate.examples
                if example.game_id is not None
            )
        )

    def test_delete_commit_blocks_other_reconcile_until_owning_source_is_gone(self) -> None:
        store_analysis_fixture(self.temporary.name)

        response, _errors = self._run_delete_while_other_game_reconciles(
            fail_delete=False
        )

        self.assertEqual(200, response.status_code)
        observations = ObservationStore(self.temporary.name).load()
        self.assertFalse(any(item.game_id == GAME_ID for item in observations))
        self.assertTrue(any(item.game_id == OTHER_GAME_ID for item in observations))
        estimates = EstimateStore(self.temporary.name).ensure_current()
        self.assertFalse(
            any(
                example.game_id == GAME_ID
                for estimate in estimates
                for example in estimate.examples
            )
        )
        self.assertTrue(
            any(
                example.game_id == OTHER_GAME_ID
                for estimate in estimates
                for example in estimate.examples
            )
        )

    def test_delete_rollback_restores_learning_before_other_reconcile(self) -> None:
        store_analysis_fixture(self.temporary.name)

        response, _errors = self._run_delete_while_other_game_reconciles(
            fail_delete=True
        )

        self.assertEqual(500, response.status_code)
        self.assertEqual("game_deletion_error", response.json()["error"]["code"])
        self.assertTrue((Path(self.temporary.name) / "games" / GAME_ID).is_dir())
        observations = ObservationStore(self.temporary.name).load()
        self.assertTrue(any(item.game_id == GAME_ID for item in observations))
        self.assertTrue(any(item.game_id == OTHER_GAME_ID for item in observations))
        example_game_ids = {
            example.game_id
            for estimate in EstimateStore(self.temporary.name).ensure_current()
            for example in estimate.examples
        }
        self.assertIn(GAME_ID, example_game_ids)
        self.assertIn(OTHER_GAME_ID, example_game_ids)

    def test_delete_learning_failure_keeps_game_artifact(self) -> None:
        store_analysis_fixture(self.temporary.name)
        with patch(
            "server.web.routes_history.delete_game_learning",
            side_effect=LearningProjectionError("learning delete failed", operation="game_delete"),
        ):
            response = self.request("DELETE", f"/api/games/{GAME_ID}")

        self.assertEqual(500, response.status_code)
        self.assertEqual("learning_storage_error", response.json()["error"]["code"])
        self.assertTrue((Path(self.temporary.name) / "games" / GAME_ID).exists())

    def test_delete_attempt_rewrite_failure_keeps_attempt_and_game_artifact(self) -> None:
        artifact = store_analysis_fixture(self.temporary.name)
        ObservationStore(self.temporary.name).ingest_analysis(artifact)
        attempt_path = Path(self.temporary.name) / "history" / "attempts.jsonl"
        attempt_path.parent.mkdir(parents=True, exist_ok=True)
        attempt = {
            "attempt_id": "attempt-delete",
            "game_id": GAME_ID,
            "critical_id": CRITICAL_ID,
            "review_side": "white",
            "verdict": "bad",
            "source": "training",
        }
        attempt_path.write_text(
            json.dumps(attempt) + "\n",
            encoding="utf-8",
        )
        from server.core import history

        real_atomic_jsonl = history._atomic_jsonl

        def fail_attempt_rewrite(path: str, records: list[dict]) -> None:
            if path == str(attempt_path):
                raise OSError("attempt log is read-only")
            real_atomic_jsonl(path, records)

        with patch("server.core.history._atomic_jsonl", side_effect=fail_attempt_rewrite):
            response = self.request("DELETE", f"/api/games/{GAME_ID}")

        self.assertEqual(500, response.status_code)
        self.assertEqual("game_deletion_error", response.json()["error"]["code"])
        self.assertIn("attempt log is read-only", response.json()["error"]["message"])
        self.assertTrue((Path(self.temporary.name) / "games" / GAME_ID).exists())
        self.assertEqual(
            [attempt],
            [json.loads(line) for line in attempt_path.read_text(encoding="utf-8").splitlines()],
        )
        self.assertTrue(
            any(item.game_id == GAME_ID for item in ObservationStore(self.temporary.name).load())
        )
        self.assertTrue(get_learning_status()["available"])
        self.assertEqual("game_delete_restore", get_learning_status()["operation"])

    def test_delete_second_attempt_failure_restores_every_index_and_learning_model(self) -> None:
        snapshots, original_observations, original_estimates = (
            self._prepare_deletion_rollback_fixture()
        )
        training_path = Path(self.temporary.name) / "training" / "attempts.jsonl"
        from server.core import history

        real_atomic_jsonl = history._atomic_jsonl
        successful_writes: list[str] = []

        def fail_second_attempt(path: str, records: list[dict]) -> None:
            real_atomic_jsonl(path, records)
            if path == str(training_path):
                raise OSError("second attempt log failed after replacement")
            successful_writes.append(path)

        with patch("server.core.history._atomic_jsonl", side_effect=fail_second_attempt):
            response = self.request("DELETE", f"/api/games/{GAME_ID}")

        self.assertEqual(500, response.status_code)
        self.assertEqual("game_deletion_error", response.json()["error"]["code"])
        self.assertEqual(
            [str(Path(self.temporary.name) / "history" / "attempts.jsonl")],
            successful_writes,
        )
        self.assertTrue((Path(self.temporary.name) / "games" / GAME_ID).is_dir())
        for path, content in snapshots.items():
            self.assertEqual(content, path.read_bytes(), str(path))
        self.assertEqual((original_observations, original_estimates), self._learning_models())
        self.assertTrue(get_learning_status()["available"])
        self.assertEqual("game_delete_restore", get_learning_status()["operation"])

    def test_delete_second_profile_failure_restores_prior_logs_profiles_and_learning(self) -> None:
        snapshots, original_observations, original_estimates = (
            self._prepare_deletion_rollback_fixture()
        )
        from server.core import history

        real_write_profile = history.write_profile
        rebuilt_players: list[str] = []

        def fail_second_profile(player_id: str, data_dir: str | None = None) -> dict:
            if len(rebuilt_players) == 1:
                raise RuntimeError("second profile cannot be replaced")
            result = real_write_profile(player_id, data_dir)
            rebuilt_players.append(player_id)
            return result

        with patch("server.core.history.write_profile", side_effect=fail_second_profile):
            response = self.request("DELETE", f"/api/games/{GAME_ID}")

        self.assertEqual(500, response.status_code)
        self.assertEqual("game_deletion_error", response.json()["error"]["code"])
        self.assertEqual(["alice"], rebuilt_players)
        self.assertTrue((Path(self.temporary.name) / "games" / GAME_ID).is_dir())
        current_profiles = set((Path(self.temporary.name) / "profiles").glob("*.json"))
        expected_profiles = {path for path in snapshots if path.parent.name == "profiles"}
        self.assertEqual(expected_profiles, current_profiles)
        for path, content in snapshots.items():
            self.assertEqual(content, path.read_bytes(), str(path))
        self.assertEqual((original_observations, original_estimates), self._learning_models())
        self.assertTrue(get_learning_status()["available"])
        self.assertEqual("game_delete_restore", get_learning_status()["operation"])

    def test_delete_rollback_learning_rebuild_failure_marks_health_unavailable(self) -> None:
        store_analysis_fixture(self.temporary.name)
        ObservationStore(self.temporary.name).ingest_analysis(
            store_analysis_fixture(self.temporary.name)
        )
        initialize_learning(self.temporary.name)
        from server.core import history

        with (
            patch(
                "server.core.history._atomic_jsonl",
                side_effect=OSError("history cannot be replaced"),
            ),
            patch(
                "server.core.learning.workflows.ObservationStore.rebuild",
                side_effect=OSError("learning cannot be rebuilt"),
            ),
        ):
            attempt_path = Path(self.temporary.name) / "history" / "attempts.jsonl"
            attempt_path.parent.mkdir(parents=True, exist_ok=True)
            attempt_path.write_text(
                json.dumps({"attempt_id": "target", "game_id": GAME_ID}) + "\n",
                encoding="utf-8",
            )
            response = self.request("DELETE", f"/api/games/{GAME_ID}")

        self.assertEqual(500, response.status_code)
        self.assertEqual("learning_storage_error", response.json()["error"]["code"])
        self.assertTrue((Path(self.temporary.name) / "games" / GAME_ID).is_dir())
        self.assertFalse(get_learning_status()["available"])
        self.assertEqual("game_delete_restore", get_learning_status()["operation"])


class _FakeAgentService:
    async def close(self) -> None:
        return None


class LearningStartupIntegrationTests(_ProducerApiCase):
    def test_degraded_learning_status_does_not_prevent_lifespan_startup(self) -> None:
        degraded = {
            "initialized": True,
            "available": False,
            "error": "backfill failed",
            "operation": "startup_backfill",
            "updated_at": "2026-08-24T00:00:00Z",
        }
        app = create_app(agent_service=_FakeAgentService())

        async def exercise() -> bool:
            with (
                patch("server.web.app.initialize_learning", return_value=degraded),
                patch("server.web.app.lifecycle.start_watchdog"),
                patch("server.web.app.lifecycle.stop_watchdog"),
                patch("server.web.app.engine.shutdown"),
            ):
                async with app.router.lifespan_context(app):
                    return app.state.learning_status == degraded

        self.assertTrue(asyncio.run(exercise()))


if __name__ == "__main__":
    unittest.main()
