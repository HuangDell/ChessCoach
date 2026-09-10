"""FastAPI integration coverage for the optional Phase 1 Agent surface."""
from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

import httpx

from server import config
from server.core.agent.models import (
    AgentError,
    AgentMessageRequest,
    AgentMessageResponse,
    AgentResponse,
    AgentSessionContextRequest,
    AgentSessionCreateRequest,
    AgentSessionResponse,
    AgentSessionState,
    AgentSessionSummary,
    SessionError,
)
from server.core.agent.service import AgentServiceFailure
from server.web.app import create_app


SESSION_ID = "1" * 32
START_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"


async def _run_inline(function, *args, **kwargs):
    return function(*args, **kwargs)


class FakeAgentService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self.failure: AgentServiceFailure | None = None
        self.closed = False
        self.state = AgentSessionState(
            session_id=SESSION_ID,
            generation=0,
            created_at="2026-08-22T00:00:00Z",
            updated_at="2026-08-22T00:00:00Z",
        )

    def capability(self) -> dict:
        return {
            "enabled": True,
            "available": True,
            "model": "test-agent-model",
            "endpoint_type": "openai_responses",
            "features": {"review_chat": True},
        }

    def _fail(self) -> None:
        if self.failure is not None:
            raise self.failure

    def create_session(self, request: AgentSessionCreateRequest) -> AgentSessionResponse:
        self.calls.append(("create", request))
        self._fail()
        self.state = AgentSessionState(
            session_id=SESSION_ID,
            active_game_id=request.game_id,
            review_side=request.review_side,
            active_ply=request.active_ply,
            active_critical_id=request.active_critical_id,
            generation=0,
            created_at="2026-08-22T00:00:00Z",
            updated_at="2026-08-22T00:00:00Z",
        )
        return AgentSessionResponse(session=self.state)

    def get_session(self, session_id: str) -> AgentSessionResponse:
        self.calls.append(("get", session_id))
        self._fail()
        return AgentSessionResponse(session=self.state)

    async def update_context(
        self,
        session_id: str,
        request: AgentSessionContextRequest,
    ) -> AgentSessionResponse:
        self.calls.append(("context", (session_id, request)))
        self._fail()
        values = self.state.model_dump(mode="python")
        values.update(
            active_game_id=request.game_id,
            review_side=request.review_side,
            active_ply=request.active_ply,
            active_critical_id=request.active_critical_id,
            activity=request.activity or self.state.activity,
            position=request.position,
            generation=request.expected_generation + 1,
            updated_at="2026-08-22T00:01:00Z",
        )
        self.state = AgentSessionState.model_validate(values)
        return AgentSessionResponse(session=self.state)

    async def send_message(
        self,
        session_id: str,
        request: AgentMessageRequest,
    ) -> AgentMessageResponse:
        self.calls.append(("message", (session_id, request)))
        self._fail()
        return AgentMessageResponse(
            session=AgentSessionSummary(
                session_id=session_id,
                generation=request.expected_generation,
            ),
            response=AgentResponse(text="The position is grounded."),
            tool_calls=[],
        )

    async def delete_session(self, session_id: str) -> None:
        self.calls.append(("delete", session_id))
        self._fail()

    def run_metrics(self, *, limit: int = 100) -> dict:
        self.calls.append(("metrics", limit))
        return {"schema_version": 1, "record_count": 2, "limit": limit}

    def clear_runs(self) -> dict:
        self.calls.append(("clear_runs", None))
        return {"records_removed": 2, "bytes_removed": 123}

    async def close(self) -> None:
        self.closed = True


class AgentRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = FakeAgentService()
        self.old_web_host = config.WEB_HOST
        self.old_app_mode = config.APP_MODE
        config.WEB_HOST = "127.0.0.1"
        config.APP_MODE = False
        with patch("server.web.app.app_liveness.start"):
            self.app = create_app(agent_service=self.service)  # type: ignore[arg-type]

    def tearDown(self) -> None:
        config.WEB_HOST = self.old_web_host
        config.APP_MODE = self.old_app_mode

    def request(self, method: str, path: str, **kwargs) -> httpx.Response:
        async def send() -> httpx.Response:
            transport = httpx.ASGITransport(app=self.app)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://testserver",
            ) as client:
                return await client.request(method, path, **kwargs)

        with patch("fastapi.routing.run_in_threadpool", new=_run_inline):
            return asyncio.run(send())

    def test_session_context_message_and_delete_contract(self) -> None:
        created = self.request("POST", "/api/agent/sessions", json={})
        self.assertEqual(200, created.status_code)
        self.assertEqual(SESSION_ID, created.json()["session"]["session_id"])

        fetched = self.request("GET", f"/api/agent/sessions/{SESSION_ID}")
        self.assertEqual(200, fetched.status_code)
        self.assertEqual(0, fetched.json()["session"]["generation"])

        context_body = {
            "expected_generation": 0,
            "game_id": None,
            "review_side": None,
            "active_ply": None,
            "active_critical_id": None,
            "activity": "position_analysis",
            "position": {
                "fen": START_FEN,
                "recent_moves_uci": [],
                "recent_moves_san": [],
                "reference": {"fen": START_FEN},
            },
        }
        updated = self.request(
            "POST",
            f"/api/agent/sessions/{SESSION_ID}/context",
            json=context_body,
        )
        self.assertEqual(200, updated.status_code)
        self.assertEqual(1, updated.json()["session"]["generation"])
        context_request = self.service.calls[2][1][1]  # type: ignore[index]
        self.assertIsInstance(context_request, AgentSessionContextRequest)
        self.assertEqual(START_FEN, context_request.position.fen)

        message = self.request(
            "POST",
            f"/api/agent/sessions/{SESSION_ID}/messages",
            json={"message": "Why this move?", "expected_generation": 1},
        )
        self.assertEqual(200, message.status_code)
        self.assertEqual("The position is grounded.", message.json()["response"]["text"])
        message_request = self.service.calls[3][1][1]  # type: ignore[index]
        self.assertIsInstance(message_request, AgentMessageRequest)

        deleted = self.request("DELETE", f"/api/agent/sessions/{SESSION_ID}")
        self.assertEqual(204, deleted.status_code)
        self.assertEqual(b"", deleted.content)
        self.assertEqual(
            ["create", "get", "context", "message", "delete"],
            [name for name, _value in self.service.calls],
        )

    def test_typed_service_failures_map_to_stable_status_codes(self) -> None:
        cases = [
            (SessionError(code="session_not_found", message="missing", recoverable=False), 404),
            (SessionError(code="stale_agent_context", message="stale", recoverable=True), 409),
            (SessionError(code="session_busy", message="busy", recoverable=True), 409),
            (SessionError(code="invalid_session_context", message="invalid", recoverable=False), 400),
            (AgentError(code="agent_provider_error", message="provider", recoverable=True), 502),
            (AgentError(code="invalid_agent_response", message="invalid output", recoverable=True), 502),
            (AgentError(code="max_turns_exceeded", message="turns", recoverable=True), 502),
            (AgentError(code="agent_unavailable", message="unavailable", recoverable=True), 503),
            (AgentError(code="agent_authentication_failed", message="auth", recoverable=True), 503),
            (AgentError(code="agent_rate_limited", message="limited", recoverable=True), 503),
            (AgentError(code="agent_timeout", message="timeout", recoverable=True), 504),
        ]
        for error, expected_status in cases:
            with self.subTest(code=error.code):
                self.service.failure = AgentServiceFailure(error)
                response = self.request(
                    "POST",
                    f"/api/agent/sessions/{SESSION_ID}/messages",
                    json={"message": "Question", "expected_generation": 0},
                )
                self.assertEqual(expected_status, response.status_code)
                self.assertEqual(
                    {"error": error.model_dump(mode="json")},
                    response.json(),
                )

    def test_fastapi_validation_rejects_invalid_agent_requests(self) -> None:
        responses = [
            self.request("POST", "/api/agent/sessions", json={"active_ply": 3}),
            self.request(
                "POST",
                f"/api/agent/sessions/{SESSION_ID}/context",
                json={"expected_generation": -1},
            ),
            self.request(
                "POST",
                f"/api/agent/sessions/{SESSION_ID}/messages",
                json={"message": "   ", "expected_generation": 0},
            ),
        ]
        self.assertTrue(all(response.status_code == 422 for response in responses))

    def test_app_config_exposes_only_safe_agent_capability(self) -> None:
        response = self.request("GET", "/api/app-config")
        self.assertEqual(200, response.status_code)
        self.assertEqual(self.service.capability(), response.json()["agent"])
        serialized = response.text.lower()
        self.assertNotIn("base_url", serialized)
        self.assertNotIn("api_key", serialized)

    def test_metrics_limit_and_run_only_cleanup_contract(self) -> None:
        metrics = self.request("GET", "/api/agent/metrics?limit=250")
        self.assertEqual(200, metrics.status_code)
        self.assertEqual(250, metrics.json()["limit"])

        cleared = self.request("DELETE", "/api/agent/runs")
        self.assertEqual(200, cleared.status_code)
        self.assertEqual(2, cleared.json()["records_removed"])

        invalid = self.request("GET", "/api/agent/metrics?limit=1001")
        self.assertEqual(422, invalid.status_code)

    def test_openapi_and_existing_guards_remain_active(self) -> None:
        paths = self.app.openapi()["paths"]
        self.assertEqual(
            {"post"},
            set(paths["/api/agent/sessions"]),
        )
        self.assertTrue(
            {"get", "delete"}.issubset(paths["/api/agent/sessions/{session_id}"])
        )
        self.assertEqual(
            {"post"},
            set(paths["/api/agent/sessions/{session_id}/context"]),
        )
        self.assertEqual(
            {"post"},
            set(paths["/api/agent/sessions/{session_id}/messages"]),
        )
        responses = paths["/api/agent/sessions/{session_id}/messages"]["post"]["responses"]
        for status in ("400", "404", "409", "502", "503", "504", "422"):
            self.assertIn(status, responses)

        self.assertEqual(200, self.request("GET", "/api/session").status_code)
        self.assertEqual(
            403,
            self.request(
                "POST",
                "/api/agent/sessions",
                json={},
                headers={"host": "attacker.example"},
            ).status_code,
        )
        self.assertEqual(
            403,
            self.request(
                "POST",
                "/api/agent/sessions",
                json={},
                headers={"origin": "https://attacker.example"},
            ).status_code,
        )


