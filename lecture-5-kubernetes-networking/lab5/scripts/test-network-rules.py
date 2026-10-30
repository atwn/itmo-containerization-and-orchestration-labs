#!/usr/bin/env python3
"""Render the chart and test network alert behavior with promtool in Docker."""
import copy
import json
from pathlib import Path
import subprocess
import tempfile

import yaml

ROOT = Path(__file__).resolve().parents[1]


def main():
    rendered = subprocess.check_output(
        ['helm', 'template', 'shop', 'charts/shop', '-n', 'shop'], cwd=ROOT, text=True)
    rule = next(d['spec'] for d in yaml.safe_load_all(rendered)
                if d and d.get('kind') == 'PrometheusRule' and d['metadata']['name'] == 'shop-network')
    baseline = {
        'node_nf_conntrack_entries{job="node-exporter",instance="node"}': '100x40',
        'node_nf_conntrack_entries_limit{job="node-exporter",instance="node"}': '1000x40',
        'up{job="coredns",instance="dns"}': '1x40',
        'coredns_dns_responses_total{job="coredns",rcode="NOERROR"}': '0+90x40',
        'coredns_dns_responses_total{job="coredns",rcode="SERVFAIL"}': '0x40',
        'up{job="shop-flow-metrics",instance="collector"}': '1x40',
        'shop_flow_collector_success{job="shop-flow-metrics",instance="collector"}': '1x40',
        'shop_flow_last_observed_timestamp_seconds{job="shop-flow-metrics",instance="collector"}': '0+15x40',
        'shop_flow_unexpected_denied_packets_window{job="shop-flow-metrics",instance="collector"}': '0x40',
    }
    tests = []

    def check(alert, when, firing):
        return {'expr': f'count(ALERTS{{alertname="{alert}",alertstate="firing"}})',
                'eval_time': when, 'exp_samples': [{'labels': '{}', 'value': 1}] if firing else []}

    def scenario(name, changes, checks):
        inputs = copy.deepcopy(baseline)
        for key, value in changes.items():
            if value is None:
                del inputs[key]
            else:
                inputs[key] = value
        tests.append({'name': name, 'interval': '15s',
                      'input_series': [{'series': k, 'values': v} for k, v in inputs.items()],
                      'promql_expr_test': checks})

    names = ['ShopConntrackPressure', 'ShopRequiredTrafficDenied', 'ShopDNSUnhealthy']
    scenario('healthy; expected negative tests do not alert', {
        'shop_flow_denied_records_window{job="shop-flow-metrics",instance="collector"}': '100x40',
    }, [check(name, '4m', False) for name in names])
    scenario('conntrack waits for duration, fires, resolves', {
        'node_nf_conntrack_entries{job="node-exporter",instance="node"}': '900x12 100x28',
    }, [check(names[0], '1m', False), check(names[0], '3m', True), check(names[0], '4m', False)])
    deny = 'shop_flow_unexpected_denied_packets_window{job="shop-flow-metrics",instance="collector"}'
    scenario('required Deny waits, fires, resolves', {deny: '0x3 5x8 0x28'}, [
        check(names[1], '1m15s', False), check(names[1], '1m30s', True), check(names[1], '4m', False)])
    scenario('failed collector cannot report a trusted Deny value', {
        deny: '5x40', 'shop_flow_collector_success{job="shop-flow-metrics",instance="collector"}': '0x40',
    }, [check(names[1], '2m', False),
        {'expr': 'shop:required_denied_packets:window', 'eval_time': '2m', 'exp_samples': []}])
    scenario('stale flow observations expire', {
        deny: '5x40', 'shop_flow_last_observed_timestamp_seconds{job="shop-flow-metrics",instance="collector"}': '0x40',
    }, [check(names[1], '1m', True), check(names[1], '4m', False)])
    scenario('SERVFAIL fraction causes DNS alert', {
        'coredns_dns_responses_total{job="coredns",rcode="SERVFAIL"}': '0+10x40',
    }, [check(names[2], '30s', False), check(names[2], '2m', True)])
    scenario('unreachable DNS targets', {'up{job="coredns",instance="dns"}': '0x40'},
             [check(names[2], '2m', True)])
    scenario('missing DNS targets', {'up{job="coredns",instance="dns"}': None},
             [check(names[2], '2m', True)])
    with tempfile.TemporaryDirectory(prefix='lab5-promtool-') as directory:
        root = Path(directory)
        root.chmod(0o755)  # The promtool image runs as an unprivileged user.
        (root / 'rules.yaml').write_text(yaml.safe_dump(rule))
        (root / 'tests.yaml').write_text(yaml.safe_dump({
            'rule_files': ['rules.yaml'], 'evaluation_interval': '15s', 'tests': tests}))
        docker = ['docker', 'run', '--rm', '-v', f'{root}:/tests:ro', '-w', '/tests',
                  '--entrypoint', '/bin/promtool', 'prom/prometheus:latest']
        subprocess.run(docker + ['check', 'rules', 'rules.yaml'], check=True)
        subprocess.run(docker + ['test', 'rules', 'tests.yaml'], check=True)
    dashboard = json.loads((ROOT / 'charts/shop/dashboards/network.json').read_text())
    assert len({p['id'] for p in dashboard['panels']}) == len(dashboard['panels'])
    print(f'PASS: {len(tests)} alert scenarios; dashboard JSON valid')


if __name__ == '__main__':
    main()
