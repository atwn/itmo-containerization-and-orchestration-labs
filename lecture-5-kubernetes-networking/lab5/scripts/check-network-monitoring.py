#!/usr/bin/env python3
"""Read-only check of the three network metric sources in Prometheus."""
import argparse
import json
import urllib.parse
import urllib.request


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prometheus', default='http://localhost:9090')
    parser.add_argument('--rules', action='store_true', help='Also check that the three network alerts are loaded, healthy and inactive')
    args = parser.parse_args()
    checks = [
        ('Conntrack nodes', 'count(node_nf_conntrack_entries{job="node-exporter"})', lambda v: v >= 3),
        ('Conntrack limits', 'count(node_nf_conntrack_entries_limit{job="node-exporter"} > 0)', lambda v: v >= 3),
        ('CoreDNS targets UP', 'sum(up{job="coredns"})', lambda v: v >= 2),
        ('CoreDNS requests/s', 'sum(rate(coredns_dns_requests_total{job="coredns"}[2m]))', lambda v: v > 0),
        ('Flow exporter UP', 'up{job="shop-flow-metrics"}', lambda v: v == 1),
        ('Whisker collection', 'shop_flow_collector_success', lambda v: v == 1),
        ('Flow data age, seconds', 'time() - shop_flow_last_observed_timestamp_seconds', lambda v: 0 <= v < 180),
        ('Unexpected Deny, window', 'shop_flow_unexpected_denied_packets_window', lambda v: v == 0),
    ]
    ok = True
    for title, query, valid in checks:
        try:
            url = args.prometheus.rstrip('/') + '/api/v1/query?' + urllib.parse.urlencode({'query': query})
            with urllib.request.urlopen(url, timeout=5) as response:
                data = json.load(response)
            if data.get('status') != 'success':
                raise ValueError('Prometheus query failed')
            rows = data['data']['result']
            if len(rows) != 1:
                raise ValueError(f'expected one sample, got {len(rows)}')
            value = float(rows[0]['value'][1])
            passed = valid(value)
            print(f'{title:28} {value:12.3f}  {"PASS" if passed else "FAIL"}')
            ok &= passed
        except (OSError, ValueError, KeyError) as exc:
            print(f'{title:28} ERROR: {exc}')
            ok = False
    if args.rules:
        try:
            with urllib.request.urlopen(args.prometheus.rstrip('/') + '/api/v1/rules?type=alert', timeout=5) as response:
                data = json.load(response)
            rules = [r for group in data['data']['groups'] for r in group['rules']]
            for name in ('ShopConntrackPressure', 'ShopRequiredTrafficDenied', 'ShopDNSUnhealthy'):
                matches = [r for r in rules if r['name'] == name]
                passed = len(matches) == 1 and matches[0].get('health') == 'ok' and matches[0].get('state') == 'inactive'
                detail = ', '.join(f"{r.get('health')} / {r.get('state')}" for r in matches) or 'MISSING'
                print(f'{name:28} {detail:20} {"PASS" if passed else "FAIL"}')
                ok &= passed
        except (OSError, ValueError, KeyError) as exc:
            print(f'Alert rules ERROR: {exc}')
            ok = False
    print('RESULT: ' + ('PASS' if ok else 'FAIL'))
    return 0 if ok else 1


if __name__ == '__main__':
    raise SystemExit(main())
