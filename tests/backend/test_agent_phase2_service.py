from __future__ import annotations

from collections.abc import Awaitable, Callable
import asyncio
import json
import tempfile
import unittest
from unittest.mock import patch

import chess
import httpx

from server import config
from server.core.agent.models import (
    AgentMessageRequest,
    AgentResponse,
    AgentRunRequest,
    AgentRunResult,
    AgentSessionCreateRequest,
    ChessReference,
    PositionReference,
    SuggestedAction,
)
from server.core.agent.service import ChessAgentService
from server.core.agent.sessions import (
    ChessSessionCheckpointStore,
    InMemoryConversationSessionFactory,
)
from server.core.agent.tools import ActiveReviewArtifact, AgentTools
from server.web.app import create_app
from tests.backend.fixtures import CRITICAL_ID, GAME_ID, TACTICAL_FEN, store_analysis_fixture


SESSION_ID = "7" * 32
START_FEN = chess.Board().fen()
RunHandler = Callable[[AgentRunRequest], Awaitable[AgentRunResult]]


class CallbackRuntime:
    def __init__(self) -> None:
        self.requests: list[AgentRunRequest] = []
        self.handler: RunHandler | None = None

    async def run(self, request: AgentRunRequest) -> AgentRunResult:
        self.requests.append(request)
        if self.handler is None:
            raise AssertionError("runtime handler is not configured")
        return await self.handler(request)


def critical_response() -> AgentResponse:
    return AgentResponse(
        text="Start with the saved blunder because it had the largest verified loss.",
        references=[
            ChessReference(
                kind="critical_position",
                game_id=GAME_ID,
                review_side="white",
                critical_id=CRITICAL_ID,
                ply=1,
                fen=TACTICAL_FEN,
            )
        ],
        evidence_refs=[f"review:{GAME_ID}:white:{CRITICAL_ID}"],
        suggested_actions=[
            SuggestedAction(
                kind="start_retry",
                label="Retry this position",
                target={
                    "game_id": GAME_ID,
                    "review_side": "white",
                    "critical_id": CRITICAL_ID,
                },
            )
        ],
    )


class FailingSummaryBuilder:
    def summarize(self, _items, _previous_summary="") -> str:
        raise RuntimeError("summary unavailable")


