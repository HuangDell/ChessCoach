"""Optional OpenAI Agents SDK adapter for the Chess Coach Agent runtime."""
from __future__ import annotations

import asyncio
import hashlib
import importlib
import json
import logging
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from pydantic import ValidationError

from server.core.model_usage import normalize_usage

from server import config
from server.config import resolve_agent_provider
from server.core.agent.context_budget import ContextBudget, ContextBudgetExceeded, RunContextWindow
from server.core.agent.summary import ConversationSummaryBuilder, SUMMARY_INSTRUCTIONS
from server.core.agent.sessions import SessionStoreError, StaleAgentContextError
from server.core.agent.schema_adapter import adapt_schema
from server.core.agent.models import (
    AGENT_TOOL_PERMISSIONS,
    AgentError,
    AgentFailureStage,
    AgentResponse,
    AgentRunRequest,
    AgentRunResult,
    AgentValidationIssue,
)
from server.core.agent.policy import (
    AgentResponseValidationError,
    build_agent_instructions,
    build_model_input,
    validate_agent_run_result,
)
from server.core.agent.routing import OrchestrationController
from server.core.agent.runtime_openai_session import SQLiteConversationSessionFactory
from server.core.agent.runtime_openai_tools import OpenAIToolAdapter
from server.core.agent.runtime import (
    AgentRuntimeAvailability,
    AgentRuntimeFailure,
    AgentRuntimeTelemetry,
    UnavailableAgentRuntime,
)
from server.core.storage.agent_traces import RawHttpTraceStore


DomainToolsFactory = Callable[[AgentRunRequest], Any]
SessionProvider = Callable[[str], Any]
AGENTS_SDK_VERSION = "0.22.0"
logger = logging.getLogger("chesscoach.agent")


@dataclass
class _LocalRunContext:
    request: AgentRunRequest
    tool_adapter: OpenAIToolAdapter
    orchestration: OrchestrationController | None = None
    window: RunContextWindow | None = None
    usage: dict[str, int | float] = field(default_factory=dict)
    cache_details_complete: bool = True
    model_requests: int = 0
    failure_stage: AgentFailureStage | None = None
    validation_errors: list[AgentValidationIssue] = field(default_factory=list)


