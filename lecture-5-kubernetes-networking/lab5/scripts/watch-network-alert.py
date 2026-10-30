#!/usr/bin/env python3
"""Observe a real network alert transition; never modify the cluster or rules."""
import argparse
from datetime import datetime
import json
import math
from pathlib import Path
import time
import urllib.parse
import urllib.request

ALERT = 'ShopRequiredTrafficDenied'
QUERIES = {
    'denied': 'shop:required_denied_packets:window',
    'collector': 'shop_flow_collector_success{job="shop-flow-metrics"}',
    'up': 'up{job="shop-flow-metrics"}',
    'age': 'time() - shop_flow_last_observed_timestamp_seconds{job="shop-flow-metrics"}',
}


def get(base, path):
    with urllib.request.urlopen(base.rstrip('/') + path, timeout=5) as response:
        payload = json.load(response)
    if payload.get('status') != 'success':
        raise ValueError('Prometheus API did not return success')
    return payload['data']


def snapshot(base):
    data = get(base, '/api/v1/rules?type=alert')
    matches = [r for group in data['groups'] for r in group['rules'] if r['name'] == ALERT]
    if len(matches) != 1:
        raise ValueError(f'Expected one {ALERT} rule, found {len(matches)}')
    result = {'time': datetime.now().astimezone().isoformat(timespec='seconds'), 'rule': matches[0]}
    for name, query in QUERIES.items():
        values = get(base, '/api/v1/query?' + urllib.parse.urlencode({'query': query}))['result']
        if len(values) != 1:
            raise ValueError(f'{name}: expected one metric sample, got {len(values)}')
        value = float(values[0]['value'][1])
        if not math.isfinite(value):
            raise ValueError(f'{name}: non-finite sample')
        result[name] = value
    return result


def reached(result, expected):
    healthy = (result['rule'].get('health') == 'ok' and result['collector'] == 1
               and result['up'] == 1 and 0 <= result['age'] < 180)
    if not healthy:
        return False
    if expected == 'firing':
        return result['rule']['state'] == 'firing' and result['denied'] > 0
    return result['rule']['state'] == 'inactive' and result['denied'] == 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--expect', choices=['firing', 'resolved'], required=True)
    parser.add_argument('--timeout', type=int, default=240)
    parser.add_argument('--prometheus', default='http://localhost:9090')
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error('timeout must be positive')
    out = Path('evidence/part7') / ('alert-' + args.expect + '-' + datetime.now().strftime('%Y%m%d-%H%M%S'))
    out.mkdir(parents=True, exist_ok=False)
    report = {'alert': ALERT, 'expected': args.expect, 'observations': [], 'ok': False}
    target = out / 'summary.json'
    print(f'{ALERT}: waiting for {args.expect} with healthy, fresh telemetry', flush=True)
    print(f'{"TIME":25} {"STATE":10} {"DENY/120s":12} {"COLLECTOR":10} {"UP":4} AGE_SECONDS', flush=True)
    deadline = time.monotonic() + args.timeout
    try:
        while time.monotonic() < deadline:
            try:
                result = snapshot(args.prometheus)
                report['observations'].append(result)
                print(f'{result["time"]:25} {result["rule"]["state"]:10} {result["denied"]:<12g} '
                      f'{result["collector"]:<10g} {result["up"]:<4g} {result["age"]:.1f}', flush=True)
                report['ok'] = reached(result, args.expect)
            except (OSError, ValueError, KeyError) as exc:
                report['observations'].append({'time': datetime.now().astimezone().isoformat(), 'error': str(exc)})
                print('Observation unavailable: ' + str(exc), flush=True)
            target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
            if report['ok']:
                break
            time.sleep(min(5, max(0, deadline - time.monotonic())))
    except KeyboardInterrupt:
        report['interrupted'] = True
    finally:
        target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
        print('Saved: ' + str(target), flush=True)
    print('RESULT: ' + ('PASS' if report['ok'] else 'NOT CONFIRMED'), flush=True)
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
