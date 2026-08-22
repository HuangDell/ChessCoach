from __future__ import annotations

import asyncio
from dataclasses import dataclass
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from server.core.agent.models import (
    AgentResponse,
    AgentRunRequest,
    AnalyzeMoveInput,
    GetPlayerProfileResult,
    LookupOpeningResult,
    ModelVisibleContext,
    PositionContext,
    TaskContext,
    ToolResult,
)
from server.core.agent.runtime import AgentRuntimeFailure, UnavailableAgentRuntime
from server.core.agent.runtime_openai import (
    OpenAIAgentsRuntime,
    SQLiteConversationSessionFactory,
    _LocalRunContext,
    _ToolBudget,
    create_openai_runtime,
)
from server.core.agent.sessions import (
    ChessSessionCheckpointStore,
    GenerationGuardedSession,
    InMemoryConversationSessionFactory,
    SessionMutationCoordinator,
)
from server.core.agent.models import AgentSessionCreateRequest


START_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"


def _context() -> ModelVisibleContext:
    return ModelVisibleContext(
        task=TaskContext(activity="position_analysis"),
        position=PositionContext(
            fen=START_FEN,
            recent_moves_uci=[],
            recent_moves_san=[],
        ),
        engine_facts=None,
        relevant_profile=None,
        relevant_memory=[],
        conversation_summary="",
        allowed_evidence_refs=[],
    )


def _request(session_id: str) -> AgentRunRequest:
    return AgentRunRequest(
        session_id=session_id,
        expected_generation=0,
        message="Explain the position conceptually.",
        model_context=_context(),
        allowed_tools=[],
        max_turns=4,
        max_total_tool_calls=6,
        max_engine_tool_calls=2,
        timeout_seconds=2,
    )


class _AuthenticationError(Exception):
    pass


class _RateLimitError(Exception):
    pass


class _APITimeoutError(Exception):
    pass


class _AsyncClient:
    created: list[dict] = []

    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.closed = False
        self.created.append(kwargs)

    async def close(self) -> None:
        self.closed = True


class _FakeOpenAI:
    AsyncOpenAI = _AsyncClient
    AuthenticationError = _AuthenticationError
    RateLimitError = _RateLimitError
    APITimeoutError = _APITimeoutError


@dataclass
class _Usage:
    requests: int = 1
    input_tokens: int = 12
    output_tokens: int = 8
    total_tokens: int = 20


class _RunResult:
    final_output = AgentResponse(text="Use development before tactics.")
    context_wrapper = type("Context", (), {"usage": _Usage()})()


class _FakeAgents:
    last_agent = None
    last_run = None
    runner_error: BaseException | None = None

    class MaxTurnsExceeded(Exception):
        pass

    class ModelBehaviorError(Exception):
        pass

    class ModelRefusalError(Exception):
        pass

    class SessionSettings:
        def __init__(self, *, limit) -> None:
            self.limit = limit

    class ToolExecutionConfig:
        def __init__(self, *, max_function_tool_concurrency) -> None:
            self.max_function_tool_concurrency = max_function_tool_concurrency

    class OpenAIProvider:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs

    class Agent:
        def __init__(self, **kwargs) -> None:
            self.__dict__.update(kwargs)
            _FakeAgents.last_agent = self

    class RunConfig:
        def __init__(self, **kwargs) -> None:
            self.__dict__.update(kwargs)

    class Runner:
        @staticmethod
        async def run(agent, input_value, **kwargs):
            _FakeAgents.last_run = (agent, input_value, kwargs)
            if _FakeAgents.runner_error is not None:
                raise _FakeAgents.runner_error
            await kwargs["session"].add_items(
                [
                    {"role": "user", "content": input_value},
                    {"role": "assistant", "content": _RunResult.final_output.text},
                ]
            )
            return _RunResult()

    @staticmethod
    def function_tool(function, **_kwargs):
        return function


