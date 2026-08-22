"""Phase 1 Chess Agent service orchestration.

The service owns the transaction boundary around one Agent run: resolve the
authoritative chess context, stage SDK conversation items, validate the project
response, compare the checkpoint generation again, then commit.
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime, timezone
import inspect
import json
import os
from pathlib import Path
import threading
from typing import Any

import chess

from server import config
from server.core.agent.context import (
    ChessContextBuilder,
    ChessContextError,
    FollowUpResolution,
    ResolvedContextBundle,
)
from server.core.agent.models import (
    AgentError,
    AgentMessageRequest,
    AgentMessageResponse,
    AgentResponse,
    AgentRunRequest,
    AgentSessionContextRequest,
    AgentSessionCreateRequest,
    AgentSessionResponse,
    AgentSessionState,
    AgentSessionSummary,
    ChessReference,
    GetPlayerProfileResult,
    PositionContext,
    PositionReference,
    SessionError,
)
from server.core.agent.policy import (
    AgentResponseValidationError,
    allowed_tools_for,
    is_follow_up_reference_request,
    is_review_priority_request,
    validate_agent_response,
)
from server.core.agent.prioritization import PrioritizationError, build_review_prioritization
from server.core.agent.runtime import AgentRuntime, AgentRuntimeAvailability, AgentRuntimeFailure
from server.core.agent.sessions import (
    ChessSessionCheckpointStore,
    ConversationSessionFactory,
    GenerationGuardedSession,
    InMemoryConversationSessionFactory,
    InvalidSessionContextError,
    SessionBusyError,
    SessionMessageGate,
    SessionMutationCoordinator,
    SessionNotFoundError,
    SessionStoreError,
    StaleAgentContextError,
)
from server.core.agent.summary import ConversationSummaryBuilder
from server.core.agent.tools import ActiveReviewArtifact, AgentTools


ToolsFactory = Callable[[ResolvedContextBundle], AgentTools]


class AgentServiceFailure(RuntimeError):
    """Stable typed failure consumed by the HTTP adapter."""

    def __init__(self, error: AgentError | SessionError):
        super().__init__(error.message)
        self.error = error


class AgentRunAuditLog:
    """Append-only, content-free summaries of successful Agent runs."""

    def __init__(self, data_dir: str | os.PathLike[str]) -> None:
        self.path = Path(data_dir) / "agent" / "runs.jsonl"
        self._lock = threading.RLock()

    def append_success(
        self,
        *,
        session_id: str,
        generation: int,
        result: Any,
    ) -> None:
        record = {
            "schema_version": 1,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "session_id": session_id,
            "generation": generation,
            "status": "success",
            "tool_calls": [
                {
                    "name": call.name,
                    "permission": call.permission,
                    "status": call.status,
                    "duration_ms": call.duration_ms,
                    "cache_hit": call.cache_hit,
                    "error_code": call.error_code,
                }
                for call in result.tool_calls
            ],
            "usage": dict(result.usage),
        }
        content = (
            json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            + "\n"
        )
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())


def _session_error(code: str, message: str, *, recoverable: bool) -> AgentServiceFailure:
    return AgentServiceFailure(
        SessionError.model_validate(
            {"code": code, "message": message, "recoverable": recoverable}
        )
    )


def _default_tools(bundle: ResolvedContextBundle) -> AgentTools:
    opening_history_fens = _opening_history_fens(bundle)
    if bundle.analysis is not None and bundle.critical is not None:
        critical_id = str(bundle.critical.get("critical_id") or "")
        return AgentTools(
            active_review=ActiveReviewArtifact.from_analysis(bundle.analysis, critical_id),
            opening_history_fens=opening_history_fens,
            personalization_enabled=config.PERSONALIZE_HISTORY,
        )
    return AgentTools(
        opening_history_fens=opening_history_fens,
        personalization_enabled=config.PERSONALIZE_HISTORY,
    )


def _opening_history_fens(bundle: ResolvedContextBundle) -> list[str]:
    """Replay the saved mainline only as far as the validated current/base position."""

    analysis = bundle.analysis
    position = bundle.context.position
    reference = position.reference if position is not None else None
    if analysis is None or position is None or reference is None or reference.ply is None:
        return []
    moves = [item for item in (analysis.get("moves") or []) if isinstance(item, dict)]
    if not moves:
        return []
    base_ply = max(0, reference.ply - 1) if reference.critical_id else reference.ply
    if base_ply > len(moves):
        return []
    try:
        board = chess.Board(str(moves[0].get("fen_before") or ""))
        fens = [board.fen()]
        for item in moves[:base_ply]:
            move = chess.Move.from_uci(str((item.get("played_move") or {}).get("uci") or ""))
            if move not in board.legal_moves:
                return []
            board.push(move)
            if item.get("fen_after") != board.fen():
                return []
            fens.append(board.fen())
        for raw_uci in position.exploration_moves_uci:
            move = chess.Move.from_uci(raw_uci)
            if move not in board.legal_moves:
                return []
            board.push(move)
            fens.append(board.fen())
    except (TypeError, ValueError):
        return []
    return fens if board.fen() == position.fen else []


def _item_position_references(items: list[Any]) -> list[PositionReference]:
    """Read only explicitly structured references from recent SDK conversation items."""

    references: list[PositionReference] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        candidates: list[Any] = []
        if item.get("position_reference") is not None:
            candidates.append(item["position_reference"])
        raw_references = item.get("references")
        if isinstance(raw_references, list):
            candidates.extend(raw_references)
        if str(item.get("role") or "").lower() == "assistant":
            content = item.get("content")
            texts = [content] if isinstance(content, str) else [
                part.get("text") or part.get("content")
                for part in (content or [])
                if isinstance(part, dict)
            ] if isinstance(content, list) else []
            for text in texts:
                if not isinstance(text, str) or not text.lstrip().startswith("{"):
                    continue
                try:
                    structured = json.loads(text)
                except json.JSONDecodeError:
                    continue
                if isinstance(structured, dict) and isinstance(
                    structured.get("references"), list
                ):
                    candidates.extend(structured["references"])
        for candidate in candidates:
            try:
                if isinstance(candidate, dict) and candidate.get("kind") == "skill":
                    continue
                values = dict(candidate) if isinstance(candidate, dict) else candidate
                if isinstance(values, dict):
                    values.pop("kind", None)
                    values.pop("skill_id", None)
                references.append(PositionReference.model_validate(values))
            except (TypeError, ValueError):
                continue
    return references


def _successful_profile_references(tools: AgentTools) -> list[ChessReference]:
    """Expose only typed references returned by a successful profile tool call this run."""

    references: list[ChessReference] = []
    identities: set[str] = set()
    for execution in getattr(tools, "executions", []):
        result = getattr(execution, "result", None)
        data = getattr(result, "data", None) if getattr(result, "ok", False) else None
        if not isinstance(data, GetPlayerProfileResult):
            continue
        for estimate in data.relevant_estimates:
            candidates = [
                ChessReference(kind="skill", skill_id=estimate.skill_id),
                *estimate.examples,
            ]
            for reference in candidates:
                identity = reference.model_dump_json(exclude_none=True)
                if identity in identities:
                    continue
                identities.add(identity)
                references.append(reference)
    return references


def _position_reference_from_chess(reference: ChessReference) -> PositionReference | None:
    if reference.kind == "skill":
        return None
    try:
        return PositionReference(
            game_id=reference.game_id,
            review_side=reference.review_side,
            critical_id=reference.critical_id,
            ply=reference.ply,
            fen=reference.fen,
        )
    except ValueError:
        return None


class ChessAgentService:
    """Framework-neutral service for checkpoint and grounded message operations."""

    def __init__(
        self,
        *,
        checkpoint_store: ChessSessionCheckpointStore,
        runtime: AgentRuntime,
        conversation_factory: ConversationSessionFactory,
        context_builder: ChessContextBuilder | None = None,
        coordinator: SessionMutationCoordinator | None = None,
        message_gate: SessionMessageGate | None = None,
        tools_factory: ToolsFactory = _default_tools,
        audit_log: AgentRunAuditLog | None = None,
        summary_builder: ConversationSummaryBuilder | None = None,
        max_turns: int = 4,
        max_total_tool_calls: int = 6,
        max_engine_tool_calls: int = 2,
        timeout_seconds: int = 120,
    ) -> None:
        self.checkpoint_store = checkpoint_store
        self.runtime = runtime
        self.conversation_factory = conversation_factory
        self.context_builder = context_builder or ChessContextBuilder()
        self.coordinator = coordinator or SessionMutationCoordinator()
        self.message_gate = message_gate or SessionMessageGate()
        self.tools_factory = tools_factory
        self.audit_log = audit_log
        self.summary_builder = summary_builder or ConversationSummaryBuilder()
        self.max_turns = max_turns
        self.max_total_tool_calls = max_total_tool_calls
        self.max_engine_tool_calls = max_engine_tool_calls
        self.timeout_seconds = timeout_seconds
        self._active_sessions: dict[str, GenerationGuardedSession] = {}
        self._active_tools: dict[str, AgentTools] = {}
        self._retired_runtimes: list[Any] = []

    def _bundle_for_reference(
        self,
        state: AgentSessionState,
        resolution: FollowUpResolution | None,
    ) -> ResolvedContextBundle:
        if (
            resolution is None
            or resolution.status != "resolved"
            or resolution.reference is None
            or resolution.source == "checkpoint"
        ):
            return self.context_builder.resolve(state)
        reference = resolution.reference
        values = state.model_dump(mode="python")
        if reference.game_id is None or (
            reference.ply is None
            and reference.critical_id is None
            and reference.fen is not None
        ):
            standalone = PositionReference(fen=reference.fen)
            values.update(
                active_game_id=None,
                review_side=None,
                active_ply=None,
                active_critical_id=None,
                position=PositionContext(
                    fen=str(standalone.fen),
                    recent_moves_uci=[],
                    recent_moves_san=[],
                    reference=standalone,
                ),
            )
        else:
            values.update(
                active_game_id=reference.game_id,
                review_side=reference.review_side,
                active_ply=(
                    max(0, int(reference.ply or 0) - 1)
                    if reference.critical_id is not None
                    else reference.ply
                ),
                active_critical_id=reference.critical_id,
                position=None,
            )
        return self.context_builder.resolve(AgentSessionState.model_validate(values))

    async def _clarify_reference(
        self,
        session_id: str,
        request: AgentMessageRequest,
        resolution: FollowUpResolution,
    ) -> AgentMessageResponse:
        text = (
            "I have more than one matching position. Please select the exact game position first."
            if resolution.status == "ambiguous"
            else "I cannot identify which position you mean. Please select it on the board first."
        )
        guarded = GenerationGuardedSession(
            self.conversation_factory.get_session(session_id),
            self.checkpoint_store,
            self.coordinator,
            expected_generation=request.expected_generation,
        )
        try:
            await guarded.add_items(
                [
                    {"role": "user", "content": request.message},
                    {"role": "assistant", "content": text},
                ]
            )
            await guarded.commit()
        except BaseException:
            guarded.discard()
            raise
        state = self.checkpoint_store.assert_generation(
            session_id, request.expected_generation
        )
        return AgentMessageResponse(
            session=AgentSessionSummary(
                session_id=session_id,
                generation=state.generation,
                conversation_summary=state.conversation_summary,
            ),
            response=AgentResponse(text=text),
            tool_calls=[],
        )

    @property
    def availability(self) -> AgentRuntimeAvailability:
        value = getattr(self.runtime, "availability", None)
        if isinstance(value, AgentRuntimeAvailability):
            return value
        return AgentRuntimeAvailability(True, True, "", "responses")

    def capability(self) -> dict[str, Any]:
        availability = self.availability
        return {
            "enabled": availability.enabled,
            "available": availability.available,
            "model": availability.model,
            "endpoint_type": availability.endpoint_type,
            "features": {"review_chat": True},
        }

    def session_for_runtime(self, session_id: str) -> GenerationGuardedSession:
        try:
            return self._active_sessions[session_id]
        except KeyError as exc:
            raise RuntimeError("Agent runtime requested a session outside an active run.") from exc

    def tools_for_runtime(self, request: AgentRunRequest) -> AgentTools:
        try:
            return self._active_tools[request.session_id]
        except KeyError as exc:
            raise RuntimeError("Agent runtime requested tools outside an active run.") from exc

    def _validate_checkpoint(self, state: Any) -> None:
        try:
            self.context_builder.resolve(state)
        except ChessContextError as exc:
            raise InvalidSessionContextError(exc.message) from exc

    def create_session(self, request: AgentSessionCreateRequest) -> AgentSessionResponse:
        validator = self._validate_checkpoint if request.game_id is not None else None
        try:
            state = self.checkpoint_store.create(request, validator=validator)
        except InvalidSessionContextError as exc:
            raise _session_error("invalid_session_context", str(exc), recoverable=False) from exc
        except SessionStoreError as exc:
            raise _session_error("invalid_session_context", str(exc), recoverable=False) from exc
        return AgentSessionResponse(session=state)

    def get_session(self, session_id: str) -> AgentSessionResponse:
        try:
            return AgentSessionResponse(session=self.checkpoint_store.get(session_id))
        except SessionNotFoundError as exc:
            raise _session_error("session_not_found", str(exc), recoverable=False) from exc
        except SessionStoreError as exc:
            raise _session_error("invalid_session_context", str(exc), recoverable=False) from exc

    async def update_context(
        self,
        session_id: str,
        request: AgentSessionContextRequest,
    ) -> AgentSessionResponse:
        try:
            state = await self.coordinator.update_context(
                self.checkpoint_store,
                session_id,
                request,
                validator=self._validate_checkpoint,
            )
        except SessionNotFoundError as exc:
            raise _session_error("session_not_found", str(exc), recoverable=False) from exc
        except StaleAgentContextError as exc:
            raise _session_error("stale_agent_context", str(exc), recoverable=True) from exc
        except (InvalidSessionContextError, SessionStoreError) as exc:
            raise _session_error("invalid_session_context", str(exc), recoverable=False) from exc
        return AgentSessionResponse(session=state)

    async def delete_session(self, session_id: str) -> None:
        async def _delete() -> None:
            async with self.message_gate.hold(session_id):
                async with self.coordinator.mutation(session_id):
                    self.checkpoint_store.get(session_id)
                    backing = self.conversation_factory.get_session(session_id)
                    previous_items = await backing.get_items()
                    await self.conversation_factory.clear_session(session_id)
                    try:
                        self.checkpoint_store.delete(session_id)
                    except BaseException:
                        if previous_items:
                            await self.conversation_factory.get_session(session_id).add_items(
                                previous_items
                            )
                        raise

        deletion = asyncio.create_task(_delete())
        try:
            await asyncio.shield(deletion)
        except asyncio.CancelledError:
            await deletion
            raise
        except SessionBusyError as exc:
            raise _session_error("session_busy", str(exc), recoverable=True) from exc
        except SessionNotFoundError as exc:
            raise _session_error("session_not_found", str(exc), recoverable=False) from exc
        except (SessionStoreError, OSError, RuntimeError) as exc:
            raise _session_error("invalid_session_context", str(exc), recoverable=False) from exc

    async def send_message(
        self,
        session_id: str,
        request: AgentMessageRequest,
    ) -> AgentMessageResponse:
        try:
            async with self.message_gate.hold(session_id):
                return await self._run_message(session_id, request)
        except SessionBusyError as exc:
            raise _session_error("session_busy", str(exc), recoverable=True) from exc
        except SessionNotFoundError as exc:
            raise _session_error("session_not_found", str(exc), recoverable=False) from exc
        except StaleAgentContextError as exc:
            raise _session_error("stale_agent_context", str(exc), recoverable=True) from exc
        except ChessContextError as exc:
            raise _session_error(
                "invalid_session_context", exc.message, recoverable=False
            ) from exc
        except AgentResponseValidationError as exc:
            raise AgentServiceFailure(
                AgentError(
                    code="invalid_agent_response",
                    message="Chess Coach Agent returned an ungrounded response.",
                    recoverable=True,
                )
            ) from exc
        except TimeoutError as exc:
            raise AgentServiceFailure(
                AgentError(
                    code="agent_timeout",
                    message="Chess Coach Agent did not finish before the timeout.",
                    recoverable=True,
                )
            ) from exc
        except AgentRuntimeFailure as exc:
            raise AgentServiceFailure(exc.error) from exc

    async def _run_message(
        self,
        session_id: str,
        request: AgentMessageRequest,
    ) -> AgentMessageResponse:
        state = self.checkpoint_store.assert_generation(
            session_id, request.expected_generation
        )
        backing = self.conversation_factory.get_session(session_id)
        recent_items = await backing.get_items(limit=12)
        resolution: FollowUpResolution | None = None
        if request.position_reference is not None or is_follow_up_reference_request(request.message):
            resolution = self.context_builder.resolve_follow_up_reference(
                state,
                explicit_references=(
                    [request.position_reference]
                    if request.position_reference is not None
                    else []
                ),
                recent_turn_references=_item_position_references(recent_items),
            )
            if resolution.status != "resolved":
                return await self._clarify_reference(session_id, request, resolution)

        bundle = self._bundle_for_reference(state, resolution)
        tools = self.tools_factory(bundle)
        model_context = await self.context_builder.build_model_context(
            bundle,
            request.message,
            review_loader=tools.get_review_context,
        )
        personalization_enabled = bool(
            config.PERSONALIZE_HISTORY
            and getattr(tools, "personalization_enabled", True)
        )
        review_priorities = None
        if is_review_priority_request(request.message) and bundle.analysis is not None:
            recurrence_loader = getattr(tools, "review_recurrence_evidence", None)
            recurrence = (
                recurrence_loader()
                if personalization_enabled and callable(recurrence_loader)
                else {}
            )
            try:
                review_priorities = build_review_prioritization(
                    bundle.analysis,
                    recurrence_evidence=recurrence if personalization_enabled else None,
                    user_goal=request.message,
                )
            except PrioritizationError as exc:
                raise ChessContextError("invalid_session_context", str(exc)) from exc
        allowed_evidence = list(model_context.allowed_evidence_refs)
        if review_priorities is not None:
            for candidate in review_priorities.candidates:
                for evidence_ref in candidate.evidence_refs:
                    if evidence_ref not in allowed_evidence:
                        allowed_evidence.append(evidence_ref)
        model_context = model_context.model_copy(
            update={
                "task": model_context.task.model_copy(
                    update={"personalization_enabled": personalization_enabled}
                ),
                "review_priorities": review_priorities,
                "allowed_evidence_refs": allowed_evidence,
            }
        )
        run_request = AgentRunRequest(
            session_id=session_id,
            expected_generation=request.expected_generation,
            message=request.message,
            model_context=model_context,
            allowed_tools=allowed_tools_for(request.message, model_context),
            max_turns=self.max_turns,
            max_total_tool_calls=self.max_total_tool_calls,
            max_engine_tool_calls=self.max_engine_tool_calls,
            timeout_seconds=self.timeout_seconds,
        )
        guarded = GenerationGuardedSession(
            backing,
            self.checkpoint_store,
            self.coordinator,
            expected_generation=request.expected_generation,
        )
        self._active_sessions[session_id] = guarded
        self._active_tools[session_id] = tools
        try:
            async with asyncio.timeout(self.timeout_seconds):
                result = await self.runtime.run(run_request)
            if len(result.tool_calls) > self.max_total_tool_calls:
                raise AgentResponseValidationError("Agent runtime exceeded its tool budget.")
            if any(call.name not in run_request.allowed_tools for call in result.tool_calls):
                raise AgentResponseValidationError("Agent runtime called a tool outside this run.")
            validate_agent_response(
                result.response,
                model_context,
                result.tool_calls,
                validated_tool_references=_successful_profile_references(tools),
            )
            if not guarded.staged_items:
                await guarded.add_items(
                    [
                        {"role": "user", "content": request.message},
                        {"role": "assistant", "content": result.response.text},
                    ]
                )
            on_committed = None
            if self.audit_log is not None:
                on_committed = lambda: self.audit_log.append_success(
                    session_id=session_id,
                    generation=request.expected_generation,
                    result=result,
                )
            await guarded.commit(on_committed=on_committed)
            final_state = await self._update_conversation_metadata(
                session_id=session_id,
                expected_generation=request.expected_generation,
                initial_state=state,
                backing=backing,
                model_context=model_context,
                response=result.response,
            )
            return AgentMessageResponse(
                session=AgentSessionSummary(
                    session_id=session_id,
                    generation=final_state.generation,
                    conversation_summary=final_state.conversation_summary,
                ),
                response=result.response,
                tool_calls=result.tool_calls,
            )
        except BaseException:
            guarded.discard()
            raise
        finally:
            self._active_sessions.pop(session_id, None)
            self._active_tools.pop(session_id, None)

    async def _update_conversation_metadata(
        self,
        *,
        session_id: str,
        expected_generation: int,
        initial_state: AgentSessionState,
        backing: Any,
        model_context: Any,
        response: AgentResponse,
    ) -> AgentSessionState:
        current = initial_state
        all_items = await backing.get_items()
        if len(all_items) > 12:
            try:
                summary = self.summary_builder.summarize(
                    all_items[:-12],
                    initial_state.conversation_summary,
                )
            except Exception:  # summary failure leaves every raw SDK item intact
                summary = None
            if summary is not None:
                try:
                    current = await self.coordinator.update_conversation_summary(
                        self.checkpoint_store,
                        session_id,
                        expected_generation=expected_generation,
                        summary=summary,
                        references=[
                            *initial_state.conversation_summary_references,
                            *initial_state.discussed_positions,
                        ],
                        reference_validator=lambda reference: self.context_builder.canonicalize_reference(
                            reference,
                            session=initial_state,
                        ),
                        validator=self._validate_checkpoint,
                    )
                except (StaleAgentContextError, InvalidSessionContextError, ChessContextError):
                    current = self.checkpoint_store.get(session_id)

        discussed: list[PositionReference] = []
        position = model_context.position
        if position is not None:
            if position.exploration_moves_uci:
                discussed.append(PositionReference(fen=position.fen))
            elif position.reference is not None:
                discussed.append(position.reference)
        discussed.extend(
            reference
            for item in response.references
            if (reference := _position_reference_from_chess(item)) is not None
        )
        if discussed:
            try:
                current = await self.coordinator.record_discussed_positions(
                    self.checkpoint_store,
                    session_id,
                    expected_generation=expected_generation,
                    references=discussed,
                    reference_validator=lambda reference: self.context_builder.canonicalize_reference(
                        reference,
                        session=initial_state,
                    ),
                )
            except StaleAgentContextError:
                current = self.checkpoint_store.get(session_id)
        return current

    async def close(self) -> None:
        first_error: BaseException | None = None
        runtimes = [self.runtime, *self._retired_runtimes]
        self._retired_runtimes.clear()
        seen_runtime_ids: set[int] = set()
        for owned in runtimes:
            if id(owned) in seen_runtime_ids:
                continue
            seen_runtime_ids.add(id(owned))
            close_runtime = getattr(owned, "close", None)
            if callable(close_runtime):
                try:
                    result = close_runtime()
                    if inspect.isawaitable(result):
                        await result
                except BaseException as exc:
                    if first_error is None:
                        first_error = exc
        try:
            self.conversation_factory.close()
        except BaseException as exc:
            if first_error is None:
                first_error = exc
        if first_error is not None:
            raise first_error


def create_default_agent_service(data_dir: str | None = None) -> ChessAgentService:
    """Create the lifecycle-owned production service without requiring the optional SDK."""

    from server.core.agent.runtime import UnavailableAgentRuntime
    from server.core.agent.runtime_openai import (
        SQLiteConversationSessionFactory,
        create_openai_runtime,
    )

    root = data_dir or config.DATA_DIR
    placeholder = UnavailableAgentRuntime(
        AgentRuntimeAvailability(
            enabled=config.AGENT_ENABLED,
            available=False,
            model=config.AGENT_MODEL,
            endpoint_type=("custom_responses" if config.AGENT_BASE_URL else "openai_responses"),
            reason="Chess Coach Agent runtime is initializing.",
        )
    )
    service = ChessAgentService(
        checkpoint_store=ChessSessionCheckpointStore(root),
        runtime=placeholder,
        conversation_factory=InMemoryConversationSessionFactory(),
        audit_log=AgentRunAuditLog(root),
        max_turns=config.AGENT_MAX_TURNS,
        max_total_tool_calls=config.AGENT_MAX_TOOL_CALLS,
        max_engine_tool_calls=config.AGENT_MAX_ENGINE_CALLS,
        timeout_seconds=config.AGENT_TIMEOUT,
    )
    try:
        runtime = create_openai_runtime(
            enabled=config.AGENT_ENABLED,
            model=config.AGENT_MODEL,
            base_url=config.AGENT_BASE_URL,
            custom_api_key=config.AGENT_API_KEY,
            openai_api_key=config.OPENAI_API_KEY,
            domain_tools_factory=service.tools_for_runtime,
            session_provider=service.session_for_runtime,
        )
    except Exception as exc:  # optional Agent initialization must not prevent Web startup
        runtime = UnavailableAgentRuntime(
            AgentRuntimeAvailability(
                enabled=config.AGENT_ENABLED,
                available=False,
                model=config.AGENT_MODEL,
                endpoint_type=(
                    "custom_responses" if config.AGENT_BASE_URL else "openai_responses"
                ),
                reason=f"Chess Coach Agent initialization failed: {type(exc).__name__}.",
            )
        )
    service.runtime = runtime
    if runtime.availability.available:
        try:
            conversation_factory = SQLiteConversationSessionFactory(root)
        except Exception as exc:
            service._retired_runtimes.append(runtime)
            service.runtime = UnavailableAgentRuntime(
                AgentRuntimeAvailability(
                    enabled=True,
                    available=False,
                    model=config.AGENT_MODEL,
                    endpoint_type=runtime.availability.endpoint_type,
                    reason=f"Agent conversation storage failed: {type(exc).__name__}.",
                )
            )
        else:
            service.conversation_factory.close()
            service.conversation_factory = conversation_factory
    return service


__all__ = [
    "AgentRunAuditLog",
    "AgentServiceFailure",
    "ChessAgentService",
    "create_default_agent_service",
]
