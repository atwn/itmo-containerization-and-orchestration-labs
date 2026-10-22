#!/usr/bin/env bash
set -euo pipefail

prometheus_path='/api/v1/namespaces/monitoring/services/http:kube-prometheus-stack-prometheus:9090/proxy/api/v1/query?query='

query() {
  local expression="$1"
  local encoded
  encoded="$(python3 -c 'import sys, urllib.parse; print(urllib.parse.quote(sys.argv[1]))' "$expression")"
  kubectl get --raw "${prometheus_path}${encoded}" | jq -r '.data.result[0].value[1]'
}

echo '=== BASELINE: mesh disabled ==='
kubectl get pods -n shop -l app.kubernetes.io/name=api \
  -o custom-columns='POD:.metadata.name,READY:.status.containerStatuses[*].ready,CONTAINERS:.spec.containers[*].name'
echo
kubectl top pod -n shop --containers | awk 'NR == 1 || /shop-api-v1/'
echo
printf 'client_p95_seconds=%s\n' "$(query 'histogram_quantile(0.95, sum by (le) (rate(shop_loadgen_request_duration_seconds_bucket{route="/order"}[30s])))')"
printf 'client_error_ratio=%s\n' "$(query '(sum(rate(shop_loadgen_requests_total{status=~"5..|transport_error"}[30s])) or vector(0)) / clamp_min(sum(rate(shop_loadgen_requests_total[30s])), 0.001)')"
printf 'order_requests_per_second=%s\n' "$(query 'sum(rate(shop_loadgen_requests_total[30s]))')"
