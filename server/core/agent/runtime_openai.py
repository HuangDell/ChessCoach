"""Optional OpenAI Agents SDK adapter for the Chess Coach Agent runtime."""
from __future__ import annotations

import asyncio
import importlib
from pathlib import Path
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Literal

from pydantic import BaseModel, ValidationError

from server.core.agent.models import (
    AGENT_TOOL_PERMISSIONS,
    AgentError,
    AgentResponse,
    AgentRunRequest,
    AgentRunResult,
    AgentToolName,
    AnalyzeMoveInput,
    AnalyzePositionInput,
    GetPlayerProfileInput,
    GetReviewContextInput,
    LookupOpeningInput,
    ToolCallRecord,
    ToolError,
    ToolResult,
)
from server.core.agent.policy import build_model_input
from server.core.agent.runtime import (
    AgentRuntimeAvailability,
    AgentRuntimeFailure,
    UnavailableAgentRuntime,
)


DomainToolsFactory = Callable[[AgentRunRequest], Any]
SessionProvider = Callable[[str], Any]


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


def _error_result(error: ToolError) -> str:
    return ToolResult[Any](ok=False, error=error).model_dump_json()


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
    ) -> None:
        agents = importlib.import_module("agents")
        openai = importlib.import_module("openai")
        self._agents = agents
        self._openai = openai
        self._model = model
        self._domain_tools_factory = domain_tools_factory
        self._session_provider = session_provider
        # Explicit values prevent the SDK from reading OPENAI_BASE_URL or another implicit endpoint.
        self._client = openai.AsyncOpenAI(api_key=api_key, base_url=base_url)
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

    async def _call_tool(
        self,
        context: _LocalRunContext,
        name: AgentToolName,
        payload: BaseModel,
    ) -> str:
        tools = context.tools
        budget_error = context.budget.reserve_total(name)
        if budget_error is not None:
            return _error_result(budget_error)

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
                    error_code=error.code,
                )
            )
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
                    evidence_refs=list(result.evidence_refs),
                    error_code=result.error.code if result.error else None,
                )
            )
            return result.model_dump_json()
        except Exception:
            duration_ms = max(0, round((time.monotonic() - started) * 1000))
            error_code = (
                "position_not_found" if name == "get_review_context" else "engine_unavailable"
            )
            context.budget.records.append(
                ToolCallRecord(
                    name=name,
                    permission=AGENT_TOOL_PERMISSIONS[name],
                    status="error",
                    duration_ms=duration_ms,
                    error_code=error_code,
                )
            )
            return _error_result(
                ToolError(
                    code=error_code,
                    message=(
                        "The saved review position is currently unavailable."
                        if name == "get_review_context"
                        else "The requested chess analysis is currently unavailable."
                    ),
                    recoverable=True,
                )
            )

    def _invalid_tool_call(
        self,
        context: _LocalRunContext,
        name: AgentToolName,
        error: ToolError,
    ) -> str:
        budget_error = context.budget.reserve_total(name)
        if budget_error is not None:
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
        return _error_result(error)

    def _sdk_tools(self, local: _LocalRunContext) -> list[Any]:
        function_tool = self._agents.function_tool
        tools: list[Any] = []

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
                )
            )

        if "get_player_profile" in local.request.allowed_tools:
            async def get_player_profile(
                focus_skill_ids: list[str] | None = None,
                focus_categories: list[str] | None = None,
                limit: int = 3,
            ) -> str:
                """Read at most three evidence-backed profile items relevant to this task."""
                try:
                    payload = GetPlayerProfileInput(
                        focus_skill_ids=focus_skill_ids or [],
                        focus_categories=focus_categories or [],
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
                        "Read up to three deterministic weakness or strength items with game-backed "
                        "evidence. Use only when personalization is enabled and relevant."
                    ),
                    strict_mode=True,
                )
            )
        return tools

    def _map_exception(self, exc: BaseException) -> AgentRuntimeFailure:
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
            error = AgentError(
                code="agent_timeout",
                message="Chess Coach Agent did not finish before the timeout.",
                recoverable=True,
            )
        elif isinstance(exc, authentication_types):
            error = AgentError(
                code="agent_authentication_failed",
                message="Chess Coach Agent authentication failed.",
                recoverable=False,
            )
        elif isinstance(exc, rate_limit_types):
            error = AgentError(
                code="agent_rate_limited",
                message="Chess Coach Agent is temporarily rate limited.",
                recoverable=True,
            )
        elif isinstance(exc, max_turn_types):
            error = AgentError(
                code="max_turns_exceeded",
                message="Chess Coach Agent reached its turn limit.",
                recoverable=True,
            )
        elif isinstance(
            exc,
            tuple(
                item
                for item in (
                    getattr(agents, "ModelBehaviorError", None),
                    getattr(agents, "ModelRefusalError", None),
                )
                if isinstance(item, type)
            ),
        ):
            error = AgentError(
                code="invalid_agent_response",
                message="Chess Coach Agent returned an invalid structured response.",
                recoverable=True,
            )
        else:
            error = AgentError(
                code="agent_provider_error",
                message="Chess Coach Agent provider request failed.",
                recoverable=True,
            )
        return AgentRuntimeFailure(error)

    async def run(self, request: AgentRunRequest) -> AgentRunResult:
        try:
            session = self._session_provider(request.session_id)
            local = _LocalRunContext(
                request=request,
                tools=self._domain_tools_factory(request),
                budget=_ToolBudget(
                    max_total=request.max_total_tool_calls,
                    max_engine=request.max_engine_tool_calls,
                ),
            )
            agent = self._agents.Agent(
                name="Chess Coach",
                instructions=build_model_input(request.model_context),
                model=self._model,
                tools=self._sdk_tools(local),
                output_type=AgentResponse,
            )
            run_config = self._agents.RunConfig(
                model_provider=self._provider,
                tracing_disabled=True,
                trace_include_sensitive_data=False,
                session_settings=self._agents.SessionSettings(limit=12),
                tool_execution=self._agents.ToolExecutionConfig(
                    max_function_tool_concurrency=1
                ),
            )
            async with asyncio.timeout(request.timeout_seconds):
                result = await self._agents.Runner.run(
                    agent,
                    request.message,
                    context=local,
                    max_turns=request.max_turns,
                    run_config=run_config,
                    session=session,
                )
            response = AgentResponse.model_validate(result.final_output)
            usage_source = getattr(getattr(result, "context_wrapper", None), "usage", None)
            usage = {
                key: value
                for key in ("requests", "input_tokens", "output_tokens", "total_tokens")
                if isinstance((value := getattr(usage_source, key, None)), (int, float))
            }
            return AgentRunResult(
                response=response,
                tool_calls=local.budget.records,
                usage=usage,
            )
        except AgentRuntimeFailure:
            raise
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            raise self._map_exception(exc) from exc


def create_openai_runtime(
    *,
    enabled: bool,
    model: str,
    base_url: str,
    custom_api_key: str,
    openai_api_key: str,
    domain_tools_factory: DomainToolsFactory,
    session_provider: SessionProvider,
) -> OpenAIAgentsRuntime | UnavailableAgentRuntime:
    endpoint_type = "custom_responses" if base_url else "openai_responses"
    if not enabled:
        return UnavailableAgentRuntime(
            AgentRuntimeAvailability(
                False,
                False,
                model,
                endpoint_type,
                "Chess Coach Agent is disabled.",
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
            )
        )
    try:
        return OpenAIAgentsRuntime(
            model=model,
            api_key=api_key,
            base_url=base_url or "https://api.openai.com/v1",
            endpoint_type=endpoint_type,
            domain_tools_factory=domain_tools_factory,
            session_provider=session_provider,
        )
    except (ImportError, ModuleNotFoundError):
        return UnavailableAgentRuntime(
            AgentRuntimeAvailability(
                True,
                False,
                model,
                endpoint_type,
                "OpenAI Agents SDK optional dependency is not installed.",
            )
        )
