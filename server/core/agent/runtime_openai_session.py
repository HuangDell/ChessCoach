"""Lifecycle and version compatibility for the optional SDK SQLite sessions."""
from __future__ import annotations

import importlib
from pathlib import Path
import sys
import threading
from typing import Any, Callable


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


