#!/usr/bin/env bash

set -u

KUBE_CONTEXT="${KUBE_CONTEXT:-kind-lab5}"
NAMESPACE="${NAMESPACE:-shop}"
MODE="${1:-}"
ONCE=false

if [[ "$MODE" == "--once" ]]; then
  ONCE=true
  MODE="${2:-}"
fi

usage() {
  printf 'Usage: %s {pods|entrypointslices}\n' "$0" >&2
  exit 2
}

command -v yq >/dev/null 2>&1 || {
  printf 'error: yq is required\n' >&2
  exit 1
}

watch_pods() {
  kubectl --context "$KUBE_CONTEXT" -n "$NAMESPACE" get pods \
    -l 'app.kubernetes.io/name in (api,worker)' \
    -o yaml |
    yq -r '
      (["POD", "WORKLOAD", "PHASE", "READY", "POD_IP", "NODE", "RESTARTS", "DELETION"] | @tsv),
      (.items | sort_by(.metadata.name)[] |
        ([
          .metadata.name,
          .metadata.labels["app.kubernetes.io/name"],
          .status.phase,
          ([.status.conditions[]? | select(.type == "Ready") | .status] | first // "-"),
          (.status.podIP // "-"),
          (.spec.nodeName // "-"),
          ([.status.containerStatuses[]?.restartCount] | add // 0),
          (if .metadata.deletionTimestamp then "terminating" else "-" end)
        ] | @tsv)
      )
    ' | column -t
}

watch_entrypointslices() {
  kubectl --context "$KUBE_CONTEXT" -n "$NAMESPACE" get endpointslice \
    -l 'kubernetes.io/service-name in (shop-api,shop-worker)' \
    -o yaml |
    yq -r '
      (["SLICE", "ADDRESSES", "READY", "SERVING", "TERMINATING", "POD", "NODE"] | @tsv),
      (.items[] as $slice |
        $slice.endpoints[] |
        ([
          $slice.metadata.name,
          (.addresses | join(",")),
          (if .conditions.ready == null then "unset" else (.conditions.ready | tostring) end),
          (if .conditions.serving == null then "unset" else (.conditions.serving | tostring) end),
          (if .conditions.terminating == null then "unset" else (.conditions.terminating | tostring) end),
          (.targetRef.name // "-"),
          (.nodeName // "-")
        ] | @tsv)
      )
    ' | column -t
}

case "$MODE" in
  pods) watch_function=watch_pods ;;
  entrypointslices) watch_function=watch_entrypointslices ;;
  *) usage ;;
esac

render() {
  printf 'Mode: %s\n' "$MODE"
  printf 'Timestamp: %s\nNamespace: %s\nContext: %s\n\n' \
    "$(date -Is)" "$NAMESPACE" "$KUBE_CONTEXT"
  "$watch_function"
}

if "$ONCE"; then
  render
else
  exec watch -n 1 --color "$0" --once "$MODE"
fi
