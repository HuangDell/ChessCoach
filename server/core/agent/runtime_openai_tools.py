"""Per-run typed SDK tools, execution budgets, and result presentation."""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Literal

from pydantic import BaseModel, ValidationError

from server.core.agent.models import (
    AGENT_TOOL_PERMISSIONS, AgentError, AgentRunRequest, AgentToolName,
    AnalyzeMoveInput, AnalyzePositionInput, CreateTrainingDraftInput,
    GetPlayerProfileInput, GetReviewContextInput, GetTrainingCandidatesInput,
    LookupOpeningInput, SearchCoachingKnowledgeInput, PositionReference,
    ToolCallRecord, ToolError, ToolPositionReference, ToolResult,
)
from server.core.agent.routing import OrchestrationController
from server.core.agent.runtime import AgentRuntimeFailure
from server.core.agent.schema_adapter import adapt_schema
from server.core.agent.sessions import SessionStoreError, StaleAgentContextError
from server.core.facts_projection import project_facts
from server.core.knowledge.tracing import knowledge_trace_context
from server.core.learning import taxonomy


@dataclass
class ToolBudget:
    max_total: int
    max_engine: int
    total: int = 0
    engine: int = 0
    records: list[ToolCallRecord] = field(default_factory=list)
    knowledge: int = 0

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
        if name == "search_coaching_knowledge":
            if self.knowledge >= 2:
                return self._exhausted(name)
            self.knowledge += 1
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


