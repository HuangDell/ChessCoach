"""Explicit, local-only frozen Explanation evaluation; never part of default tests."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from server import config
from server.core.explanation.builder import build_request


def write_json(path: Path, value: object) -> None:
    path.touch(mode=0o600, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2))


def freeze(output: Path) -> None:
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    root = Path(config.DATA_DIR)
    analyses = [(p, json.loads(p.read_text())) for p in sorted(
        root.glob('games/*/analysis.json'), key=lambda p: (-p.stat().st_mtime, str(p)))]
    baseline_ids = ['ply-28', 'ply-36', 'ply-60', 'ply-54', 'ply-44', 'ply-26', 'ply-48', 'ply-46']
    selected = []
    traces = sorted(root.glob('agent/traces/20260912T13*-explanation-*/001-request.json'))
    for trace in traces:
        raw = json.loads(trace.read_text())
        context = json.loads(raw['messages'][1]['content'].split('position_context:\n', 1)[1])
        pos = context['engine']['position']
        for _, analysis in analyses:
            critical = next((c for c in analysis.get('critical_positions', [])
                             if c['critical_id'] == pos['critical_id'] and c['fen_before'] == pos['fen_before']), None)
            if critical:
                selected.append((analysis, critical))
                break
    selection = 'historical-baseline'
    if [c['critical_id'] for _, c in selected] != baseline_ids:
        selection = 'latest-analysis-fallback'
        candidates = [(a, c) for _, a in analyses for c in a.get('critical_positions', [])]
        no_motif = [pair for pair in candidates if not pair[1].get('facts', {}).get('motifs')][:2]
        selected = no_motif + [pair for pair in candidates if pair not in no_motif][:8-len(no_motif)]
    if len(selected) != 8:
        raise ValueError('Need eight available critical positions.')
    cases = []
    for analysis, critical in selected:
        request = build_request(analysis, critical)
        cases.append({'analysis': analysis, 'critical_id': critical['critical_id'],
                      'memory': request.payload.get('relevant_memory', []),
                      'old_request': request.model_dump(mode='json')})
    frozen = {'selection': selection, 'model': config.EXPLANATION_MODEL,
              'base_url': config.EXPLANATION_BASE_URL, 'language': config.EXPLANATION_LANGUAGE,
              'temperature': 0.2, 'cases': cases}
    write_json(output / 'frozen.json', frozen)
    print(json.dumps({'selection': selection, 'cases': len(cases),
                      'no_motif': sum(not c.get('facts', {}).get('motifs') for _, c in selected),
                      'sha256': hashlib.sha256((output / 'frozen.json').read_bytes()).hexdigest()}))


def prepare(directory: Path) -> None:
    from copy import deepcopy
    from unittest.mock import patch
    from server.core.agent.models import LearningMemoryItem
    from server.core.facts_projection import fact_evidence_refs, project_facts, resolve_fact_ref

    frozen = json.loads((directory / 'frozen.json').read_text())
    requests = []
    for case in frozen['cases']:
        analysis = case['analysis']
        critical = next(c for c in analysis['critical_positions'] if c['critical_id'] == case['critical_id'])
        original = deepcopy(critical)
        with patch('server.core.explanation.builder._relevant_memory', return_value=[
            LearningMemoryItem.model_validate(item) for item in case['memory']
        ]), patch.object(config, 'EXPLANATION_LANGUAGE', frozen['language']):
            request = build_request(analysis, critical)
        facts = request.payload['facts']
        for ref in fact_evidence_refs(critical['facts']):
            source = {**critical['facts'], 'signals': critical.get('signals') or []}
            assert resolve_fact_ref(source, ref) == resolve_fact_ref(facts, ref)
        for ref in request.allowed_evidence_refs:
            if not ref.startswith('learning:'):
                node = request.payload
                for part in ref.split('.'):
                    node = (node[int(part)] if part.isdecimal() else next(x for x in node if x == part)) if isinstance(node, list) else node[part]
        assert critical == original
        assert facts == project_facts(critical['facts'], critical.get('signals') or [])
        requests.append(request.model_dump(mode='json'))
    target = directory / 'new_requests.json'
    with target.open('x') as stream:
        json.dump(requests, stream, ensure_ascii=False, sort_keys=True, indent=2)
    old_size = sum(len(c['old_request']['user_prompt']) for c in frozen['cases'])
    new_size = sum(len(r['user_prompt']) for r in requests)
    print(json.dumps({'references_valid': True, 'old_context_chars': old_size,
                      'new_context_chars': new_size, 'character_reduction': 1-new_size/old_size}))


def run(directory: Path) -> None:
    import time
    from server.core.explanation.models import ExplanationRequest
    from server.core.explanation.providers import OpenAICompatibleProvider
    from server.core.explanation.service import _validated_explanation
    from server.core.model_usage import normalize_usage
    from server.core.storage.agent_traces import RawHttpTraceStore

    frozen = json.loads((directory / 'frozen.json').read_text())
    new = json.loads((directory / 'new_requests.json').read_text())
    if len(frozen['cases']) != 8 or len(new) != 8 or frozen['temperature'] != 0.2:
        raise ValueError('Comparison requires exactly eight paired requests')
    if (config.EXPLANATION_MODEL, config.EXPLANATION_BASE_URL, config.EXPLANATION_LANGUAGE) != (
        frozen['model'], frozen['base_url'], frozen['language']
    ):
        raise ValueError('Configured model, endpoint or language differs from frozen configuration')
    # Exclusive creation prevents accidental replay or automatic retries after interruption.
    runs = directory / 'calls'
    runs.mkdir(exist_ok=False)
    for round_index, variant in enumerate(('old', 'new', 'new', 'old')):
        for index, case in enumerate(frozen['cases']):
            call = round_index * 8 + index
            request = ExplanationRequest.model_validate(case['old_request'] if variant == 'old' else new[index])
            call_dir = runs / f'{call:02d}'
            call_dir.mkdir()
            write_json(call_dir / 'request.json', request.model_dump(mode='json'))
            record = {'call': call, 'case': index, 'variant': variant, 'round': round_index,
                      'has_motif': bool(case['old_request']['payload']['facts'].get('motifs')),
                      'model_calls': 1, 'valid': False, 'usage': {}}
            write_json(call_dir / 'record.json', record)
            provider = OpenAICompatibleProvider(
                base_url=frozen['base_url'], model=frozen['model'], api_key=config.EXPLANATION_API_KEY,
                raw_trace_store=RawHttpTraceStore(call_dir))
            started = time.monotonic()
            try:
                response = provider.explain_position(request)
                write_json(call_dir / 'response.json', {'text': response.text})
                validated = _validated_explanation(response.text, request)
                write_json(call_dir / 'validated.json', validated.model_dump(mode='json'))
                record['valid'] = True
            except Exception as exc:
                # Detailed errors stay local; console/report expose only the exception type.
                write_json(call_dir / 'error.json', {'type': type(exc).__name__, 'message': str(exc), 'cause': type(exc.__cause__).__name__ if exc.__cause__ else None})
                record['error_type'] = type(exc).__name__
            record['duration_ms'] = round((time.monotonic()-started)*1000)
            traces = sorted(call_dir.glob('agent/traces/*/*-response.json'))
            if traces:
                try:
                    raw = json.loads(traces[-1].read_text())
                    record['usage'] = normalize_usage(raw.get('usage'), protocol='chat_completions')
                except (ValueError, UnicodeError):
                    pass
            write_json(call_dir / 'record.json', record)
            print(json.dumps(record), flush=True)
            if not traces and record.get('error_type') == 'ExplanationProviderError':
                raise RuntimeError('Transport failed before a response; stopped without retry')
    report(directory)


def report(directory: Path) -> None:
    from server.core.model_usage import cache_statistics
    frozen = json.loads((directory / 'frozen.json').read_text())
    records = [json.loads(p.read_text()) for p in sorted(directory.glob('calls/*/record.json'))]
    def aggregate(rows: list[dict]) -> dict:
        valid = sum(row['valid'] for row in rows)
        tokens = {key: sum(row['usage'].get(key, 0) for row in rows) for key in (
            'input_tokens', 'output_tokens', 'reasoning_tokens')}
        return {**tokens, 'usage_reported_calls': sum('input_tokens' in r['usage'] for r in rows),
                'reasoning_reported_calls': sum('reasoning_tokens' in r['usage'] for r in rows),
                'cache': cache_statistics(row['usage'] for row in rows),
                'model_calls': len(rows), 'valid': valid, 'valid_rate': valid/len(rows) if rows else None,
                'input_per_valid': tokens['input_tokens']/valid if valid else None,
                'duration_ms': sum(row.get('duration_ms', 0) for row in rows)}
    result = {'model': frozen['model'], 'language': frozen['language'], 'temperature': frozen['temperature'],
              'selection': frozen['selection'],
              'data_sha256': hashlib.sha256((directory / 'frozen.json').read_bytes()).hexdigest(),
              'new_requests_sha256': hashlib.sha256((directory / 'new_requests.json').read_bytes()).hexdigest(),
              'first_measurement_is_cold': False, 'quality_review': 'pending',
              'variants': {v: aggregate([r for r in records if r['variant'] == v]) for v in ('old', 'new')},
              'rounds': {str(i): aggregate([r for r in records if r['round'] == i]) for i in range(4)},
              'groups': {v: {group: aggregate([r for r in records if r['variant'] == v and r['has_motif'] == motif])
                            for group, motif in [('motif', True), ('no_motif', False)]} for v in ('old', 'new')}}
    # Anonymous interleaving: identities and the key are separate from the quality review file.
    order = sorted(range(32), key=lambda call: hashlib.sha256(f"blind:{call}".encode()).hexdigest())
    prior_path = directory / 'blind_review.json'
    prior = {
        entry['id']: entry['review'] for entry in json.loads(prior_path.read_text())
    } if prior_path.exists() else {}
    blind, key = [], {}
    for row in sorted(records, key=lambda row: order.index(row['call'])):
        label = f"sample-{order.index(row['call'])+1:02d}"
        call_dir = directory / 'calls' / f"{row['call']:02d}"
        response = call_dir / 'response.json'
        if not response.exists():
            continue
        # Always supply the same full source facts to the reviewer, without prompt version.
        case = frozen['cases'][row['case']]
        blind.append({'id': label, 'response': json.loads(response.read_text()),
                      'source': case['old_request']['payload'],
                      'review': prior.get(label, {
                          'core_problem': None, 'board_reasons': None, 'unsupported_claims': None})})
        key[label] = {'variant': row['variant'], 'case': row['case'], 'round': row['round']}
    write_json(directory / 'blind_review.json', blind)
    write_json(directory / 'blind_key.json', key)
    reviewed = json.loads((directory / 'blind_review.json').read_text())
    quality = {}
    for variant in ('old', 'new'):
        quality[variant] = {}
        for group, motif in [('all', None), ('motif', True), ('no_motif', False)]:
            selected = [entry for entry in reviewed if key[entry['id']]['variant'] == variant
                        and (motif is None or bool(frozen['cases'][key[entry['id']]['case']]
                                                 ['old_request']['payload']['facts'].get('motifs')) == motif)]
            complete = [entry for entry in selected if all(isinstance(entry['review'].get(field), bool)
                        for field in ('core_problem', 'board_reasons', 'unsupported_claims'))]
            quality[variant][group] = {
                'reviewed': len(complete),
                **{field: sum(entry['review'][field] for entry in complete) / len(complete) if complete else None
                   for field in ('core_problem', 'board_reasons', 'unsupported_claims')},
            }
    result['quality'] = quality
    quality_complete = sum(quality[v]['all']['reviewed'] for v in ('old', 'new')) == 32
    result['quality_review'] = 'complete' if quality_complete else 'pending'
    old, new = (result['variants'][v] for v in ('old', 'new'))
    input_complete = all(row['usage_reported_calls'] == 16 for row in (old, new))
    cache_complete = all(row['cache']['reported_record_count'] == 16 for row in (old, new))
    result['input_reduction'] = 1-new['input_tokens']/old['input_tokens'] if input_complete and old['input_tokens'] else None
    result['success'] = None
    if quality_complete and input_complete and cache_complete:
        result['success'] = (
            result['input_reduction'] >= 0.30 and new['valid_rate'] >= old['valid_rate']
            and new['cache']['miss_tokens'] <= old['cache']['miss_tokens']
            and new['input_per_valid'] is not None and old['input_per_valid'] is not None
            and new['input_per_valid'] <= old['input_per_valid']
            and all(quality['new'][g][f] >= quality['old'][g][f]
                    for g in ('all', 'motif', 'no_motif') for f in ('core_problem', 'board_reasons'))
            and all(quality['new'][g]['unsupported_claims'] <= quality['old'][g]['unsupported_claims']
                    for g in ('all', 'motif', 'no_motif'))
        )
    write_json(directory / 'report.json', result)
    print(json.dumps(result, ensure_ascii=False))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['freeze', 'prepare', 'run', 'report'])
    parser.add_argument('--directory', type=Path, required=True)
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[2]
    if args.directory.resolve().is_relative_to(repo):
        parser.error('Frozen personal data and raw responses must be outside the repository')
    {'freeze': freeze, 'prepare': prepare, 'run': run, 'report': report}[args.command](args.directory)


if __name__ == '__main__':
    main()