class Phase2AgentServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="agent-phase2-service-")
        self.addCleanup(self._temporary.cleanup)
        self._old_data_dir = config.DATA_DIR
        self._old_personalize = config.PERSONALIZE_HISTORY
        config.DATA_DIR = self._temporary.name
        config.PERSONALIZE_HISTORY = True
        self.addAsyncCleanup(self._restore_config)
        store_analysis_fixture(self._temporary.name)
        self.store = ChessSessionCheckpointStore(
            self._temporary.name,
            id_factory=lambda: SESSION_ID,
        )
        self.conversations = InMemoryConversationSessionFactory()
        self.runtime = CallbackRuntime()
        self.service = ChessAgentService(
            checkpoint_store=self.store,
            runtime=self.runtime,
            conversation_factory=self.conversations,
        )

    async def _restore_config(self) -> None:
        config.DATA_DIR = self._old_data_dir
        config.PERSONALIZE_HISTORY = self._old_personalize

    def create_review_session(self):
        return self.service.create_session(
            AgentSessionCreateRequest(
                game_id=GAME_ID,
                review_side="white",
                active_ply=0,
                active_critical_id=CRITICAL_ID,
            )
        ).session

    async def test_explicit_reference_resolves_before_run_without_mutating_checkpoint(self) -> None:
        session = self.service.create_session(AgentSessionCreateRequest()).session

        async def run(request: AgentRunRequest) -> AgentRunResult:
            self.assertEqual(TACTICAL_FEN, request.model_context.position.fen)
            self.assertEqual(CRITICAL_ID, request.model_context.position.reference.critical_id)
            return AgentRunResult(response=critical_response(), tool_calls=[])

        self.runtime.handler = run
        response = await self.service.send_message(
            session.session_id,
            AgentMessageRequest(
                message="Explain that position.",
                expected_generation=0,
                position_reference=PositionReference(
                    game_id=GAME_ID,
                    review_side="white",
                    critical_id=CRITICAL_ID,
                ),
            ),
        )

        self.assertEqual(critical_response(), response.response)
        checkpoint = self.store.get(session.session_id)
        self.assertIsNone(checkpoint.active_game_id)
        self.assertEqual(CRITICAL_ID, checkpoint.discussed_positions[-1].critical_id)

    async def test_ambiguous_recent_turn_references_request_clarification_without_runtime(self) -> None:
        session = self.service.create_session(AgentSessionCreateRequest()).session
        backing = self.conversations.get_session(session.session_id)
        await backing.add_items(
            [
                {
                    "role": "assistant",
                    "content": [{
                        "type": "output_text",
                        "text": json.dumps({"references": [{"fen": START_FEN}]}),
                    }],
                },
                {
                    "role": "assistant",
                    "content": [{
                        "type": "output_text",
                        "text": json.dumps({"references": [{"fen": TACTICAL_FEN}]}),
                    }],
                },
            ]
        )

        result = await self.service.send_message(
            session.session_id,
            AgentMessageRequest(message="What about there?", expected_generation=0),
        )

        self.assertIn("more than one", result.response.text)
        self.assertEqual([], self.runtime.requests)
        self.assertEqual(4, len(await backing.get_items()))

    async def test_long_history_is_summarized_without_deleting_raw_sdk_items(self) -> None:
        session = self.create_review_session()
        backing = self.conversations.get_session(session.session_id)
        await backing.add_items(
            [
                {
                    "role": "user" if index % 2 == 0 else "assistant",
                    "content": f"Old conversation item {index}?",
                }
                for index in range(14)
            ]
        )

        async def run(_request: AgentRunRequest) -> AgentRunResult:
            recent = await self.service.session_for_runtime(session.session_id).get_items()
            self.assertEqual(12, len(recent))
            return AgentRunResult(response=critical_response(), tool_calls=[])

        self.runtime.handler = run
        result = await self.service.send_message(
            session.session_id,
            AgentMessageRequest(message="Explain this position.", expected_generation=0),
        )

        self.assertEqual(16, len(await backing.get_items()))
        checkpoint = self.store.get(session.session_id)
        self.assertTrue(checkpoint.conversation_summary)
        self.assertIn("continuity only", checkpoint.conversation_summary)
        self.assertEqual(checkpoint.conversation_summary, result.session.conversation_summary)

    async def test_summary_failure_keeps_success_and_all_raw_items(self) -> None:
        self.service.summary_builder = FailingSummaryBuilder()  # type: ignore[assignment]
        session = self.create_review_session()
        backing = self.conversations.get_session(session.session_id)
        await backing.add_items(
            [{"role": "user", "content": f"old {index}"} for index in range(13)]
        )
        self.runtime.handler = lambda _request: _completed_result(critical_response())

        response = await self.service.send_message(
            session.session_id,
            AgentMessageRequest(message="Explain this.", expected_generation=0),
        )

        self.assertEqual(critical_response(), response.response)
        self.assertEqual(15, len(await backing.get_items()))
        self.assertEqual("", self.store.get(session.session_id).conversation_summary)

    async def test_review_priority_context_is_bounded_and_keeps_largest_error(self) -> None:
        session = self.create_review_session()

        async def run(request: AgentRunRequest) -> AgentRunResult:
            priorities = request.model_context.review_priorities
            self.assertIsNotNone(priorities)
            self.assertLessEqual(len(priorities.candidates), 8)
            self.assertTrue(priorities.candidates[0].largest_error)
            self.assertIn("get_player_profile", request.allowed_tools)
            return AgentRunResult(response=critical_response(), tool_calls=[])

        self.runtime.handler = run
        result = await self.service.send_message(
            session.session_id,
            AgentMessageRequest(message="这盘先复盘哪里？", expected_generation=0),
        )

        self.assertEqual(CRITICAL_ID, result.response.references[0].critical_id)

    async def test_disabled_personalization_does_not_read_profile_for_priorities(self) -> None:
        session = self.create_review_session()
        profile_reads = 0

        def load_profile() -> dict:
            nonlocal profile_reads
            profile_reads += 1
            return {"recent": {"categories": []}}

        def tools_factory(bundle) -> AgentTools:
            return AgentTools(
                active_review=ActiveReviewArtifact.from_analysis(
                    bundle.analysis,
                    CRITICAL_ID,
                ),
                profile_loader=load_profile,
                # The global user setting must still win over an injected tool configuration.
                personalization_enabled=True,
            )

        self.service.tools_factory = tools_factory
        config.PERSONALIZE_HISTORY = False

        async def run(request: AgentRunRequest) -> AgentRunResult:
            self.assertFalse(request.model_context.task.personalization_enabled)
            self.assertNotIn("get_player_profile", request.allowed_tools)
            self.assertTrue(
                all(
                    candidate.recurrence_evidence == 0
                    for candidate in request.model_context.review_priorities.candidates
                )
            )
            return AgentRunResult(response=critical_response(), tool_calls=[])

        self.runtime.handler = run
        await self.service.send_message(
            session.session_id,
            AgentMessageRequest(message="这盘先复盘哪里？", expected_generation=0),
        )

        self.assertEqual(0, profile_reads)


async def _completed_result(response: AgentResponse) -> AgentRunResult:
    return AgentRunResult(response=response, tool_calls=[])


