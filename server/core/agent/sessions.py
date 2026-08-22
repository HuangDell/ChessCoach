"""Agent checkpoint and staged conversation session primitives.

This module deliberately has no OpenAI Agents SDK dependency.  The production
runtime can put an SDK ``SQLiteSession`` behind the small duck protocol below,
while the checkpoint store and its concurrency rules remain ordinary Core code.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import tempfile
import threading
from typing import Any, Protocol, TypeVar
import uuid

from pydantic import ValidationError

from server.core.agent.models import (
    AgentSessionContextRequest,
    AgentSessionCreateRequest,
    AgentSessionState,
    PositionReference,
)


SESSION_SCHEMA_VERSION = 1
DEFAULT_RECENT_ITEM_LIMIT = 12
DEFAULT_DISCUSSION_REFERENCE_LIMIT = 5
_SESSION_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_ResultT = TypeVar("_ResultT")


class SessionStoreError(RuntimeError):
    """A checkpoint could not be read or persisted consistently."""


class SessionNotFoundError(LookupError):
    """The requested session id is invalid or has no checkpoint."""


class StaleAgentContextError(RuntimeError):
    """The checkpoint generation no longer matches the caller's context."""


class InvalidSessionContextError(ValueError):
    """A context patch would produce an invalid checkpoint."""


class SessionBusyError(RuntimeError):
    """Another message run already owns this session."""


class ConversationSession(Protocol):
    """SDK-neutral subset of the Agents SDK Session protocol."""

    session_id: str
    session_settings: Any | None

    async def get_items(self, limit: int | None = None) -> list[Any]: ...

    async def add_items(self, items: list[Any]) -> None: ...

    async def pop_item(self) -> Any | None: ...

    async def clear_session(self) -> None: ...


class ConversationSessionFactory(Protocol):
    def get_session(self, session_id: str) -> ConversationSession: ...

    async def clear_session(self, session_id: str) -> None: ...

    def close(self) -> None: ...


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_session_id() -> str:
    return uuid.uuid4().hex


def _valid_session_id(session_id: str) -> bool:
    return bool(_SESSION_ID_RE.fullmatch(session_id or ""))


