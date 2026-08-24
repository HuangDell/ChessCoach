"""Lifecycle and producer orchestration for canonical learning memory.

Source artifacts are committed before they are projected into observations.  A
projection failure therefore remains recoverable by the next startup backfill,
while callers receive an explicit error instead of claiming personalization was
updated successfully.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from server import config
from server.core.learning.observations import ObservationStore


_STATUS_LOCK = threading.RLock()
_PUZZLE_ATTEMPT_LOCK = threading.RLock()
_status: dict[str, Any] = {
    "initialized": False,
    # Core/tests may use retrieval without an ASGI lifespan.  Only an observed
    # initialization/producer failure closes the canonical read boundary.
    "available": True,
    "error": None,
    "operation": None,
    "updated_at": None,
}


class LearningProjectionError(RuntimeError):
    """A durable source exists but its canonical learning projection failed."""

    code = "learning_storage_error"

    def __init__(
        self,
        message: str,
        *,
        operation: str,
        attempt_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.operation = operation
        self.attempt_id = attempt_id


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _record_status(*, available: bool, operation: str, error: str | None) -> dict[str, Any]:
    with _STATUS_LOCK:
        _status.update(
            initialized=True,
            available=available,
            error=error,
            operation=operation,
            updated_at=_now_iso(),
        )
        return dict(_status)


def _record_projection_success(*, operation: str) -> dict[str, Any]:
    """Record producer health without clearing an unrepaired projection failure."""
    with _STATUS_LOCK:
        if _status["initialized"] and not _status["available"]:
            return dict(_status)
        _status.update(
            initialized=True,
            available=True,
            error=None,
            operation=operation,
            updated_at=_now_iso(),
        )
        return dict(_status)


def get_learning_status() -> dict[str, Any]:
    """Return process-local canonical learning health without reading user files."""
    with _STATUS_LOCK:
        return dict(_status)


def is_learning_available() -> bool:
    """Whether canonical memory may be read; pre-lifespan state is intentionally open."""
    with _STATUS_LOCK:
        return not _status["initialized"] or bool(_status["available"])


def initialize_learning(data_dir: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    """Reconcile current owning sources and estimates, degrading on failure."""
    try:
        ObservationStore(data_dir).rebuild(sync_estimates=True)
    except Exception as exc:  # noqa: BLE001 - optional personalization must not block Web startup
        return _record_status(
            available=False,
            operation="startup_backfill",
            error=f"Canonical learning startup failed: {type(exc).__name__}: {exc}",
        )
    return _record_status(available=True, operation="startup_backfill", error=None)


def _projection_error(exc: Exception, *, operation: str, attempt_id: str | None = None) -> None:
    message = f"Canonical learning {operation} failed: {type(exc).__name__}: {exc}"
    _record_status(available=False, operation=operation, error=message)
    raise LearningProjectionError(
        message,
        operation=operation,
        attempt_id=attempt_id,
    ) from exc


def sync_analysis_artifact(
    analysis: Mapping[str, Any],
    *,
    data_dir: str | os.PathLike[str] | None = None,
) -> None:
    """Project one already-persisted analysis and rebuild canonical estimates."""
    try:
        ObservationStore(data_dir).reconcile_analysis(analysis, sync_estimates=True)
    except Exception as exc:  # noqa: BLE001 - normalized into the producer contract
        _projection_error(exc, operation="analysis_sync")
    _record_projection_success(operation="analysis_sync")


def project_training_attempt(
    attempt: Mapping[str, Any],
    *,
    analysis: Mapping[str, Any],
    data_dir: str | os.PathLike[str] | None = None,
) -> None:
    """Project one durable game-backed attempt and refresh estimates."""
    attempt_id = str(attempt.get("attempt_id") or "") or None
    try:
        ObservationStore(data_dir).ingest_attempt(
            attempt,
            analysis=analysis,
            sync_estimates=True,
        )
    except Exception as exc:  # noqa: BLE001 - normalized into the producer contract
        _projection_error(exc, operation="attempt_sync", attempt_id=attempt_id)
    _record_projection_success(operation="attempt_sync")


def _canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _atomic_jsonl(path: Path, rows: list[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(_canonical_json(row) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _persist_puzzle_attempt(
    attempt: Mapping[str, Any], *, data_dir: str | os.PathLike[str] | None = None
) -> dict[str, Any]:
    path = Path(data_dir or config.DATA_DIR) / "history" / "puzzle_attempts.jsonl"
    with _PUZZLE_ATTEMPT_LOCK:
        rows: list[dict[str, Any]] = []
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            lines = []
        for line in lines:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise OSError(f"Puzzle attempt history is corrupt: {path}.") from exc
            if isinstance(row, dict):
                rows.append(row)
        attempt_id = str(attempt["attempt_id"])
        existing = next((row for row in rows if str(row.get("attempt_id")) == attempt_id), None)
        normalized = dict(attempt)
        if existing is not None:
            if _canonical_json(existing) != _canonical_json(normalized):
                raise OSError(f"Conflicting puzzle attempt id: {attempt_id}.")
            return existing
        rows.append(normalized)
        _atomic_jsonl(path, rows)
        return normalized


def finalize_puzzle_attempt(
    progress: Any,
    *,
    outcome: str,
    source: str,
    selected_move: str | None = None,
    data_dir: str | os.PathLike[str] | None = None,
) -> dict[str, Any] | None:
    """Persist and project one curated puzzle terminal outcome at most once per session."""
    if outcome not in {"success", "partial", "failure"}:
        raise ValueError("Puzzle attempt outcome must be success, partial, or failure.")
    if source not in {"lichess", "storm"}:
        raise ValueError("Curated puzzle attempt source must be lichess or storm.")
    with progress.finalize_lock:
        if progress.scored:
            return None
        # Import locally to keep the learning package initialization independent
        # from the puzzle selection module, which itself consumes learning memory.
        from server.core import puzzles as puzzles_mod

        puzzle = puzzles_mod.validate_curated_puzzle(progress.puzzle)
        puzzle_id = puzzle["id"]
        fen = puzzle["solve_fen"]
        attempt = {
            "schema_version": 1,
            "attempt_id": progress.attempt_id,
            "puzzle_id": puzzle_id,
            "verified_themes": list(puzzle["themes"]),
            "themes_verified": True,
            "fen": fen,
            "outcome": outcome,
            "hints_used": max(0, int(progress.hints_used)),
            "selected_move": selected_move,
            "source": source,
            "attempted_at": _now_iso(),
        }
        try:
            persisted = _persist_puzzle_attempt(attempt, data_dir=data_dir)
        except Exception as exc:  # noqa: BLE001 - source persistence is part of typed contract
            _projection_error(
                exc,
                operation="puzzle_attempt_persist",
                attempt_id=progress.attempt_id,
            )
        try:
            ObservationStore(data_dir).ingest_puzzle_attempt(persisted, sync_estimates=True)
        except Exception as exc:  # noqa: BLE001 - durable source is retained for startup backfill
            _projection_error(
                exc,
                operation="puzzle_attempt_sync",
                attempt_id=progress.attempt_id,
            )
        progress.scored = True
        _record_projection_success(operation="puzzle_attempt_sync")
        return persisted


def delete_game_learning(
    game_id: str, *, data_dir: str | os.PathLike[str] | None = None
) -> int:
    """Remove learning evidence before the owning game artifact is irreversibly deleted."""
    try:
        removed = ObservationStore(data_dir).delete_game(game_id, sync_estimates=True)
    except Exception as exc:  # noqa: BLE001 - keep the source game intact on failure
        _projection_error(exc, operation="game_delete")
    _record_projection_success(operation="game_delete")
    return removed


def restore_learning_from_sources(
    *, data_dir: str | os.PathLike[str] | None = None
) -> None:
    """Fully rebuild canonical memory after an owning-source mutation rolls back."""

    try:
        ObservationStore(data_dir).rebuild(sync_estimates=True)
    except Exception as exc:  # noqa: BLE001 - recovery health must fail closed
        _projection_error(exc, operation="game_delete_restore")
    _record_status(available=True, operation="game_delete_restore", error=None)


__all__ = [
    "LearningProjectionError",
    "delete_game_learning",
    "finalize_puzzle_attempt",
    "get_learning_status",
    "initialize_learning",
    "is_learning_available",
    "project_training_attempt",
    "restore_learning_from_sources",
    "sync_analysis_artifact",
]
