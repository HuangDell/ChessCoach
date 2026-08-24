"""Structured, deterministic retrieval over canonical learning estimates.

This boundary deliberately has no prompt or model behavior.  It validates every
focus against the versioned taxonomy, reads only rebuildable canonical estimates,
and produces bounded summaries backed by verified chess references.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable
import hashlib
import os
from typing import Any

from server import config
from server.core.agent.models import (
    ChessReference,
    LearningMemoryItem,
    MemoryQuery,
    SkillEstimate,
)
from server.core.learning.estimates import EstimateStore, rank_estimates
from server.core.learning import taxonomy


EstimateLoader = Callable[[str], Iterable[SkillEstimate]]


def health_status() -> dict[str, Any]:
    """Return process health without reading learning storage."""

    try:
        from server.core import learning

        load = getattr(learning, "get_learning_status", None)
        if load is None:
            return {"initialized": False, "available": True, "operation": None, "error": None}
        status = load()
        return dict(status) if isinstance(status, dict) else {"available": False}
    except Exception:  # noqa: BLE001 - status inspection itself fails closed
        return {"available": False}


def is_available() -> bool:
    """Return process learning health without touching any learning files.

    The status API is installed by the application lifecycle.  Core-only callers
    and tests remain usable before lifecycle initialization.
    """

    try:
        from server.core import learning

        check = getattr(learning, "is_learning_available", None)
        return True if check is None else bool(check())
    except Exception:  # noqa: BLE001 - health itself must fail closed
        return False


def _resolve_focus(query: MemoryQuery) -> str | None | bool:
    skill = (
        taxonomy.resolve_skill_id(query.focus_skill_id)
        if query.focus_skill_id is not None
        else None
    )
    category = (
        taxonomy.resolve_skill_id(query.focus_category)
        if query.focus_category is not None
        else None
    )
    if query.focus_skill_id is not None and skill is None:
        return False
    if query.focus_category is not None and category is None:
        return False
    if skill is not None and category is not None and skill != category:
        return False
    return skill or category


def _current_fact_skills(current_facts: dict[str, Any]) -> set[str]:
    if not current_facts:
        return set()
    try:
        mappings = taxonomy.map_fact_evidence(current_facts)
    except (TypeError, ValueError):
        return set()
    return {item.skill_id for item in mappings}


def retrieve_estimates(
    query: MemoryQuery,
    *,
    data_dir: str | os.PathLike[str] | None = None,
    personalization_enabled: bool | None = None,
    estimate_loader: EstimateLoader | None = None,
) -> list[SkillEstimate]:
    """Return canonical estimates selected by one validated memory query.

    Invalid or conflicting focus values return no data.  Storage failures are left
    to the caller so tool adapters can expose their typed degradation path.
    """

    enabled = config.PERSONALIZE_HISTORY if personalization_enabled is None else bool(
        personalization_enabled
    )
    if not enabled or not is_available():
        return []
    focus = _resolve_focus(query)
    if focus is False:
        return []
    loader = estimate_loader or (
        lambda window: EstimateStore(data_dir or config.DATA_DIR).ensure_current(window=window)
    )
    estimates = [
        estimate
        for estimate in loader(query.window)
        if estimate.evidence_count > 0 and bool(estimate.examples)
    ]
    ranked = rank_estimates(
        estimates,
        focus_skill_id=focus if isinstance(focus, str) else None,
        relevant_skill_ids=_current_fact_skills(query.current_facts),
    )
    if isinstance(focus, str):
        ranked = [estimate for estimate in ranked if estimate.skill_id == focus]
    return ranked[: query.limit]


def _reference_identity(reference: ChessReference) -> str:
    payload = reference.model_dump_json(exclude_none=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def evidence_refs_for_estimate(estimate: SkillEstimate) -> list[str]:
    return [
        f"learning:{estimate.skill_id}:{_reference_identity(reference)}"
        for reference in estimate.examples
    ]


def _summary(estimate: SkillEstimate, *, window: str) -> str:
    definition = taxonomy.get_skill_definition(estimate.skill_id)
    label = definition.label if definition is not None else estimate.skill_id
    period = "Recent" if window == "recent" else "Lifetime"
    scope = (
        f"{estimate.evidence_count} observations across "
        f"{estimate.distinct_positions} positions"
    )
    if estimate.status == "weakness":
        return f"{period} evidence marks {label} as an {estimate.confidence_level} weakness ({scope})."
    if estimate.status == "strength":
        return f"{period} evidence marks {label} as an {estimate.confidence_level} strength ({scope})."
    if estimate.status == "watch":
        return f"{period} evidence puts {label} on watch ({scope}); it is not an established weakness."
    return f"{period} evidence for {label} is not yet a strength or weakness ({scope})."


def retrieve_memory(
    query: MemoryQuery,
    *,
    data_dir: str | os.PathLike[str] | None = None,
    personalization_enabled: bool | None = None,
    estimate_loader: EstimateLoader | None = None,
) -> list[LearningMemoryItem]:
    """Retrieve up to five deterministic learning-memory items.

    A damaged or unavailable learning store degrades to no memory.  Agent tools use
    ``retrieve_estimates`` directly when they need to preserve the typed error.
    """

    enabled = config.PERSONALIZE_HISTORY if personalization_enabled is None else bool(
        personalization_enabled
    )
    if not enabled:
        return []
    try:
        estimates = retrieve_estimates(
            query,
            data_dir=data_dir,
            personalization_enabled=True,
            estimate_loader=estimate_loader,
        )
    except Exception:  # noqa: BLE001 - optional memory must not affect Engine Review
        return []
    return [
        LearningMemoryItem(
            skill_id=estimate.skill_id,
            summary=_summary(estimate, window=query.window),
            status=estimate.status,
            confidence_level=estimate.confidence_level,
            window=query.window,
            evidence_count=estimate.evidence_count,
            window_games=estimate.distinct_games,
            examples=estimate.examples,
            evidence_refs=evidence_refs_for_estimate(estimate),
        )
        for estimate in estimates
    ]


__all__ = [
    "EstimateLoader",
    "evidence_refs_for_estimate",
    "health_status",
    "is_available",
    "retrieve_estimates",
    "retrieve_memory",
]