class AgentLifecycleTests(unittest.TestCase):
    def test_lifespan_creates_and_closes_only_the_default_agent_service(self) -> None:
        default_service = FakeAgentService()
        with (
            patch("server.web.app.create_default_agent_service", return_value=default_service),
            patch("server.web.app.app_liveness.start"),
            patch("server.web.app.lifecycle.start_watchdog"),
            patch("server.web.app.lifecycle.stop_watchdog"),
            patch("server.web.app.engine.shutdown"),
        ):
            app = create_app()

            async def exercise() -> None:
                async with app.router.lifespan_context(app):
                    self.assertIs(default_service, app.state.agent_service)

            asyncio.run(exercise())

        self.assertTrue(default_service.closed)

        injected_service = FakeAgentService()
        with (
            patch("server.web.app.app_liveness.start"),
            patch("server.web.app.lifecycle.start_watchdog"),
            patch("server.web.app.lifecycle.stop_watchdog"),
            patch("server.web.app.engine.shutdown"),
        ):
            app = create_app(agent_service=injected_service)  # type: ignore[arg-type]

            async def exercise_injected() -> None:
                async with app.router.lifespan_context(app):
                    self.assertIs(injected_service, app.state.agent_service)

            asyncio.run(exercise_injected())

        self.assertFalse(injected_service.closed)


if __name__ == "__main__":
    unittest.main()
