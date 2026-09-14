"""Local, optional retrieval diagnostics; never part of the model-facing contract."""
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import threading
from uuid import uuid4

logger = logging.getLogger("chesscoach.agent")
_context = ContextVar("knowledge_trace_context", default=None)


@contextmanager
def knowledge_trace_context(entrypoint: str, **references):
    token = _context.set({"entrypoint": entrypoint, **references})
    try:
        yield
    finally:
        _context.reset(token)


class KnowledgeTraceStore:
    _lock = threading.RLock()

    def __init__(self, data_dir, *, max_records=100):
        self.root = Path(data_dir) / "knowledge" / "traces"
        self.max_records = max(1, max_records)

    def start(self):
        return {
            "schema_version": 1, "trace_id": uuid4().hex,
            "timestamp": datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ"),
            "context": dict(_context.get() or {"entrypoint": "direct"}),
            "status": "error", "timings_ms": {},
        }

    def save(self, record):
        temporary = None
        try:
            body = json.dumps(record, ensure_ascii=False, allow_nan=False, indent=2)
            with self._lock:
                self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
                path = self.root / f'{record["timestamp"]}-{record["trace_id"]}.json'
                temporary = path.with_suffix(".tmp")
                fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(body)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, path)
                for old in sorted(self.root.glob("*.json"))[:-self.max_records]:
                    old.unlink()
            logger.debug("event=knowledge_trace_saved trace=%s status=%s dense=%s lexical=%s duration_ms=%s path=%s",
                         record["trace_id"], record["status"], len(record.get("dense", [])),
                         len(record.get("lexical", [])), record["timings_ms"].get("total"), path)
        except Exception as exc:
            logger.warning("event=knowledge_trace_write_failed error=%s", type(exc).__name__)
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
