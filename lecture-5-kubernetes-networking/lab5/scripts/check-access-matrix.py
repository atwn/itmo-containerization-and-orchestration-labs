#!/usr/bin/env python3
"""Live NetworkPolicy checks from real Pod network namespaces; run by the student.

Adds restricted ephemeral containers and one temporary stray Pod. Never changes
NetworkPolicy, workload labels, Services, or application replicas. A timeout is
accepted as DENY only with passing positive controls to the same destination
before and after the checks. Only the external control may retry timeouts.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import ipaddress
import json
from pathlib import Path
import socket
import subprocess
import sys
import time
import uuid


def ready(pod):
    return (not pod['metadata'].get('deletionTimestamp')
            and pod['status'].get('phase') == 'Running'
            and any(c['type'] == 'Ready' and c['status'] == 'True'
                    for c in pod['status'].get('conditions', [])))


def identity(pod):
    return {'name': pod['metadata']['name'], 'namespace': pod['metadata']['namespace'],
            'uid': pod['metadata']['uid'], 'ip': pod['status']['podIP'],
            'node': pod['spec']['nodeName'], 'labels': pod['metadata'].get('labels', {})}


def classify_row(tests, expected, controls_ok):
    statuses = [test['status'] for test in tests]
    wanted = 'ALLOW' if expected == 'ALLOW' else 'TIMEOUT'
    passed = sum(s == wanted for s in statuses)
    actual = ('ALLOW' if all(s == 'ALLOW' for s in statuses) else
              'DENY' if all(s == 'TIMEOUT' for s in statuses) else
              'ERROR' if 'ERROR' in statuses else 'MIXED')
    if expected == 'DENY' and actual == 'DENY' and not controls_ok:
        actual = 'UNVERIFIED'
    return actual, passed, passed == len(tests) and (expected == 'ALLOW' or controls_ok)


def target_key(probe, aliases):
    args = probe['args']
    if args[0] == 'tcp':
        host = args[1].rstrip('.').lower()
        return ('tcp', aliases.get(host, host), args[2])
    return tuple(args)


def target_controls(before, after, aliases):
    # An available endpoint must not become "unverified" because a different
    # endpoint (for example, the external Internet control) timed out.
    available_before = {target_key(p, aliases) for p in before if p['status'] == 'ALLOW'}
    available_after = {target_key(p, aliases) for p in after if p['status'] == 'ALLOW'}
    return available_before & available_after


def evaluate_groups(groups, before, after, aliases):
    available = target_controls(before, after, aliases)
    for group in groups:
        controls_ok = all(target_key(p, aliases) in available for p in group['results'])
        actual, passed, ok = classify_row(group['results'], group['expected'], controls_ok)
        group.update(actual=actual, passed=passed, ok=ok, controls_ok=controls_ok)


class Matrix:
    def __init__(self, args):
        self.args = args
        self.run_id = uuid.uuid4().hex[:8]
        self.container = 'matrix-' + self.run_id
        self.stray_name = 'matrix-stray-' + self.run_id
        self.stray_created = False
        self.helpers = []
        self.groups = []
        self.inventory = {}
        self.roles = {
            'api': ('shop', 'app.kubernetes.io/instance=shop,app.kubernetes.io/name=api'),
            'worker': ('shop', 'app.kubernetes.io/instance=shop,app.kubernetes.io/name=worker'),
            'postgres': ('shop', 'cnpg.io/cluster=shop-postgres'),
            'load': ('shop', 'app.kubernetes.io/name=chaos-load'),
            'prometheus': ('obs', 'app.kubernetes.io/name=prometheus,prometheus=kps-prometheus'),
            'operator': ('cnpg-system', 'app.kubernetes.io/name=cloudnative-pg,app.kubernetes.io/instance=cnpg'),
        }

    def k(self, *args, timeout=25):
        cmd = ['kubectl', '--context', self.args.context, '--request-timeout=15s', *args]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if result.returncode:
            raise RuntimeError(f"kubectl {' '.join(args[:4])}: {result.stderr.strip()}")
        return result.stdout

    def get(self, *args):
        return json.loads(self.k(*args, '-o', 'json'))

    def pods(self, ns, selector):
        pods = self.get('-n', ns, 'get', 'pods', '-l', selector)['items']
        if not pods or not all(ready(p) for p in pods):
            raise RuntimeError(f'Expected all selected Pods Ready: {ns} / {selector}')
        return sorted((identity(p) for p in pods), key=lambda p: p['name'])

    def wait_container(self, pod, ephemeral):
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            current = self.get('-n', pod['namespace'], 'get', 'pod', pod['name'])
            key = 'ephemeralContainerStatuses' if ephemeral else 'containerStatuses'
            for state in current['status'].get(key, []):
                if ephemeral and state['name'] != self.container:
                    continue
                if 'running' in state['state']:
                    return
                if 'terminated' in state['state']:
                    raise RuntimeError(f'Helper terminated: {pod["name"]}: {state["state"]}')
            time.sleep(1)
        raise RuntimeError(f'Helper not running: {pod["namespace"]}/{pod["name"]}; check image loading/events')

    def prepare(self):
        policies = self.get('-n', 'shop', 'get', 'networkpolicy')['items']
        default = next((p for p in policies if p['metadata']['name'] == 'shop-default-deny'), None)
        if not default or default['spec'].get('podSelector') != {} or set(default['spec'].get('policyTypes', [])) != {'Ingress', 'Egress'}:
            raise RuntimeError('Expected shop-default-deny selecting all Pods in both directions')
        self.policy_specs = {p['metadata']['name']: p['spec'] for p in policies}
        for role, (ns, selector) in self.roles.items():
            self.inventory[role] = self.pods(ns, selector)
        self.service = self.get('-n', 'shop', 'get', 'service', 'shop-api')
        self.pg_service = self.get('-n', 'shop', 'get', 'service', 'shop-postgres-rw')
        self.target_aliases = {
            'shop-api.shop.svc.cluster.local': self.service['spec']['clusterIP'],
            'shop-postgres-rw.shop.svc.cluster.local': self.pg_service['spec']['clusterIP'],
        }
        self.dns_ip = self.get('-n', 'kube-system', 'get', 'service', 'kube-dns')['spec']['clusterIP']
        self.kube_service = self.get('-n', 'default', 'get', 'service', 'kubernetes')
        slices = self.get('-n', 'default', 'get', 'endpointslice', '-l', 'kubernetes.io/service-name=kubernetes')['items']
        self.kube_endpoints = [(ip, port['port']) for sl in slices for ep in sl['endpoints']
                               if ep.get('conditions', {}).get('ready', True)
                               for ip in ep['addresses'] for port in sl['ports'] if port.get('protocol', 'TCP') == 'TCP']
        if not self.kube_endpoints:
            raise RuntimeError('No Kubernetes API endpoints')
        self.nodes = self.get('get', 'nodes')['items']
        print('Adding diagnostic containers to real Pods (no workload label changes)...', flush=True)
        for pods in self.inventory.values():
            for pod in pods:
                self.k('-n', pod['namespace'], 'debug', 'pod/' + pod['name'],
                       '--container=' + self.container, '--image=' + self.args.image,
                       '--image-pull-policy=Never', '--profile=restricted', '--attach=false',
                       '--', '/netprobe', 'hold')
                self.helpers.append(pod)
                self.wait_container(pod, ephemeral=True)
        self.k('-n', 'shop', 'run', self.stray_name, '--image=' + self.args.image,
               '--image-pull-policy=Never', '--restart=Never',
               '--labels=app.kubernetes.io/name=matrix-probe', '--command', '--', '/netprobe', 'hold')
        self.stray_created = True
        stray = {'name': self.stray_name, 'namespace': 'shop'}
        self.wait_container(stray, ephemeral=False)
        self.inventory['stray'] = [identity(self.get('-n', 'shop', 'get', 'pod', self.stray_name))]

    def group(self, name, expected, probes):
        self.groups.append({'name': name, 'expected': expected, 'probes': probes})

    def tcp(self, sources, targets, port):
        return [{'source': s, 'args': ['tcp', t, str(port)]} for s in sources for t in targets]

    def plan(self):
        inv = self.inventory
        ips = lambda role: [p['ip'] for p in inv[role]]
        for role in ('api', 'worker'):
            self.group(role + ' -> PG Pod IP:5432', 'ALLOW', self.tcp(inv[role], ips('postgres'), 5432))
            self.group(role + ' -> PG Service DNS:5432', 'ALLOW', self.tcp(inv[role], ['shop-postgres-rw.shop.svc.cluster.local.'], 5432))
        self.group('load -> API Service DNS:80', 'ALLOW', self.tcp(inv['load'], ['shop-api.shop.svc.cluster.local.'], 80))
        for src, dst, port in [('api', 'worker', 8080), ('worker', 'api', 8080),
                               ('postgres', 'api', 8080), ('postgres', 'worker', 8080),
                               ('load', 'worker', 8080), ('load', 'postgres', 5432),
                               ('stray', 'api', 8080), ('stray', 'worker', 8080), ('stray', 'postgres', 5432)]:
            self.group(f'{src} -> {dst} Pod IP:{port}', 'DENY', self.tcp(inv[src], ips(dst), port))
        # Also prove the Service VIP cannot bypass the deny for a stray Pod.
        self.group('stray -> PG ClusterIP:5432', 'DENY', self.tcp(inv['stray'], [self.pg_service['spec']['clusterIP']], 5432))
        for role in ('api', 'worker', 'postgres', 'load', 'stray'):
            for protocol in ('udp', 'tcp'):
                probes = [{'source': p, 'args': ['dns', self.dns_ip, protocol, 'shop-api.shop.svc.cluster.local.', self.service['spec']['clusterIP']]} for p in inv[role]]
                self.group(f'{role} -> CoreDNS {protocol.upper()}:53', 'DENY' if role == 'stray' else 'ALLOW', probes)
        for role in ('api', 'worker'):
            self.group('prometheus -> ' + role + ':8080', 'ALLOW', self.tcp(inv['prometheus'], ips(role), 8080))
        for port in (8000, 5432):
            self.group(f'operator -> PG:{port}', 'ALLOW', self.tcp(inv['operator'], ips('postgres'), port))
        self.group('PG -> Kubernetes Service:443', 'ALLOW', self.tcp(inv['postgres'], [self.kube_service['spec']['clusterIP']], 443))
        self.group('PG -> Kubernetes endpoint', 'ALLOW', [{'source': p, 'args': ['tcp', ip, str(port)]} for p in inv['postgres'] for ip, port in self.kube_endpoints])
        external_controls = self.tcp(inv['prometheus'], [self.args.external_ip], self.args.external_port)
        for probe in external_controls:
            probe['external_control'] = True
        self.group('external positive control', 'ALLOW', external_controls)
        for role in ('api', 'worker', 'postgres', 'load', 'stray'):
            self.group(role + ' -> external IP', 'DENY', self.tcp(inv[role], [self.args.external_ip], self.args.external_port))
        port = next(p['nodePort'] for p in self.service['spec']['ports'] if p['name'] == 'http')
        node_ips = [a['address'] for node in self.nodes for a in node['status']['addresses'] if a['type'] == 'InternalIP']
        self.group('host -> API NodePort (all nodes)', 'ALLOW', self.tcp([None], node_ips, port))

    def probe(self, item):
        try:
            if item['source'] is None:
                with socket.create_connection((item['args'][1], int(item['args'][2])), timeout=3):
                    result = {'status': 'ALLOW', 'detail': 'connected'}
            else:
                pod = item['source']
                container = self.stray_name if pod['name'] == self.stray_name else self.container
                output = self.k('-n', pod['namespace'], 'exec', pod['name'], '-c', container,
                                '--', '/netprobe', *item['args'])
                result = json.loads(output)
                if result.get('status') not in ('ALLOW', 'TIMEOUT', 'ERROR'):
                    raise ValueError('Invalid helper result: ' + output)
        except socket.timeout as exc:
            result = {'status': 'TIMEOUT', 'detail': str(exc)}
        except (RuntimeError, subprocess.TimeoutExpired, ValueError, OSError) as exc:
            result = {'status': 'ERROR', 'detail': str(exc)}
        return {**item, **result}

    def checked_probe(self, item):
        if not item.get('external_control'):
            return self.probe(item)
        # Retrying this control establishes endpoint availability before/after
        # the experiment. Actual allow/deny matrix probes remain single-shot.
        attempts = []
        for attempt in range(3):
            result = self.probe(item)
            attempts.append({k: result[k] for k in ('status', 'detail')})
            if result['status'] != 'TIMEOUT' or attempt == 2:
                return {**result, 'control_attempts': attempts}
            time.sleep(1)

    def run_probes(self, probes):
        with ThreadPoolExecutor(max_workers=6) as pool:
            return list(pool.map(self.checked_probe, probes))

    def check_unchanged(self):
        for role, (ns, selector) in self.roles.items():
            if self.pods(ns, selector) != self.inventory[role]:
                raise RuntimeError('Pod identity/readiness changed during checks: ' + role)
        specs = {p['metadata']['name']: p['spec'] for p in self.get('-n', 'shop', 'get', 'networkpolicy')['items']}
        if specs != self.policy_specs:
            raise RuntimeError('NetworkPolicy changed during checks')

    def execute(self):
        positives = [p for g in self.groups if g['expected'] == 'ALLOW' for p in g['probes']]
        print('Checking positive controls, matrix, then positive controls again...', flush=True)
        before = self.run_probes(positives)
        for group in self.groups:
            group['results'] = self.run_probes(group.pop('probes'))
        after = self.run_probes(positives)
        self.check_unchanged()
        controls_ok = all(p['status'] == 'ALLOW' for p in before + after)
        evaluate_groups(self.groups, before, after, self.target_aliases)
        all_results = before + after + [p for g in self.groups for p in g['results']]
        control_retries = sum(max(0, len(p.get('control_attempts', [])) - 1) for p in all_results)
        return {'timestamp': datetime.now().astimezone().isoformat(), 'context': self.args.context,
                'inventory': self.inventory, 'policies': self.policy_specs,
                'external_target': f'{self.args.external_ip}:{self.args.external_port}',
                'target_aliases': self.target_aliases, 'control_retries': control_retries,
                'controls_ok': controls_ok, 'controls_before': before, 'controls_after': after,
                'groups': self.groups, 'ok': controls_ok and all(g['ok'] for g in self.groups)}

    def cleanup(self):
        for pod in self.helpers:
            try:
                self.k('-n', pod['namespace'], 'exec', pod['name'], '-c', self.container,
                       '--', '/netprobe', 'stop')
            except Exception as exc:
                print(f'Cleanup: {exc}; helper also exits after 20 minutes.', file=sys.stderr)
        if self.stray_created:
            try:
                self.k('-n', 'shop', 'delete', 'pod', self.stray_name, '--wait=false', '--ignore-not-found')
            except Exception as exc:
                print(f'Cleanup: {exc}; remove Pod {self.stray_name} manually.', file=sys.stderr)


def display(report):
    print(f"\n{report['timestamp']} | {report['context']} | NEW connections")
    print('All selected replicas tested; TCP denials use IP, not DNS.')
    print(f"{'SOURCE -> DESTINATION':42} {'EXPECT':6} {'ACTUAL':10} {'MATCH':7} RESULT")
    for group in report['groups']:
        ratio = f"{group['passed']}/{len(group['results'])}"
        print(f"{group['name']:42} {group['expected']:6} {group['actual']:10} {ratio:7} {'PASS' if group['ok'] else 'FAIL'}")
    print('Positive controls before/after: ' + ('PASS' if report['controls_ok'] else 'FAIL'))
    if report.get('control_retries'):
        print(f"External control retries: {report['control_retries']} (all attempts saved)")
    for phase in ('controls_before', 'controls_after'):
        failed = [p for p in report[phase] if p['status'] != 'ALLOW']
        for probe in failed[:5]:
            source = (probe.get('source') or {}).get('name', 'host')
            print(f"{phase}: {source} -> {' '.join(probe['args'])}: {probe['status']}")
        if len(failed) > 5:
            print(f'{phase}: {len(failed) - 5} more failed controls in summary.json')
    print('RESULT: ' + ('PASS' if report['ok'] else 'FAIL — inspect summary.json; ERROR is not DENY'))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--context', default='kind-lab5')
    parser.add_argument('--image', default='ghcr.io/shop-lab/netprobe:v1')
    parser.add_argument('--external-ip', default='1.1.1.1')
    parser.add_argument('--external-port', type=int, default=443)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--show', type=Path, help='Display a previously saved summary; does not run probes')
    args = parser.parse_args()
    if args.show:
        display(json.loads(args.show.read_text()))
        return 0
    ipaddress.ip_address(args.external_ip)
    out = args.output or Path('evidence/part5') / ('matrix-' + datetime.now().strftime('%Y%m%d-%H%M%S'))
    out.mkdir(parents=True, exist_ok=False)
    matrix = Matrix(args)
    try:
        matrix.prepare()
        matrix.plan()
        report = matrix.execute()
        (out / 'summary.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
        display(report)
        print('Saved: ' + str(out / 'summary.json'))
        return 0 if report['ok'] else 1
    except (RuntimeError, subprocess.TimeoutExpired, ValueError, OSError) as exc:
        (out / 'error.txt').write_text(str(exc) + '\n')
        print('ERROR: ' + str(exc), file=sys.stderr)
        return 1
    finally:
        matrix.cleanup()


if __name__ == '__main__':
    sys.exit(main())
