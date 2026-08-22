from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import json
from pathlib import Path
import tempfile
import unittest

from server import config
from server.core.agent.models import (
    AgentMessageRequest,
    AgentResponse,
    AgentRunRequest,
    AgentRunResult,
    AgentSessionContextRequest,
    AgentSessionCreateRequest,
    ChessReference,
    SuggestedAction,
    ToolCallRecord,
)
from server.core.agent.service import AgentRunAuditLog, AgentServiceFailure, ChessAgentService
from server.core.agent.sessions import (
    ChessSessionCheckpointStore,
    InMemoryConversationSessionFactory,
)
from tests.backend.fixtures import CRITICAL_ID, GAME_ID, TACTICAL_FEN, store_analysis_fixture


RunHandler = Callable[[AgentRunRequest], Awaitable[AgentRunResult]]


class CallbackRuntime:
    def __init__(self) -> None:
        self.requests: list[AgentRunRequest] = []
        self.handler: RunHandler | None = None

    async def run(self, request: AgentRunRequest) -> AgentRunResult:
        self.requests.append(request)
        if self.handler is None:
            raise AssertionError("CallbackRuntime has no handler")
        return await self.handler(request)


def valid_response() -> AgentResponse:
    return AgentResponse(
        text="Rh2 leaves the queen en prise; Qxd3 was the grounded alternative.",
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


def result_for(
    response: AgentResponse | None = None,
    *,
    tool_calls: list[ToolCallRecord] | None = None,
) -> AgentRunResult:
    return AgentRunResult(
        response=response or valid_response(),
        tool_calls=tool_calls or [],
        usage={"input_tokens": 120, "output_tokens": 45},
    )


class AgentServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="agent-service-")
        self._old_data_dir = config.DATA_DIR
        config.DATA_DIR = self._temporary.name
        store_analysis_fixture(self._temporary.name)
        self.store = ChessSessionCheckpointStore(
            self._temporary.name,
            id_factory=lambda: "b" * 32,
        )
        self.conversations = InMemoryConversationSessionFactory()
        self.runtime = CallbackRuntime()
        self.audit = AgentRunAuditLog(self._temporary.name)
        self.service = ChessAgentService(
            checkpoint_store=self.store,
            runtime=self.runtime,
            conversation_factory=self.conversations,
            audit_log=self.audit,
            max_turns=3,
            max_total_tool_calls=2,
            max_engine_tool_calls=1,
            timeout_seconds=9,
        )
        self.session = self.service.create_session(
            AgentSessionCreateRequest(
                game_id=GAME_ID,
                review_side="white",
                active_ply=0,
                active_critical_id=CRITICAL_ID,
            )
        ).session

    def tearDown(self) -> None:
        config.DATA_DIR = self._old_data_dir
        self._temporary.cleanup()

    async def conversation_items(self) -> list[object]:
        return await self.conversations.get_session(self.session.session_id).get_items()

    async def test_success_commits_staged_conversation_and_passes_canonical_budgeted_run(self) -> None:
        staged = [
            {"role": "user", "content": "Why not Qxd3?"},
            {"role": "assistant", "content": "Structured SDK output"},
        ]

        async def run(request: AgentRunRequest) -> AgentRunResult:
            await self.service.session_for_runtime(request.session_id).add_items(staged)
            return result_for()

        self.runtime.handler = run

        response = await self.service.send_message(
            self.session.session_id,
            AgentMessageRequest(message="Why not d1d3?", expected_generation=0),
        )

        self.assertEqual(0, response.session.generation)
        self.assertEqual(valid_response(), response.response)
        self.assertEqual(staged, await self.conversation_items())
        run_request = self.runtime.requests[0]
        self.assertEqual(3, run_request.max_turns)
        self.assertEqual(2, run_request.max_total_tool_calls)
        self.assertEqual(1, run_request.max_engine_tool_calls)
        self.assertEqual(9, run_request.timeout_seconds)
        self.assertEqual(
            ["get_review_context", "analyze_move"],
            run_request.allowed_tools,
        )
        self.assertEqual(TACTICAL_FEN, run_request.model_context.position.fen)
        self.assertEqual(1, run_request.model_context.position.reference.ply)
        self.assertLessEqual(len(run_request.model_context.engine_facts.candidates), 3)

        records = self.audit.path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(1, len(records))
        audit = json.loads(records[0])
        self.assertEqual("success", audit["status"])
        self.assertEqual(self.session.session_id, audit["session_id"])
        serialized = json.dumps(audit, sort_keys=True)
        self.assertNotIn("Why not", serialized)
        self.assertNotIn(response.response.text, serialized)
        self.assertNotIn(response.response.evidence_refs[0], serialized)

    async def test_stale_generation_discards_runtime_staging(self) -> None:
        async def run(request: AgentRunRequest) -> AgentRunResult:
            await self.service.session_for_runtime(request.session_id).add_items(
                [{"role": "assistant", "content": "must be discarded"}]
            )
            await self.service.update_context(
                request.session_id,
                AgentSessionContextRequest(
                    expected_generation=0,
                    activity="game_review",
                ),
            )
            return result_for()

        self.runtime.handler = run

        with self.assertRaises(AgentServiceFailure) as raised:
            await self.service.send_message(
                self.session.session_id,
                AgentMessageRequest(message="Explain this.", expected_generation=0),
            )

        self.assertEqual("stale_agent_context", raised.exception.error.code)
        self.assertEqual(1, self.store.get(self.session.session_id).generation)
        self.assertEqual([], await self.conversation_items())
        self.assertFalse(self.audit.path.exists())

    async def test_busy_run_and_cancellation_never_commit_partial_items(self) -> None:
        entered = asyncio.Event()
        release = asyncio.Event()

        async def block(request: AgentRunRequest) -> AgentRunResult:
            await self.service.session_for_runtime(request.session_id).add_items(
                [{"role": "user", "content": "partial"}]
            )
            entered.set()
            await release.wait()
            return result_for()

        self.runtime.handler = block
        first = asyncio.create_task(
            self.service.send_message(
                self.session.session_id,
                AgentMessageRequest(message="First", expected_generation=0),
            )
        )
        await asyncio.wait_for(entered.wait(), timeout=1)

        with self.assertRaises(AgentServiceFailure) as raised:
            await self.service.send_message(
                self.session.session_id,
                AgentMessageRequest(message="Second", expected_generation=0),
            )
        self.assertEqual("session_busy", raised.exception.error.code)
        self.assertEqual([], await self.conversation_items())

        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
        self.assertEqual([], await self.conversation_items())
        self.assertFalse(self.audit.path.exists())

    async def test_ungrounded_evidence_reference_and_action_are_rejected_without_commit(self) -> None:
        invalid_responses = {
            "evidence": AgentResponse(
                text="Unsupported citation.",
                evidence_refs=["invented:evidence"],
            ),
            "reference": AgentResponse(
                text="Wrong game.",
                references=[ChessReference(kind="game", game_id="f" * 20)],
            ),
            "action": AgentResponse(
                text="Unsupported action.",
                suggested_actions=[
                    SuggestedAction(
                        kind="start_training",
                        label="Start training",
                        target={"game_id": GAME_ID},
                    )
                ],
            ),
        }

        for label, response in invalid_responses.items():
            async def run(request: AgentRunRequest, value: AgentResponse = response) -> AgentRunResult:
                await self.service.session_for_runtime(request.session_id).add_items(
                    [{"role": "assistant", "content": label}]
                )
                return result_for(value)

            with self.subTest(case=label):
                self.runtime.handler = run
                with self.assertRaises(AgentServiceFailure) as raised:
                    await self.service.send_message(
                        self.session.session_id,
                        AgentMessageRequest(message="Explain this.", expected_generation=0),
                    )
                self.assertEqual("invalid_agent_response", raised.exception.error.code)
                self.assertEqual([], await self.conversation_items())

        self.assertFalse(self.audit.path.exists())

    async def test_tool_budget_overrun_discards_conversation(self) -> None:
        call = ToolCallRecord(
            name="get_review_context",
            permission="read",
            status="ok",
            duration_ms=1,
        )

        async def run(request: AgentRunRequest) -> AgentRunResult:
            await self.service.session_for_runtime(request.session_id).add_items(
                [{"role": "assistant", "content": "over budget"}]
            )
            return result_for(tool_calls=[call, call, call])

        self.runtime.handler = run

        with self.assertRaises(AgentServiceFailure) as raised:
            await self.service.send_message(
                self.session.session_id,
                AgentMessageRequest(message="Explain this.", expected_generation=0),
            )

        self.assertEqual("invalid_agent_response", raised.exception.error.code)
        self.assertEqual([], await self.conversation_items())
        self.assertFalse(self.audit.path.exists())

    async def test_wall_clock_timeout_is_typed_and_discards_conversation(self) -> None:
        self.service.timeout_seconds = 1

        async def run(request: AgentRunRequest) -> AgentRunResult:
            await self.service.session_for_runtime(request.session_id).add_items(
                [{"role": "assistant", "content": "partial timeout output"}]
            )
            await asyncio.sleep(2)
            return result_for()

        self.runtime.handler = run

        with self.assertRaises(AgentServiceFailure) as raised:
            await self.service.send_message(
                self.session.session_id,
                AgentMessageRequest(message="Explain this.", expected_generation=0),
            )

        self.assertEqual("agent_timeout", raised.exception.error.code)
        self.assertEqual([], await self.conversation_items())
        self.assertFalse(self.audit.path.exists())

    async def test_audit_failure_rolls_back_conversation(self) -> None:
        class FailingAudit:
            def append_success(self, **_kwargs) -> None:
                raise OSError("disk full")

        self.service.audit_log = FailingAudit()

        async def run(request: AgentRunRequest) -> AgentRunResult:
            await self.service.session_for_runtime(request.session_id).add_items(
                [{"role": "assistant", "content": "must roll back"}]
            )
            return result_for()

        self.runtime.handler = run
        with self.assertRaises(OSError):
            await self.service.send_message(
                self.session.session_id,
                AgentMessageRequest(message="Explain this.", expected_generation=0),
            )
        self.assertEqual([], await self.conversation_items())

    async def test_delete_is_busy_while_message_run_is_active(self) -> None:
        entered = asyncio.Event()
        release = asyncio.Event()

        async def run(_request: AgentRunRequest) -> AgentRunResult:
            entered.set()
            await release.wait()
            return result_for()

        self.runtime.handler = run
        message = asyncio.create_task(
            self.service.send_message(
                self.session.session_id,
                AgentMessageRequest(message="Explain this.", expected_generation=0),
            )
        )
        await entered.wait()
        with self.assertRaises(AgentServiceFailure) as raised:
            await self.service.delete_session(self.session.session_id)
        self.assertEqual("session_busy", raised.exception.error.code)
        release.set()
        await message
        await self.service.delete_session(self.session.session_id)
        self.assertEqual([], await self.conversation_items())


if __name__ == "__main__":
    unittest.main()
