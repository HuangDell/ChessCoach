"""Optional OpenAI Agents SDK adapter for the Chess Coach Agent runtime."""
from __future__ import annotations

import asyncio
import hashlib
import importlib
import json
import logging
from pathlib import Path
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Literal

from pydantic import BaseModel, ValidationError

from server.core.facts_projection import project_facts
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
    AgentToolName,
    AgentValidationIssue,
    AnalyzeMoveInput,
    AnalyzePositionInput,
    CreateTrainingDraftInput,
    GetPlayerProfileInput,
    GetReviewContextInput,
    GetTrainingCandidatesInput,
    LookupOpeningInput,
    PositionReference,
    ToolCallRecord,
    ToolError,
    ToolPositionReference,
    ToolResult,
)
from server.core.agent.policy import (
    AgentResponseValidationError,
    build_agent_instructions,
    build_model_input,
    validate_agent_run_result,
)
from server.core.agent.routing import OrchestrationController
from server.core.agent.runtime import (
    AgentRuntimeAvailability,
    AgentRuntimeFailure,
    AgentRuntimeTelemetry,
    UnavailableAgentRuntime,
)
from server.core.learning import taxonomy
from server.core.storage.agent_traces import RawHttpTraceStore


DomainToolsFactory = Callable[[AgentRunRequest], Any]
SessionProvider = Callable[[str], Any]
AGENTS_SDK_VERSION = "0.22.0"
logger = logging.getLogger("chesscoach.agent")


