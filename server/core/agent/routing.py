"""Authorization and immutable candidate snapshots for orchestration research."""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
import hashlib
import inspect
import json
from typing import Any, Protocol

from pydantic import BaseModel, Field

from server.core.agent.execution_state import (
    ExecutionEvent,
    ExecutionState,
    ResourceSnapshot,
    ToolObservation,
    project,
)
from server.core.agent.models import (
    AGENT_TOOL_PERMISSIONS,
    AgentRunRequest,
    AgentToolName,
    AnalyzeMoveInput,
    AnalyzeMoveResult,
    AnalyzePositionInput,
    AnalyzePositionResult,
    ContractModel,
    CreateTrainingDraftInput,
    GetPlayerProfileInput,
    GetPlayerProfileResult,
    GetReviewContextInput,
    GetReviewContextResult,
    GetTrainingCandidatesInput,
    GetTrainingCandidatesResult,
    LookupOpeningInput,
    LookupOpeningResult,
    ToolError,
    ToolResult,
    TrainingDraft,
)


@dataclass(frozen=True)
class ToolCapability:
    name: AgentToolName
    input_type: type[BaseModel]
    result_type: type[BaseModel]
    purpose: str
    prerequisites: tuple[str, ...]
    artifact_kind: str
    cost_category: str


TOOL_CAPABILITIES: tuple[ToolCapability, ...] = (
    ToolCapability("get_review_context", GetReviewContextInput, GetReviewContextResult, "Load saved Engine facts for the selected critical position.", ("owned_review_reference",), "review_facts", "read"),
    ToolCapability("analyze_position", AnalyzePositionInput, AnalyzePositionResult, "Analyze bounded candidates for the current FEN.", ("current_position",), "position_analysis", "engine_conditional"),
    ToolCapability("analyze_move", AnalyzeMoveInput, AnalyzeMoveResult, "Check one what-if move from the current FEN.", ("current_position",), "move_analysis", "engine_conditional"),
    ToolCapability("lookup_opening", LookupOpeningInput, LookupOpeningResult, "Read local opening metadata for the current checkpoint.", ("current_position",), "opening_metadata", "read"),
    ToolCapability("get_player_profile", GetPlayerProfileInput, GetPlayerProfileResult, "Read bounded evidence-backed profile items.", ("personalization",), "profile", "read"),
    ToolCapability("get_training_candidates", GetTrainingCandidatesInput, GetTrainingCandidatesResult, "Retrieve verified training positions.", ("personalization",), "training_candidates", "read"),
    ToolCapability("create_training_draft", CreateTrainingDraftInput, TrainingDraft, "Create a temporary draft from retrieved positions.", ("personalization", "training_candidates"), "training_draft", "compute"),
)
CAPABILITY_BY_NAME = {item.name: item for item in TOOL_CAPABILITIES}


class RoutingView(ContractModel):
    state_revision: int = Field(ge=0)
    generation: int = Field(ge=0)
    upper_bound: list[AgentToolName]
    personalization_enabled: bool
    has_position: bool
    has_owned_review_reference: bool
    has_engine_facts: bool
    successful_tools: list[AgentToolName]
    training_candidate_count: int | None = Field(default=None, ge=0)
    resources: ResourceSnapshot
    terminal: bool


class ToolCandidateProvider(Protocol):
    def candidates(
        self,
        view: RoutingView,
        authorized: Sequence[ToolCapability],
    ) -> Sequence[AgentToolName] | Awaitable[Sequence[AgentToolName]]: ...


class FixedCandidateProvider:
    """Small deterministic provider used by P0 wiring and replay tests."""

    def __init__(self, candidates: Sequence[AgentToolName], *, version: str = "fixed-v1") -> None:
        self._candidates = tuple(candidates)
        self.version = version
        self.calls = 0

    def candidates(
        self, view: RoutingView, authorized: Sequence[ToolCapability]
    ) -> Sequence[AgentToolName]:
        del view, authorized
        self.calls += 1
        return self._candidates


