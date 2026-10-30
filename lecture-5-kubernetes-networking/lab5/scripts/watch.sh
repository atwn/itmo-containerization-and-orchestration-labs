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
  printf 'Usage: %s {pods|endpointslices|iptables}\n' "$0" >&2
  exit 2
}

command -v yq >/dev/null 2>&1 || {
  printf 'error: yq is required\n' >&2
  exit 1
}

watch_pods() {
  {
    printf '%s\n' $'POD\tWORKLOAD\tPHASE\tREADY\tPOD_IP\tNODE\tRESTARTS\tDELETION'
    kubectl --context "$KUBE_CONTEXT" -n "$NAMESPACE" get pods \
      -l 'app.kubernetes.io/name in (api,worker)' \
      -o yaml |
      yq -r '
        (.items | sort_by(.metadata.name)[] |
          ([
            .metadata.name,
            .metadata.labels["app.kubernetes.io/name"],
            .status.phase,
            ([.status.conditions[]? | select(.type == "Ready") | .status] | .[0] // "-"),
            (.status.podIP // "-"),
            (.spec.nodeName // "-"),
            (.status.containerStatuses[0].restartCount // 0),
            (if .metadata.deletionTimestamp then "terminating" else "-" end)
          ] | @tsv)
        )
      '
  } | column -t
}

watch_endpointslices() {
  {
    printf '%s\n' $'SLICE\tADDRESSES\tREADY\tSERVING\tTERMINATING\tPOD\tNODE'
    kubectl --context "$KUBE_CONTEXT" -n "$NAMESPACE" get endpointslice \
      -l 'kubernetes.io/service-name in (shop-api,shop-worker)' \
      -o yaml |
      yq -r '
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
      '
  } | column -t
}

watch_iptables() {
  command -v docker >/dev/null 2>&1 || {
    printf 'error: docker is required for iptables mode\n' >&2
    return 1
  }

  local cluster_ip
  local node
  local endpoint_ip
  local endpoint_ips

  cluster_ip=$(kubectl --context "$KUBE_CONTEXT" -n "$NAMESPACE" \
    get svc shop-api -o jsonpath='{.spec.clusterIP}')

  mapfile -t endpoint_ips < <(
    kubectl --context "$KUBE_CONTEXT" -n "$NAMESPACE" get endpointslice \
      -l 'kubernetes.io/service-name=shop-api' -o yaml |
      yq -r '.items[].endpoints[]?.addresses[]?' | sort -u
  )

  printf 'SERVICE: shop-api  CLUSTER-IP: %s\n' "$cluster_ip"
  printf 'ENDPOINTS: '
  if ((${#endpoint_ips[@]} == 0)); then
    printf '%s\n' '(none)'
  else
    printf '%s\n' "${endpoint_ips[*]}"
  fi

  for node in $(kubectl --context "$KUBE_CONTEXT" get nodes \
    -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}'); do
    printf '\nNODE: %s\n' "$node"
    printf '  Service and endpoint NAT rules:\n'

    docker exec "$node" sh -c 'iptables-save -t nat' |
      grep -F -- "$cluster_ip" |
      sed -E 's/ -m comment --comment "[^"]*"//g; s/^-A /  /' || true

    for endpoint_ip in "${endpoint_ips[@]}"; do
      docker exec "$node" sh -c 'iptables-save -t nat' |
        grep -F -- "$endpoint_ip" |
        sed -E 's/ -m comment --comment "[^"]*"//g; s/^-A /  /' || true
    done
  done
}

case "$MODE" in
  pods) watch_function=watch_pods ;;
  endpointslices) watch_function=watch_endpointslices ;;
  iptables) watch_function=watch_iptables ;;
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