class Phase2AgentRouteIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="agent-phase2-route-")
        self.old_data_dir = config.DATA_DIR
        self.old_web_host = config.WEB_HOST
        config.DATA_DIR = self._temporary.name
        config.WEB_HOST = "127.0.0.1"
        store_analysis_fixture(self._temporary.name)
        self.runtime = CallbackRuntime()
        self.service = ChessAgentService(
            checkpoint_store=ChessSessionCheckpointStore(
                self._temporary.name,
                id_factory=lambda: SESSION_ID,
            ),
            runtime=self.runtime,
            conversation_factory=InMemoryConversationSessionFactory(),
        )
        with patch("server.web.app.app_liveness.start"):
            self.app = create_app(agent_service=self.service)

    def tearDown(self) -> None:
        config.DATA_DIR = self.old_data_dir
        config.WEB_HOST = self.old_web_host
        self._temporary.cleanup()

    def request(self, method: str, path: str, **kwargs) -> httpx.Response:
        async def send() -> httpx.Response:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=self.app),
                base_url="http://testserver",
            ) as client:
                return await client.request(method, path, **kwargs)

        return asyncio.run(send())

    def test_exploration_context_is_replayed_owned_and_generation_guarded(self) -> None:
        created = self.request(
            "POST",
            "/api/agent/sessions",
            json={
                "game_id": GAME_ID,
                "review_side": "white",
                "active_ply": 0,
                "active_critical_id": CRITICAL_ID,
            },
        )
        self.assertEqual(200, created.status_code)

        board = chess.Board(TACTICAL_FEN)
        move = chess.Move.from_uci("d1d3")
        san = board.san(move)
        board.push(move)
        body = {
            "expected_generation": 0,
            "game_id": GAME_ID,
            "review_side": "white",
            "active_ply": 0,
            "active_critical_id": CRITICAL_ID,
            "activity": "position_analysis",
            "position": {
                "fen": board.fen(),
                "recent_moves_uci": [],
                "recent_moves_san": [],
                "exploration_moves_uci": [move.uci()],
                "exploration_moves_san": [san],
                "reference": {
                    "game_id": GAME_ID,
                    "review_side": "white",
                    "critical_id": CRITICAL_ID,
                    "ply": 1,
                    "fen": TACTICAL_FEN,
                },
            },
        }
        updated = self.request(
            "POST",
            f"/api/agent/sessions/{SESSION_ID}/context",
            json=body,
        )
        self.assertEqual(200, updated.status_code, updated.text)
        self.assertEqual(1, updated.json()["session"]["generation"])
        self.assertEqual(board.fen(), updated.json()["session"]["position"]["fen"])

        repeated = self.request(
            "POST",
            f"/api/agent/sessions/{SESSION_ID}/context",
            json={**body, "expected_generation": 1},
        )
        self.assertEqual(200, repeated.status_code)
        self.assertEqual(1, repeated.json()["session"]["generation"])

        stale = self.request(
            "POST",
            f"/api/agent/sessions/{SESSION_ID}/context",
            json={**body, "expected_generation": 0},
        )
        self.assertEqual(409, stale.status_code)
        self.assertEqual("stale_agent_context", stale.json()["error"]["code"])

    def test_training_activity_clears_review_ownership_and_can_return_to_review(self) -> None:
        created = self.request(
            "POST",
            "/api/agent/sessions",
            json={
                "game_id": GAME_ID,
                "review_side": "white",
                "active_ply": 0,
                "active_critical_id": CRITICAL_ID,
            },
        )
        self.assertEqual(200, created.status_code)

        training = self.request(
            "POST",
            f"/api/agent/sessions/{SESSION_ID}/context",
            json={
                "expected_generation": 0,
                "game_id": None,
                "review_side": None,
                "active_ply": None,
                "active_critical_id": None,
                "activity": "training",
                "focus_ref": None,
                "position": {
                    "fen": START_FEN,
                    "recent_moves_uci": [],
                    "recent_moves_san": [],
                    "reference": {"fen": START_FEN},
                },
            },
        )
        self.assertEqual(200, training.status_code, training.text)
        training_state = training.json()["session"]
        self.assertEqual(1, training_state["generation"])
        self.assertEqual("training", training_state["activity"])
        self.assertIsNone(training_state["active_game_id"])
        self.assertEqual(START_FEN, training_state["position"]["fen"])

        review = self.request(
            "POST",
            f"/api/agent/sessions/{SESSION_ID}/context",
            json={
                "expected_generation": 1,
                "game_id": GAME_ID,
                "review_side": "white",
                "active_ply": 0,
                "active_critical_id": CRITICAL_ID,
                "activity": "game_review",
                "focus_ref": f"critical:{CRITICAL_ID}",
                "position": {
                    "fen": TACTICAL_FEN,
                    "recent_moves_uci": [],
                    "recent_moves_san": [],
                    "reference": {
                        "game_id": GAME_ID,
                        "review_side": "white",
                        "critical_id": CRITICAL_ID,
                        "ply": 1,
                        "fen": TACTICAL_FEN,
                    },
                },
            },
        )
        self.assertEqual(200, review.status_code, review.text)
        review_state = review.json()["session"]
        self.assertEqual(2, review_state["generation"])
        self.assertEqual(GAME_ID, review_state["active_game_id"])
        self.assertEqual(CRITICAL_ID, review_state["active_critical_id"])


if __name__ == "__main__":
    unittest.main()
