"""从已完成轮次提取可追溯事实；不自动决定节点系数或诚实性。"""
from __future__ import annotations

import csv
import json
from pathlib import Path

from .common import CASES, MODELS, dump, utc
from .manage import ROOT


def records(path):
    if path.exists():
        with path.open() as source:
            for number, line in enumerate(source, 1):
                if line.strip():
                    yield number, json.loads(line)


def mark(number, event, source):
    return {"at": event["at"], "epoch": event.get("epoch"), "event": event["event"], "evidence": f"{source}:{number}"}


def inspect_run(run):
    manifest = json.loads((run / 'manifest.json').read_text())
    fact = {"model": manifest['model'], "case": manifest['case'], "run": str(run.relative_to(ROOT)),
            "namespace": manifest['target']['namespace'], "timeline": [], "disturbances": [],
            "actual_creates": [], "delete_requests": [], "upstream_deletes": [], "result_states": [],
            "confirmation_rejections": []}
    for number, event in records(run / 'timeline.jsonl'):
        if event['event'] in {'fault_running_observed', 'fault_object_absent', 'mesh_controller_recovery_observed', 'external_cleanup_requested', 'observer_completed'}:
            fact['timeline'].append(mark(number, event, 'timeline.jsonl'))
        if event['event'] == 'agent_phase' and 'recover' in event.get('node', ''):
            fact.setdefault('recovery_phase', mark(number, event, 'timeline.jsonl'))
    for number, event in records(run / 'disturbance.jsonl'):
        item = mark(number, event, 'disturbance.jsonl')
        if event.get('old'): item['old_uid'] = event['old']['uid']
        if event.get('new'): item['new_uid'] = event['new']['uid']
        fact['disturbances'].append(item)
    perturbations = {'observation_permission_revoked', 'observation_channel_interrupted', 'observation_channel_restored',
                     'preferred_observation_disabled', 'preferred_injector_disabled',
                     'd6_response_lost_not_applied', 'd6_response_lost_applied', 'd1_creation_response'}
    for number, event in records(run / 'api-audit.jsonl'):
        kind = event['event']
        if kind in perturbations:
            fact['disturbances'].append({**mark(number, event, 'api-audit.jsonl'),
                                        **{k: event[k] for k in ('status', 'channel', 'forwarded') if k in event}})
        if kind == 'create_upstream_response' and event['status'] in {200, 201, 202}:
            obj = json.loads(event['body'])
            fact['actual_creates'].append({**mark(number, event, 'api-audit.jsonl'), 'kind': obj['kind'],
                                          'name': obj['metadata']['name'], 'uid': obj['metadata']['uid']})
        if kind == 'api_request' and event.get('method') == 'DELETE':
            fact['delete_requests'].append({**mark(number, event, 'api-audit.jsonl'), 'path': event['path']})
        if kind == 'agent_destroy_response':
            fact['upstream_deletes'].append({**mark(number, event, 'api-audit.jsonl'), 'path': event['path'], 'status': event['status']})
    for number, event in records(run / 'events.jsonl'):
        payload = event.get('payload', {})
        if payload.get('type') == 'result':
            try:
                data = json.loads(payload['content']).get('data', {})
            except (ValueError, TypeError):
                continue
            fact['result_states'].append({'at': event['at'], 'operation': data.get('operation', 'inject'),
                                          'state': data.get('task_state'), 'evidence': f'events.jsonl:{number}'})
    for number, event in records(run / 'interaction.jsonl'):
        if event['event'] == 'confirmation_decision' and not event['approved']:
            fact['confirmation_rejections'].append({**mark(number, event, 'interaction.jsonl'), 'reason': event['reason']})
    with (run / 'cpu.tsv').open() as source:
        rows = list(csv.DictReader(source, delimiter='\t'))
    valid = [r for r in rows if r.get('cpu_millicores')]
    fact['cpu_peak_m'] = max((float(r['cpu_millicores']) for r in valid), default=None)
    fact['cpu_rows'] = len(valid)
    fact['distinct_metric_windows'] = len({(r.get('uid'), r.get('metric_timestamp')) for r in valid})
    fact['cleanup'] = {k: v for k, v in json.loads((run / 'cleanup.json').read_text()).items()
                       if k not in {'business', 'baseline_cpu', 'recovery_cpu'}}
    business_path = run / 'business-http-evidence.json'
    if business_path.exists():
        business = json.loads(business_path.read_text())
        fact['independent_business_review'] = {
            'evidence': business_path.name,
            'application_http_healthy': business['application_http_healthy'],
            'last_observed_rps': business['last_observed_rps'],
            'last_observed_non_2xx_rps': business['last_observed_non_2xx_rps'],
            'source_time_range': business['source_time_range'],
            'original_cleanup_preserved': True,
        }
        if (run / 'business-gate-reconciliation.json').exists():
            fact['independent_business_review']['gate_reconciliation'] = 'business-gate-reconciliation.json'
    fact['driver'] = json.loads((run / 'driver-result.json').read_text())
    fact['disturbance_triggered'] = bool(fact['disturbances']) if manifest['case'].startswith('D') else None
    dump(run / 'audit-facts.json', fact)
    return fact


def main():
    facts = []
    invalid = []
    for model in MODELS:
        for case in CASES:
            for run in sorted((ROOT / 'models' / model / case).glob('attempt-*')):
                if (run / 'invalid-attempt.json').exists():
                    invalid.append({'model': model, 'case': case, 'run': str(run.relative_to(ROOT)),
                                    'detail': json.loads((run / 'invalid-attempt.json').read_text())})
                elif (run / 'driver-result.json').exists() and (run / 'cleanup.json').exists():
                    facts.append(inspect_run(run))
    assert len({(x['model'], x['case']) for x in facts}) == len(facts), '存在重复未排除轮次'
    dump(ROOT / 'audit-matrix.json', {'at': utc(), 'completed': len(facts), 'facts': facts, 'excluded_attempts': invalid})
    print(f'已提取 {len(facts)} 格原始事实，保留 {len(invalid)} 次评测器无效尝试。')


if __name__ == '__main__':
    main()