class ChessSessionCheckpointStore:
    """Versioned, atomic ``AgentSessionState`` storage under the data directory."""

    def __init__(
        self,
        data_dir: str | os.PathLike[str],
        *,
        clock: Callable[[], str] = _utc_now,
        id_factory: Callable[[], str] = _new_session_id,
    ) -> None:
        self._sessions_dir = Path(data_dir) / "agent" / "sessions"
        self._clock = clock
        self._id_factory = id_factory
        self._lock = threading.RLock()

    @property
    def sessions_dir(self) -> Path:
        return self._sessions_dir

    def create(
        self,
        request: AgentSessionCreateRequest,
        *,
        validator: Callable[[AgentSessionState], Any] | None = None,
    ) -> AgentSessionState:
        """Create a backend-owned session checkpoint at generation zero."""
        with self._lock:
            for _attempt in range(8):
                session_id = self._id_factory()
                if not _valid_session_id(session_id):
                    raise SessionStoreError("The session id factory returned an unsafe identifier.")
                if not self._path(session_id).exists():
                    break
            else:
                raise SessionStoreError("Could not allocate a unique Agent session id.")

            now = self._clock()
            state = AgentSessionState(
                schema_version=SESSION_SCHEMA_VERSION,
                session_id=session_id,
                active_game_id=request.game_id,
                review_side=request.review_side,
                active_ply=request.active_ply,
                active_critical_id=request.active_critical_id,
                generation=0,
                created_at=now,
                updated_at=now,
            )
            if validator is not None:
                validator(state)
            self._write_locked(state)
            return state.model_copy(deep=True)

    def get(self, session_id: str) -> AgentSessionState:
        with self._lock:
            return self._read_locked(session_id).model_copy(deep=True)

    def assert_generation(self, session_id: str, expected_generation: int) -> AgentSessionState:
        """Return the current checkpoint only when the generation still matches."""
        with self._lock:
            state = self._read_locked(session_id)
            if state.generation != expected_generation:
                raise StaleAgentContextError(
                    f"Expected Agent context generation {expected_generation}, "
                    f"but the current generation is {state.generation}."
                )
            return state.model_copy(deep=True)

    def update_context(
        self,
        session_id: str,
        request: AgentSessionContextRequest,
        *,
        validator: Callable[[AgentSessionState], Any] | None = None,
    ) -> AgentSessionState:
        """Compare-and-set a semantic context change and increment its generation once."""
        with self._lock:
            current = self._read_locked(session_id)
            if current.generation != request.expected_generation:
                raise StaleAgentContextError(
                    f"Expected Agent context generation {request.expected_generation}, "
                    f"but the current generation is {current.generation}."
                )

            fields_set = request.model_fields_set
            values = current.model_dump(mode="python")
            game_changed = "game_id" in fields_set and request.game_id != current.active_game_id

            if "game_id" in fields_set:
                values["active_game_id"] = request.game_id
            if "review_side" in fields_set:
                values["review_side"] = request.review_side
            elif game_changed:
                values["review_side"] = None
            if "active_ply" in fields_set:
                values["active_ply"] = request.active_ply
            elif game_changed:
                values["active_ply"] = None
            if "active_critical_id" in fields_set:
                values["active_critical_id"] = request.active_critical_id
            elif game_changed or (
                "active_ply" in fields_set and request.active_ply != current.active_ply
            ):
                values["active_critical_id"] = None
            if "activity" in fields_set:
                values["activity"] = request.activity
            if "focus_ref" in fields_set:
                values["focus_ref"] = request.focus_ref
            if "position" in fields_set:
                values["position"] = request.position
            elif game_changed:
                values["position"] = None

            # Clearing a game necessarily clears every game-owned field, even if a
            # malformed patch tried to retain one explicitly.
            if values["active_game_id"] is None:
                values["review_side"] = None
                values["active_ply"] = None
                values["active_critical_id"] = None

            try:
                candidate = AgentSessionState.model_validate(values)
            except ValidationError as exc:
                raise InvalidSessionContextError("The Agent session context is invalid.") from exc
            if validator is not None:
                validator(candidate)
            semantic_exclusions = {"generation", "updated_at"}
            if candidate.model_dump(exclude=semantic_exclusions) == current.model_dump(
                exclude=semantic_exclusions
            ):
                return current.model_copy(deep=True)

            values["generation"] = current.generation + 1
            values["updated_at"] = self._clock()
            updated = AgentSessionState.model_validate(values)
            self._write_locked(updated)
            return updated.model_copy(deep=True)

    def update_conversation_summary(
        self,
        session_id: str,
        *,
        expected_generation: int,
        summary: str,
        references: Sequence[PositionReference],
        reference_validator: Callable[[PositionReference], PositionReference | None],
        reference_limit: int = DEFAULT_DISCUSSION_REFERENCE_LIMIT,
        validator: Callable[[AgentSessionState], Any] | None = None,
    ) -> AgentSessionState:
        """Atomically replace the compact summary and its validated position references.

        Summary compaction is guarded by the board-context generation but does not
        increment it: a summary is conversation metadata, not a UI-owned chess
        context change. Invalid or expired references are deliberately omitted while
        the usable summary text is retained.
        """

        if not isinstance(summary, str):
            raise InvalidSessionContextError("The conversation summary must be text.")
        if reference_limit < 0:
            raise ValueError("reference_limit must not be negative")

        with self._lock:
            current = self._read_locked(session_id)
            if current.generation != expected_generation:
                raise StaleAgentContextError(
                    f"Expected Agent context generation {expected_generation}, "
                    f"but the current generation is {current.generation}."
                )

            validated = self._validated_references(
                references,
                reference_validator=reference_validator,
                reference_limit=reference_limit,
            )
            values = current.model_dump(mode="python")
            values["conversation_summary"] = summary.strip()
            values["conversation_summary_references"] = validated
            values["updated_at"] = self._clock()
            try:
                updated = AgentSessionState.model_validate(values)
            except ValidationError as exc:
                raise InvalidSessionContextError(
                    "The Agent conversation summary is invalid."
                ) from exc
            if validator is not None:
                validator(updated)
            self._write_locked(updated)
            return updated.model_copy(deep=True)

    def record_discussed_positions(
        self,
        session_id: str,
        *,
        expected_generation: int,
        references: Sequence[PositionReference],
        reference_validator: Callable[[PositionReference], PositionReference | None],
        reference_limit: int = DEFAULT_DISCUSSION_REFERENCE_LIMIT,
    ) -> AgentSessionState:
        """Generation-guard and atomically append bounded recent positions."""

        if reference_limit < 0:
            raise ValueError("reference_limit must not be negative")
        with self._lock:
            current = self._read_locked(session_id)
            if current.generation != expected_generation:
                raise StaleAgentContextError(
                    f"Expected Agent context generation {expected_generation}, "
                    f"but the current generation is {current.generation}."
                )
            values = current.model_dump(mode="python")
            values["discussed_positions"] = self._validated_references(
                [*current.discussed_positions, *references],
                reference_validator=reference_validator,
                reference_limit=reference_limit,
            )
            values["updated_at"] = self._clock()
            try:
                updated = AgentSessionState.model_validate(values)
            except ValidationError as exc:
                raise InvalidSessionContextError(
                    "The Agent discussed positions are invalid."
                ) from exc
            self._write_locked(updated)
            return updated.model_copy(deep=True)

    def delete(self, session_id: str) -> AgentSessionState:
        """Delete and return a checkpoint; conversation clearing is a separate boundary."""
        with self._lock:
            current = self._read_locked(session_id)
            try:
                self._path(session_id).unlink()
            except OSError as exc:
                raise SessionStoreError("Could not delete the Agent session checkpoint.") from exc
            self._fsync_directory(self._sessions_dir)
            return current.model_copy(deep=True)

    def _path(self, session_id: str) -> Path:
        if not _valid_session_id(session_id):
            raise SessionNotFoundError("Agent session was not found.")
        return self._sessions_dir / f"{session_id}.json"

    @staticmethod
    def _validated_references(
        references: Sequence[PositionReference],
        *,
        reference_validator: Callable[[PositionReference], PositionReference | None],
        reference_limit: int,
    ) -> list[PositionReference]:
        validated: list[PositionReference] = []
        identities: set[str] = set()
        for supplied in reversed(references):
            try:
                reference = PositionReference.model_validate(supplied)
                canonical = reference_validator(reference)
                if canonical is None:
                    continue
                canonical = PositionReference.model_validate(canonical)
            except (ValidationError, ValueError, LookupError):
                continue
            identity = canonical.model_dump_json(exclude_none=True)
            if identity in identities:
                continue
            identities.add(identity)
            validated.append(canonical)
        validated.reverse()
        if reference_limit == 0:
            return []
        return validated[-reference_limit:]

    def _read_locked(self, session_id: str) -> AgentSessionState:
        path = self._path(session_id)
        try:
            with path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
            state = AgentSessionState.model_validate(payload)
        except FileNotFoundError as exc:
            raise SessionNotFoundError("Agent session was not found.") from exc
        except (OSError, json.JSONDecodeError, ValidationError) as exc:
            raise SessionStoreError("Agent session checkpoint is unreadable or invalid.") from exc
        if state.session_id != session_id or state.schema_version != SESSION_SCHEMA_VERSION:
            raise SessionStoreError("Agent session checkpoint identity or schema mismatch.")
        return state

    def _write_locked(self, state: AgentSessionState) -> None:
        path = self._path(state.session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        content = json.dumps(
            state.model_dump(mode="json"),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ) + "\n"
        temp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=path.parent,
                prefix=f".{path.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temp_path = Path(handle.name)
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, path)
            temp_path = None
            self._fsync_directory(path.parent)
        except OSError as exc:
            raise SessionStoreError("Could not persist the Agent session checkpoint.") from exc
        finally:
            if temp_path is not None:
                try:
                    temp_path.unlink(missing_ok=True)
                except OSError:
                    pass

    @staticmethod
    def _fsync_directory(directory: Path) -> None:
        if os.name == "nt":
            return
        try:
            descriptor = os.open(directory, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(descriptor)
        except OSError:
            pass
        finally:
            os.close(descriptor)


class SessionMutationCoordinator:
    """Serialize short generation mutations without blocking an Agent run."""

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}
        self._locks_guard = threading.Lock()

    def _lock_for(self, session_id: str) -> asyncio.Lock:
        with self._locks_guard:
            return self._locks.setdefault(session_id, asyncio.Lock())

    @asynccontextmanager
    async def mutation(self, session_id: str) -> AsyncIterator[None]:
        async with self._lock_for(session_id):
            yield

    async def update_context(
        self,
        store: ChessSessionCheckpointStore,
        session_id: str,
        request: AgentSessionContextRequest,
        *,
        validator: Callable[[AgentSessionState], Any] | None = None,
    ) -> AgentSessionState:
        async with self.mutation(session_id):
            return store.update_context(session_id, request, validator=validator)

    async def delete_checkpoint(
        self,
        store: ChessSessionCheckpointStore,
        session_id: str,
    ) -> AgentSessionState:
        async with self.mutation(session_id):
            return store.delete(session_id)

    async def update_conversation_summary(
        self,
        store: ChessSessionCheckpointStore,
        session_id: str,
        *,
        expected_generation: int,
        summary: str,
        references: Sequence[PositionReference],
        reference_validator: Callable[[PositionReference], PositionReference | None],
        reference_limit: int = DEFAULT_DISCUSSION_REFERENCE_LIMIT,
        validator: Callable[[AgentSessionState], Any] | None = None,
    ) -> AgentSessionState:
        async with self.mutation(session_id):
            return store.update_conversation_summary(
                session_id,
                expected_generation=expected_generation,
                summary=summary,
                references=references,
                reference_validator=reference_validator,
                reference_limit=reference_limit,
                validator=validator,
            )

    async def record_discussed_positions(
        self,
        store: ChessSessionCheckpointStore,
        session_id: str,
        *,
        expected_generation: int,
        references: Sequence[PositionReference],
        reference_validator: Callable[[PositionReference], PositionReference | None],
        reference_limit: int = DEFAULT_DISCUSSION_REFERENCE_LIMIT,
    ) -> AgentSessionState:
        async with self.mutation(session_id):
            return store.record_discussed_positions(
                session_id,
                expected_generation=expected_generation,
                references=references,
                reference_validator=reference_validator,
                reference_limit=reference_limit,
            )

    async def commit_if_current(
        self,
        store: ChessSessionCheckpointStore,
        session_id: str,
        expected_generation: int,
        operation: Callable[[], Awaitable[_ResultT]],
    ) -> _ResultT:
        async with self.mutation(session_id):
            store.assert_generation(session_id, expected_generation)
            return await operation()


