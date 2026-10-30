#!/usr/bin/env python3
"""Inject one random policy fault through values.yaml, or restore the baseline.

Run by the student. Diagnose using Whisker before reading the diff or using
reveal. State and exact backups stay under the ignored evidence directory.
"""
import argparse
import copy
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys

import yaml

ROOT = Path(__file__).resolve().parents[1]
VALUES = ROOT / 'charts/shop/values.yaml'
STATE_ROOT = ROOT / 'evidence/part6'
ACTIVE = STATE_ROOT / 'active.json'

# The catalogue is public; the random choice is saved privately, not printed.
FAULTS = [
    ('wrong-db-egress-port', ['app-to-postgres', 'egress', 0, 'ports', 0, 'port'], 5432, 5433),
    ('wrong-db-ingress-label', ['postgres-from-app', 'ingress', 0, 'from', 0,
                              'podSelector', 'matchLabels', 'app.kubernetes.io/instance'], 'shop', 'shop-typo'),
    ('missing-app-dns', ['dns', 'podSelector', 'matchExpressions', 0, 'values'],
     ['api', 'worker', 'postgres', 'chaos-load'], ['postgres', 'chaos-load']),
    ('missing-worker-egress', ['app-to-postgres', 'podSelector', 'matchExpressions', 0, 'values'],
     ['api', 'worker'], ['api']),
]


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


def at_path(value, path):
    for key in path:
        value = value[key]
    return value


def replace_value(text, path, value):
    """Replace exactly one YAML node while retaining all unrelated formatting."""
    document = yaml.safe_load(text)
    expected = copy.deepcopy(document)
    parent = at_path(expected, path[:-1])
    parent[path[-1]] = value
    node = yaml.compose(text)
    for key in path:
        if isinstance(key, int):
            node = node.value[key]
        else:
            node = next(v for k, v in node.value if k.value == key)
    replacement = yaml.safe_dump(value, default_flow_style=True).strip()
    if replacement.endswith('\n...'):
        replacement = replacement[:-4]
    result = text[:node.start_mark.index] + replacement + text[node.end_mark.index:]
    if yaml.safe_load(result) != expected:
        raise ValueError('YAML replacement changed more than the selected value')
    return result


def make_fault(text, fault):
    _, relative_path, old, new = fault
    path = ['networkPolicy', 'policies', *relative_path]
    if at_path(yaml.safe_load(text), path) != old:
        raise ValueError('Baseline differs from the expected working configuration')
    return replace_value(text, path, new)


def private_write(path, text):
    fd = os.open(path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(fd, 'w') as stream:
        stream.write(text)


def save_state(state):
    content = json.dumps(state, ensure_ascii=False, indent=2) + '\n'
    private_write(STATE_ROOT / state['run'] / 'state.json', content)
    private_write(ACTIVE, content)


def read_state():
    if not ACTIVE.exists():
        raise ValueError('No recorded fault experiment')
    return json.loads(ACTIVE.read_text())


def run(command):
    subprocess.run(command, cwd=ROOT, check=True)


def apply_and_restart(context):
    run(['helm', '--kube-context', context, 'upgrade', 'shop', 'charts/shop',
         '--namespace', 'shop', '--reset-values', '--wait', '--timeout', '5m'])
    k = ['kubectl', '--context', context, '-n', 'shop', '--request-timeout=180s']
    # /health is independent of the database. Fresh Pods ensure fresh DNS and
    # database connections for every candidate, even if conntrack keeps old flows.
    run(k + ['rollout', 'restart', 'deployment/shop-api', 'deployment/shop-worker'])
    for name in ('shop-api', 'shop-worker'):
        run(k + ['rollout', 'status', 'deployment/' + name, '--timeout=180s'])


def inject(context):
    if ACTIVE.exists() and read_state()['phase'] != 'restored':
        raise ValueError('An experiment is already active; diagnose or restore it first')
    original = VALUES.read_text()
    baseline = yaml.safe_load(original)
    if not baseline.get('networkPolicy', {}).get('enabled'):
        raise ValueError('NetworkPolicy must be enabled before this experiment')
    for fault in FAULTS:
        make_fault(original, fault)  # Verify the catalogue against the baseline.
    live = json.loads(subprocess.check_output(
        ['helm', '--kube-context', context, '-n', 'shop', 'get', 'values', 'shop', '--all', '-o', 'json'],
        cwd=ROOT, text=True))
    if live.get('networkPolicy') != baseline['networkPolicy']:
        raise ValueError('Local NetworkPolicy values differ from the deployed release')
    chosen = secrets.choice(FAULTS)
    changed = make_fault(original, chosen)
    run_name = 'fault-' + datetime.now().strftime('%Y%m%d-%H%M%S') + '-' + secrets.token_hex(2)
    directory = STATE_ROOT / run_name
    directory.mkdir(parents=True, exist_ok=False)
    private_write(directory / 'baseline-values.yaml', original)
    private_write(directory / 'fault-values.yaml', changed)
    state = {'run': run_name, 'context': context, 'phase': 'prepared',
             'started_at': datetime.now().astimezone().isoformat(),
             'fault': chosen[0], 'path': chosen[1], 'old': chosen[2], 'new': chosen[3],
             'baseline_sha256': digest(original), 'fault_sha256': digest(changed)}
    save_state(state)
    VALUES.write_text(changed)
    run(['helm', 'lint', 'charts/shop'])
    state['phase'] = 'applying'
    save_state(state)
    apply_and_restart(context)
    state['phase'] = 'injected'
    state['injected_at'] = datetime.now().astimezone().isoformat()
    save_state(state)
    print('One hidden fault applied. API and worker now use fresh connections.')
    print('Next: generate orders and diagnose denied flows in Whisker.')
    print('Saved experiment: ' + str(directory.relative_to(ROOT)))


def restore():
    state = read_state()
    if state['phase'] == 'restored':
        print('Baseline already restored.')
        return
    original = (STATE_ROOT / state['run'] / 'baseline-values.yaml').read_text()
    if digest(original) != state['baseline_sha256']:
        raise ValueError('Baseline backup checksum mismatch')
    if digest(VALUES.read_text()) not in (state['baseline_sha256'], state['fault_sha256']):
        raise ValueError('values.yaml has additional edits; restore refuses to overwrite them')
    state['phase'] = 'restoring'
    save_state(state)
    VALUES.write_text(original)
    apply_and_restart(state['context'])
    state['phase'] = 'restored'
    state['restored_at'] = datetime.now().astimezone().isoformat()
    save_state(state)
    print('Original values restored. Verify orders and rerun the access matrix.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['inject', 'restore', 'reveal'])
    parser.add_argument('--context', default='kind-lab5', help='Context for injection; restore uses the recorded context')
    args = parser.parse_args()
    try:
        if args.action == 'inject':
            inject(args.context)
        elif args.action == 'restore':
            restore()
        else:
            state = read_state()
            print(json.dumps({k: state[k] for k in ('fault', 'path', 'old', 'new', 'phase')}, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, KeyError, OSError, subprocess.CalledProcessError) as exc:
        print('ERROR: ' + str(exc), file=sys.stderr)
        print('No automatic rollback. If changes were applied, use restore to return to the saved baseline.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
