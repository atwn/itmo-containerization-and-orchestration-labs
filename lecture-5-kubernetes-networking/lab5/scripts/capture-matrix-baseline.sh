#!/usr/bin/env bash
# Inventory and one pre-policy connectivity probe. Does not change policies.
set -euo pipefail

KUBE_CONTEXT="${KUBE_CONTEXT:-kind-lab5}"
NAMESPACE="${NAMESPACE:-shop}"
OUTPUT_DIR="${1:?Usage: $0 <new-evidence-directory>}"
PROBE="matrix-probe-$(date +%s)"
created=false

k() { kubectl --context "$KUBE_CONTEXT" --request-timeout=15s "$@"; }
cleanup() {
  if "$created"; then
    k -n "$NAMESPACE" delete pod "$PROBE" --wait=false --ignore-not-found >&2 || true
  fi
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

if [[ -e "$OUTPUT_DIR" ]]; then
  printf 'Directory already exists: %s\n' "$OUTPUT_DIR" >&2
  exit 2
fi
mkdir -p "$OUTPUT_DIR"

# Keep identity and network fields; do not save environment values or Secrets.
for ns in "$NAMESPACE" obs cnpg-system kube-system; do
  k -n "$ns" get pods -o json | jq '{items: [.items[] | {
    name: .metadata.name, namespace: .metadata.namespace, labels: .metadata.labels,
    ip: .status.podIP, node: .spec.nodeName,
    containers: [.spec.containers[] | {name, image, ports}]
  }]}' > "$OUTPUT_DIR/$ns-pods.json"
  k -n "$ns" get services -o json | jq '{items: [.items[] | {
    name: .metadata.name, namespace: .metadata.namespace, labels: .metadata.labels,
    type: .spec.type, clusterIP: .spec.clusterIP, ports: .spec.ports,
    selector: .spec.selector, externalTrafficPolicy: .spec.externalTrafficPolicy
  }]}' > "$OUTPUT_DIR/$ns-services.json"
done
k get namespaces -o json | jq '{items: [.items[] | {name: .metadata.name, labels: .metadata.labels}]}' \
  > "$OUTPUT_DIR/namespaces.json"
k -n "$NAMESPACE" get networkpolicies -o json > "$OUTPUT_DIR/networkpolicies-before.json"
k -n default get endpointslice -l kubernetes.io/service-name=kubernetes -o json \
  > "$OUTPUT_DIR/kubernetes-api-endpoints.json"

pg_image=$(jq -er '.items[] | select(.labels["cnpg.io/cluster"] == "shop-postgres") |
  .containers[] | select(.name == "postgres") | .image' "$OUTPUT_DIR/$NAMESPACE-pods.json" | head -n 1)

k -n "$NAMESPACE" run "$PROBE" --image="$pg_image" --image-pull-policy=IfNotPresent \
  --restart=Never --labels='app.kubernetes.io/name=matrix-probe' \
  --command -- sleep 600
created=true
k -n "$NAMESPACE" wait --for=condition=Ready "pod/$PROBE" --timeout=120s
k -n "$NAMESPACE" get pod "$PROBE" -o json | jq '{
  name: .metadata.name, namespace: .metadata.namespace, labels: .metadata.labels,
  ip: .status.podIP, image: .spec.containers[0].image
}' > "$OUTPUT_DIR/probe-pod.json"

if k -n "$NAMESPACE" exec "$PROBE" -- \
  pg_isready -h shop-postgres-rw -p 5432 -t 3 > "$OUTPUT_DIR/postgres-probe.txt" 2>&1; then
  probe_exit=0
else
  probe_exit=$?
fi
cat "$OUTPUT_DIR/postgres-probe.txt"
jq -n --arg timestamp "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --arg context "$KUBE_CONTEXT" --arg namespace "$NAMESPACE" \
  --arg pod "$PROBE" --argjson probe_exit "$probe_exit" \
  '{timestamp: $timestamp, context: $context, namespace: $namespace,
    source: $pod, destination: "shop-postgres-rw:5432", probe_exit: $probe_exit,
    note: "pg_isready checks PostgreSQL availability, not authorization to read data"}' \
  > "$OUTPUT_DIR/probe-result.json"
printf 'Captured: %s\n' "$OUTPUT_DIR"