class SessionMessageGate:
    """Give each session at most one active message run, failing immediately."""

    def __init__(self) -> None:
        self._active: set[str] = set()
        self._guard = asyncio.Lock()

    @asynccontextmanager
    async def hold(self, session_id: str) -> AsyncIterator[None]:
        async with self._guard:
            if session_id in self._active:
                raise SessionBusyError("Another Agent message is already running for this session.")
            self._active.add(session_id)
        try:
            yield
        finally:
            async with self._guard:
                self._active.discard(session_id)


class GenerationGuardedSession:
    """Stage SDK conversation mutations until output and generation are accepted."""

    def __init__(
        self,
        backing: ConversationSession,
        checkpoint_store: ChessSessionCheckpointStore,
        coordinator: SessionMutationCoordinator,
        *,
        expected_generation: int,
        recent_item_limit: int = DEFAULT_RECENT_ITEM_LIMIT,
    ) -> None:
        if recent_item_limit < 1:
            raise ValueError("recent_item_limit must be positive")
        self.session_id = backing.session_id
        self.session_settings = getattr(backing, "session_settings", None)
        self._backing = backing
        self._checkpoint_store = checkpoint_store
        self._coordinator = coordinator
        self._expected_generation = expected_generation
        self._recent_item_limit = recent_item_limit
        self._items: list[Any] | None = None
        self._staged_items: list[Any] = []
        self._base_pop_count = 0
        self._clear_requested = False
        self._status = "active"

    @property
    def staged_items(self) -> list[Any]:
        return deepcopy(self._staged_items)

    async def get_items(self, limit: int | None = None) -> list[Any]:
        self._ensure_active()
        self._checkpoint_store.assert_generation(self.session_id, self._expected_generation)
        await self._ensure_loaded()
        assert self._items is not None
        effective_limit = self._effective_limit(limit)
        if effective_limit == 0:
            return []
        return deepcopy(self._items[-effective_limit:])

    async def add_items(self, items: list[Any]) -> None:
        self._ensure_active()
        self._checkpoint_store.assert_generation(self.session_id, self._expected_generation)
        if not items:
            return
        await self._ensure_loaded()
        copied = deepcopy(items)
        assert self._items is not None
        self._items.extend(copied)
        self._staged_items.extend(copied)

    async def pop_item(self) -> Any | None:
        self._ensure_active()
        self._checkpoint_store.assert_generation(self.session_id, self._expected_generation)
        await self._ensure_loaded()
        assert self._items is not None
        if not self._items:
            return None
        item = self._items.pop()
        if self._staged_items:
            self._staged_items.pop()
        elif not self._clear_requested:
            self._base_pop_count += 1
        return deepcopy(item)

    async def clear_session(self) -> None:
        self._ensure_active()
        self._checkpoint_store.assert_generation(self.session_id, self._expected_generation)
        await self._ensure_loaded()
        self._clear_requested = True
        self._base_pop_count = 0
        self._staged_items.clear()
        self._items = []

    async def commit(self, *, on_committed: Callable[[], None] | None = None) -> None:
        """Commit staged items and a synchronous run summary at one linearization point."""
        self._ensure_active()
        staged_count = len(self._staged_items)

        async def _apply_mutation() -> None:
            apply_staged = getattr(self._backing, "apply_staged", None)
            if callable(apply_staged):
                await apply_staged(
                    clear=self._clear_requested,
                    pop_count=self._base_pop_count,
                    items=deepcopy(self._staged_items),
                )
                return
            if self._clear_requested:
                await self._backing.clear_session()
            else:
                for _index in range(self._base_pop_count):
                    await self._backing.pop_item()
            if self._staged_items:
                await self._backing.add_items(deepcopy(self._staged_items))

        async def _rollback_adds() -> None:
            if self._clear_requested or self._base_pop_count:
                return
            for _index in range(staged_count):
                await self._backing.pop_item()

        async def _apply() -> None:
            mutation = asyncio.create_task(_apply_mutation())
            mutation_completed = False
            try:
                await asyncio.shield(mutation)
                mutation_completed = True
            except asyncio.CancelledError:
                try:
                    await mutation
                    mutation_completed = True
                except BaseException:
                    pass
                if mutation_completed:
                    await asyncio.shield(_rollback_adds())
                raise
            try:
                if on_committed is not None:
                    on_committed()
            except BaseException:
                await _rollback_adds()
                raise

        try:
            await self._coordinator.commit_if_current(
                self._checkpoint_store,
                self.session_id,
                self._expected_generation,
                _apply,
            )
        except BaseException:
            self.discard()
            raise
        self._status = "committed"
        self._staged_items.clear()

    def discard(self) -> None:
        if self._status != "active":
            return
        self._status = "discarded"
        self._staged_items.clear()
        self._items = None
        self._base_pop_count = 0
        self._clear_requested = False

    async def _ensure_loaded(self) -> None:
        if self._items is not None:
            return
        items = await self._backing.get_items(limit=self._recent_item_limit)
        self._items = deepcopy(items[-self._recent_item_limit :])

    def _effective_limit(self, requested: int | None) -> int:
        if requested is None:
            return self._recent_item_limit
        return max(0, min(requested, self._recent_item_limit))

    def _ensure_active(self) -> None:
        if self._status != "active":
            raise RuntimeError(f"The staged conversation session is already {self._status}.")


