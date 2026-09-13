"""Pure model-visible facts, with exact evidence restored from the source artifact."""
from __future__ import annotations

from copy import deepcopy
from typing import Any


class FactsProjectionError(ValueError):
    """The source artifact cannot support one of its own evidence references."""


def resolve_fact_ref(facts: dict, reference: str) -> Any:
    """Resolve dotted nodes, numeric list indices, and named signal membership."""
    parts = reference.removeprefix('facts.').split('.')
    node: Any = facts
    try:
        for part in parts:
            if isinstance(node, list):
                node = node[int(part)] if part.isdecimal() else next(x for x in node if x == part)
            else:
                node = node[part]
    except (KeyError, IndexError, TypeError, StopIteration) as exc:
        raise FactsProjectionError(f'Missing required facts evidence: {reference}') from exc
    if node is None:
        raise FactsProjectionError(f'Missing required facts evidence: {reference}')
    return node


def fact_evidence_refs(facts: dict) -> list[str]:
    refs = list(facts.get('classification_evidence') or [])
    for motif in facts.get('motifs') or []:
        refs.extend(motif.get('evidence_refs') or [])
    return sorted(set(refs))


def _changes(before: Any, after: Any) -> Any:
    """Changed leaf values after one move; omitted leaves are unchanged, not zero."""
    if isinstance(before, dict) and isinstance(after, dict):
        return {key: _changes(before.get(key), value) for key, value in after.items()
                if before.get(key) != value}
    return deepcopy(after)


def project_facts(facts: dict, signals: list[str]) -> dict:
    """Keep teaching facts and compact background without modifying either input.

    Snapshots describe one move. Line results and existing deltas describe the
    replayed variation endpoints. Lists preserve source order, including restored
    evidence branches (no truncation or sparse index substitution).
    """
    source = {**facts, 'signals': signals}
    result = deepcopy({key: value for key, value in source.items()
                       if key not in {'snapshots', 'opponent_direct_replies'}})
    replies = facts.get('opponent_direct_replies') or {}
    result['opponent_direct_replies'] = deepcopy({
        key: value for key, value in replies.items() if key not in {'checks', 'captures'}})
    snapshots = facts.get('snapshots') or {}
    before = snapshots.get('before') or {}
    background_keys = ('turn', 'in_check', 'phase', 'material', 'mobility', 'king_safety', 'structure')
    background = {key: deepcopy(before[key]) for key in background_keys if key in before}
    if isinstance(background.get('phase'), dict):
        background['phase'] = background['phase'].get('name')
    comparison = {'before': background}
    for name in ('after_played', 'after_best'):
        after = snapshots.get(name) or {}
        comparison[name + '_changed_values'] = {
            key: _changes(before.get(key), after[key])
            for key in ('material', 'mobility', 'king_safety', 'structure')
            if key in after and before.get(key) != after[key]
        }
    result['snapshot_comparison'] = comparison
    for reference in fact_evidence_refs(facts):
        resolve_fact_ref(source, reference)
        parts = reference.removeprefix('facts.').split('.')
        original, target = source, result
        for index, part in enumerate(parts):
            value = original[part]
            # A cited branch is exact; lists remain complete to preserve indices/order.
            if index == len(parts) - 1 or isinstance(value, list):
                target[part] = deepcopy(value)
                break
            original = value
            target = target.setdefault(part, {})
        resolve_fact_ref(result, reference)
    return result