class OpenAIAgentsRuntime:
    """Single-Agent Responses runtime; SDK types stay inside the OpenAI adapters."""

    def __init__(
        self,
        *,
        model: str,
        api_key: str,
        base_url: str,
        endpoint_type: str,
        domain_tools_factory: DomainToolsFactory,
        session_provider: SessionProvider,
        provider: str = "",
        orchestration_factory: Callable[[AgentRunRequest], OrchestrationController] | None = None,
        research_model: Any | None = None,
        http_event_hooks: dict[str, list[Callable[..., Any]]] | None = None,
        raw_trace_store: RawHttpTraceStore | None = None,
        debug: bool = False,
        context_budget: ContextBudget | None = None,
        summary_builder: ConversationSummaryBuilder | None = None,
    ) -> None:
        agents = importlib.import_module("agents")
        openai = importlib.import_module("openai")
        self._debug = debug
        self._raw_trace_store = raw_trace_store
        self.schema_adapter = resolve_agent_provider(
            provider, base_url if endpoint_type == "custom_responses" else ""
        )
        self._agents = agents
        self._openai = openai
        self._model = model
        self._domain_tools_factory = domain_tools_factory
        self._session_provider = session_provider
        self._orchestration_factory = orchestration_factory
        self._research_model = research_model
        self.context_budget = context_budget or ContextBudget(
            capacity=config.AGENT_CONTEXT_TOKENS or (1_000_000 if model == "deepseek-flash" else 128_000),
            trigger_ratio=config.AGENT_CONTEXT_TRIGGER_RATIO,
            target_ratio=config.AGENT_CONTEXT_TARGET_RATIO,
            max_output_tokens=config.AGENT_MAX_OUTPUT_TOKENS,
            summary_max_output_tokens=config.AGENT_SUMMARY_MAX_OUTPUT_TOKENS,
        )
        self._summary_builder = summary_builder
        self._endpoint_fingerprint = hashlib.sha256(base_url.encode()).hexdigest()
        self.sdk_version = str(getattr(agents, "__version__", "unknown"))
        self._telemetry: dict[str, AgentRuntimeTelemetry] = {}
        self._orchestration_traces: dict[str, dict[str, Any]] = {}
        # Explicit values prevent the SDK from reading OPENAI_BASE_URL or another implicit endpoint.
        client_options: dict[str, Any] = {"api_key": api_key, "base_url": base_url}
        event_hooks = {
            name: list(callbacks) for name, callbacks in (http_event_hooks or {}).items()
        }
        if raw_trace_store is not None:
            hook_factory = getattr(raw_trace_store, "async_event_hooks", None)
            raw_trace_hooks = (
                hook_factory() if hook_factory is not None else raw_trace_store.event_hooks()
            )
            for name, callbacks in raw_trace_hooks.items():
                event_hooks.setdefault(name, []).extend(callbacks)
        if event_hooks:
            client_options["http_client"] = openai.DefaultAsyncHttpxClient(
                event_hooks=event_hooks
            )
        self._client = openai.AsyncOpenAI(**client_options)
        self._provider = agents.OpenAIProvider(
            openai_client=self._client,
            use_responses=True,
            strict_feature_validation=True,
        )
        self.availability = AgentRuntimeAvailability(
            enabled=True,
            available=True,
            model=model,
            endpoint_type=endpoint_type,
        )

    async def close(self) -> None:
        self._telemetry.clear()
        close = getattr(self._client, "close", None)
        if close is not None:
            result = close()
            if hasattr(result, "__await__"):
                await result

    def take_telemetry(self, run_id: str) -> AgentRuntimeTelemetry | None:
        return self._telemetry.pop(run_id, None)

    def take_orchestration_trace(self, run_id: str) -> dict[str, Any] | None:
        return self._orchestration_traces.pop(run_id, None)

    def _debug_event(self, run_id: str, event: str, **fields: Any) -> None:
        if not self._debug:
            return
        details = " ".join(
            f"{name}={value}" for name, value in fields.items() if value is not None
        )
        logger.debug("event=%s run=%s%s", event, run_id, f" {details}" if details else "")

    @staticmethod
    def _validation_issues(exc: ValidationError) -> list[AgentValidationIssue]:
        issues: list[AgentValidationIssue] = []
        for item in exc.errors(include_input=False, include_url=False)[:10]:
            location = item.get("loc", ())
            path = ".".join(str(part) for part in location) or "$"
            message = str(item.get("msg") or "Invalid value")[:240]
            issues.append(
                AgentValidationIssue(
                    path=path,
                    error_type=str(item.get("type") or "validation_error"),
                    message=message,
                )
            )
        return issues

    def _record_validation_failure(
        self,
        local: _LocalRunContext,
        exc: ValidationError,
    ) -> None:
        local.failure_stage = "structured_output"
        local.validation_errors = self._validation_issues(exc)
        summary = json.dumps(
            [issue.model_dump(mode="json") for issue in local.validation_errors],
            ensure_ascii=True,
            separators=(",", ":"),
        )
        self._debug_event(
            local.request.run_id,
            "structured_output_invalid",
            issues=summary,
        )

    @staticmethod
    def _record_research_model_response(local: _LocalRunContext, response: Any) -> None:
        orchestration = local.orchestration
        snapshot = orchestration.active_snapshot if orchestration is not None else None
        if orchestration is None or snapshot is None:
            return
        for output in getattr(response, "output", ()):
            if getattr(output, "type", None) != "function_call":
                continue
            name = getattr(output, "name", None)
            if name not in AGENT_TOOL_PERMISSIONS or name in snapshot.candidate_tools:
                continue
            raw_arguments = getattr(output, "arguments", "{}")
            try:
                arguments = (
                    json.loads(raw_arguments)
                    if isinstance(raw_arguments, str)
                    else raw_arguments
                )
            except (TypeError, ValueError):
                arguments = {"invalid": True}
            encoded = json.dumps(
                arguments, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
            orchestration.record_intercepted_proposal(
                name,
                hashlib.sha256(encoded).hexdigest(),
                snapshot.decision_id,
                error_category="outside_snapshot",
            )

    def _model_for_run(self, local: _LocalRunContext) -> Any:
        delegate = self._research_model or self._provider.get_model(self._model)
        runtime = self

        class _BudgetedModelProxy(self._agents.Model):
            async def get_response(self, **kwargs: Any) -> Any:
                window = local.window
                assert window is not None
                schema = kwargs.get("output_schema")
                fixed = {
                    "model": runtime._model,
                    "endpoint_fingerprint": runtime._endpoint_fingerprint,
                    "instructions": kwargs.get("system_instructions"),
                    "tools": [{"name": tool.name, "description": tool.description,
                               "parameters": tool.params_json_schema, "strict": tool.strict_json_schema}
                              for tool in kwargs.get("tools", [])],
                    "schema": schema.json_schema() if schema is not None else None,
                }
                builder = runtime._summary_builder or ConversationSummaryBuilder(
                    lambda payload: runtime._generate_summary(local, payload),
                    input_limit=runtime.context_budget.capacity - runtime.context_budget.summary_max_output_tokens - 1024,
                )
                items = kwargs["input"]
                if isinstance(items, str):
                    items = [{"role": "user", "content": items}]
                try:
                    kwargs["input"] = await window.prepare(fixed, items, builder)
                except ContextBudgetExceeded as exc:
                    local.failure_stage = "context_budget"
                    raise AgentRuntimeFailure(AgentError(
                        code="agent_context_budget_exceeded", message=str(exc), recoverable=True,
                        run_id=local.request.run_id, failure_stage="context_budget",
                    )) from exc
                # Summarization can take time. Recheck the generation before spending
                # another model call on a position that may already have changed.
                await runtime._session_provider(local.request.session_id).get_items(limit=0)
                local.model_requests += 1
                request_index = local.model_requests
                started = time.monotonic()
                runtime._debug_event(
                    local.request.run_id,
                    "model_request_started",
                    request=request_index,
                    input_items=len(kwargs["input"]),
                )
                response = await delegate.get_response(**kwargs)
                duration_ms = max(0, round((time.monotonic() - started) * 1000))
                raw_usage = getattr(response, "raw_usage", None)
                if raw_usage is None:
                    # SDK-normalized details can invent cached_tokens=0. Keep only totals
                    # when native usage is unavailable, so cache/reasoning remain unknown.
                    source = getattr(response, "usage", None)
                    raw_usage = {key: getattr(source, key, None) for key in (
                        "input_tokens", "output_tokens", "total_tokens"
                    )}
                runtime._record_usage(local, raw_usage)
                window.observe(fixed, kwargs["input"], getattr(getattr(response, "usage", None), "input_tokens", None))
                runtime._record_research_model_response(local, response)
                runtime._debug_event(
                    local.request.run_id,
                    "model_response_received",
                    request=request_index,
                    duration_ms=duration_ms,
                    status=getattr(response, "status", None),
                    output_types=",".join(
                        str(getattr(item, "type", "unknown"))
                        for item in getattr(response, "output", ())
                    ),
                    input_tokens=getattr(getattr(response, "usage", None), "input_tokens", None),
                    output_tokens=getattr(getattr(response, "usage", None), "output_tokens", None),
                )
                return response

            def stream_response(self, *args: Any, **kwargs: Any) -> Any:
                return delegate.stream_response(*args, **kwargs)

            def get_retry_advice(self, request: Any) -> Any:
                return delegate.get_retry_advice(request)

            async def _cleanup_on_run_end(self, owner: object) -> None:
                cleanup = getattr(delegate, "_cleanup_on_run_end", None)
                if cleanup is not None:
                    await cleanup(owner)

        return _BudgetedModelProxy()

    @staticmethod
    def _record_usage(local: _LocalRunContext, source: Any, *, summary: bool = False) -> None:
        local.usage["requests"] = local.usage.get("requests", 0) + 1
        if summary:
            local.usage["summary_requests"] = local.usage.get("summary_requests", 0) + 1
        normalized = normalize_usage(source, protocol="responses")
        for key, value in normalized.items():
            if key == "requests":
                continue
            local.usage[key] = local.usage.get(key, 0) + value
            if summary and key in {"input_tokens", "output_tokens", "total_tokens"}:
                local.usage["summary_" + key] = local.usage.get("summary_" + key, 0) + value
        # A run with any unreported call must not look like complete cache telemetry.
        if "input_cache_hit_tokens" not in normalized:
            local.cache_details_complete = False
        if not local.cache_details_complete:
            local.usage.pop("input_cache_hit_tokens", None)
            local.usage.pop("input_cache_miss_tokens", None)

    async def _generate_summary(self, local: _LocalRunContext, payload: str) -> str:
        started = time.monotonic()
        local.model_requests += 1
        request_index = local.model_requests
        self._debug_event(
            local.request.run_id,
            "model_request_started",
            request=request_index,
            kind="summary",
            input_items=1,
        )
        try:
            await self._session_provider(local.request.session_id).get_items(limit=0)
            response = await self._client.responses.create(
                model=self._model, instructions=SUMMARY_INSTRUCTIONS,
                input=[{"role": "user", "content": payload}], tools=[],
                max_output_tokens=self.context_budget.summary_max_output_tokens,
                store=False,
            )
            self._record_usage(local, response.usage, summary=True)
            await self._session_provider(local.request.session_id).get_items(limit=0)
            if response.status != "completed" or not response.output_text.strip():
                raise ValueError("The summary model returned an incomplete or empty summary.")
            self._debug_event(
                local.request.run_id,
                "model_response_received",
                request=request_index,
                kind="summary",
                duration_ms=max(0, round((time.monotonic() - started) * 1000)),
                status=response.status,
                input_tokens=getattr(response.usage, "input_tokens", None),
                output_tokens=getattr(response.usage, "output_tokens", None),
            )
            return response.output_text
        finally:
            local.usage["summary_duration_ms"] = local.usage.get("summary_duration_ms", 0) + round((time.monotonic() - started) * 1000)

    def _output_schema(self, local: _LocalRunContext) -> Any:
        original = self._agents.AgentOutputSchema(AgentResponse)
        wire_schema = (
            adapt_schema(original.json_schema(), self.schema_adapter)
            if self.schema_adapter == "deepseek"
            else original.json_schema()
        )
        runtime = self

        class ProviderOutputSchema(self._agents.AgentOutputSchemaBase):
            def is_plain_text(self) -> bool:
                return original.is_plain_text()

            def name(self) -> str:
                return original.name()

            def json_schema(self) -> dict[str, Any]:
                return wire_schema

            def is_strict_json_schema(self) -> bool:
                return original.is_strict_json_schema()

            def validate_json(self, json_str: str) -> Any:
                try:
                    return AgentResponse.model_validate_json(json_str, strict=True)
                except ValidationError as exc:
                    runtime._record_validation_failure(local, exc)
                    raise runtime._agents.ModelBehaviorError(
                        "The model output did not match the Agent response schema."
                    ) from None

        return ProviderOutputSchema()

    def _map_exception(
        self,
        exc: BaseException,
        request: AgentRunRequest,
        local: _LocalRunContext | None,
    ) -> AgentRuntimeFailure:
        agents = self._agents
        openai = self._openai
        timeout_types = tuple(
            item
            for item in (asyncio.TimeoutError, getattr(openai, "APITimeoutError", None))
            if isinstance(item, type)
        )
        authentication_types = tuple(
            item
            for item in (getattr(openai, "AuthenticationError", None),)
            if isinstance(item, type)
        )
        rate_limit_types = tuple(
            item for item in (getattr(openai, "RateLimitError", None),) if isinstance(item, type)
        )
        max_turn_types = tuple(
            item for item in (getattr(agents, "MaxTurnsExceeded", None),) if isinstance(item, type)
        )
        if isinstance(exc, timeout_types):
            stage: AgentFailureStage = "timeout"
            error = AgentError(
                code="agent_timeout",
                message="Chess Coach Agent did not finish before the timeout.",
                recoverable=True,
                run_id=request.run_id,
                failure_stage=stage,
            )
        elif isinstance(exc, authentication_types):
            stage = "provider_request"
            error = AgentError(
                code="agent_authentication_failed",
                message="Chess Coach Agent authentication failed.",
                recoverable=False,
                run_id=request.run_id,
                failure_stage=stage,
            )
        elif isinstance(exc, rate_limit_types):
            stage = "provider_request"
            error = AgentError(
                code="agent_rate_limited",
                message="Chess Coach Agent is temporarily rate limited.",
                recoverable=True,
                run_id=request.run_id,
                failure_stage=stage,
            )
        elif isinstance(exc, max_turn_types):
            stage = "turn_limit"
            error = AgentError(
                code="max_turns_exceeded",
                message="Chess Coach Agent reached its turn limit.",
                recoverable=True,
                run_id=request.run_id,
                failure_stage=stage,
            )
        elif isinstance(exc, getattr(agents, "ModelRefusalError", ())):
            stage = "model_refusal"
            error = AgentError(
                code="invalid_agent_response",
                message="Chess Coach Agent declined to produce a structured response.",
                recoverable=True,
                run_id=request.run_id,
                failure_stage=stage,
            )
        elif isinstance(exc, getattr(agents, "ModelBehaviorError", ())):
            stage = local.failure_stage if local is not None else None
            stage = stage or "structured_output"
            error = AgentError(
                code="invalid_agent_response",
                message="Chess Coach Agent returned an invalid structured response.",
                recoverable=True,
                run_id=request.run_id,
                failure_stage=stage,
            )
        else:
            stage = "provider_request"
            error = AgentError(
                code="agent_provider_error",
                message="Chess Coach Agent provider request failed.",
                recoverable=True,
                run_id=request.run_id,
                failure_stage=stage,
            )
        if local is not None:
            local.failure_stage = stage
        self._debug_event(
            request.run_id,
            "runtime_exception",
            exception_type=type(exc).__name__,
            status_code=getattr(exc, "status_code", None),
            error_code=error.code,
            failure_stage=stage,
        )
        return AgentRuntimeFailure(error)

    async def run(self, request: AgentRunRequest) -> AgentRunResult:
        local: _LocalRunContext | None = None
        usage: dict[str, int | float] = {}
        trace_context = (
            self._raw_trace_store.activate(request.run_id)
            if self._raw_trace_store is not None
            else None
        )
        if trace_context is not None:
            trace_context.__enter__()
        self._debug_event(
            request.run_id,
            "run_started",
            model=self._model,
            activity=request.model_context.task.activity,
        )
        try:
            session = self._session_provider(request.session_id)
            history = await session.get_items()
            checkpoint = getattr(session, "context_checkpoint", None)
            orchestration = (
                self._orchestration_factory(request)
                if self._orchestration_factory is not None else None
            )
            local = _LocalRunContext(
                request=request,
                tool_adapter=OpenAIToolAdapter(
                    request=request, tools=self._domain_tools_factory(request),
                    agents=self._agents, schema_adapter=self.schema_adapter,
                    debug_event=self._debug_event, orchestration=orchestration,
                ),
                orchestration=orchestration,
                window=RunContextWindow(
                    budget=self.context_budget, history=history,
                    summary=getattr(checkpoint, "conversation_summary", request.model_context.conversation_summary),
                    covered_items=getattr(checkpoint, "conversation_summary_covered_items", 0),
                    summary_version=getattr(checkpoint, "conversation_summary_version", 0),
                    measurement=getattr(checkpoint, "context_input_measurement", None),
                ),
            )
            agent = self._agents.Agent(
                name="Chess Coach",
                instructions=build_agent_instructions(),
                model=self._model_for_run(local),
                model_settings=self._agents.ModelSettings(
                    max_tokens=self.context_budget.max_output_tokens, preserve_raw_usage=True,
                    reasoning=(
                        {"effort": config.AGENT_REASONING_EFFORT}
                        if config.AGENT_REASONING_EFFORT else None
                    ),
                ),
                tools=local.tool_adapter.build_sdk_tools(),
                output_type=self._output_schema(local),
            )
            run_config = self._agents.RunConfig(
                model_provider=self._provider,
                tracing_disabled=True,
                trace_include_sensitive_data=False,
                session_settings=self._agents.SessionSettings(limit=None),
                tool_execution=self._agents.ToolExecutionConfig(
                    max_function_tool_concurrency=1
                ),
            )
            async with asyncio.timeout(request.timeout_seconds):
                try:
                    result = await self._agents.Runner.run(
                        agent,
                        [
                            {"role": "developer", "content": build_model_input(request.model_context)},
                            {"role": "user", "content": request.message},
                        ],
                        context=local,
                        max_turns=request.max_turns,
                        run_config=run_config,
                        session=session,
                    )
                except getattr(self._agents, "UserError", ()) as exc:
                    # SDK 0.22 wraps exceptions escaping function tools in UserError.
                    # Restore our typed failure before the run's normal error handling.
                    if isinstance(exc.__cause__, (
                        AgentRuntimeFailure, StaleAgentContextError, SessionStoreError,
                        asyncio.CancelledError,
                    )):
                        raise exc.__cause__ from None
                    raise
            try:
                response = AgentResponse.model_validate(result.final_output)
            except ValidationError as exc:
                self._record_validation_failure(local, exc)
                raise self._agents.ModelBehaviorError(
                    "The model output did not match the Agent response schema."
                ) from None
            usage_source = getattr(getattr(result, "context_wrapper", None), "usage", None)
            usage = normalize_usage(
                {key: getattr(usage_source, key, None) for key in (
                    "requests", "input_tokens", "output_tokens", "total_tokens"
                )},
                protocol="responses",
            )
            if local.usage:
                usage = dict(local.usage)
            assert local.window is not None
            usage.update(local.window.metrics)
            stage_context = getattr(session, "stage_context", None)
            if callable(stage_context):
                stage_context(
                    summary=local.window.summary,
                    covered_items=local.window.covered_items + local.window.cut,
                    summary_version=local.window.summary_version,
                    input_measurement=local.window.measurement,
                )
            run_result = AgentRunResult(
                response=response,
                tool_calls=local.tool_adapter.budget.records,
                usage=usage,
            )
            if local.orchestration is not None:
                successful_results = getattr(local.tool_adapter.tools, "successful_tool_results", None)
                try:
                    validate_agent_run_result(
                        run_result,
                        request,
                        successful_tool_results=(
                            successful_results() if callable(successful_results) else ()
                        ),
                    )
                except AgentResponseValidationError as exc:
                    local.failure_stage = "grounding_validation"
                    local.validation_errors = [
                        AgentValidationIssue(
                            path="$",
                            error_type="grounding_validation",
                            message=str(exc)[:240] or "Grounding validation failed.",
                        )
                    ]
                    self._debug_event(
                        request.run_id,
                        "grounding_validation_failed",
                        reason=local.validation_errors[0].message,
                    )
                    local.orchestration.terminate(
                        "rejected", reason="production_validation"
                    )
                    raise AgentRuntimeFailure(
                        AgentError(
                            code="invalid_agent_response",
                            message="Chess Coach Agent returned an ungrounded response.",
                            recoverable=True,
                            run_id=request.run_id,
                            failure_stage="grounding_validation",
                        )
                    ) from exc
                local.orchestration.terminate("accepted")
            self._debug_event(
                request.run_id,
                "run_succeeded",
                model_requests=local.model_requests,
                tool_calls=len(local.tool_adapter.budget.records),
            )
            return run_result
        except AgentRuntimeFailure as exc:
            if local is not None and local.failure_stage is None:
                local.failure_stage = exc.error.failure_stage
            if (
                local is not None
                and local.orchestration is not None
                and local.orchestration.state is not None
                and local.orchestration.state.terminal_status == "running"
            ):
                local.orchestration.terminate("rejected", reason="runtime_failure")
            if exc.error.run_id is None:
                raise AgentRuntimeFailure(
                    exc.error.model_copy(update={"run_id": request.run_id})
                ) from exc
            raise
        except (StaleAgentContextError, SessionStoreError):
            if (
                local is not None
                and local.orchestration is not None
                and local.orchestration.state is not None
                and local.orchestration.state.terminal_status == "running"
            ):
                local.orchestration.terminate("rejected", reason="runtime_failure")
            raise
        except asyncio.CancelledError:
            if local is not None and local.orchestration is not None:
                local.orchestration.terminate("cancelled")
            raise
        except BaseException as exc:
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            if (
                local is not None
                and local.orchestration is not None
                and local.orchestration.state is not None
                and local.orchestration.state.terminal_status == "running"
            ):
                local.orchestration.terminate(
                    "rejected", reason=type(exc).__name__
                )
            raise self._map_exception(exc, request, local) from exc
        finally:
            if local is not None:
                if local.usage:
                    usage = dict(local.usage)
                if local.window is not None:
                    usage.update(local.window.metrics)
            self._telemetry[request.run_id] = AgentRuntimeTelemetry(
                tool_calls=list(local.tool_adapter.budget.records) if local is not None else [],
                usage=dict(usage),
                failure_stage=local.failure_stage if local is not None else None,
                validation_errors=(
                    tuple(local.validation_errors) if local is not None else ()
                ),
            )
            if local is not None and local.orchestration is not None:
                self._orchestration_traces[request.run_id] = local.orchestration.trace()
            if trace_context is not None:
                directory = self._raw_trace_store.current_directory()
                trace_context.__exit__(*sys.exc_info())
                if directory is not None:
                    self._debug_event(
                        request.run_id,
                        "raw_trace_saved",
                        path=directory,
                    )


def create_openai_runtime(
    *,
    enabled: bool,
    model: str,
    base_url: str,
    custom_api_key: str,
    openai_api_key: str,
    domain_tools_factory: DomainToolsFactory,
    session_provider: SessionProvider,
    provider: str = "",
    debug: bool = False,
    raw_trace_store: RawHttpTraceStore | None = None,
) -> OpenAIAgentsRuntime | UnavailableAgentRuntime:
    provider = resolve_agent_provider(provider, base_url)
    endpoint_type = "custom_responses" if base_url else "openai_responses"
    if not enabled:
        return UnavailableAgentRuntime(
            AgentRuntimeAvailability(
                False,
                False,
                model,
                endpoint_type,
                "Chess Coach Agent is disabled.",
                "agent_unavailable",
            )
        )
    if not model:
        return UnavailableAgentRuntime(
            AgentRuntimeAvailability(
                True,
                False,
                "",
                endpoint_type,
                "CHESS_AGENT_MODEL is not configured.",
                "agent_unavailable",
            )
        )
    api_key = custom_api_key if base_url else openai_api_key
    if not api_key:
        name = "CHESS_AGENT_API_KEY" if base_url else "OPENAI_API_KEY"
        return UnavailableAgentRuntime(
            AgentRuntimeAvailability(
                True,
                False,
                model,
                endpoint_type,
                f"{name} is not configured.",
                "agent_unavailable",
            )
        )
    try:
        return OpenAIAgentsRuntime(
            model=model,
            api_key=api_key,
            base_url=base_url or "https://api.openai.com/v1",
            endpoint_type=endpoint_type,
            provider=provider,
            domain_tools_factory=domain_tools_factory,
            session_provider=session_provider,
            debug=debug,
            raw_trace_store=raw_trace_store,
        )
    except (ImportError, ModuleNotFoundError):
        return UnavailableAgentRuntime(
            AgentRuntimeAvailability(
                True,
                False,
                model,
                endpoint_type,
                "OpenAI Agents SDK optional dependency is not installed.",
                "agent_unavailable",
            )
        )
