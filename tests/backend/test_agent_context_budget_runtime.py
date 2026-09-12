from __future__ import annotations

import importlib.util
import asyncio
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from server.core.agent.context_budget import ContextBudget
from server.core.agent.models import AgentResponse, AgentSessionCreateRequest, AgentSessionContextRequest
from server.core.agent.runtime import AgentRuntimeFailure
from server.core.agent.runtime_openai import OpenAIAgentsRuntime, SQLiteConversationSessionFactory
from server.core.agent.sessions import (
    ChessSessionCheckpointStore, GenerationGuardedSession, SessionMutationCoordinator, StaleAgentContextError,
)
from server.core.storage.agent_runs import AgentUsageSummary
from tests.backend.test_agent_context_budget import turns
from tests.backend.test_agent_runtime_openai import _request


@unittest.skipUnless(importlib.util.find_spec("agents"), "optional Agent SDK is not installed")
class BudgetRuntimeIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import agents
        from openai.types.responses import ResponseOutputMessage, ResponseOutputText

        self.temp = tempfile.TemporaryDirectory(prefix="agent-context-runtime-")
        self.addCleanup(self.temp.cleanup)
        self.store = ChessSessionCheckpointStore(self.temp.name)
        self.state = self.store.create(AgentSessionCreateRequest())
        self.factory = SQLiteConversationSessionFactory(self.temp.name)
        self.addCleanup(self.factory.close)
        self.backing = self.factory.get_session(self.state.session_id)
        self.guarded = self.guard()
        captured = self.inputs = []

        class Model(agents.Model):
            async def get_response(self, **kwargs):
                captured.append(kwargs)
                return agents.ModelResponse(
                    output=[ResponseOutputMessage(id="msg-test", type="message", role="assistant", status="completed",
                        content=[ResponseOutputText(type="output_text", annotations=[],
                            text=AgentResponse(text="Focus on development.").model_dump_json())])],
                    usage=agents.Usage(requests=1, input_tokens=8000, output_tokens=100, total_tokens=8100),
                    response_id=None,
                )

            def stream_response(self, *args, **kwargs):
                raise AssertionError("Streaming must not be used")

        self.runtime = OpenAIAgentsRuntime(
            model="deepseek-flash", api_key="test-only", base_url="http://localhost:9999/v1",
            endpoint_type="custom_responses", provider="deepseek",
            domain_tools_factory=lambda request: object(), session_provider=lambda sid: self.guarded,
            research_model=Model(),
            context_budget=ContextBudget(capacity=65536, max_output_tokens=1024, summary_max_output_tokens=4096),
        )
        self.addAsyncCleanup(self.runtime.close)
        self.summary = "保留用户学习目标和未解决的问题。" * 120
        self.summary_call = AsyncMock(return_value=SimpleNamespace(
            usage=SimpleNamespace(input_tokens=25000, output_tokens=1500, total_tokens=26500),
            status="completed", output_text=self.summary,
        ))
        self.summary_patch = patch.object(self.runtime._client.responses, "create", self.summary_call)
        self.summary_patch.start()
        self.addCleanup(self.summary_patch.stop)

    def guard(self):
        return GenerationGuardedSession(self.backing, self.store, SessionMutationCoordinator(), expected_generation=0)

    async def commit(self):
        await self.guarded.commit(on_committed=lambda: self.store.update_conversation_summary(
            self.state.session_id, expected_generation=0, **self.guarded.pending_context,
            references=[], reference_validator=lambda ref: ref,
        ))

    async def test_real_sdk_compacts_persists_and_restores_without_duplicate_summary(self):
        history = turns(20)
        await self.backing.add_items(history)
        result = await self.runtime.run(_request(self.state.session_id).model_copy(update={"timeout_seconds": 10}))
        self.assertGreater(self.summary_call.await_count, 0)
        self.assertEqual(8000, result.usage["context_last_input_tokens"])
        self.assertGreater(result.usage["input_tokens"], 8000)
        self.assertEqual(8000, result.usage["context_peak_input_tokens"])
        AgentUsageSummary.model_validate(result.usage)
        self.assertGreater(len(self.summary), 1500)
        await self.commit()
        checkpoint = self.store.get(self.state.session_id)
        self.assertEqual(self.summary, checkpoint.conversation_summary)
        self.assertGreater(checkpoint.conversation_summary_covered_items, 0)
        raw = await self.backing.get_items()
        self.assertEqual(history, raw[:len(history)])
        self.assertNotIn(self.summary, str(raw[len(history):]))
        for call in self.summary_call.await_args_list:
            self.assertEqual([], call.kwargs["tools"])
            self.assertEqual("deepseek-flash", call.kwargs["model"])
            self.assertFalse(call.kwargs["store"])
        self.guarded = self.guard()
        self.summary_call.reset_mock()
        await self.runtime.run(_request(self.state.session_id))
        self.summary_call.assert_not_awaited()
        inputs = self.inputs[-1]["input"]
        self.assertEqual(1, sum(self.summary in str(item) for item in inputs))
        self.assertNotIn("conversation_summary", inputs[-2]["content"])
        await self.commit()

    async def test_incomplete_summary_blocks_oversized_input_without_commit(self):
        history = turns(30)
        await self.backing.add_items(history)
        self.summary_call.return_value.status = "incomplete"
        request = _request(self.state.session_id)
        with self.assertRaises(AgentRuntimeFailure) as raised:
            await self.runtime.run(request)
        self.assertEqual("agent_context_budget_exceeded", raised.exception.error.code)
        self.assertEqual([], self.inputs)
        self.assertIsNone(self.guarded.pending_context)
        self.assertEqual(history, await self.backing.get_items())
        self.assertEqual(0, self.store.get(self.state.session_id).conversation_summary_covered_items)
        telemetry = self.runtime.take_telemetry(request.run_id)
        self.assertEqual(25000, telemetry.usage["summary_input_tokens"])
        self.assertNotIn("context_last_input_tokens", telemetry.usage)

    async def test_small_history_uses_full_sdk_input_without_summary_request(self):
        history = turns(6, 20)
        await self.backing.add_items(history)
        await self.runtime.run(_request(self.state.session_id))
        self.summary_call.assert_not_awaited()
        self.assertEqual(history, self.inputs[-1]["input"][:len(history)])
        self.assertEqual(1024, self.inputs[-1]["model_settings"].max_tokens)

    async def test_context_switch_during_summary_prevents_next_model_call(self):
        history = turns(20)
        await self.backing.add_items(history)

        async def change_context(**kwargs):
            self.store.update_context(self.state.session_id, AgentSessionContextRequest(
                expected_generation=0, activity="game_review",
            ))
            return self.summary_call.return_value

        self.summary_call.side_effect = change_context
        with self.assertRaises(StaleAgentContextError):
            await self.runtime.run(_request(self.state.session_id))
        self.assertEqual([], self.inputs)
        self.assertIsNone(self.guarded.pending_context)
        self.assertEqual(history, await self.backing.get_items())
        self.assertEqual("", self.store.get(self.state.session_id).conversation_summary)

    async def test_cancelled_summary_keeps_checkpoint_and_raw_history(self):
        history = turns(20)
        await self.backing.add_items(history)
        self.summary_call.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.runtime.run(_request(self.state.session_id))
        self.guarded.discard()
        self.assertEqual([], self.inputs)
        self.assertIsNone(self.guarded.pending_context)
        self.assertEqual(history, await self.backing.get_items())
        self.assertEqual(0, self.store.get(self.state.session_id).conversation_summary_covered_items)