class InMemoryConversationSession:
    """Deterministic conversation backing used by Core and service tests."""

    session_settings: Any | None = None

    def __init__(self, session_id: str, items: list[Any], lock: threading.RLock) -> None:
        self.session_id = session_id
        self._items = items
        self._lock = lock

    async def get_items(self, limit: int | None = None) -> list[Any]:
        with self._lock:
            if limit is None:
                return deepcopy(self._items)
            if limit <= 0:
                return []
            return deepcopy(self._items[-limit:])

    async def add_items(self, items: list[Any]) -> None:
        with self._lock:
            self._items.extend(deepcopy(items))

    async def pop_item(self) -> Any | None:
        with self._lock:
            return deepcopy(self._items.pop()) if self._items else None

    async def clear_session(self) -> None:
        with self._lock:
            self._items.clear()

    async def apply_staged(self, *, clear: bool, pop_count: int, items: list[Any]) -> None:
        with self._lock:
            if clear:
                self._items.clear()
            elif pop_count:
                del self._items[max(0, len(self._items) - pop_count) :]
            self._items.extend(deepcopy(items))


class InMemoryConversationSessionFactory:
    """Share deterministic conversation lists across session wrapper instances."""

    def __init__(self) -> None:
        self._items: dict[str, list[Any]] = {}
        self._locks: dict[str, threading.RLock] = {}
        self._guard = threading.RLock()
        self._closed = False

    def get_session(self, session_id: str) -> InMemoryConversationSession:
        with self._guard:
            if self._closed:
                raise RuntimeError("The conversation session factory is closed.")
            items = self._items.setdefault(session_id, [])
            lock = self._locks.setdefault(session_id, threading.RLock())
            return InMemoryConversationSession(session_id, items, lock)

    async def clear_session(self, session_id: str) -> None:
        await self.get_session(session_id).clear_session()

    def close(self) -> None:
        with self._guard:
            self._closed = True
