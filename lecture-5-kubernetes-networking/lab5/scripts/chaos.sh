#!/usr/bin/env bash

set -u

KUBE_CONTEXT="${KUBE_CONTEXT:-kind-lab5}"
NAMESPACE="${NAMESPACE:-shop}"

stop_chaos() {
  printf '\nChaos stopped\n'
  exit 130
}

trap stop_chaos INT TERM

workload="api"

while true; do
  # only kill 'api' for now
  #if (( RANDOM % 2 == 0 )); then
  #  workload="api"
  #else
  #  workload="worker"
  #fi

  mapfile -t pods < <(
    kubectl --context "$KUBE_CONTEXT" -n "$NAMESPACE" get pods \
      -l "app.kubernetes.io/name=$workload" \
      --field-selector=status.phase=Running \
      -o name
  )

  if ((${#pods[@]} == 0)); then
    printf '%s no Running %s pod found; retrying\n' "$(date -Is)" "$workload"
    sleep 5
    continue
  fi

  pod="${pods[RANDOM % ${#pods[@]}]}"
  node=$(kubectl --context "$KUBE_CONTEXT" -n "$NAMESPACE" get "$pod" \
    -o jsonpath='{.spec.nodeName}')
  ip=$(kubectl --context "$KUBE_CONTEXT" -n "$NAMESPACE" get "$pod" \
    -o jsonpath='{.status.podIP}')

  printf '%s deleting %s ip=%s node=%s\n' \
    "$(date -Is)" "$pod" "${ip:-<unknown>}" "$node"

  kubectl --context "$KUBE_CONTEXT" -n "$NAMESPACE" delete "$pod" --wait=false

  sleep $((RANDOM % 6 + 5))
done