class SequencedCandidateProvider:
    """Return a preset sequence by decision, without inspecting fixtures or gold."""

    def __init__(
        self,
        decisions: Sequence[Sequence[AgentToolName]],
        *,
        version: str = "sequenced-v1",
    ) -> None:
        self._decisions = [tuple(item) for item in decisions]
        self.version = version
        self.calls = 0

    def candidates(
        self, view: RoutingView, authorized: Sequence[ToolCapability]
    ) -> Sequence[AgentToolName]:
        del view, authorized
        index = self.calls
        self.calls += 1
        return self._decisions[index] if index < len(self._decisions) else ()


class CandidateSnapshot(ContractModel):
    decision_id: str = Field(min_length=1)
    state_revision: int = Field(ge=0)
    authorized_tools: list[AgentToolName]
    candidate_tools: list[AgentToolName]
    filtered_reasons: dict[str, str] = Field(default_factory=dict)
    retrieval_version: str = Field(min_length=1)
    cumulative_exposed_tools: list[AgentToolName]
    fallback_of: str | None = None


def routing_view(request: AgentRunRequest, state: ExecutionState) -> RoutingView:
    position = request.model_context.position
    reference = position.reference if position is not None else None
    candidate_counts = [
        item.item_count
        for item in state.observations
        if item.tool == "get_training_candidates" and item.item_count is not None
    ]
    return RoutingView(
        state_revision=state.revision,
        generation=state.generation,
        upper_bound=list(request.allowed_tools),
        personalization_enabled=state.personalization_enabled,
        has_position=position is not None,
        has_owned_review_reference=bool(
            reference
            and reference.game_id
            and reference.review_side
            and reference.critical_id
        ),
        has_engine_facts=state.has_engine_facts or "review_facts" in state.completed_artifacts,
        successful_tools=[item.tool for item in state.observations],
        training_candidate_count=candidate_counts[-1] if candidate_counts else None,
        resources=state.resources,
        terminal=state.terminal_status != "running",
    )


def authorized_capabilities(view: RoutingView) -> tuple[ToolCapability, ...]:
    """Compute the query-independent P0 authorization subset for the current state."""

    if view.terminal or view.resources.total_calls_remaining <= 0:
        return ()
    authorized: list[ToolCapability] = []
    for capability in TOOL_CAPABILITIES:
        name = capability.name
        if name not in view.upper_bound:
            continue
        if name == "get_review_context" and (
            not view.has_owned_review_reference or view.has_engine_facts
        ):
            continue
        if name in {"analyze_position", "analyze_move", "lookup_opening"} and not view.has_position:
            continue
        if name in {
            "get_player_profile",
            "get_training_candidates",
            "create_training_draft",
        } and not view.personalization_enabled:
            continue
        if name == "create_training_draft" and not view.training_candidate_count:
            continue
        authorized.append(capability)
    return tuple(authorized)


