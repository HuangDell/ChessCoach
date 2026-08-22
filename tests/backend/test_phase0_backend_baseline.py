"""Phase 0 regression baseline for existing backend coaching surfaces.

The new Agent API did not exist when this baseline was recorded. ``/api/chat`` below is the
legacy CLI-backed contract and is intentionally exercised only through mocks; it is not a model
transport compatibility requirement for the new Agent runtime.
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

# Config is import-time state. Point it at a disposable directory and disable both watchdogs
# before importing the app, then restore the process environment in tearDownModule.
_TEST_ENV_KEYS = (
    "CHESS_APP_MODE",
    "CHESSCOACH_DATA_DIR",
    "CHESS_SESSION_TTL",
    "CHESS_WEB_OPEN",
)
_ORIGINAL_ENV = {key: os.environ.get(key) for key in _TEST_ENV_KEYS}
_BOOTSTRAP_DATA = tempfile.TemporaryDirectory(prefix="chesscoach-phase0-bootstrap-")
os.environ.update(
    {
        "CHESS_APP_MODE": "0",
        "CHESSCOACH_DATA_DIR": _BOOTSTRAP_DATA.name,
        "CHESS_SESSION_TTL": "0",
        "CHESS_WEB_OPEN": "0",
    }
)

import httpx

from server import claude_bridge, config
from server.core import app_liveness, history, lifecycle, training
from server.core.explanation.models import ProviderResponse
from server.core.explanation.providers import (
    ExplanationProvider,
    ExplanationProviderError,
    ProviderInfo,
)
from server.core.session import clear_session
from server.web.app import create_app
from tests.backend.fixtures import (
    CRITICAL_ID,
    GAME_ID,
    analysis_artifact,
    history_record,
    store_analysis_fixture,
)


async def _run_inline(function, *args, **kwargs):
    """Bypass AnyIO worker-thread dispatch, which hangs in this managed Python 3.14 environment."""
    return function(*args, **kwargs)


def tearDownModule() -> None:
    app_liveness.stop()
    lifecycle.stop_watchdog()
    _BOOTSTRAP_DATA.cleanup()
    for key, value in _ORIGINAL_ENV.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


class _BackendBaselineCase(unittest.TestCase):
    def setUp(self) -> None:
        self._data = tempfile.TemporaryDirectory(prefix="chesscoach-phase0-test-")
        self._env = patch.dict(
            os.environ,
            {
                "CHESS_APP_MODE": "0",
                "CHESSCOACH_DATA_DIR": self._data.name,
                "CHESS_SESSION_TTL": "0",
                "CHESS_WEB_OPEN": "0",
            },
        )
        self._env.start()
        self._old_data_dir = config.DATA_DIR
        self._old_username = config.USERNAME
        self._old_personalize = config.PERSONALIZE_HISTORY
        self._old_app_mode = config.APP_MODE
        self._old_web_open = config.WEB_OPEN
        self._old_session_ttl = config.SESSION_TTL_SECONDS
        config.DATA_DIR = self._data.name
        config.USERNAME = ""
        config.PERSONALIZE_HISTORY = False
        config.APP_MODE = False
        config.WEB_OPEN = False
        config.SESSION_TTL_SECONDS = 0
        app_liveness.stop()
        lifecycle.stop_watchdog()
        clear_session()
        self.app = create_app()

    def request(self, method: str, path: str, **kwargs) -> httpx.Response:
        async def send() -> httpx.Response:
            transport = httpx.ASGITransport(app=self.app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as client:
                return await client.request(method, path, **kwargs)

        # FastAPI normally dispatches ``def`` handlers through AnyIO's worker threads. That
        # dispatch hangs in this managed Python 3.14 environment, so only the dispatch primitive
        # is replaced; requests still traverse the real router, middleware, validation and
        # serialization.
        with patch("fastapi.routing.run_in_threadpool", new=_run_inline):
            return asyncio.run(send())

    def tearDown(self) -> None:
        clear_session()
        app_liveness.stop()
        lifecycle.stop_watchdog()
        config.DATA_DIR = self._old_data_dir
        config.USERNAME = self._old_username
        config.PERSONALIZE_HISTORY = self._old_personalize
        config.APP_MODE = self._old_app_mode
        config.WEB_OPEN = self._old_web_open
        config.SESSION_TTL_SECONDS = self._old_session_ttl
        self._env.stop()
        self._data.cleanup()


class LegacyChatContractTests(_BackendBaselineCase):
    def test_openapi_exposes_current_coaching_surface_contracts(self) -> None:
        schema = self.app.openapi()
        paths = schema["paths"]
        expected_methods = {
            "/api/chat": {"post"},
            "/api/profile": {"get"},
            "/api/training/attempt": {"post"},
            "/api/training/hint": {"get"},
            "/api/training/attempts": {"get"},
            "/api/games/{game_id}/explanations": {"get", "post"},
        }
        for path, methods in expected_methods.items():
            with self.subTest(path=path):
                self.assertTrue(methods.issubset(paths[path]))
                for method in methods:
                    self.assertIn("200", paths[path][method]["responses"])
                    self.assertIn("422", paths[path][method]["responses"])

        self.assertEqual(
            "#/components/schemas/ChatBody",
            paths["/api/chat"]["post"]["requestBody"]["content"]["application/json"][
                "schema"
            ]["$ref"],
        )
        self.assertEqual(
            "#/components/schemas/AttemptBody",
            paths["/api/training/attempt"]["post"]["requestBody"]["content"][
                "application/json"
            ]["schema"]["$ref"],
        )
        explanation_body = paths["/api/games/{game_id}/explanations"]["post"][
            "requestBody"
        ]["content"]["application/json"]["schema"]
        self.assertIn(
            "#/components/schemas/GenerateExplanationsBody",
            {item.get("$ref") for item in explanation_body["anyOf"]},
        )
        profile_parameters = {
            item["name"]: item for item in paths["/api/profile"]["get"]["parameters"]
        }
        self.assertEqual(30, profile_parameters["days"]["schema"]["default"])
        hint_parameters = {
            item["name"]: item for item in paths["/api/training/hint"]["get"]["parameters"]
        }
        self.assertTrue(hint_parameters["game_id"]["required"])
        self.assertTrue(hint_parameters["critical_id"]["required"])

    def test_fastapi_rejects_invalid_bodies_and_query_values(self) -> None:
        responses = [
            self.request("POST", "/api/chat", json={}),
            self.request(
                "POST",
                "/api/training/attempt",
                json={
                    "game_id": GAME_ID,
                    "critical_id": CRITICAL_ID,
                    "selected_move": "d1d3",
                    "hints_used": 5,
                },
            ),
            self.request(
                "POST",
                f"/api/games/{GAME_ID}/explanations",
                json={"force": {"not": "a boolean"}},
            ),
            self.request("GET", "/api/profile?days=not-an-integer"),
        ]
        self.assertTrue(all(response.status_code == 422 for response in responses))

    def test_chat_forwards_position_context_and_returns_legacy_response(self) -> None:
        response_payload = {"answer": "The pawn is pinned.", "session_id": "legacy-session"}
        with patch("server.web.routes_chat.claude_bridge.ask", return_value=response_payload) as ask:
            response = self.request(
                "POST",
                "/api/chat",
                json={
                    "question": "Why not take?",
                    "fen": "test-fen",
                    "last_move": "Nxd5",
                    "move_fen": "move-fen",
                    "session_id": "previous-session",
                    "use_profile": True,
                },
            )

        self.assertEqual(200, response.status_code)
        self.assertEqual(response_payload, response.json())
        ask.assert_called_once_with(
            "Why not take?",
            fen="test-fen",
            last_move="Nxd5",
            move_fen="move-fen",
            session_id="previous-session",
            use_profile=True,
        )

    def test_chat_rejects_blank_question_without_calling_model(self) -> None:
        with patch("server.web.routes_chat.claude_bridge.ask") as ask:
            response = self.request("POST", "/api/chat", json={"question": "   "})

        self.assertEqual(400, response.status_code)
        self.assertEqual({"error": "Empty question."}, response.json())
        ask.assert_not_called()

    def test_chat_model_failure_has_stable_503_degradation(self) -> None:
        with patch(
            "server.web.routes_chat.claude_bridge.ask",
            side_effect=claude_bridge.ChatError("Model unavailable."),
        ):
            response = self.request("POST", "/api/chat", json={"question": "What now?"})

        self.assertEqual(503, response.status_code)
        self.assertEqual({"error": "Model unavailable."}, response.json())


class ProfileContractTests(_BackendBaselineCase):
    def test_profile_aggregates_games_weakness_and_training_from_local_files(self) -> None:
        for index in range(1, 4):
            history.append_record(history_record(index), data_dir=self._data.name)
        training.record_attempt(
            game_id=f"{1:020x}",
            critical_id="ply-3",
            selected_move="d2d4",
            verdict="best",
            hints_used=1,
            solved=True,
            source="retry",
            category="missed_opponent_threat",
            phase="middlegame",
            review_side="white",
            data_dir=self._data.name,
        )

        response = self.request("GET", "/api/profile?days=0")

        self.assertEqual(200, response.status_code)
        profile = response.json()
        self.assertEqual(1, profile["schema_version"])
        self.assertEqual("me", profile["player_id"])
        self.assertEqual(0, profile["days"])
        self.assertEqual(3, profile["games"])
        self.assertEqual(1, profile["training"]["total"])
        self.assertEqual(100.0, profile["training"]["solve_rate"])
        self.assertEqual("missed_opponent_threat", profile["weaknesses"][0]["category"])
        self.assertTrue(profile["coach_summary"]["ready"])

    def test_profile_storage_failure_degrades_without_breaking_route(self) -> None:
        with patch("server.web.routes_history.history.insights", side_effect=OSError("bad history")):
            response = self.request("GET", "/api/profile?days=30")

        self.assertEqual(200, response.status_code)
        self.assertEqual({"games": 0, "error": "bad history"}, response.json())


class TrainingContractTests(_BackendBaselineCase):
    def setUp(self) -> None:
        super().setUp()
        store_analysis_fixture(self._data.name)

    def test_cached_best_move_is_scored_and_persisted_without_engine(self) -> None:
        with patch("server.core.training.lines.engine_line") as engine_line:
            response = self.request(
                "POST",
                "/api/training/attempt",
                json={
                    "game_id": GAME_ID,
                    "critical_id": CRITICAL_ID,
                    "selected_move": "d1d3",
                    "review_side": "white",
                    "hints_used": 2,
                    "source": "retry",
                },
            )

        self.assertEqual(200, response.status_code)
        result = response.json()
        self.assertEqual("best", result["verdict"])
        self.assertTrue(result["solved"])
        self.assertTrue(result["used_cached_analysis"])
        self.assertEqual("Qxd3", result["selected_move"]["san"])
        self.assertEqual("white", result["engine_evaluation"]["score"]["pov"])
        engine_line.assert_not_called()

        attempts_response = self.request(
            "GET", f"/api/training/attempts?game_id={GAME_ID}&critical_id={CRITICAL_ID}"
        )
        self.assertEqual(200, attempts_response.status_code)
        attempts = attempts_response.json()
        self.assertEqual(1, attempts["count"])
        self.assertEqual(2, attempts["attempts"][0]["hints_used"])

    def test_uncached_move_survives_engine_failure_as_unknown_attempt(self) -> None:
        with patch("server.core.training.lines.engine_line", side_effect=TimeoutError("engine timeout")):
            response = self.request(
                "POST",
                "/api/training/attempt",
                json={
                    "game_id": GAME_ID,
                    "critical_id": CRITICAL_ID,
                    "selected_move": "h1g1",
                    "review_side": "white",
                },
            )

        self.assertEqual(200, response.status_code)
        result = response.json()
        self.assertEqual("unknown", result["verdict"])
        self.assertFalse(result["solved"])
        self.assertFalse(result["used_cached_analysis"])
        self.assertIsNone(result["win_gap_from_best"])
        self.assertEqual("unknown", result["engine_evaluation"]["label"])
        self.assertEqual(1, len(training.load_attempts(data_dir=self._data.name)))

        analysis = self.request(
            "GET", f"/api/games/{GAME_ID}/analysis?review_side=white"
        )
        hint = self.request(
            "GET",
            f"/api/training/hint?game_id={GAME_ID}&critical_id={CRITICAL_ID}"
            "&review_side=white&level=1",
        )
        self.assertEqual(200, analysis.status_code)
        self.assertEqual(analysis_artifact(), analysis.json())
        self.assertEqual(200, hint.status_code)

    def test_illegal_move_is_rejected_and_hint_is_artifact_backed(self) -> None:
        invalid = self.request(
            "POST",
            "/api/training/attempt",
            json={
                "game_id": GAME_ID,
                "critical_id": CRITICAL_ID,
                "selected_move": "e1e3",
                "review_side": "white",
            },
        )
        hint = self.request(
            "GET",
            f"/api/training/hint?game_id={GAME_ID}&critical_id={CRITICAL_ID}"
            "&review_side=white&level=4",
        )

        self.assertEqual(400, invalid.status_code)
        self.assertIn("not legal", invalid.json()["error"])
        self.assertEqual(200, hint.status_code)
        self.assertEqual("show_line", hint.json()["kind"])
        self.assertEqual(["Qxd3", "Kf7"], hint.json()["line"]["san"])
        self.assertEqual([], training.load_attempts(data_dir=self._data.name))


class _SuccessfulExplanationProvider(ExplanationProvider):
    def __init__(self) -> None:
        self.calls = 0
        self.requests = []

    @property
    def info(self) -> ProviderInfo:
        return ProviderInfo(provider="fake", model="fixed-response")

    def explain_position(self, request) -> ProviderResponse:
        self.calls += 1
        self.requests.append(request)
        played = " ".join(request.payload["variations"]["played_line"]["san"])
        best = " ".join(request.payload["variations"]["best_line"]["san"])
        payload = {
            **request.expected,
            "why_it_looked_reasonable": "It claims space.",
            "core_problem": "It misses the opponent's immediate plan.",
            "why_recommended": ["The saved line keeps the position stable."],
            "played_line_summary": f"{played} gives the opponent the reply.",
            "best_line_summary": f"{best} follows the saved principal variation.",
            "transferable_principle": "Check the opponent's forcing ideas first.",
            "next_time_checklist": ["Checks", "Captures", "Threats"],
            "evidence_refs": [
                "facts.move_effects.best.captured_piece",
                "facts.deltas.material_delta",
            ],
        }
        return ProviderResponse(text=json.dumps(payload))


class _FailingExplanationProvider(ExplanationProvider):
    @property
    def info(self) -> ProviderInfo:
        return ProviderInfo(provider="fake", model="unavailable")

    def explain_position(self, request) -> ProviderResponse:
        raise ExplanationProviderError("fake provider unavailable")


class BoundedExplanationContractTests(_BackendBaselineCase):
    def setUp(self) -> None:
        super().setUp()
        self.analysis = store_analysis_fixture(self._data.name)

    def test_validated_explanation_is_persisted_and_then_cached(self) -> None:
        provider = _SuccessfulExplanationProvider()
        with patch(
            "server.core.explanation.service.configured_provider", return_value=provider
        ):
            first = self.request(
                "POST",
                f"/api/games/{GAME_ID}/explanations",
                json={"review_side": "white", "critical_id": CRITICAL_ID},
            )
            second = self.request(
                "POST",
                f"/api/games/{GAME_ID}/explanations",
                json={"review_side": "white", "critical_id": CRITICAL_ID},
            )

        self.assertEqual(200, first.status_code)
        self.assertEqual([CRITICAL_ID], first.json()["generated"])
        self.assertEqual(200, second.status_code)
        self.assertEqual([CRITICAL_ID], second.json()["cached"])
        self.assertEqual(1, provider.calls)

        request = provider.requests[0]
        critical = self.analysis["critical_positions"][0]
        payload = request.payload
        self.assertEqual(critical["scores"], payload["position"]["scores"])
        for moment, expected_value in (("before", 900), ("after", -900)):
            scores = payload["position"]["scores"][moment]
            self.assertEqual({"white", "mover", "review_side"}, set(scores))
            self.assertTrue(all(score["pov"] == "white" for score in scores.values()))
            self.assertEqual(expected_value, scores["white"]["value"])
        for moment, expected_white in (("before", 92.0), ("after", 8.0)):
            wins = payload["position"]["win_percent"][moment]
            self.assertEqual({"white", "black", "mover", "review_side"}, set(wins))
            self.assertEqual(expected_white, wins["white"])
            self.assertEqual(expected_white, wins["review_side"])
        self.assertEqual(critical["played_line"], payload["variations"]["played_line"])
        self.assertEqual(critical["best_line"], payload["variations"]["best_line"])
        self.assertEqual(critical["fen_before"], payload["position"]["fen_before"])
        self.assertEqual(critical["facts"]["snapshots"], payload["facts"]["snapshots"])
        self.assertEqual(
            critical["facts"]["move_effects"], payload["facts"]["move_effects"]
        )
        self.assertEqual("missed_capture", payload["facts"]["primary_category"])
        self.assertEqual(
            ["missed_opponent_threat"], payload["facts"]["secondary_categories"]
        )
        self.assertEqual(
            ["missed_capture", "missed_opponent_threat"],
            [motif["name"] for motif in payload["facts"]["motifs"]],
        )
        self.assertEqual(
            [candidate["line"] for candidate in critical["candidates"]],
            [candidate["line"] for candidate in payload["variations"]["multi_pv"]],
        )
        for candidate in payload["variations"]["multi_pv"]:
            self.assertEqual({"white", "mover", "review_side"}, set(candidate["scores"]))
            self.assertTrue(
                all(score["pov"] == "white" for score in candidate["scores"].values())
            )
            self.assertEqual(
                {"white", "black", "mover", "review_side"},
                set(candidate["win_percent"]),
            )
        self.assertIn(
            "facts.move_effects.best.captured_piece", request.allowed_evidence_refs
        )
        self.assertIn("facts.deltas.material_delta", request.allowed_evidence_refs)

        stored = self.request(
            "GET", f"/api/games/{GAME_ID}/explanations?review_side=white"
        )
        self.assertEqual(200, stored.status_code)
        self.assertEqual(CRITICAL_ID, stored.json()["positions"][0]["critical_id"])
        self.assertNotIn("api_key", stored.json())

    def test_provider_failure_returns_stable_error_and_does_not_write_artifact(self) -> None:
        analysis_path = Path(self._data.name) / "games" / GAME_ID / "analysis.json"
        analysis_before = analysis_path.read_bytes()
        with patch(
            "server.core.explanation.service.configured_provider",
            return_value=_FailingExplanationProvider(),
        ):
            response = self.request(
                "POST",
                f"/api/games/{GAME_ID}/explanations",
                json={"review_side": "white", "critical_id": CRITICAL_ID},
            )

        self.assertEqual(503, response.status_code)
        self.assertEqual("explanation_failed", response.json()["error"]["code"])
        self.assertIn("fake provider unavailable", response.json()["error"]["message"])
        self.assertFalse(
            (Path(self._data.name) / "games" / GAME_ID / "explanations.json").exists()
        )
        self.assertEqual(analysis_before, analysis_path.read_bytes())

        analysis = self.request(
            "GET", f"/api/games/{GAME_ID}/analysis?review_side=white"
        )
        hint = self.request(
            "GET",
            f"/api/training/hint?game_id={GAME_ID}&critical_id={CRITICAL_ID}"
            "&review_side=white&level=1",
        )
        profile = self.request("GET", "/api/profile?days=0")
        self.assertEqual(200, analysis.status_code)
        self.assertEqual(self.analysis, analysis.json())
        self.assertEqual(200, hint.status_code)
        self.assertEqual("think", hint.json()["kind"])
        self.assertEqual(200, profile.status_code)

    def test_missing_explanation_input_uses_not_found_envelope(self) -> None:
        response = self.request(
            "POST",
            f"/api/games/{GAME_ID}/explanations",
            json={"review_side": "white", "critical_id": "missing"},
        )

        self.assertEqual(404, response.status_code)
        self.assertEqual("explanation_input_not_found", response.json()["error"]["code"])


if __name__ == "__main__":
    unittest.main()
