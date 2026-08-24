"""Process-local coordination for shared storage artifacts."""
from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager


_ATTEMPT_LOG_MUTATION_LOCK = threading.RLock()
_LEARNING_SOURCE_MUTATION_LOCK = threading.RLock()


@contextmanager
def coordinated_learning_source_mutation() -> Iterator[None]:
    """Serialize source snapshots with game source commit or rollback.

    Callers that also hold a per-game mutation acquire that lock first.  Attempt-log
    and module-local locks are acquired after this boundary, keeping the global order
    ``game -> learning source -> attempt log -> local``.
    """

    with _LEARNING_SOURCE_MUTATION_LOCK:
        yield


@contextmanager
def coordinated_attempt_log_mutation() -> Iterator[None]:
    """Serialize append and whole-file replacement of shared attempt logs.

    Callers that also hold a per-game mutation must acquire that lock first. Module-local
    history or append locks are acquired after this shared boundary.
    """

    with _ATTEMPT_LOG_MUTATION_LOCK:
        yield