class SnapshotManager:
    def __init__(
        self,
        provider: ToolCandidateProvider,
        *,
        allow_fallback: bool = True,
    ) -> None:
        self.provider = provider
        self.allow_fallback = allow_fallback
        self.snapshots: list[CandidateSnapshot] = []
        self.computation_count = 0
        self._fallback_used = False
        self._cached_by_revision: dict[int, CandidateSnapshot] = {}
        self._cumulative: list[AgentToolName] = []
        self._lock = asyncio.Lock()

    async def get(self, view: RoutingView) -> CandidateSnapshot:
        cached = self._cached_by_revision.get(view.state_revision)
        if cached is not None:
            return cached
        async with self._lock:
            cached = self._cached_by_revision.get(view.state_revision)
            if cached is not None:
                return cached
            self.computation_count += 1
            authorized = authorized_capabilities(view)
            authorized_names = [item.name for item in authorized]
            filtered: dict[str, str] = {}
            failed = False
            try:
                proposed = self.provider.candidates(view, authorized)
                if inspect.isawaitable(proposed):
                    proposed = await proposed
            except Exception:
                proposed = ()
                failed = True
            candidates: list[AgentToolName] = []
            for name in proposed:
                if name not in CAPABILITY_BY_NAME:
                    filtered[str(name)] = "unknown_tool"
                elif name not in authorized_names:
                    filtered[name] = "unauthorized"
                elif name not in candidates:
                    candidates.append(name)
            for name in authorized_names:
                if name not in candidates:
                    filtered.setdefault(name, "not_selected")
            retrieval_version = str(getattr(self.provider, "version", "candidate-provider-v1"))
            initial_id = f"decision-{len(self.snapshots) + 1}"
            initial = CandidateSnapshot(
                decision_id=initial_id,
                state_revision=view.state_revision,
                authorized_tools=authorized_names,
                candidate_tools=candidates,
                filtered_reasons=filtered,
                retrieval_version=retrieval_version,
                cumulative_exposed_tools=list(self._cumulative),
            )
            self.snapshots.append(initial)
            selected = initial
            if (
                self.allow_fallback
                and not self._fallback_used
                and not candidates
                and authorized_names
            ):
                self._fallback_used = True
                fallback_id = f"decision-{len(self.snapshots) + 1}"
                for name in authorized_names:
                    if name not in self._cumulative:
                        self._cumulative.append(name)
                selected = CandidateSnapshot(
                    decision_id=fallback_id,
                    state_revision=view.state_revision,
                    authorized_tools=authorized_names,
                    candidate_tools=authorized_names,
                    filtered_reasons={
                        "candidate_provider": "error" if failed else "empty"
                    },
                    retrieval_version=f"{retrieval_version}:fallback",
                    cumulative_exposed_tools=list(self._cumulative),
                    fallback_of=initial_id,
                )
                self.snapshots.append(selected)
            else:
                for name in candidates:
                    if name not in self._cumulative:
                        self._cumulative.append(name)
                selected = selected.model_copy(
                    update={"cumulative_exposed_tools": list(self._cumulative)}
                )
                self.snapshots[-1] = selected
            self._cached_by_revision[view.state_revision] = selected
            return selected


def arguments_fingerprint(payload: BaseModel) -> str:
    encoded = json.dumps(
        payload.model_dump(mode="json", exclude_none=True),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _reference_fingerprint(value: Any) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json", exclude_none=True)
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:16]


def observation_from_result(
    name: AgentToolName,
    result: ToolResult[Any],
    result_ref: str,
) -> ToolObservation:
    if not result.ok or result.data is None:
        raise ValueError("only successful typed tool results become observations")
    data = result.data
    dumped = data.model_dump(mode="json", exclude_none=True)
    item_count: int | None = None
    skill_ids: list[str] = []
    references: list[Any] = []
    if name == "get_player_profile":
        estimates = dumped.get("relevant_estimates", [])
        item_count = len(estimates)
        skill_ids.extend(item.get("skill_id") for item in estimates if item.get("skill_id"))
        references.extend(example for item in estimates for example in item.get("examples", []))
    elif name == "get_training_candidates":
        candidates = dumped.get("candidates", [])
        item_count = len(candidates)
        skill_ids.extend(skill for item in candidates for skill in item.get("skill_ids", []))
        references.extend(item.get("reference") for item in candidates if item.get("reference"))
    elif name == "create_training_draft":
        references.extend(dumped.get("position_references", []))
        skill_ids.extend(dumped.get("objective_skill_ids", []))
        item_count = len(references)
    elif dumped.get("reference"):
        references.append(dumped["reference"])
    elif dumped.get("fen") or dumped.get("fen_before"):
        references.append({"fen": dumped.get("fen") or dumped.get("fen_before")})
    return ToolObservation(
        tool=name,
        artifact_kind=CAPABILITY_BY_NAME[name].artifact_kind,
        result_ref=result_ref,
        item_count=item_count,
        skill_ids=list(dict.fromkeys(skill_ids))[:10],
        reference_ids=[_reference_fingerprint(item) for item in references[:10]],
        evidence_refs=list(result.evidence_refs)[:20],
    )


