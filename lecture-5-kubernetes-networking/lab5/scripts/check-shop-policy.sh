#!/usr/bin/env bash
# Post-upgrade smoke check. Creates one order; does not change policies or Pods.
# This checks the application path, not every allow/deny edge of the matrix.
set -euo pipefail

KUBE_CONTEXT="${KUBE_CONTEXT:-kind-lab5}"
NAMESPACE="${NAMESPACE:-shop}"
API_SERVICE="${API_SERVICE:-shop-api}"
k() { kubectl --context "$KUBE_CONTEXT" --request-timeout=15s "$@"; }

node_ip="${NODE_IP:-$(k get nodes -o jsonpath='{.items[0].status.addresses[?(@.type=="InternalIP")].address}')}"
node_port=$(k -n "$NAMESPACE" get service "$API_SERVICE" \
  -o jsonpath='{.spec.ports[?(@.name=="http")].nodePort}')
if [[ -z "$node_ip" || -z "$node_port" ]]; then
  echo 'Missing node IP or API NodePort' >&2
  exit 1
fi
api_url="http://$node_ip:$node_port"

date -Iseconds
printf 'Context: %s | namespace: %s | API: %s\n' "$KUBE_CONTEXT" "$NAMESPACE" "$api_url"
k -n "$NAMESPACE" get networkpolicy
k -n "$NAMESPACE" get pods \
  -l 'app.kubernetes.io/name in (api,worker,postgres,chaos-load)'

# Use /orders, not /health: /health deliberately does not access PostgreSQL.
k -n "$NAMESPACE" exec deployment/chaos-load -- \
  curl --fail --silent --show-error --connect-timeout 2 --max-time 5 \
  -o /dev/null -w 'DNS -> API -> PostgreSQL: HTTP %{http_code}\n' \
  "http://$API_SERVICE.$NAMESPACE.svc.cluster.local./orders"

order=$(curl --fail --silent --show-error --connect-timeout 2 --max-time 5 \
  -H 'Content-Type: application/json' -d '{"experiment":"part4-network-policy"}' \
  "$api_url/order")
order_id=$(jq -er '.id | select(type == "number")' <<< "$order")
printf 'NodePort -> API -> PostgreSQL: created order %s\n' "$order_id"

for ((attempt=1; attempt<=30; attempt++)); do
  orders=$(curl --fail --silent --show-error --connect-timeout 2 --max-time 5 \
    "$api_url/orders")
  if jq -e --argjson id "$order_id" \
      'any(.[]; .id == $id and .processed == true)' <<< "$orders" >/dev/null; then
    printf 'worker -> PostgreSQL: order %s processed=true\n' "$order_id"
    echo 'PASS: application path works (full access matrix is checked separately).'
    exit 0
  fi
  sleep 1
done

printf 'FAIL: order %s was not processed within the polling window\n' "$order_id" >&2
exit 1
