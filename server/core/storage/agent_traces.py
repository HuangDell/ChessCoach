"""Opt-in local storage for header-free raw model HTTP bodies."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
import logging
import os
from pathlib import Path
import re
import shutil
import threading
from typing import Any, Iterator


logger = logging.getLogger("chesscoach.agent")
_SAFE_ID = re.compile(r"[^a-zA-Z0-9_-]+")


@dataclass
class _ActiveTrace:
    run_id: str
    timestamp: str
    directory: Path | None = None
    call_index: int = 0
    response_paths: dict[int, Path] = field(default_factory=dict)
    disabled: bool = False


class RawHttpTraceStore:
    """Write header-free raw HTTP bodies and retain only the newest trace directories."""

    def __init__(self, data_dir: str | os.PathLike[str], *, max_runs: int = 20) -> None:
        self.root = Path(data_dir) / "agent" / "traces"
        self.max_runs = max(1, int(max_runs))
        self._current: ContextVar[_ActiveTrace | None] = ContextVar(
            "chesscoach_raw_http_trace", default=None
        )
        self._lock = threading.RLock()
        self._active_directories: set[Path] = set()

    @contextmanager
    def activate(self, run_id: str) -> Iterator[None]:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        state = _ActiveTrace(run_id=run_id, timestamp=timestamp)
        token = self._current.set(state)
        try:
            yield
        finally:
            self._current.reset(token)
            if state.directory is not None:
                with self._lock:
                    self._active_directories.discard(state.directory)
                self._prune()

    def async_event_hooks(self) -> dict[str, list[Any]]:
        return {"request": [self.on_request], "response": [self.on_response]}

    def sync_event_hooks(self) -> dict[str, list[Any]]:
        return {"request": [self.on_sync_request], "response": [self.on_sync_response]}

    # Compatibility for callers that used the original Agent-only API.
    def event_hooks(self) -> dict[str, list[Any]]:
        return self.async_event_hooks()

    def current_directory(self) -> Path | None:
        state = self._current.get()
        return state.directory if state is not None else None

    async def on_request(self, request: Any) -> None:
        state = self._current.get()
        if state is None or state.disabled:
            return
        try:
            directory = self._ensure_directory(state)
            state.call_index += 1
            path = directory / f"{state.call_index:03d}-request.json"
            response_path = directory / f"{state.call_index:03d}-response.json"
            body = await request.aread()
            self._write_private(path, body)
            state.response_paths[id(request)] = response_path
        except Exception as exc:  # trace diagnostics must never break an Agent request
            self._disable(state, exc)

    async def on_response(self, response: Any) -> None:
        state = self._current.get()
        if state is None or state.disabled:
            return
        path = state.response_paths.pop(id(response.request), None)
        if path is None:
            return
        try:
            self._write_private(path, await response.aread())
        except Exception as exc:  # trace diagnostics must never replace the provider response
            self._disable(state, exc)

    def on_sync_request(self, request: Any) -> None:
        state = self._current.get()
        if state is None or state.disabled:
            return
        try:
            directory = self._ensure_directory(state)
            state.call_index += 1
            path = directory / f"{state.call_index:03d}-request.json"
            response_path = directory / f"{state.call_index:03d}-response.json"
            self._write_private(path, request.read())
            state.response_paths[id(request)] = response_path
        except Exception as exc:  # trace diagnostics must never break a provider request
            self._disable(state, exc)

    def on_sync_response(self, response: Any) -> None:
        state = self._current.get()
        if state is None or state.disabled:
            return
        path = state.response_paths.pop(id(response.request), None)
        if path is None:
            return
        try:
            self._write_private(path, response.read())
        except Exception as exc:  # trace diagnostics must never replace the provider response
            self._disable(state, exc)

    def _ensure_directory(self, state: _ActiveTrace) -> Path:
        if state.directory is not None:
            return state.directory
        safe_run_id = _SAFE_ID.sub("_", state.run_id) or "unknown"
        directory = self.root / f"{state.timestamp}-{safe_run_id}"
        with self._lock:
            self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(self.root, 0o700)
            directory.mkdir(mode=0o700, exist_ok=False)
            os.chmod(directory, 0o700)
            self._active_directories.add(directory)
        state.directory = directory
        return directory

    @staticmethod
    def _write_private(path: Path, body: bytes) -> None:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())

    def _disable(self, state: _ActiveTrace, exc: Exception) -> None:
        state.disabled = True
        logger.warning(
            "event=raw_trace_write_failed run=%s error=%s",
            state.run_id,
            type(exc).__name__,
        )

    def _prune(self) -> None:
        try:
            with self._lock:
                if not self.root.exists():
                    return
                directories = sorted(
                    (
                        path
                        for path in self.root.iterdir()
                        if path.is_dir()
                        and not path.is_symlink()
                        and path not in self._active_directories
                    ),
                    key=lambda path: path.name,
                )
                for path in directories[:-self.max_runs]:
                    shutil.rmtree(path)
        except Exception as exc:  # retention failure must not affect the completed model request
            logger.warning("event=raw_trace_prune_failed error=%s", type(exc).__name__)


# Preserve the public name used by existing integrations while the store is now shared by both
# model paths.
AgentRawTraceStore = RawHttpTraceStore


__all__ = ["AgentRawTraceStore", "RawHttpTraceStore"]
