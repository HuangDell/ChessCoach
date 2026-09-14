"""Regression coverage for tool execution, presentation, and audit boundaries."""
from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from server.core.agent.models import AGENT_TOOL_PERMISSIONS, AgentResponse, GetReviewContextInput, LookupOpeningInput, ToolError, ToolResult
from server.core.agent.runtime import AgentRuntimeFailure
from server.core.agent.runtime_openai import OpenAIAgentsRuntime, _LocalRunContext
from server.core.agent.runtime_openai_tools import OpenAIToolAdapter
from server.core.agent.sessions import InMemoryConversationSessionFactory, SessionStoreError, StaleAgentContextError
from tests.backend.test_agent_runtime_openai import _request, START_FEN, _FakeAgents


class ToolFailureBoundaryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.result = ToolResult(ok=True, data={"opening": "test"})
        self.execution = SimpleNamespace(result=self.result, cache_hit=True, engine_calls=0)
        self.tools = SimpleNamespace(
            estimated_engine_calls=lambda name, payload: 1,
            execute=AsyncMock(return_value=self.execution),
        )
        self.adapter = OpenAIToolAdapter(
            request=_request("test-session"), tools=self.tools,
            agents=_FakeAgents, schema_adapter="openai", debug_event=Mock(),
        )
        self.payload = LookupOpeningInput(fen=START_FEN)

    async def call(self, name="lookup_opening", payload=None):
        return await self.adapter.call(name, payload or self.payload)

    def assert_internal_failure(self, raised, engine_calls=0):
        self.assertEqual("agent_runtime_error", raised.exception.error.code)
        self.assertEqual("tool_execution", raised.exception.error.failure_stage)
        self.assertFalse(raised.exception.error.recoverable)
        self.assertNotIn("private-data", str(raised.exception))
        self.assertEqual(1, len(self.adapter.budget.records))
        self.assertEqual("tool_execution_failed", self.adapter.budget.records[0].error_code)
        self.assertEqual(engine_calls, self.adapter.budget.engine)

    async def test_unexpected_execution_failure_aborts_and_keeps_reservation(self):
        self.tools.execute.side_effect = RuntimeError("private-data")
        with self.assertRaises(AgentRuntimeFailure) as raised:
            await self.call()
        self.assert_internal_failure(raised, engine_calls=1)

    async def test_projection_failure_aborts_and_refunds_cache_reservation(self):
        self.execution.result = Mock(ok=True, data=SimpleNamespace(facts={}))
        self.execution.result.model_dump.return_value = {"ok": True, "data": {"facts": {}}}
        payload = GetReviewContextInput(game_id="game", review_side="white", critical_id="c1")
        with patch("server.core.agent.runtime_openai_tools.project_facts", side_effect=ValueError("private-data")) as projection:
            with self.assertRaises(AgentRuntimeFailure) as raised:
                await self.call("get_review_context", payload)
        projection.assert_called_once()
        self.assert_internal_failure(raised)

    async def test_serialization_failure_aborts_without_success_record(self):
        self.execution.result = Mock(ok=True, evidence_refs=[], error=None)
        self.execution.result.model_dump.return_value = {"ok": True, "data": object()}
        with self.assertRaises(AgentRuntimeFailure) as raised:
            await self.call()
        self.assert_internal_failure(raised)

    async def test_audit_failure_does_not_duplicate_record_or_audit(self):
        orchestration = SimpleNamespace(
            before_call=AsyncMock(return_value=("call-1", None)),
            after_call=Mock(side_effect=RuntimeError("private-data")),
        )
        self.adapter.orchestration = orchestration
        with self.assertRaises(AgentRuntimeFailure):
            await self.call()
        orchestration.after_call.assert_called_once()
        self.assertEqual(1, len(self.adapter.budget.records))
        self.assertEqual("ok", self.adapter.budget.records[0].status)
        self.assertEqual(0, self.adapter.budget.engine)

    async def test_expected_tool_failure_remains_model_visible(self):
        self.execution.result = ToolResult(ok=False, error=ToolError(
            code="engine_unavailable", message="Engine unavailable", recoverable=True,
        ))
        result = json.loads(await self.call())
        self.assertEqual("engine_unavailable", result["error"]["code"])
        self.assertEqual(1, len(self.adapter.budget.records))

    async def test_session_control_errors_are_not_converted_to_tool_failures(self):
        for error in (SessionStoreError("private-data"), StaleAgentContextError("private-data")):
            with self.subTest(error=type(error).__name__):
                self.tools.execute.side_effect = error
                with self.assertRaises(type(error)):
                    await self.call()