class OrchestrationController:
    """Own one run's state, events, snapshots, and per-call authorization checks."""

    def __init__(
        self,
        request: AgentRunRequest,
        provider: ToolCandidateProvider,
        *,
        allow_fallback: bool = True,
        generation_is_current: Callable[[], bool] | None = None,
        missing_artifacts: Sequence[str] = (),
    ) -> None:
        self.request = request
        self.snapshots = SnapshotManager(provider, allow_fallback=allow_fallback)
        self.generation_is_current = generation_is_current
        self.events: list[ExecutionEvent] = []
        self._sequence = 0
        self._call_count = 0
        self._active_snapshot: CandidateSnapshot | None = None
        position = request.model_context.position
        references = []
        if position is not None and position.reference is not None:
            references.append(_reference_fingerprint(position.reference))
        self.state: ExecutionState | None = None
        self._apply(
            ExecutionEvent(
                event_id="event-1",
                sequence=1,
                kind="initialized",
                run_id=request.run_id,
                session_id=request.session_id,
                generation=request.expected_generation,
                user_goal=request.message,
                position_fen=position.fen if position is not None else None,
                position_reference_ids=references,
                personalization_enabled=request.model_context.task.personalization_enabled,
                has_engine_facts=request.model_context.engine_facts is not None,
                missing_artifacts=list(missing_artifacts),
                resources=ResourceSnapshot(
                    total_calls_remaining=request.max_total_tool_calls,
                    engine_calls_remaining=request.max_engine_tool_calls,
                    deadline_ms_remaining=request.timeout_seconds * 1000,
                ),
            )
        )

    def _next_sequence(self) -> int:
        return self._sequence + 1

    @property
    def active_snapshot(self) -> CandidateSnapshot | None:
        return self._active_snapshot

    def _apply(self, event: ExecutionEvent) -> None:
        self.state = project(self.state, event)
        self.events.append(event)
        self._sequence = event.sequence

    def _resources(self, total_used: int, engine_used: int) -> ResourceSnapshot:
        return ResourceSnapshot(
            total_calls_used=total_used,
            total_calls_remaining=max(0, self.request.max_total_tool_calls - total_used),
            engine_calls_used=engine_used,
            engine_calls_remaining=max(0, self.request.max_engine_tool_calls - engine_used),
            deadline_ms_remaining=self.state.resources.deadline_ms_remaining if self.state else None,
            usage=dict(self.state.resources.usage) if self.state else {},
        )

    async def is_enabled(self, name: AgentToolName) -> bool:
        assert self.state is not None
        snapshot = await self.snapshots.get(routing_view(self.request, self.state))
        if self._active_snapshot is None or self._active_snapshot.decision_id != snapshot.decision_id:
            self._active_snapshot = snapshot
            self._apply(
                ExecutionEvent(
                    event_id=f"event-{self._next_sequence()}",
                    sequence=self._next_sequence(),
                    kind="candidates_submitted",
                    decision_id=snapshot.decision_id,
                )
            )
        return name in snapshot.candidate_tools

    async def before_call(
        self,
        name: AgentToolName,
        payload: BaseModel,
        *,
        total_used: int,
        engine_used: int,
    ) -> tuple[str, ToolError | None]:
        assert self.state is not None
        if self._active_snapshot is None:
            await self.is_enabled(name)
        assert self._active_snapshot is not None
        self._call_count += 1
        call_id = f"call-{self._call_count}"
        fingerprint = arguments_fingerprint(payload)
        error: ToolError | None = None
        category: str | None = None
        if self.generation_is_current is not None and not self.generation_is_current():
            category = "stale_generation"
        elif name not in self._active_snapshot.authorized_tools:
            category = "unauthorized"
        elif name not in self._active_snapshot.candidate_tools:
            category = "outside_snapshot"
        if category is not None:
            error = ToolError(
                code=(
                    "profile_unavailable"
                    if name == "get_player_profile"
                    else "training_unavailable"
                    if name in {"get_training_candidates", "create_training_draft"}
                    else "position_not_found"
                ),
                message="The tool is not available for the current orchestration decision.",
                recoverable=False,
            )
        self._apply(
            ExecutionEvent(
                event_id=f"event-{self._next_sequence()}",
                sequence=self._next_sequence(),
                kind="call_intercepted" if error else "call_attempted",
                call_id=call_id,
                tool=name,
                arguments_fingerprint=fingerprint,
                snapshot_id=self._active_snapshot.decision_id,
                generation=self.state.generation,
                error_category=category,
                resources=self._resources(total_used, engine_used),
            )
        )
        return call_id, error

    def after_call(
        self,
        call_id: str,
        name: AgentToolName,
        payload: BaseModel,
        result: ToolResult[Any],
        *,
        cache_hit: bool,
        engine_calls: int,
        total_used: int,
        engine_used: int,
        error_category: str | None = None,
    ) -> None:
        assert self.state is not None
        attempted = next(item for item in self.state.attempts if item.call_id == call_id)
        generation = attempted.generation
        stale = self.generation_is_current is not None and not self.generation_is_current()
        if stale:
            generation = self.state.generation + 1
        fingerprint = arguments_fingerprint(payload)
        redundant = any(
            item.status == "ok"
            and item.tool == name
            and item.arguments_fingerprint == fingerprint
            for item in self.state.attempts
            if item.call_id != call_id
        )
        if result.ok:
            result_ref = f"result-{call_id}"
            self._apply(
                ExecutionEvent(
                    event_id=f"event-{self._next_sequence()}",
                    sequence=self._next_sequence(),
                    kind="result_validated",
                    call_id=call_id,
                    generation=generation,
                    observation=observation_from_result(name, result, result_ref),
                    result_ref=result_ref,
                    cache_hit=cache_hit,
                    engine_calls=engine_calls,
                    redundant=redundant,
                    resources=self._resources(total_used, engine_used),
                )
            )
            return
        self._apply(
            ExecutionEvent(
                event_id=f"event-{self._next_sequence()}",
                sequence=self._next_sequence(),
                kind="result_failed",
                call_id=call_id,
                error_category=(
                    error_category
                    or (result.error.code if result.error is not None else "unknown")
                ),
                cache_hit=cache_hit,
                engine_calls=engine_calls,
                resources=self._resources(total_used, engine_used),
            )
        )

    def generation_changed(self, generation: int) -> None:
        self._apply(
            ExecutionEvent(
                event_id=f"event-{self._next_sequence()}",
                sequence=self._next_sequence(),
                kind="generation_changed",
                generation=generation,
            )
        )

    def record_intercepted_proposal(
        self,
        name: AgentToolName,
        arguments_fingerprint: str,
        snapshot_id: str,
        *,
        error_category: str = "outside_snapshot",
    ) -> None:
        assert self.state is not None
        self._call_count += 1
        self._apply(
            ExecutionEvent(
                event_id=f"event-{self._next_sequence()}",
                sequence=self._next_sequence(),
                kind="call_intercepted",
                call_id=f"call-{self._call_count}",
                tool=name,
                arguments_fingerprint=arguments_fingerprint,
                snapshot_id=snapshot_id,
                generation=self.state.generation,
                error_category=error_category,
                resources=self.state.resources,
            )
        )

    def terminate(self, kind: str, *, reason: str | None = None) -> None:
        assert self.state is not None
        if self.state.terminal_status != "running":
            return
        mapped = {
            "cancelled": "cancelled",
            "budget": "budget_terminated",
            "accepted": "final_accepted",
            "rejected": "final_rejected",
        }[kind]
        self._apply(
            ExecutionEvent.model_validate(
                {
                    "event_id": f"event-{self._next_sequence()}",
                    "sequence": self._next_sequence(),
                    "kind": mapped,
                    "reason": reason,
                }
            )
        )

    def trace(self) -> dict[str, Any]:
        assert self.state is not None
        return {
            "schema_version": 1,
            "state": self.state.model_dump(mode="json"),
            "events": [item.model_dump(mode="json", exclude_none=True) for item in self.events],
            "snapshots": [
                item.model_dump(mode="json", exclude_none=True)
                for item in self.snapshots.snapshots
            ],
            "candidate_computations": self.snapshots.computation_count,
        }


def tool_permission(name: AgentToolName) -> str:
    return AGENT_TOOL_PERMISSIONS[name]