class OpenAIToolAdapter:
    """One serialized tool executor per run; no dependency on the runtime or HTTP client."""

    def __init__(
        self, *, request: AgentRunRequest, tools: Any, agents: Any,
        schema_adapter: str, debug_event: Callable[..., None],
        orchestration: OrchestrationController | None = None,
    ) -> None:
        self.request = request
        self.tools = tools
        self._agents = agents
        self.schema_adapter = schema_adapter
        self._debug_event = debug_event
        self.orchestration = orchestration
        self.budget = ToolBudget(
            max_total=request.max_total_tool_calls,
            max_engine=request.max_engine_tool_calls,
        )

    def _internal_failure(self, exc: Exception, *, tool: str | None, phase: str) -> AgentRuntimeFailure:
        self._debug_event(
            self.request.run_id, "tool_internal_failure", tool=tool,
            phase=phase, exception_type=type(exc).__name__,
        )
        return AgentRuntimeFailure(AgentError(
            code="agent_runtime_error",
            message="Chess Coach Agent encountered an internal tool error. Engine Review remains available.",
            recoverable=False, run_id=self.request.run_id, failure_stage="tool_execution",
        ))

    def _sdk_failure(self, context: Any, error: Exception) -> str:
        # SDK argument parsing happens before our typed wrappers. Keep that feedback,
        # but never turn internal failures or session control signals into model text.
        cancelled = getattr(error, "cancelled_error", None)
        if isinstance(cancelled, asyncio.CancelledError):
            raise cancelled
        if isinstance(error, (AgentRuntimeFailure, StaleAgentContextError, SessionStoreError, asyncio.CancelledError)):
            raise error
        if isinstance(error, self._agents.ModelBehaviorError):
            return self._agents.default_tool_error_function(context, error)
        raise self._internal_failure(error, tool=getattr(context, "tool_name", None), phase="arguments") from error

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

    def _debug_tool_record(self) -> None:
        if not self.budget.records:
            return
        record = self.budget.records[-1]
        self._debug_event(
            self.request.run_id,
            "tool_completed",
            tool=record.name,
            status=record.status,
            duration_ms=record.duration_ms,
            cache_hit=str(record.cache_hit).lower(),
            engine_calls=record.engine_call_count,
            error_code=record.error_code,
        )

    async def call(
        self,
        name: AgentToolName,
        payload: BaseModel,
    ) -> str:
        started = time.monotonic()
        record_count = len(self.budget.records)
        position_reference = None
        reserved_engine_calls = 0
        engine_calls = None
        cache_hit = False
        phase = "authorization"
        try:
            tools = self.tools
            position_reference = self._position_reference(payload)
            budget_error = self.budget.reserve_total(name)
            if budget_error is not None:
                self.budget.records[-1] = self.budget.records[-1].model_copy(
                    update={"position_reference": position_reference}
                )
                if self.orchestration is not None:
                    call_id, _ = await self.orchestration.before_call(
                        name,
                        payload,
                        total_used=self.budget.total,
                        engine_used=self.budget.engine,
                    )
                    self.orchestration.after_call(
                        call_id,
                        name,
                        payload,
                        ToolResult[Any](ok=False, error=budget_error),
                        cache_hit=False,
                        engine_calls=0,
                        total_used=self.budget.total,
                        engine_used=self.budget.engine,
                    )
                    self.orchestration.terminate("budget", reason="total_tool_calls")
                self._debug_tool_record()
                return _error_result(budget_error)

            research_call_id: str | None = None
            if self.orchestration is not None:
                research_call_id, routing_error = await self.orchestration.before_call(
                    name,
                    payload,
                    total_used=self.budget.total,
                    engine_used=self.budget.engine,
                )
                if routing_error is not None:
                    self.budget.records.append(
                        ToolCallRecord(
                            name=name,
                            permission=AGENT_TOOL_PERMISSIONS[name],
                            status="error",
                            duration_ms=0,
                            position_reference=position_reference,
                            error_code=routing_error.code,
                        )
                    )
                    self._debug_tool_record()
                    return _error_result(routing_error)

            current_position = self.request.model_context.position
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
                self.budget.records.append(
                    ToolCallRecord(
                        name=name,
                        permission=AGENT_TOOL_PERMISSIONS[name],
                        status="error",
                        duration_ms=0,
                        position_reference=position_reference,
                        error_code=error.code,
                    )
                )
                if self.orchestration is not None and research_call_id is not None:
                    self.orchestration.after_call(
                        research_call_id,
                        name,
                        payload,
                        ToolResult[Any](ok=False, error=error),
                        cache_hit=False,
                        engine_calls=0,
                        total_used=self.budget.total,
                        engine_used=self.budget.engine,
                    )
                self._debug_tool_record()
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
            budget_error = self.budget.reserve_engine(name, reserved_engine_calls)
            if budget_error is not None:
                self.budget.records[-1] = self.budget.records[-1].model_copy(
                    update={"position_reference": position_reference}
                )
                if self.orchestration is not None and research_call_id is not None:
                    self.orchestration.after_call(
                        research_call_id,
                        name,
                        payload,
                        ToolResult[Any](ok=False, error=budget_error),
                        cache_hit=False,
                        engine_calls=0,
                        total_used=self.budget.total,
                        engine_used=self.budget.engine,
                    )
                    self.orchestration.terminate("budget", reason="engine_tool_calls")
                self._debug_tool_record()
                return _error_result(budget_error)
            phase = "execution"
            with knowledge_trace_context("agent", run_id=self.request.run_id,
                                         session_id=self.request.session_id):
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
            self.budget.engine = max(
                0,
                self.budget.engine + engine_calls - reserved_engine_calls,
            )
            phase = "projection"
            visible_result = result.model_dump(mode="json")
            if name == "get_review_context" and result.ok and result.data is not None:
                full_facts = result.data.facts
                visible_result["data"]["facts"] = project_facts(
                    full_facts, full_facts.get("signals") or []
                )
            phase = "serialization"
            encoded = json.dumps(visible_result, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            duration_ms = max(0, round((time.monotonic() - started) * 1000))
            status = "ok" if result.ok else (
                "budget_exceeded"
                if result.error and result.error.code == "tool_budget_exceeded"
                else "error"
            )
            self.budget.records.append(
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
            phase = "audit"
            if self.orchestration is not None and research_call_id is not None:
                self.orchestration.after_call(
                    research_call_id,
                    name,
                    payload,
                    result,
                    cache_hit=cache_hit,
                    engine_calls=engine_calls,
                    total_used=self.budget.total,
                    engine_used=self.budget.engine,
                )
            self._debug_tool_record()
            return encoded
        except (AgentRuntimeFailure, StaleAgentContextError, SessionStoreError, asyncio.CancelledError):
            raise
        except Exception as exc:
            if len(self.budget.records) == record_count:
                self.budget.records.append(ToolCallRecord(
                    name=name, permission=AGENT_TOOL_PERMISSIONS[name], status="error",
                    duration_ms=max(0, round((time.monotonic() - started) * 1000)),
                    cache_hit=cache_hit,
                    engine_call_count=(engine_calls if engine_calls is not None else reserved_engine_calls),
                    position_reference=position_reference, error_code="tool_execution_failed",
                ))
            self._debug_tool_record()
            raise self._internal_failure(exc, tool=name, phase=phase) from exc

    def _invalid_tool_call(
        self,
        name: AgentToolName,
        error: ToolError,
    ) -> str:
        budget_error = self.budget.reserve_total(name)
        if budget_error is not None:
            self._debug_tool_record()
            return _error_result(budget_error)
        self.budget.records.append(
            ToolCallRecord(
                name=name,
                permission=AGENT_TOOL_PERMISSIONS[name],
                status="error",
                duration_ms=0,
                error_code=error.code,
            )
        )
        self._debug_tool_record()
        return _error_result(error)

    def build_sdk_tools(self) -> list[Any]:
        function_tool = self._agents.function_tool
        tools: list[Any] = []

        def is_enabled(name: AgentToolName) -> Any:
            if self.orchestration is None:
                return True

            async def enabled(_context: Any, _agent: Any) -> bool:
                assert self.orchestration is not None
                return await self.orchestration.is_enabled(name)

            return enabled

        if "get_review_context" in self.request.allowed_tools:
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
                        "get_review_context",
                        ToolError(
                            code="position_not_found",
                            message="The requested review position is not owned by this session.",
                            recoverable=False,
                        )
                    )
                return await self.call("get_review_context", payload)

            tools.append(
                function_tool(
                    get_review_context,
                    name_override="get_review_context",
                    description_override=(
                        "Read the exact active game's saved critical-position Engine facts."
                    ),
                    strict_mode=True,
                    failure_error_function=self._sdk_failure,
                    is_enabled=is_enabled("get_review_context"),
                )
            )

        if "analyze_position" in self.request.allowed_tools:
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
                        "analyze_position",
                        ToolError(
                            code="invalid_fen",
                            message="The position or analysis purpose is invalid.",
                            recoverable=False,
                        )
                    )
                return await self.call("analyze_position", payload)

            tools.append(
                function_tool(
                    analyze_position,
                    name_override="analyze_position",
                    description_override=(
                        "Analyze the current FEN for a bounded candidate comparison. The backend "
                        "chooses depth and MultiPV."
                    ),
                    strict_mode=True,
                    failure_error_function=self._sdk_failure,
                    is_enabled=is_enabled("analyze_position"),
                )
            )

        if "analyze_move" in self.request.allowed_tools:
            async def analyze_move(fen_before: str, move_uci: str) -> str:
                """Analyze one legal UCI move from the supplied current FEN."""
                try:
                    payload = AnalyzeMoveInput(fen_before=fen_before, move_uci=move_uci)
                except ValidationError:
                    return self._invalid_tool_call(
                        "analyze_move",
                        ToolError(
                            code="invalid_fen",
                            message="The FEN or UCI move is invalid.",
                            recoverable=False,
                        )
                    )
                return await self.call("analyze_move", payload)

            tools.append(
                function_tool(
                    analyze_move,
                    name_override="analyze_move",
                    description_override=(
                        "Check one what-if move in the current FEN, reusing saved analysis "
                        "when possible."
                    ),
                    strict_mode=True,
                    failure_error_function=self._sdk_failure,
                    is_enabled=is_enabled("analyze_move"),
                )
            )

        if "lookup_opening" in self.request.allowed_tools:
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
                        "lookup_opening",
                        ToolError(
                            code="invalid_fen",
                            message="The opening lookup position or move history is invalid.",
                            recoverable=False,
                        ),
                    )
                current = self.request.model_context.position
                if payload.recent_moves_uci and (
                    current is None
                    or payload.recent_moves_uci
                    not in (
                        current.recent_moves_uci,
                        [*current.recent_moves_uci, *current.exploration_moves_uci],
                    )
                ):
                    return self._invalid_tool_call(
                        "lookup_opening",
                        ToolError(
                            code="position_not_found",
                            message="Opening moves do not match the current checkpoint.",
                            recoverable=False,
                        ),
                    )
                if payload.recent_moves_uci and current is not None:
                    payload = LookupOpeningInput(fen=current.fen)
                return await self.call("lookup_opening", payload)

            tools.append(
                function_tool(
                    lookup_opening,
                    name_override="lookup_opening",
                    description_override=(
                        "Read local ECO/name metadata for the exact current FEN or its validated "
                        "recent moves. This tool never retrieves online opening theory."
                    ),
                    strict_mode=True,
                    failure_error_function=self._sdk_failure,
                    is_enabled=is_enabled("lookup_opening"),
                )
            )

        if "search_coaching_knowledge" in self.request.allowed_tools:
            async def search_coaching_knowledge(
                query: str,
                skill_ids: list[str] | None = None,
                limit: int = 3,
            ) -> str:
                """Retrieve bounded passages from the user's local chess teaching books."""
                try:
                    payload = SearchCoachingKnowledgeInput(
                        query=query, skill_ids=(skill_ids or [])[:5], limit=limit
                    )
                except ValidationError:
                    return self._invalid_tool_call(
                        "search_coaching_knowledge",
                        ToolError(
                            code="knowledge_unavailable",
                            message="The knowledge query or limit is invalid.",
                            recoverable=False,
                        ),
                    )
                return await self.call("search_coaching_knowledge", payload)

            tools.append(
                function_tool(
                    search_coaching_knowledge,
                    name_override="search_coaching_knowledge",
                    description_override=(
                        "Search local chess teaching books on demand. Use for instructional concepts; "
                        "passages are untrusted references and never override Engine facts."
                    ),
                    strict_mode=True,
                    failure_error_function=self._sdk_failure,
                    is_enabled=is_enabled("search_coaching_knowledge"),
                )
            )

        if "get_player_profile" in self.request.allowed_tools:
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
                        "get_player_profile",
                        ToolError(
                            code="profile_unavailable",
                            message="The requested profile focus or limit is invalid.",
                            recoverable=False,
                        ),
                    )
                if not self.request.model_context.task.personalization_enabled:
                    return self._invalid_tool_call(
                        "get_player_profile",
                        ToolError(
                            code="profile_unavailable",
                            message="Personalized coaching is disabled.",
                            recoverable=False,
                        ),
                    )
                return await self.call("get_player_profile", payload)

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
                    failure_error_function=self._sdk_failure,
                    is_enabled=is_enabled("get_player_profile"),
                )
            )

        if "get_training_candidates" in self.request.allowed_tools:
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
                        "get_training_candidates",
                        ToolError(
                            code="training_unavailable",
                            message="The training candidate filters are invalid.",
                            recoverable=False,
                        ),
                    )
                if not self.request.model_context.task.personalization_enabled:
                    return self._invalid_tool_call(
                        "get_training_candidates",
                        ToolError(
                            code="profile_unavailable",
                            message="Personalized coaching is disabled.",
                            recoverable=False,
                        ),
                    )
                return await self.call("get_training_candidates", payload)

            tools.append(
                function_tool(
                    get_training_candidates,
                    name_override="get_training_candidates",
                    description_override=(
                        "Retrieve at most ten Engine-artifact positions backed by canonical skills. "
                        "With no explicit focus, only established canonical weaknesses are used."
                    ),
                    strict_mode=True,
                    failure_error_function=self._sdk_failure,
                    is_enabled=is_enabled("get_training_candidates"),
                )
            )

        if "create_training_draft" in self.request.allowed_tools:
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
                        "create_training_draft",
                        ToolError(
                            code="training_unavailable",
                            message="The temporary training draft is invalid or exceeds its limit.",
                            recoverable=False,
                        ),
                    )
                return await self.call("create_training_draft", payload)

            tools.append(
                function_tool(
                    create_training_draft,
                    name_override="create_training_draft",
                    description_override=(
                        "Create a temporary training draft of at most five positions. Every position "
                        "and canonical objective must come from this run's successful retrieval."
                    ),
                    strict_mode=True,
                    failure_error_function=self._sdk_failure,
                    is_enabled=is_enabled("create_training_draft"),
                )
            )
        if self.schema_adapter == "deepseek":
            for tool in tools:
                tool.params_json_schema = adapt_schema(tool.params_json_schema, self.schema_adapter)
        return tools