class _InlineSQLiteAsyncio:
    """Python 3.14 compatibility proxy for the SDK's short local SQLite operations."""

    def __init__(self, delegate: Any) -> None:
        self._delegate = delegate

    def __getattr__(self, name: str) -> Any:
        return getattr(self._delegate, name)

    async def to_thread(self, function: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        return function(*args, **kwargs)


def _configure_sqlite_compatibility(agents: Any) -> None:
    if sys.version_info < (3, 14) or getattr(agents, "__version__", None) != "0.22.0":
        return
    sqlite_module = importlib.import_module("agents.memory.sqlite_session")
    if isinstance(sqlite_module.asyncio, _InlineSQLiteAsyncio):
        return
    # CPython 3.14 can leave the SDK's sqlite3 worker Future pending after the worker has
    # completed. These operations are bounded local session reads/writes, so keep them inline.
    sqlite_module.asyncio = _InlineSQLiteAsyncio(sqlite_module.asyncio)


class SQLiteConversationSessionFactory:
    """Lifecycle-owned SDK SQLiteSession instances sharing one local database."""

    def __init__(self, data_dir: str) -> None:
        agents = importlib.import_module("agents")
        _configure_sqlite_compatibility(agents)
        directory = Path(data_dir) / "agent"
        directory.mkdir(parents=True, exist_ok=True)
        self._agents = agents
        self._path = directory / "conversations.sqlite3"
        self._sessions: dict[str, Any] = {}
        self._lock = threading.RLock()
        self._closed = False

    def get_session(self, session_id: str) -> Any:
        with self._lock:
            if self._closed:
                raise RuntimeError("Agent conversation store is closed.")
            session = self._sessions.get(session_id)
            if session is None:
                session = self._agents.SQLiteSession(
                    session_id,
                    db_path=self._path,
                )
                self._sessions[session_id] = session
            return session

    async def clear_session(self, session_id: str) -> None:
        with self._lock:
            if self._closed:
                raise RuntimeError("Agent conversation store is closed.")
            session = self._sessions.pop(session_id, None)
            if session is None:
                session = self._agents.SQLiteSession(
                    session_id,
                    db_path=self._path,
                )
        try:
            await session.clear_session()
        finally:
            session.close()

    def close(self) -> None:
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
            self._closed = True
        first_error: BaseException | None = None
        for session in sessions:
            try:
                session.close()
            except BaseException as exc:  # close all owned connections before reporting failure
                if first_error is None:
                    first_error = exc
        if first_error is not None:
            raise first_error


@dataclass
class _ToolBudget:
    max_total: int
    max_engine: int
    total: int = 0
    engine: int = 0
    records: list[ToolCallRecord] = field(default_factory=list)

    def _exhausted(self, name: AgentToolName) -> ToolError:
        self.records.append(
            ToolCallRecord(
                name=name,
                permission=AGENT_TOOL_PERMISSIONS[name],
                status="budget_exceeded",
                duration_ms=0,
                error_code="tool_budget_exceeded",
            )
        )
        return ToolError(
            code="tool_budget_exceeded",
            message="The tool budget for this Agent run has been exhausted.",
            recoverable=True,
        )

    def reserve_total(self, name: AgentToolName) -> ToolError | None:
        if self.total + 1 > self.max_total:
            return self._exhausted(name)
        self.total += 1
        return None

    def reserve_engine(self, name: AgentToolName, engine_calls: int) -> ToolError | None:
        engine_calls = max(0, int(engine_calls))
        if self.engine + engine_calls > self.max_engine:
            return self._exhausted(name)
        self.engine += engine_calls
        return None


@dataclass
class _LocalRunContext:
    request: AgentRunRequest
    tools: Any
    budget: _ToolBudget
    orchestration: OrchestrationController | None = None
    window: RunContextWindow | None = None
    usage: dict[str, int | float] = field(default_factory=dict)
    cache_details_complete: bool = True
    model_requests: int = 0
    failure_stage: AgentFailureStage | None = None
    validation_errors: list[AgentValidationIssue] = field(default_factory=list)


def _error_result(error: ToolError) -> str:
    return ToolResult[Any](ok=False, error=error).model_dump_json()


def _canonical_tool_focus(
    skill_ids: list[str], categories: list[str]
) -> tuple[list[str], list[str]]:
    """Prefer explicit resolvable skill IDs over redundant model-supplied categories."""

    resolved = [taxonomy.resolve_skill_id(value) for value in skill_ids]
    if skill_ids and all(value is not None for value in resolved):
        return list(dict.fromkeys(value for value in resolved if value is not None)), []
    return skill_ids, categories


class OpenAIAgentsRuntime:
    """Single-Agent Responses API runtime; SDK types remain private to this module."""

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

    @staticmethod
    def _execution_metadata(tools: Any) -> tuple[bool, int]:
        execution = getattr(tools, "last_execution", None)
        return (
            bool(getattr(execution, "cache_hit", False)),
            int(getattr(execution, "engine_calls", 0) or 0),
        )

    @staticmethod
    def _position_reference(payload: BaseModel) -> ToolPositionReference | None:
        values = payload.model_dump(mode="python")
        game_id = values.get("game_id")
        critical_id = values.get("critical_id")
        fen = values.get("fen") or values.get("fen_before")
        nested = values.get("position_references")
        if isinstance(nested, list) and nested and isinstance(nested[0], dict):
            game_id = game_id or nested[0].get("game_id")
            critical_id = critical_id or nested[0].get("critical_id")
            fen = fen or nested[0].get("fen")
        fingerprint = (
            hashlib.sha256(str(fen).encode("utf-8")).hexdigest()[:16] if fen else None
        )
        if not any((game_id, critical_id, fingerprint)):
            return None
        return ToolPositionReference(
            game_id=str(game_id) if game_id else None,
            critical_id=str(critical_id) if critical_id else None,
            fen_fingerprint=fingerprint,
        )

    def _debug_tool_record(self, context: _LocalRunContext) -> None:
        if not context.budget.records:
            return
        record = context.budget.records[-1]
        self._debug_event(
            context.request.run_id,
            "tool_completed",
            tool=record.name,
            status=record.status,
            duration_ms=record.duration_ms,
            cache_hit=str(record.cache_hit).lower(),
            engine_calls=record.engine_call_count,
            error_code=record.error_code,
        )

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

    async def _call_tool(
        self,
        context: _LocalRunContext,
        name: AgentToolName,
        payload: BaseModel,
    ) -> str:
        tools = context.tools
        position_reference = self._position_reference(payload)
        budget_error = context.budget.reserve_total(name)
        if budget_error is not None:
            context.budget.records[-1] = context.budget.records[-1].model_copy(
                update={"position_reference": position_reference}
            )
            if context.orchestration is not None:
                call_id, _ = await context.orchestration.before_call(
                    name,
                    payload,
                    total_used=context.budget.total,
                    engine_used=context.budget.engine,
                )
                context.orchestration.after_call(
                    call_id,
                    name,
                    payload,
                    ToolResult[Any](ok=False, error=budget_error),
                    cache_hit=False,
                    engine_calls=0,
                    total_used=context.budget.total,
                    engine_used=context.budget.engine,
                )
                context.orchestration.terminate("budget", reason="total_tool_calls")
            self._debug_tool_record(context)
            return _error_result(budget_error)

        research_call_id: str | None = None
        if context.orchestration is not None:
            research_call_id, routing_error = await context.orchestration.before_call(
                name,
                payload,
                total_used=context.budget.total,
                engine_used=context.budget.engine,
            )
            if routing_error is not None:
                context.budget.records.append(
                    ToolCallRecord(
                        name=name,
                        permission=AGENT_TOOL_PERMISSIONS[name],
                        status="error",
                        duration_ms=0,
                        position_reference=position_reference,
                        error_code=routing_error.code,
                    )
                )
                self._debug_tool_record(context)
                return _error_result(routing_error)

        current_position = context.request.model_context.position
        supplied_fen = (
            payload.fen
            if isinstance(payload, (AnalyzePositionInput, LookupOpeningInput))
            else payload.fen_before
            if isinstance(payload, AnalyzeMoveInput)
            else None
        )
        if supplied_fen is not None and (
            current_position is None or supplied_fen != current_position.fen
        ):
            error = ToolError(
                code="position_not_found",
                message="The tool request does not match the current Agent position.",
                recoverable=False,
            )
            context.budget.records.append(
                ToolCallRecord(
                    name=name,
                    permission=AGENT_TOOL_PERMISSIONS[name],
                    status="error",
                    duration_ms=0,
                    position_reference=position_reference,
                    error_code=error.code,
                )
            )
            if context.orchestration is not None and research_call_id is not None:
                context.orchestration.after_call(
                    research_call_id,
                    name,
                    payload,
                    ToolResult[Any](ok=False, error=error),
                    cache_hit=False,
                    engine_calls=0,
                    total_used=context.budget.total,
                    engine_used=context.budget.engine,
                )
            self._debug_tool_record(context)
            return _error_result(error)

        estimator = getattr(tools, "estimated_engine_calls", None)
        if estimator is not None:
            reserved_engine_calls = max(0, int(estimator(name, payload)))
        else:
            checker = getattr(tools, "would_use_engine", None)
            reserved_engine_calls = (
                int(bool(checker(name, payload)))
                if checker is not None
                else int(name == "analyze_position")
            )
        budget_error = context.budget.reserve_engine(name, reserved_engine_calls)
        if budget_error is not None:
            context.budget.records[-1] = context.budget.records[-1].model_copy(
                update={"position_reference": position_reference}
            )
            if context.orchestration is not None and research_call_id is not None:
                context.orchestration.after_call(
                    research_call_id,
                    name,
                    payload,
                    ToolResult[Any](ok=False, error=budget_error),
                    cache_hit=False,
                    engine_calls=0,
                    total_used=context.budget.total,
                    engine_used=context.budget.engine,
                )
                context.orchestration.terminate("budget", reason="engine_tool_calls")
            self._debug_tool_record(context)
            return _error_result(budget_error)
        started = time.monotonic()
        try:
            executor = getattr(tools, "execute", None)
            if executor is not None:
                execution = await executor(name, payload)
                result = execution.result
                cache_hit = bool(getattr(execution, "cache_hit", False))
                engine_calls = int(getattr(execution, "engine_calls", 0) or 0)
            else:
                result = await getattr(tools, name)(payload)
                cache_hit, engine_calls = self._execution_metadata(tools)
            visible_result = result.model_dump(mode="json")
            if name == "get_review_context" and result.ok and result.data is not None:
                full_facts = result.data.facts
                visible_result["data"]["facts"] = project_facts(
                    full_facts, full_facts.get("signals") or []
                )
            # Reconcile the conservative pre-call reservation with actual cache/Engine metadata.
            # A cache hit refunds the reservation; an operation with multiple misses consumes each.
            context.budget.engine = max(
                0,
                context.budget.engine + engine_calls - reserved_engine_calls,
            )
            duration_ms = max(0, round((time.monotonic() - started) * 1000))
            status = "ok" if result.ok else (
                "budget_exceeded"
                if result.error and result.error.code == "tool_budget_exceeded"
                else "error"
            )
            context.budget.records.append(
                ToolCallRecord(
                    name=name,
                    permission=AGENT_TOOL_PERMISSIONS[name],
                    status=status,
                    duration_ms=duration_ms,
                    cache_hit=cache_hit,
                    engine_call_count=engine_calls,
                    position_reference=position_reference,
                    evidence_refs=list(result.evidence_refs),
                    error_code=result.error.code if result.error else None,
                )
            )
            if context.orchestration is not None and research_call_id is not None:
                context.orchestration.after_call(
                    research_call_id,
                    name,
                    payload,
                    result,
                    cache_hit=cache_hit,
                    engine_calls=engine_calls,
                    total_used=context.budget.total,
                    engine_used=context.budget.engine,
                )
            self._debug_tool_record(context)
            return json.dumps(visible_result, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        except Exception as exc:
            duration_ms = max(0, round((time.monotonic() - started) * 1000))
            error_code = (
                "position_not_found"
                if name == "get_review_context"
                else "profile_unavailable"
                if name == "get_player_profile"
                else "training_unavailable"
                if name in {"get_training_candidates", "create_training_draft"}
                else "engine_unavailable"
            )
            context.budget.records.append(
                ToolCallRecord(
                    name=name,
                    permission=AGENT_TOOL_PERMISSIONS[name],
                    status="error",
                    duration_ms=duration_ms,
                    engine_call_count=reserved_engine_calls,
                    position_reference=position_reference,
                    error_code=error_code,
                )
            )
            error = ToolError(
                code=error_code,
                message=(
                    "The saved review position is currently unavailable."
                    if name == "get_review_context"
                    else "Personalized training is currently unavailable."
                    if name in {"get_training_candidates", "create_training_draft"}
                    else "The requested chess analysis is currently unavailable."
                ),
                recoverable=True,
            )
            if context.orchestration is not None and research_call_id is not None:
                context.orchestration.after_call(
                    research_call_id,
                    name,
                    payload,
                    ToolResult[Any](ok=False, error=error),
                    cache_hit=False,
                    engine_calls=reserved_engine_calls,
                    total_used=context.budget.total,
                    engine_used=context.budget.engine,
                    error_category=(
                        "storage_consistency"
                        if "Consistency" in type(exc).__name__
                        else type(exc).__name__
                    ),
                )
            self._debug_tool_record(context)
            return _error_result(error)

    def _invalid_tool_call(
        self,
        context: _LocalRunContext,
        name: AgentToolName,
        error: ToolError,
    ) -> str:
        budget_error = context.budget.reserve_total(name)
        if budget_error is not None:
            self._debug_tool_record(context)
            return _error_result(budget_error)
        context.budget.records.append(
            ToolCallRecord(
                name=name,
                permission=AGENT_TOOL_PERMISSIONS[name],
                status="error",
                duration_ms=0,
                error_code=error.code,
            )
        )
        self._debug_tool_record(context)
        return _error_result(error)

    def _sdk_tools(self, local: _LocalRunContext) -> list[Any]:
        function_tool = self._agents.function_tool
        tools: list[Any] = []

        def is_enabled(name: AgentToolName) -> Any:
            if local.orchestration is None:
                return True

            async def enabled(_context: Any, _agent: Any) -> bool:
                assert local.orchestration is not None
                return await local.orchestration.is_enabled(name)

            return enabled

        if "get_review_context" in local.request.allowed_tools:
            async def get_review_context(
                game_id: str,
                review_side: Literal["white", "black"],
                critical_id: str,
            ) -> str:
                """Load the one explicitly selected saved critical position and its Engine facts."""
                try:
                    payload = GetReviewContextInput(
                        game_id=game_id,
                        review_side=review_side,
                        critical_id=critical_id,
                    )
                except ValidationError:
                    return self._invalid_tool_call(
                        local,
                        "get_review_context",
                        ToolError(
                            code="position_not_found",
                            message="The requested review position is not owned by this session.",
                            recoverable=False,
                        )
                    )
                return await self._call_tool(local, "get_review_context", payload)

            tools.append(
                function_tool(
                    get_review_context,
                    name_override="get_review_context",
                    description_override=(
                        "Read the exact active game's saved critical-position Engine facts."
                    ),
                    strict_mode=True,
                    is_enabled=is_enabled("get_review_context"),
                )
            )

        if "analyze_position" in local.request.allowed_tools:
            async def analyze_position(
                fen: str,
                purpose: Literal[
                    "compare_candidates", "find_best_move", "explain_position"
                ],
            ) -> str:
                """Compare up to three Engine candidates for the supplied current FEN."""
                try:
                    payload = AnalyzePositionInput(fen=fen, purpose=purpose)
                except ValidationError:
                    return self._invalid_tool_call(
                        local,
                        "analyze_position",
                        ToolError(
                            code="invalid_fen",
                            message="The position or analysis purpose is invalid.",
                            recoverable=False,
                        )
                    )
                return await self._call_tool(local, "analyze_position", payload)

            tools.append(
                function_tool(
                    analyze_position,
                    name_override="analyze_position",
                    description_override=(
                        "Analyze the current FEN for a bounded candidate comparison. The backend "
                        "chooses depth and MultiPV."
                    ),
                    strict_mode=True,
                    is_enabled=is_enabled("analyze_position"),
                )
            )

        if "analyze_move" in local.request.allowed_tools:
            async def analyze_move(fen_before: str, move_uci: str) -> str:
                """Analyze one legal UCI move from the supplied current FEN."""
                try:
                    payload = AnalyzeMoveInput(fen_before=fen_before, move_uci=move_uci)
                except ValidationError:
                    return self._invalid_tool_call(
                        local,
                        "analyze_move",
                        ToolError(
                            code="invalid_fen",
                            message="The FEN or UCI move is invalid.",
                            recoverable=False,
                        )
                    )
                return await self._call_tool(local, "analyze_move", payload)

            tools.append(
                function_tool(
                    analyze_move,
                    name_override="analyze_move",
                    description_override=(
                        "Check one what-if move in the current FEN, reusing saved analysis "
                        "when possible."
                    ),
                    strict_mode=True,
                    is_enabled=is_enabled("analyze_move"),
                )
            )

        if "lookup_opening" in local.request.allowed_tools:
            async def lookup_opening(
                fen: str | None = None,
                recent_moves_uci: list[str] | None = None,
            ) -> str:
                """Look up local ECO metadata for the current position without network access."""
                try:
                    payload = LookupOpeningInput(
                        fen=fen,
                        recent_moves_uci=recent_moves_uci or [],
                    )
                except ValidationError:
                    return self._invalid_tool_call(
                        local,
                        "lookup_opening",
                        ToolError(
                            code="invalid_fen",
                            message="The opening lookup position or move history is invalid.",
                            recoverable=False,
                        ),
                    )
                current = local.request.model_context.position
                if payload.recent_moves_uci and (
                    current is None
                    or payload.recent_moves_uci
                    not in (
                        current.recent_moves_uci,
                        [*current.recent_moves_uci, *current.exploration_moves_uci],
                    )
                ):
                    return self._invalid_tool_call(
                        local,
                        "lookup_opening",
                        ToolError(
                            code="position_not_found",
                            message="Opening moves do not match the current checkpoint.",
                            recoverable=False,
                        ),
                    )
                if payload.recent_moves_uci and current is not None:
                    payload = LookupOpeningInput(fen=current.fen)
                return await self._call_tool(local, "lookup_opening", payload)

            tools.append(
                function_tool(
                    lookup_opening,
                    name_override="lookup_opening",
                    description_override=(
                        "Read local ECO/name metadata for the exact current FEN or its validated "
                        "recent moves. This tool never retrieves online opening theory."
                    ),
                    strict_mode=True,
                    is_enabled=is_enabled("lookup_opening"),
                )
            )

        if "get_player_profile" in local.request.allowed_tools:
            async def get_player_profile(
                focus_skill_ids: list[str] | None = None,
                focus_categories: list[str] | None = None,
                limit: int = 3,
            ) -> str:
                """Read at most five evidence-backed profile items relevant to this task."""
                try:
                    canonical_ids, canonical_categories = _canonical_tool_focus(
                        focus_skill_ids or [], focus_categories or []
                    )
                    payload = GetPlayerProfileInput(
                        focus_skill_ids=canonical_ids,
                        focus_categories=canonical_categories,
                        limit=limit,
                    )
                except ValidationError:
                    return self._invalid_tool_call(
                        local,
                        "get_player_profile",
                        ToolError(
                            code="profile_unavailable",
                            message="The requested profile focus or limit is invalid.",
                            recoverable=False,
                        ),
                    )
                if not local.request.model_context.task.personalization_enabled:
                    return self._invalid_tool_call(
                        local,
                        "get_player_profile",
                        ToolError(
                            code="profile_unavailable",
                            message="Personalized coaching is disabled.",
                            recoverable=False,
                        ),
                    )
                return await self._call_tool(local, "get_player_profile", payload)

            tools.append(
                function_tool(
                    get_player_profile,
                    name_override="get_player_profile",
                    description_override=(
                        "Read up to five deterministic weakness or strength items with verified "
                        "canonical examples that may reference a game, critical position, position, "
                        "or puzzle. Use only when personalization is enabled and relevant."
                    ),
                    strict_mode=True,
                    is_enabled=is_enabled("get_player_profile"),
                )
            )

        if "get_training_candidates" in local.request.allowed_tools:
            async def get_training_candidates(
                skill_ids: list[str] | None = None,
                categories: list[str] | None = None,
                window: Literal["recent", "lifetime"] = "recent",
                limit: int = 10,
                exclude_recently_practiced: bool = True,
                exclude_recently_solved: bool = True,
                exclude_current_game: bool = False,
                recent_practice_days: int = 7,
            ) -> str:
                """Retrieve verified, bounded own-game positions for personalized practice."""
                try:
                    canonical_ids, canonical_categories = _canonical_tool_focus(
                        skill_ids or [], categories or []
                    )
                    payload = GetTrainingCandidatesInput(
                        skill_ids=canonical_ids,
                        categories=canonical_categories,
                        window=window,
                        limit=limit,
                        exclude_recently_practiced=exclude_recently_practiced,
                        exclude_recently_solved=exclude_recently_solved,
                        exclude_current_game=exclude_current_game,
                        recent_practice_days=recent_practice_days,
                    )
                except ValidationError:
                    return self._invalid_tool_call(
                        local,
                        "get_training_candidates",
                        ToolError(
                            code="training_unavailable",
                            message="The training candidate filters are invalid.",
                            recoverable=False,
                        ),
                    )
                if not local.request.model_context.task.personalization_enabled:
                    return self._invalid_tool_call(
                        local,
                        "get_training_candidates",
                        ToolError(
                            code="profile_unavailable",
                            message="Personalized coaching is disabled.",
                            recoverable=False,
                        ),
                    )
                return await self._call_tool(local, "get_training_candidates", payload)

            tools.append(
                function_tool(
                    get_training_candidates,
                    name_override="get_training_candidates",
                    description_override=(
                        "Retrieve at most ten Engine-artifact positions backed by canonical skills. "
                        "With no explicit focus, only established canonical weaknesses are used."
                    ),
                    strict_mode=True,
                    is_enabled=is_enabled("get_training_candidates"),
                )
            )

        if "create_training_draft" in local.request.allowed_tools:
            async def create_training_draft(
                title: str,
                objective_skill_ids: list[str],
                position_references: list[PositionReference],
                rationale: str,
                recommended_count: int,
                evidence_refs: list[str] | None = None,
                source: Literal["agent_training_draft"] = "agent_training_draft",
            ) -> str:
                """Create a temporary draft from positions retrieved successfully in this run."""
                try:
                    payload = CreateTrainingDraftInput(
                        title=title,
                        objective_skill_ids=objective_skill_ids,
                        position_references=position_references,
                        rationale=rationale,
                        recommended_count=recommended_count,
                        evidence_refs=evidence_refs or [],
                        source=source,
                    )
                except ValidationError:
                    return self._invalid_tool_call(
                        local,
                        "create_training_draft",
                        ToolError(
                            code="training_unavailable",
                            message="The temporary training draft is invalid or exceeds its limit.",
                            recoverable=False,
                        ),
                    )
                return await self._call_tool(local, "create_training_draft", payload)

            tools.append(
                function_tool(
                    create_training_draft,
                    name_override="create_training_draft",
                    description_override=(
                        "Create a temporary training draft of at most five positions. Every position "
                        "and canonical objective must come from this run's successful retrieval."
                    ),
                    strict_mode=True,
                    is_enabled=is_enabled("create_training_draft"),
                )
            )
        if self.schema_adapter == "deepseek":
            for tool in tools:
                tool.params_json_schema = adapt_schema(tool.params_json_schema, self.schema_adapter)
        return tools

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
            local = _LocalRunContext(
                request=request,
                tools=self._domain_tools_factory(request),
                budget=_ToolBudget(
                    max_total=request.max_total_tool_calls,
                    max_engine=request.max_engine_tool_calls,
                ),
                orchestration=(
                    self._orchestration_factory(request)
                    if self._orchestration_factory is not None
                    else None
                ),
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
                tools=self._sdk_tools(local),
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
                tool_calls=local.budget.records,
                usage=usage,
            )
            if local.orchestration is not None:
                successful_results = getattr(local.tools, "successful_tool_results", None)
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
                tool_calls=len(local.budget.records),
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
                tool_calls=list(local.budget.records) if local is not None else [],
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
