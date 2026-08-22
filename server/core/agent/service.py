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

from server import config
from server.core.agent.context import ChessContextBuilder, ChessContextError, ResolvedContextBundle
from server.core.agent.models import (
    AgentError,
    AgentMessageRequest,
    AgentMessageResponse,
    AgentRunRequest,
    AgentSessionContextRequest,
    AgentSessionCreateRequest,
    AgentSessionResponse,
    AgentSessionSummary,
    SessionError,
)
from server.core.agent.policy import (
    AgentResponseValidationError,
    allowed_tools_for,
    validate_agent_response,
)
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
    if bundle.analysis is not None and bundle.critical is not None:
        critical_id = str(bundle.critical.get("critical_id") or "")
        return AgentTools(
            active_review=ActiveReviewArtifact.from_analysis(bundle.analysis, critical_id)
        )
    return AgentTools()


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
        self.max_turns = max_turns
        self.max_total_tool_calls = max_total_tool_calls
        self.max_engine_tool_calls = max_engine_tool_calls
        self.timeout_seconds = timeout_seconds
        self._active_sessions: dict[str, GenerationGuardedSession] = {}
        self._active_tools: dict[str, AgentTools] = {}
        self._retired_runtimes: list[Any] = []

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
            "features": {"review_chat": bool(config.AGENT_REVIEW_CHAT_ENABLED)},
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
        bundle = self.context_builder.resolve(state)
        tools = self.tools_factory(bundle)
        model_context = await self.context_builder.build_model_context(
            bundle,
            request.message,
            review_loader=tools.get_review_context,
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
            self.conversation_factory.get_session(session_id),
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
            validate_agent_response(result.response, model_context, result.tool_calls)
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
            return AgentMessageResponse(
                session=AgentSessionSummary(
                    session_id=session_id,
                    generation=request.expected_generation,
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
