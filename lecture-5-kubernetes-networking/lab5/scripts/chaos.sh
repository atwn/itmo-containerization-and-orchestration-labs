#!/usr/bin/env bash

set -euo pipefail

KUBE_CONTEXT="${KUBE_CONTEXT:-kind-lab5}"
NAMESPACE="${NAMESPACE:-shop}"
DURATION_SECONDS="${DURATION_SECONDS:-0}"
[[ "$DURATION_SECONDS" =~ ^[0-9]+$ ]] || exit 2

stop_chaos() {
  printf '\nChaos stopped\n' >&2
  exit 130
}

trap stop_chaos INT TERM

workload="api"

while true; do
  if ((DURATION_SECONDS > 0 && SECONDS >= DURATION_SECONDS)); then
    break
  fi

  candidates=$(kubectl --context "$KUBE_CONTEXT" --request-timeout=10s -n "$NAMESPACE" get pods \
    -l "app.kubernetes.io/name=$workload,app.kubernetes.io/instance=shop" -o json)
  mapfile -t pods < <(jq -r '.items[] | select(.metadata.deletionTimestamp == null) |
    select(any(.status.conditions[]?; .type == "Ready" and .status == "True")) |
    [.metadata.name, .status.podIP, .spec.nodeName] | @tsv' <<< "$candidates")

  if ((${#pods[@]} == 0)); then
    printf '%s no Ready %s pod found; retrying\n' "$(date -Is)" "$workload" >&2
    sleep 5
    continue
  fi

  IFS=$'\t' read -r pod ip node <<< "${pods[RANDOM % ${#pods[@]}]}"
  ts=$(date -u +%Y-%m-%dT%H:%M:%S.%3NZ)

  kubectl --context "$KUBE_CONTEXT" --request-timeout=10s -n "$NAMESPACE" \
    delete pod "$pod" --wait=false >&2
  printf '{"ts":"%s","event":"delete","pod":"%s","ip":"%s","node":"%s"}\n' \
    "$ts" "$pod" "$ip" "$node"

  sleep $((RANDOM % 6 + 5))
done
