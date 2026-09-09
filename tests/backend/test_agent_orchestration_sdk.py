from __future__ import annotations

import asyncio
import importlib.util
import unittest

from server.core.agent.models import AgentResponse, AgentRunRequest
from server.core.agent.routing import OrchestrationController, SequencedCandidateProvider
from server.core.agent.runtime import AgentRuntimeFailure
from server.core.agent.runtime_openai import OpenAIAgentsRuntime
from server.core.agent.sessions import InMemoryConversationSessionFactory
from tests.evals.orchestration.contracts import load_scripts
from tests.evals.orchestration.fixtures import FixtureExecutor
from tests.evals.orchestration.runner import (
    _payload,
    _request,
    _task_variant,
    run_sdk_fake_variant,
)


@unittest.skipUnless(importlib.util.find_spec("agents"), "optional Agent SDK is not installed")
class OrchestrationSDKTests(unittest.IsolatedAsyncioTestCase):
    def _runtime(
        self,
        variant_id: str,
        model,
        decisions: list[list[str]],
    ):
        task, variant = _task_variant(variant_id)
        executor = FixtureExecutor(variant.fixture_ids)
        sessions = InMemoryConversationSessionFactory()
        controllers: dict[str, OrchestrationController] = {}

        def orchestration_factory(request: AgentRunRequest) -> OrchestrationController:
            controller = OrchestrationController(
                request,
                SequencedCandidateProvider(decisions),
                allow_fallback=False,
                missing_artifacts=task.checkpoint.missing_artifacts,
            )
            controllers[request.run_id] = controller
            return controller

        runtime = OpenAIAgentsRuntime(
            model="p0-scripted-model",
            api_key="p0-no-network",
            base_url="https://api.openai.com/v1",
            endpoint_type="openai_responses",
            domain_tools_factory=lambda _request: executor,
            session_provider=lambda session_id: sessions.get_session(session_id),
            orchestration_factory=orchestration_factory,
            research_model=model,
        )
        return task, variant, executor, sessions, controllers, runtime

    async def test_real_runner_refreshes_schemas_and_keeps_old_history(self) -> None:
        trace = await run_sdk_fake_variant("S01-main")

        requests = trace["sdk_model_requests"]
        self.assertEqual(
            [
                ["get_player_profile"],
                ["get_training_candidates"],
                ["create_training_draft"],
                [],
            ],
            [item["names"] for item in requests],
        )
        self.assertEqual(
            "3db1e9595c18d4b91507ce649a7300c4dcd529062711c0e1b0501e0121ae544c",
            requests[0]["schema_sha256"]["get_player_profile"],
        )
        second_history = requests[1]["history"]
        self.assertIn(
            {
                "type": "function_call",
                "name": "get_player_profile",
                "call_id": "fixture-call-1",
            },
            second_history,
        )
        self.assertIn(
            {"type": "function_call_output", "call_id": "fixture-call-1"},
            second_history,
        )
        self.assertEqual(4, trace["candidate_computations"])
        self.assertEqual(
            [
                "get_player_profile",
                "get_training_candidates",
                "create_training_draft",
            ],
            trace["snapshots"][-1]["cumulative_exposed_tools"],
        )
        self.assertEqual("succeeded", trace["state"]["terminal_status"])
        self.assertEqual("accepted", trace["production_validation"])

    async def test_old_hidden_tool_proposal_is_intercepted_before_sdk_rejects_it(self) -> None:
        from agents.testing import ScriptedModel, function_call

        task, variant = _task_variant("S01-main")
        action = load_scripts().scripts[variant.script_id].actions[0]
        name, payload, _ = _payload(action)
        model = ScriptedModel(
            [
                [function_call(name, payload.model_dump(mode="json"), call_id="valid-1")],
                [function_call(name, payload.model_dump(mode="json"), call_id="old-2")],
            ]
        )
        task, variant, executor, sessions, controllers, runtime = self._runtime(
            "S01-main", model, [["get_player_profile"], ["get_training_candidates"]]
        )
        request = _request(task, variant)
        try:
            with self.assertRaises(AgentRuntimeFailure) as raised:
                await runtime.run(request)
        finally:
            await runtime.close()
            sessions.close()

        trace = runtime.take_orchestration_trace(request.run_id)
        self.assertEqual("invalid_agent_response", raised.exception.error.code)
        self.assertEqual(1, len(executor.executions))
        self.assertEqual(
            [["get_player_profile"], ["get_training_candidates"]],
            [[tool.name for tool in call.tools] for call in model.calls],
        )
        intercepted = [
            item for item in trace["state"]["attempts"] if item["status"] == "intercepted"
        ]
        self.assertEqual(1, len(intercepted))
        self.assertEqual("get_player_profile", intercepted[0]["tool"])
        self.assertEqual("decision-2", intercepted[0]["snapshot_id"])
        self.assertEqual("outside_snapshot", intercepted[0]["error_category"])
        self.assertEqual("failed", trace["state"]["terminal_status"])
        self.assertEqual("ModelBehaviorError", trace["state"]["terminal_reason"])

    async def test_production_validation_rejects_fabricated_evidence(self) -> None:
        from agents.testing import ScriptedModel, assistant_message

        response = AgentResponse(
            text="This claim cites evidence that was never provided.",
            evidence_refs=["fabricated:evidence"],
        )
        model = ScriptedModel([[assistant_message(response.model_dump_json())]])
        task, variant, _executor, sessions, _controllers, runtime = self._runtime(
            "N01-main", model, []
        )
        request = _request(task, variant)
        try:
            with self.assertRaises(AgentRuntimeFailure) as raised:
                await runtime.run(request)
        finally:
            await runtime.close()
            sessions.close()

        trace = runtime.take_orchestration_trace(request.run_id)
        self.assertEqual("invalid_agent_response", raised.exception.error.code)
        self.assertEqual("failed", trace["state"]["terminal_status"])
        self.assertEqual("production_validation", trace["state"]["terminal_reason"])

    async def test_sdk_cancellation_leaves_no_success_observation(self) -> None:
        from agents.testing import ScriptedModel, function_call

        task, variant = _task_variant("X02-main")
        action = load_scripts().scripts[variant.script_id].actions[0]
        name, payload, _ = _payload(action)
        model = ScriptedModel(
            [[function_call(name, payload.model_dump(mode="json"), call_id="cancel-1")]]
        )
        task, variant, executor, sessions, _controllers, runtime = self._runtime(
            "X02-main", model, [["analyze_position"]]
        )
        request = _request(task, variant)
        started = asyncio.Event()
        never_finishes = asyncio.Event()

        async def blocking_execute(tool_name, tool_payload):
            del tool_name, tool_payload
            started.set()
            await never_finishes.wait()

        executor.execute = blocking_execute
        try:
            run_task = asyncio.create_task(runtime.run(request))
            await asyncio.wait_for(started.wait(), timeout=2)
            run_task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await run_task
        finally:
            await runtime.close()
            sessions.close()

        trace = runtime.take_orchestration_trace(request.run_id)
        self.assertEqual("cancelled", trace["state"]["terminal_status"])
        self.assertEqual([], trace["state"]["observations"])