class RuntimeOpenAITests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        _AsyncClient.created.clear()
        _FakeAgents.last_agent = None
        _FakeAgents.last_run = None
        _FakeAgents.runner_error = None

    @staticmethod
    def _imports(name: str):
        if name == "agents":
            return _FakeAgents
        if name == "openai":
            return _FakeOpenAI
        raise ModuleNotFoundError(name)

    def test_missing_credential_and_sdk_fail_closed(self) -> None:
        missing_key = create_openai_runtime(
            enabled=True,
            model="gpt-test",
            base_url="",
            custom_api_key="",
            openai_api_key="",
            domain_tools_factory=lambda _request: object(),
            session_provider=lambda _session_id: object(),
        )
        self.assertIsInstance(missing_key, UnavailableAgentRuntime)
        self.assertIn("OPENAI_API_KEY", missing_key.availability.reason or "")

        with patch(
            "server.core.agent.runtime_openai.importlib.import_module",
            side_effect=ModuleNotFoundError("agents"),
        ):
            missing_sdk = create_openai_runtime(
                enabled=True,
                model="gpt-test",
                base_url="",
                custom_api_key="",
                openai_api_key="secret",
                domain_tools_factory=lambda _request: object(),
                session_provider=lambda _session_id: object(),
            )
        self.assertIsInstance(missing_sdk, UnavailableAgentRuntime)
        self.assertIn("optional dependency", missing_sdk.availability.reason or "")

    async def test_exact_sdk_contract_uses_responses_and_staged_session(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory(prefix="agent-runtime-") as data_dir:
            store = ChessSessionCheckpointStore(data_dir)
            state = store.create(AgentSessionCreateRequest())
            coordinator = SessionMutationCoordinator()
            backing = InMemoryConversationSessionFactory()
            guarded = GenerationGuardedSession(
                backing.get_session(state.session_id),
                store,
                coordinator,
                expected_generation=0,
            )
            with patch(
                "server.core.agent.runtime_openai.importlib.import_module",
                side_effect=self._imports,
            ):
                runtime = OpenAIAgentsRuntime(
                    model="gpt-test",
                    api_key="secret",
                    base_url="https://api.openai.com/v1",
                    endpoint_type="openai_responses",
                    domain_tools_factory=lambda _request: object(),
                    session_provider=lambda _session_id: guarded,
                )
                result = await runtime.run(_request(state.session_id))
                await runtime.close()

            self.assertEqual("Use development before tactics.", result.response.text)
            self.assertEqual(20, result.usage["total_tokens"])
            self.assertEqual(
                {"api_key": "secret", "base_url": "https://api.openai.com/v1"},
                _AsyncClient.created[0],
            )
            provider = _FakeAgents.last_run[2]["run_config"].model_provider
            self.assertTrue(provider.kwargs["use_responses"])
            self.assertTrue(provider.kwargs["strict_feature_validation"])
            run_config = _FakeAgents.last_run[2]["run_config"]
            self.assertTrue(run_config.tracing_disabled)
            self.assertFalse(run_config.trace_include_sensitive_data)
            self.assertEqual(12, run_config.session_settings.limit)
            self.assertEqual(1, run_config.tool_execution.max_function_tool_concurrency)
            self.assertEqual([], await backing.get_session(state.session_id).get_items())
            self.assertEqual(2, len(guarded.staged_items))

    async def test_provider_timeout_and_incompatible_endpoint_map_to_typed_errors(self) -> None:
        with patch(
            "server.core.agent.runtime_openai.importlib.import_module",
            side_effect=self._imports,
        ):
            runtime = OpenAIAgentsRuntime(
                model="gpt-test",
                api_key="secret",
                base_url="http://localhost:9999/v1",
                endpoint_type="custom_responses",
                domain_tools_factory=lambda _request: object(),
                session_provider=lambda _session_id: object(),
            )

            for failure, expected_code in (
                (_APITimeoutError(), "agent_timeout"),
                (_FakeAgents.MaxTurnsExceeded(), "max_turns_exceeded"),
                (TypeError("responses unsupported"), "agent_provider_error"),
            ):
                with self.subTest(expected_code=expected_code):
                    _FakeAgents.runner_error = failure
                    with self.assertRaises(AgentRuntimeFailure) as raised:
                        await runtime.run(_request("session-1"))
                    self.assertEqual(expected_code, raised.exception.error.code)
            await runtime.close()

    async def test_engine_budget_is_reserved_before_multi_analysis_move(self) -> None:
        class Tools:
            called = False

            @staticmethod
            def estimated_engine_calls(_name, _payload) -> int:
                return 2

            async def execute(self, _name, _payload):
                self.called = True
                raise AssertionError("Engine-backed tool must not start over budget")

        tools = Tools()
        request = _request("session-1").model_copy(
            update={"allowed_tools": ["analyze_move"], "max_engine_tool_calls": 1}
        )
        local = _LocalRunContext(
            request=request,
            tools=tools,
            budget=_ToolBudget(max_total=6, max_engine=1),
        )
        with patch(
            "server.core.agent.runtime_openai.importlib.import_module",
            side_effect=self._imports,
        ):
            runtime = OpenAIAgentsRuntime(
                model="gpt-test",
                api_key="secret",
                base_url="https://api.openai.com/v1",
                endpoint_type="openai_responses",
                domain_tools_factory=lambda _request: tools,
                session_provider=lambda _session_id: object(),
            )
            payload = AnalyzeMoveInput(fen_before=START_FEN, move_uci="e2e4")
            result = json.loads(await runtime._call_tool(local, "analyze_move", payload))
            await runtime.close()

        self.assertFalse(tools.called)
        self.assertEqual("tool_budget_exceeded", result["error"]["code"])
        self.assertEqual("budget_exceeded", local.budget.records[0].status)

    async def test_invalid_tool_arguments_consume_total_budget_and_are_audited(self) -> None:
        request = _request("session-1").model_copy(
            update={"allowed_tools": ["analyze_position"], "max_total_tool_calls": 1}
        )
        local = _LocalRunContext(
            request=request,
            tools=object(),
            budget=_ToolBudget(max_total=1, max_engine=2),
        )
        with patch(
            "server.core.agent.runtime_openai.importlib.import_module",
            side_effect=self._imports,
        ):
            runtime = OpenAIAgentsRuntime(
                model="gpt-test",
                api_key="secret",
                base_url="https://api.openai.com/v1",
                endpoint_type="openai_responses",
                domain_tools_factory=lambda _request: object(),
                session_provider=lambda _session_id: object(),
            )
            analyze_position = runtime._sdk_tools(local)[0]
            first = json.loads(await analyze_position("not-a-fen", "compare_candidates"))
            second = json.loads(await analyze_position("still-not-a-fen", "compare_candidates"))
            await runtime.close()

        self.assertEqual("invalid_fen", first["error"]["code"])
        self.assertEqual("tool_budget_exceeded", second["error"]["code"])
        self.assertEqual(
            ["error", "budget_exceeded"],
            [item.status for item in local.budget.records],
        )

    async def test_phase2_read_tools_are_registered_gated_and_audited(self) -> None:
        class Tools:
            async def lookup_opening(self, _payload):
                return ToolResult[LookupOpeningResult](
                    ok=True,
                    data=LookupOpeningResult(
                        eco="A00",
                        name="Starting Position",
                        classification="recognized",
                    ),
                    evidence_refs=["opening:a00"],
                )

            async def get_player_profile(self, _payload):
                return ToolResult[GetPlayerProfileResult](
                    ok=True,
                    data=GetPlayerProfileResult(analyzed_games=0),
                )

        context = _context()
        context = context.model_copy(
            update={
                "task": context.task.model_copy(
                    update={"personalization_enabled": True}
                )
            }
        )
        request = _request("session-1").model_copy(
            update={
                "model_context": context,
                "allowed_tools": ["lookup_opening", "get_player_profile"],
            }
        )
        local = _LocalRunContext(
            request=request,
            tools=Tools(),
            budget=_ToolBudget(max_total=6, max_engine=2),
        )
        with patch(
            "server.core.agent.runtime_openai.importlib.import_module",
            side_effect=self._imports,
        ):
            runtime = OpenAIAgentsRuntime(
                model="gpt-test",
                api_key="secret",
                base_url="https://api.openai.com/v1",
                endpoint_type="openai_responses",
                domain_tools_factory=lambda _request: Tools(),
                session_provider=lambda _session_id: object(),
            )
            registered = {tool.__name__: tool for tool in runtime._sdk_tools(local)}
            opening = json.loads(await registered["lookup_opening"](START_FEN, []))
            profile = json.loads(await registered["get_player_profile"]([], [], 3))
            await runtime.close()

        self.assertEqual("A00", opening["data"]["eco"])
        self.assertEqual(0, profile["data"]["analyzed_games"])
        self.assertEqual(
            ["lookup_opening", "get_player_profile"],
            [record.name for record in local.budget.records],
        )
        self.assertTrue(all(record.permission == "read" for record in local.budget.records))


@unittest.skipUnless(importlib.util.find_spec("agents"), "optional Agent SDK is not installed")
class LockedSDKIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def test_agent_response_is_accepted_as_strict_sdk_output(self) -> None:
        import agents

        output = agents.AgentOutputSchema(AgentResponse, strict_json_schema=True)
        self.assertTrue(output.is_strict_json_schema())
        self.assertFalse(output.json_schema()["additionalProperties"])

    async def test_function_tool_schema_restricts_position_purpose(self) -> None:
        runtime = OpenAIAgentsRuntime(
            model="gpt-test",
            api_key="test",
            base_url="https://api.openai.com/v1",
            endpoint_type="openai_responses",
            domain_tools_factory=lambda _request: object(),
            session_provider=lambda _session_id: object(),
        )
        request = _request("session-1").model_copy(
            update={"allowed_tools": ["analyze_position"]}
        )
        local = _LocalRunContext(
            request=request,
            tools=object(),
            budget=_ToolBudget(max_total=6, max_engine=2),
        )

        tool = runtime._sdk_tools(local)[0]

        self.assertEqual(
            {"compare_candidates", "find_best_move", "explain_position"},
            set(tool.params_json_schema["properties"]["purpose"]["enum"]),
        )
        await runtime.close()

    async def test_runtime_builds_locked_sdk_agent_and_run_config(self) -> None:
        runtime = OpenAIAgentsRuntime(
            model="gpt-test",
            api_key="test",
            base_url="https://api.openai.com/v1",
            endpoint_type="openai_responses",
            domain_tools_factory=lambda _request: object(),
            session_provider=lambda _session_id: object(),
        )
        runner = AsyncMock(return_value=_RunResult())
        with patch.object(runtime._agents.Runner, "run", new=runner):
            result = await runtime.run(_request("session-1"))

        self.assertEqual("Use development before tactics.", result.response.text)
        kwargs = runner.await_args.kwargs
        self.assertTrue(kwargs["run_config"].tracing_disabled)
        self.assertEqual(12, kwargs["run_config"].session_settings.limit)
        await runtime.close()

    async def test_sdk_sqlite_session_round_trip_and_factory_cleanup(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-sdk-session-") as data_dir:
            factory = SQLiteConversationSessionFactory(data_dir)
            session_id = "c" * 32
            session = factory.get_session(session_id)
            await session.add_items(
                [{"role": "user", "content": f"item {index}"} for index in range(20)]
            )
            self.assertEqual(20, len(await session.get_items()))
            self.assertIsNone(session.session_settings.limit)
            self.assertEqual(12, len(await session.get_items(limit=12)))

            await factory.clear_session(session_id)

            self.assertEqual({}, factory._sessions)
            self.assertTrue((Path(data_dir) / "agent" / "conversations.sqlite3").is_file())
            factory.close()


if __name__ == "__main__":
    unittest.main()
