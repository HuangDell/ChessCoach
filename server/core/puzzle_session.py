"""In-memory state for the puzzle currently being solved.

A process singleton, mirroring `session.py`'s ReviewSession, so the Web process has one
"current puzzle". Holds the puzzle plus solve progress (which ply the solver is on,
attempts, whether hints were used, and whether it has already been failed/scored).
"""

from __future__ import annotations

from contextlib import contextmanager
import time
import threading
import uuid
from collections.abc import Iterator
from typing import Optional


class PuzzleProgress:
    """The puzzle in front of the user right now + how the attempt is going."""

    def __init__(self, puzzle: dict) -> None:
        self.puzzle: dict = puzzle
        self.ply_index: int = 1  # moves[0] is the auto-played setup; the solver starts at 1
        self.attempts: int = 0
        # Every move the solver submitted, in order: {uci, fen_before, correct, ply_index}. Lets the
        # coach discuss what the player actually tried (engine-grounded), not just the failing move.
        self.tried: list[dict] = []
        self.hints_used: int = 0
        self.failed: bool = False  # the solver played at least one wrong move (rating already lost)
        self.scored: bool = False  # guard so one puzzle moves the rating at most once
        self.finished: bool = False  # solving is over (solved, or the solution was revealed)
        self.started_at: float = time.time()
        # Stable for the lifetime of this served puzzle, including retries after a wrong move.
        self.attempt_id: str = uuid.uuid4().hex
        self.learning_sync_error: str | None = None
        self.finalize_lock = threading.RLock()

    @property
    def id(self) -> str:
        return self.puzzle.get("id", "")

    @property
    def first_try(self) -> bool:
        return self.attempts == 0 and self.hints_used == 0


_CURRENT: Optional[PuzzleProgress] = None
_TRANSITION_LOCK = threading.RLock()


@contextmanager
def transition() -> Iterator[None]:
    """Keep selection and replacement of the process-wide puzzle coherent.

    Callers that coordinate singleton replacement with a ``PuzzleProgress`` mutation must
    acquire this lock first and that progress' ``finalize_lock`` second. The lock is reentrant
    so transition owners may use ``set_current`` and ``clear_current`` atomically.
    """
    with _TRANSITION_LOCK:
        yield


def set_current(puzzle: dict) -> PuzzleProgress:
    global _CURRENT
    with _TRANSITION_LOCK:
        _CURRENT = PuzzleProgress(puzzle)
        return _CURRENT


def get_current() -> Optional[PuzzleProgress]:
    with _TRANSITION_LOCK:
        return _CURRENT


def clear_current() -> None:
    global _CURRENT
    with _TRANSITION_LOCK:
        _CURRENT = None
