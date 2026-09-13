"""Provider usage normalization and token-weighted cache statistics (no I/O)."""
from __future__ import annotations

import math
from typing import Any, Iterable


def _get(source: Any, key: str) -> Any:
    fields_set = getattr(source, "model_fields_set", None)
    if fields_set is not None and key not in fields_set:
        return None
    return source.get(key) if isinstance(source, dict) else getattr(source, key, None)


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0


def normalize_usage(source: Any, *, protocol: str) -> dict:
    """Parse native Chat Completions or Responses fields; missing cache is unknown."""
    if protocol not in {'chat_completions', 'responses'}:
        raise ValueError('Unsupported usage protocol')
    chat = protocol == 'chat_completions'
    result = {}
    fields = {'requests': 'requests', 'input_tokens': 'prompt_tokens' if chat else 'input_tokens',
              'output_tokens': 'completion_tokens' if chat else 'output_tokens', 'total_tokens': 'total_tokens'}
    for target, key in fields.items():
        value = _get(source, key)
        if _number(value):
            result[target] = value
    reasoning = _get(_get(source, 'completion_tokens_details' if chat else 'output_tokens_details'), 'reasoning_tokens')
    if _number(reasoning):
        result['reasoning_tokens'] = reasoning
    total = result.get('input_tokens')
    hit = _get(source, 'prompt_cache_hit_tokens') if chat else None
    miss = _get(source, 'prompt_cache_miss_tokens') if chat else None
    if hit is None:
        hit = _get(_get(source, 'prompt_tokens_details' if chat else 'input_tokens_details'), 'cached_tokens')
    if _number(total) and _number(hit) and hit <= total:
        if miss is None:
            miss = total - hit
        if _number(miss) and hit + miss == total:
            result.update(input_cache_hit_tokens=hit, input_cache_miss_tokens=miss)
    return result


def cache_statistics(usages: Iterable[Any]) -> dict:
    complete = []
    for usage in usages:
        hit, miss = (_get(usage, key) for key in ('input_cache_hit_tokens', 'input_cache_miss_tokens'))
        if _number(hit) and _number(miss):
            complete.append((hit, miss))
    hits = sum(hit for hit, _ in complete)
    misses = sum(miss for _, miss in complete)
    return {'reported_record_count': len(complete), 'hit_tokens': hits, 'miss_tokens': misses,
            'hit_rate': hits / (hits + misses) if hits + misses else None}
