"""Bounded, redacted persistence and aggregation for Agent run telemetry."""
from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import threading
from typing import Iterator, Literal

from pydantic import BaseModel, ConfigDict, Field

from server.core.agent.models import AgentActivity, ToolCallRecord


RUN_RECORD_SCHEMA_VERSION = 1
RESPONSE_SCHEMA_VERSION = 1
_PROCESS_LOCK = threading.RLock()

RunStatus = Literal[
    "success",
    "provider_failure",
    "timeout",
    "invalid_output",
    "stale",
    "cancelled",
]


class AgentUsageSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    requests: int | float | None = Field(default=None, ge=0)
    input_tokens: int | float | None = Field(default=None, ge=0)
    output_tokens: int | float | None = Field(default=None, ge=0)
    total_tokens: int | float | None = Field(default=None, ge=0)


class AgentRunRecord(BaseModel):
    """Persisted contract. It deliberately has no prompt, URL, FEN, PV, or traceback fields."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    schema_version: Literal[1] = RUN_RECORD_SCHEMA_VERSION
    run_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    generation: int = Field(ge=0)
    task_kind: str = Field(min_length=1)
    activity: AgentActivity
    model: str
    endpoint_type: str = Field(min_length=1)
    sdk_version: str = Field(min_length=1)
    policy_version: int = Field(ge=1)
    response_schema_version: int = Field(ge=1)
    started_at: str = Field(min_length=1)
    duration_ms: int = Field(ge=0)
    usage: AgentUsageSummary = Field(default_factory=AgentUsageSummary)
    status: RunStatus
    error_code: str | None = None
    tool_calls: list[ToolCallRecord] = Field(default_factory=list)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@contextmanager
def _file_lock(path: Path) -> Iterator[None]:
    """Serialize writers across threads and Web processes using a sibling lock file."""

    path.parent.mkdir(parents=True, exist_ok=True)
    with _PROCESS_LOCK:
        with path.open("a+b") as handle:
            try:
                import fcntl
            except ImportError:  # pragma: no cover - Windows uses the process lock fallback
                yield
                return
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:  # pragma: no cover - filesystem-specific durability best effort
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _valid_records(path: Path) -> list[AgentRunRecord]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    records: list[AgentRunRecord] = []
    for line in lines:
        try:
            raw = json.loads(line)
            records.append(AgentRunRecord.model_validate(raw))
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
    return records


def _percentile(values: list[int], proportion: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    return ordered[max(0, math.ceil(proportion * len(ordered)) - 1)]


class AgentRunStore:
    def __init__(self, data_dir: str | os.PathLike[str], *, max_records: int = 1000) -> None:
        self.path = Path(data_dir) / "agent" / "runs.jsonl"
        self.lock_path = self.path.with_suffix(".lock")
        self.max_records = max(1, int(max_records))

    def append(self, record: AgentRunRecord) -> None:
        payload = record.model_dump_json(exclude_none=True) + "\n"
        with _file_lock(self.lock_path):
            records = _valid_records(self.path)
            if len(records) + 1 <= self.max_records:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                return
            retained = [*records, record][-self.max_records :]
            temp = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
            with temp.open("w", encoding="utf-8") as handle:
                for item in retained:
                    handle.write(item.model_dump_json(exclude_none=True) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, self.path)
            _fsync_directory(self.path.parent)

    def read(self, *, limit: int = 100) -> list[AgentRunRecord]:
        bounded = max(1, min(int(limit), 1000))
        with _file_lock(self.lock_path):
            return _valid_records(self.path)[-bounded:]

    def clear(self) -> dict[str, int]:
        with _file_lock(self.lock_path):
            records = _valid_records(self.path)
            bytes_removed = self.path.stat().st_size if self.path.exists() else 0
            if self.path.exists():
                temp = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
                with temp.open("w", encoding="utf-8") as handle:
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temp, self.path)
                _fsync_directory(self.path.parent)
            return {"records_removed": len(records), "bytes_removed": bytes_removed}

    def metrics(self, *, limit: int = 100) -> dict[str, object]:
        records = self.read(limit=limit)
        statuses = Counter(item.status for item in records)
        errors = Counter(item.error_code for item in records if item.error_code)
        tool_names: Counter[str] = Counter()
        tool_statuses: Counter[str] = Counter()
        engine_calls = 0
        cache_hits = 0
        tool_calls = 0
        for record in records:
            for call in record.tool_calls:
                tool_calls += 1
                tool_names[call.name] += 1
                tool_statuses[call.status] += 1
                engine_calls += call.engine_call_count
                cache_hits += int(call.cache_hit)
        latencies = [item.duration_ms for item in records]
        return {
            "schema_version": RUN_RECORD_SCHEMA_VERSION,
            "record_count": len(records),
            "status_counts": dict(sorted(statuses.items())),
            "error_counts": dict(sorted(errors.items())),
            "tools": {
                "calls": tool_calls,
                "by_name": dict(sorted(tool_names.items())),
                "by_status": dict(sorted(tool_statuses.items())),
                "engine_calls": engine_calls,
                "cache_hits": cache_hits,
            },
            "latency_ms": {
                "p50": _percentile(latencies, 0.50),
                "p95": _percentile(latencies, 0.95),
                "max": max(latencies, default=0),
            },
        }


__all__ = [
    "AgentRunRecord",
    "AgentRunStore",
    "AgentUsageSummary",
    "RESPONSE_SCHEMA_VERSION",
    "RUN_RECORD_SCHEMA_VERSION",
    "RunStatus",
    "utc_now",
]
