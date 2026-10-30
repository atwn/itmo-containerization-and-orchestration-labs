#!/usr/bin/env python3
"""Generate observable application traffic and show whether new orders complete."""
import argparse
from datetime import datetime
import json
import subprocess
import time
import urllib.error
import urllib.request


def discover_url(context):
    def get(*args):
        return json.loads(subprocess.check_output(
            ['kubectl', '--context', context, '--request-timeout=10s', *args, '-o', 'json'], text=True))
    service = get('-n', 'shop', 'get', 'service', 'shop-api')
    node = get('get', 'nodes')['items'][0]
    ip = next(a['address'] for a in node['status']['addresses'] if a['type'] == 'InternalIP')
    port = next(p['nodePort'] for p in service['spec']['ports'] if p['name'] == 'http')
    return f'http://{ip}:{port}'


def request(url, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=3) as response:
            body = response.read()
            return str(response.status), json.loads(body) if url.endswith(('/order', '/orders')) else None
    except urllib.error.HTTPError as exc:
        exc.close()
        return str(exc.code), None
    except (TimeoutError, urllib.error.URLError):
        return 'TIMEOUT/NET', None
    except ValueError:
        return 'BAD_JSON', None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--duration', type=int, default=120)
    parser.add_argument('--context', default='kind-lab5')
    parser.add_argument('--url', help='Override the discovered NodePort URL')
    args = parser.parse_args()
    if args.duration < 1:
        parser.error('duration must be positive')
    url = (args.url or discover_url(args.context)).rstrip('/')
    print('Application path: ' + url, flush=True)
    print(f"{'TIME':25} {'HEALTH':12} {'POST /order':12} {'ORDER':8} PROCESSED", flush=True)
    deadline = time.monotonic() + args.duration
    failures = 0
    while time.monotonic() < deadline:
        started = time.monotonic()
        stamp = datetime.now().astimezone().isoformat(timespec='seconds')
        health, _ = request(url + '/health')
        status, order = request(url + '/order', {'experiment': 'part6', 'time': stamp})
        order_id = order.get('id') if isinstance(order, dict) else None
        processed = '-'
        if order_id is not None:
            processed = 'false'
            poll_until = time.monotonic() + 10
            while time.monotonic() < poll_until:
                read_status, orders = request(url + '/orders')
                if read_status != '200' or not isinstance(orders, list):
                    processed = 'read:' + read_status
                    break
                if any(o.get('id') == order_id and o.get('processed') is True for o in orders):
                    processed = 'true'
                    break
                time.sleep(1)
        if health != '200' or status != '201' or processed != 'true':
            failures += 1
        print(f'{stamp:25} {health:12} {status:12} {str(order_id or "-"):8} {processed}', flush=True)
        time.sleep(max(0, min(5 - (time.monotonic() - started), deadline - time.monotonic())))
    print('RESULT: ' + ('DEGRADED' if failures else 'OK'), flush=True)
    return 1 if failures else 0


if __name__ == '__main__':
    raise SystemExit(main())