@unittest.skipUnless(importlib.util.find_spec("agents"), "optional Agent SDK is not installed")
class SDKToolFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_wire_schemas_match_pre_split_sdk_contract_for_all_providers(self):
        # Captured from the pre-split runtime with SDK 0.22.0. These fingerprints
        # guard the externally visible request contract, including all eight tools.
        expected = {
            "get_review_context": "f194e78e38a2d326733649c61e09da6692bf39e6bd2feb2a333721bd73bad2c4",
            "analyze_position": "d224f0c3154a5a9a7b7330b9d5478b5d8e82e1654c90151d7f7e11d0e1d2ab7d",
            "analyze_move": "9ffe287578208ba6f1d97e615e988f8162536a6e64c5868d24561440c6b622ce",
            "lookup_opening": "5e515f29f28f79b4a22d1768ef0b6c9b31d5786ebb69d89f2843327b621909d2",
            "search_coaching_knowledge": "35258e60760c6bf9cb43c207067b6dd1591095d0c9d616951a92e40941d7ad6c",
            "get_player_profile": "3db1e9595c18d4b91507ce649a7300c4dcd529062711c0e1b0501e0121ae544c",
            "get_training_candidates": "aa9e8a69df1d01bca9b3c792c53686204f362eee24502bb0df9726687a0444c6",
            "create_training_draft": "140b69a8bba24241f54db1afef4dd02878472210ee4a2a169b859702ef668ed6",
        }
        runtime, request, _ = self.runtime(object())
        request = request.model_copy(update={"allowed_tools": list(AGENT_TOOL_PERMISSIONS)})
        for provider in ("openai", "generic", "deepseek"):
            with self.subTest(provider=provider):
                runtime.schema_adapter = provider
                adapter = OpenAIToolAdapter(
                    request=request, tools=object(), agents=runtime._agents,
                    schema_adapter=provider, debug_event=Mock(),
                )
                schemas = {tool.name: tool.params_json_schema for tool in adapter.build_sdk_tools()}
                schemas["response"] = runtime._output_schema(
                    _LocalRunContext(request=request, tool_adapter=adapter),
                ).json_schema()
                response_hash = (
                    "cd414c104e6d601a7fcc0947a548bc0c4cd2a1d98f7ed74bfb3ba2ccfe6bb88b"
                    if provider == "deepseek" else
                    "383185e6531ca388c00c5024e92efe313c0d240abb29b2ed4c8e112f6a75e613"
                )
                self.assertEqual({**expected, "response": response_hash}, {
                    name: hashlib.sha256(json.dumps(schema, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
                    for name, schema in schemas.items()
                })

    def runtime(self, tools, *, invalid_arguments=False):
        from agents.testing import ScriptedModel, assistant_message, function_call

        call = function_call("lookup_opening", {"fen": START_FEN, "recent_moves_uci": None}, call_id="call-1")
        if invalid_arguments:
            call.arguments = "{invalid-json"
        model = ScriptedModel([
            [call], [assistant_message(AgentResponse(text="Focus on development.").model_dump_json())],
        ])
        sessions = InMemoryConversationSessionFactory()
        self.addCleanup(sessions.close)
        runtime = OpenAIAgentsRuntime(
            model="test-model", api_key="test-only", base_url="https://api.openai.com/v1",
            endpoint_type="openai_responses", domain_tools_factory=lambda request: tools,
            session_provider=sessions.get_session, research_model=model, debug=True,
        )
        self.addAsyncCleanup(runtime.close)
        request = _request("sdk-tools").model_copy(update={"allowed_tools": ["lookup_opening"]})
        return runtime, request, model

    async def test_internal_error_crosses_real_sdk_and_stops_before_second_model_call(self):
        tools = SimpleNamespace(execute=AsyncMock(side_effect=RuntimeError("private-data")))
        runtime, request, model = self.runtime(tools)
        with self.assertLogs("chesscoach.agent", level="DEBUG") as logs:
            with self.assertRaises(AgentRuntimeFailure) as raised:
                await runtime.run(request)
        self.assertEqual("agent_runtime_error", raised.exception.error.code)
        self.assertEqual(1, len(model.calls))
        telemetry = runtime.take_telemetry(request.run_id)
        self.assertEqual("tool_execution", telemetry.failure_stage)
        self.assertEqual(1, len(telemetry.tool_calls))
        self.assertEqual("tool_execution_failed", telemetry.tool_calls[0].error_code)
        self.assertIn("phase=execution", "\n".join(logs.output))
        self.assertNotIn("private-data", "\n".join(logs.output))

    async def test_session_and_cancellation_signals_cross_real_sdk(self):
        for error in (SessionStoreError("private-data"), StaleAgentContextError("private-data"), asyncio.CancelledError()):
            with self.subTest(error=type(error).__name__):
                runtime, request, model = self.runtime(SimpleNamespace(execute=AsyncMock(side_effect=error)))
                with self.assertRaises(type(error)):
                    await runtime.run(request)
                self.assertEqual(1, len(model.calls))

    async def test_expected_failure_remains_a_tool_result_for_real_sdk(self):
        execution = SimpleNamespace(result=ToolResult(ok=False, error=ToolError(
            code="engine_unavailable", message="Engine unavailable", recoverable=True,
        )), cache_hit=False, engine_calls=0)
        runtime, request, model = self.runtime(SimpleNamespace(execute=AsyncMock(return_value=execution)))
        result = await runtime.run(request)
        self.assertEqual(2, len(model.calls))
        self.assertEqual("engine_unavailable", result.tool_calls[0].error_code)

    async def test_sdk_argument_parsing_error_keeps_existing_feedback(self):
        tools = SimpleNamespace(execute=AsyncMock())
        runtime, request, model = self.runtime(tools, invalid_arguments=True)
        result = await runtime.run(request)
        tools.execute.assert_not_called()
        self.assertEqual(2, len(model.calls))
        self.assertEqual([], result.tool_calls)
