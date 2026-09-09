"""Deterministic, framework-neutral execution state for orchestration research."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, model_validator

from server.core.agent.models import AgentToolName, ContractModel


ExecutionEventKind = Literal[
    "initialized",
    "candidates_submitted",
    "call_attempted",
    "call_intercepted",
    "result_validated",
    "result_failed",
    "generation_changed",
    "cancelled",
    "budget_terminated",
    "final_accepted",
    "final_rejected",
]
ExecutionTerminalStatus = Literal[
    "running",
    "succeeded",
    "failed",
    "cancelled",
    "aborted",
]


class ResourceSnapshot(ContractModel):
    total_calls_used: int = Field(default=0, ge=0)
    total_calls_remaining: int = Field(default=0, ge=0)
    engine_calls_used: int = Field(default=0, ge=0)
    engine_calls_remaining: int = Field(default=0, ge=0)
    deadline_ms_remaining: int | None = Field(default=None, ge=0)
    usage: dict[str, int | float | None] = Field(default_factory=dict)


class ToolObservation(ContractModel):
    tool: AgentToolName
    artifact_kind: str = Field(min_length=1)
    result_ref: str = Field(min_length=1)
    item_count: int | None = Field(default=None, ge=0)
    skill_ids: list[str] = Field(default_factory=list, max_length=10)
    reference_ids: list[str] = Field(default_factory=list, max_length=10)
    evidence_refs: list[str] = Field(default_factory=list, max_length=20)


class ExecutionAttempt(ContractModel):
    call_id: str = Field(min_length=1)
    tool: AgentToolName
    arguments_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    snapshot_id: str | None = None
    generation: int = Field(ge=0)
    status: Literal["attempted", "intercepted", "ok", "error", "stale"]
    result_ref: str | None = None
    error_category: str | None = None
    cache_hit: bool = False
    engine_calls: int = Field(default=0, ge=0)
    redundant: bool = False


class RecoveryState(ContractModel):
    last_error_category: str | None = None
    failed_arguments_fingerprints: list[str] = Field(default_factory=list)
    alternative_attempts: int = Field(default=0, ge=0)
    no_progress_count: int = Field(default=0, ge=0)


class ExecutionState(ContractModel):
    schema_version: int = Field(default=1, ge=1)
    run_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    generation: int = Field(ge=0)
    revision: int = Field(default=0, ge=0)
    last_event_sequence: int = Field(default=0, ge=0)
    applied_event_ids: list[str] = Field(default_factory=list)
    user_goal: str = Field(min_length=1)
    plan: str | None = None
    subgoal: str | None = None
    position_fen: str | None = None
    position_reference_ids: list[str] = Field(default_factory=list)
    personalization_enabled: bool = False
    has_engine_facts: bool = False
    attempts: list[ExecutionAttempt] = Field(default_factory=list)
    observations: list[ToolObservation] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    completed_artifacts: list[str] = Field(default_factory=list)
    missing_artifacts: list[str] = Field(default_factory=list)
    recovery: RecoveryState = Field(default_factory=RecoveryState)
    resources: ResourceSnapshot = Field(default_factory=ResourceSnapshot)
    terminal_status: ExecutionTerminalStatus = "running"
    terminal_reason: str | None = None
    decision_ids: list[str] = Field(default_factory=list)


class ExecutionEvent(ContractModel):
    event_id: str = Field(min_length=1)
    sequence: int = Field(gt=0)
    kind: ExecutionEventKind
    run_id: str | None = None
    session_id: str | None = None
    generation: int | None = Field(default=None, ge=0)
    user_goal: str | None = None
    position_fen: str | None = None
    position_reference_ids: list[str] = Field(default_factory=list)
    personalization_enabled: bool | None = None
    has_engine_facts: bool | None = None
    missing_artifacts: list[str] = Field(default_factory=list)
    decision_id: str | None = None
    call_id: str | None = None
    tool: AgentToolName | None = None
    arguments_fingerprint: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    snapshot_id: str | None = None
    observation: ToolObservation | None = None
    result_ref: str | None = None
    error_category: str | None = None
    cache_hit: bool = False
    engine_calls: int = Field(default=0, ge=0)
    redundant: bool = False
    resources: ResourceSnapshot | None = None
    reason: str | None = None

    @model_validator(mode="after")
    def _required_fields_for_kind(self) -> "ExecutionEvent":
        if self.kind == "initialized" and not all(
            (self.run_id, self.session_id, self.user_goal, self.generation is not None)
        ):
            raise ValueError("initialized events require run, session, generation, and goal")
        if self.kind == "candidates_submitted" and not self.decision_id:
            raise ValueError("candidate events require decision_id")
        if self.kind in {"call_attempted", "call_intercepted"} and not all(
            (self.call_id, self.tool, self.arguments_fingerprint, self.generation is not None)
        ):
            raise ValueError("call events require call, tool, arguments, and generation")
        if self.kind in {"result_validated", "result_failed"} and not self.call_id:
            raise ValueError("result events require call_id")
        if self.kind == "result_validated" and not all((self.observation, self.result_ref)):
            raise ValueError("validated results require an observation and result_ref")
        if self.kind == "generation_changed" and self.generation is None:
            raise ValueError("generation changes require generation")
        return self


def _replace_attempt(
    attempts: list[ExecutionAttempt], call_id: str, **updates: Any
) -> list[ExecutionAttempt]:
    found = False
    replaced: list[ExecutionAttempt] = []
    for attempt in attempts:
        if attempt.call_id != call_id:
            replaced.append(attempt)
            continue
        replaced.append(attempt.model_copy(update=updates))
        found = True
    if not found:
        raise ValueError(f"result references unknown call_id: {call_id}")
    return replaced


def project(state: ExecutionState | None, event: ExecutionEvent) -> ExecutionState:
    """Apply one explicit event without reading clocks, storage, environment, or labels."""

    if state is None:
        if event.kind != "initialized":
            raise ValueError("the first execution event must initialize the state")
        assert event.run_id is not None
        assert event.session_id is not None
        assert event.generation is not None
        assert event.user_goal is not None
        return ExecutionState(
            run_id=event.run_id,
            session_id=event.session_id,
            generation=event.generation,
            last_event_sequence=event.sequence,
            applied_event_ids=[event.event_id],
            user_goal=event.user_goal,
            position_fen=event.position_fen,
            position_reference_ids=list(event.position_reference_ids),
            personalization_enabled=bool(event.personalization_enabled),
            has_engine_facts=bool(event.has_engine_facts),
            missing_artifacts=list(dict.fromkeys(event.missing_artifacts)),
            resources=event.resources or ResourceSnapshot(),
        )

    if event.event_id in state.applied_event_ids:
        return state.model_copy(deep=True)
    if event.sequence <= state.last_event_sequence:
        raise ValueError("new execution events must have an increasing sequence")

    updates: dict[str, Any] = {
        "last_event_sequence": event.sequence,
        "applied_event_ids": [*state.applied_event_ids, event.event_id],
    }
    if event.resources is not None:
        updates["resources"] = event.resources

    if event.kind == "candidates_submitted":
        updates["decision_ids"] = [*state.decision_ids, str(event.decision_id)]
        return state.model_copy(update=updates, deep=True)

    updates["revision"] = state.revision + 1
    if event.kind in {"call_attempted", "call_intercepted"}:
        assert event.call_id is not None
        assert event.tool is not None
        assert event.arguments_fingerprint is not None
        assert event.generation is not None
        updates["attempts"] = [
            *state.attempts,
            ExecutionAttempt(
                call_id=event.call_id,
                tool=event.tool,
                arguments_fingerprint=event.arguments_fingerprint,
                snapshot_id=event.snapshot_id,
                generation=event.generation,
                status="attempted" if event.kind == "call_attempted" else "intercepted",
                error_category=event.error_category,
            ),
        ]
        if event.kind == "call_intercepted":
            updates["recovery"] = state.recovery.model_copy(
                update={
                    "last_error_category": event.error_category or "routing_intercepted",
                    "failed_arguments_fingerprints": [
                        *state.recovery.failed_arguments_fingerprints,
                        event.arguments_fingerprint,
                    ],
                    "no_progress_count": state.recovery.no_progress_count + 1,
                }
            )
    elif event.kind == "result_validated":
        assert event.call_id is not None
        assert event.observation is not None
        assert event.result_ref is not None
        stale = event.generation is not None and event.generation != state.generation
        updates["attempts"] = _replace_attempt(
            state.attempts,
            event.call_id,
            status="stale" if stale else "ok",
            result_ref=event.result_ref,
            cache_hit=event.cache_hit,
            engine_calls=event.engine_calls,
            redundant=event.redundant,
            error_category="stale_generation" if stale else None,
        )
        if stale:
            updates["recovery"] = state.recovery.model_copy(
                update={
                    "last_error_category": "stale_generation",
                    "no_progress_count": state.recovery.no_progress_count + 1,
                }
            )
        else:
            new_evidence = list(
                dict.fromkeys([*state.evidence_refs, *event.observation.evidence_refs])
            )
            new_artifacts = list(
                dict.fromkeys([*state.completed_artifacts, event.observation.artifact_kind])
            )
            progressed = (
                len(new_evidence) > len(state.evidence_refs)
                or len(new_artifacts) > len(state.completed_artifacts)
            )
            redundant = event.redundant or not progressed
            updates["attempts"] = _replace_attempt(
                updates["attempts"], event.call_id, redundant=redundant
            )
            updates["observations"] = [*state.observations, event.observation]
            updates["evidence_refs"] = new_evidence
            updates["completed_artifacts"] = new_artifacts
            updates["missing_artifacts"] = [
                item for item in state.missing_artifacts if item != event.observation.artifact_kind
            ]
            updates["recovery"] = state.recovery.model_copy(
                update={
                    "no_progress_count": (
                        state.recovery.no_progress_count + 1 if redundant else 0
                    )
                }
            )
    elif event.kind == "result_failed":
        assert event.call_id is not None
        attempted = next(
            (item for item in state.attempts if item.call_id == event.call_id), None
        )
        updates["attempts"] = _replace_attempt(
            state.attempts,
            event.call_id,
            status="error",
            error_category=event.error_category or "unknown",
            cache_hit=event.cache_hit,
            engine_calls=event.engine_calls,
        )
        failed_fingerprints = list(state.recovery.failed_arguments_fingerprints)
        if attempted is not None:
            failed_fingerprints.append(attempted.arguments_fingerprint)
        updates["recovery"] = state.recovery.model_copy(
            update={
                "last_error_category": event.error_category or "unknown",
                "failed_arguments_fingerprints": failed_fingerprints,
                "alternative_attempts": state.recovery.alternative_attempts + 1,
                "no_progress_count": state.recovery.no_progress_count + 1,
            }
        )
    elif event.kind == "generation_changed":
        assert event.generation is not None
        updates["generation"] = event.generation
    elif event.kind == "cancelled":
        updates.update(terminal_status="cancelled", terminal_reason=event.reason or "cancelled")
    elif event.kind == "budget_terminated":
        updates.update(terminal_status="aborted", terminal_reason=event.reason or "budget")
    elif event.kind == "final_accepted":
        updates.update(terminal_status="succeeded", terminal_reason=None)
    elif event.kind == "final_rejected":
        updates.update(terminal_status="failed", terminal_reason=event.reason or "rejected")
    return state.model_copy(update=updates, deep=True)
