#!/usr/bin/env bash

set -u

KUBE_CONTEXT="${KUBE_CONTEXT:-kind-lab5}"
NAMESPACE="${NAMESPACE:-obs}"
STATE_DIR="${XDG_RUNTIME_DIR:-/tmp}/lab5-port-forward"

# name|resource|local-port:service-port
FORWARDS=(
  "grafana|svc/grafana|3000:80"
  "prometheus|svc/kps-prometheus|9090:9090"
  "loki|svc/loki-gateway|3100:80"
  "alertmanager|svc/kps-alertmanager|9093:9093"
)

usage() {
  cat <<EOF
Usage: $0 {start|stop|status}

Environment overrides:
  KUBE_CONTEXT  Kubernetes context (default: kind-lab5)
  NAMESPACE     namespace containing the services (default: obs)
EOF
}

pid_file() {
  printf '%s/%s.pid\n' "$STATE_DIR" "$1"
}

log_file() {
  printf '%s/%s.log\n' "$STATE_DIR" "$1"
}

is_running() {
  local pid_file_path="$1"
  [[ -s "$pid_file_path" ]] || return 1

  local pid
  pid=$(<"$pid_file_path")
  kill -0 "$pid" 2>/dev/null
}

start_one() {
  local name="$1" resource="$2" mapping="$3"
  local pid_file_path
  pid_file_path=$(pid_file "$name")

  if is_running "$pid_file_path"; then
    printf '%-12s already running (PID %s)\n' "$name" "$(<"$pid_file_path")"
    return 0
  fi

  nohup kubectl --context "$KUBE_CONTEXT" -n "$NAMESPACE" \
    port-forward "$resource" "$mapping" \
    >"$(log_file "$name")" 2>&1 < /dev/null &

  local pid=$!
  printf '%s\n' "$pid" >"$pid_file_path"
  printf '%-12s started (PID %s), log: %s\n' \
    "$name" "$pid" "$(log_file "$name")"
}

start_all() {
  mkdir -p "$STATE_DIR"
  printf 'Starting port-forwards in namespace %s, context %s\n' \
    "$NAMESPACE" "$KUBE_CONTEXT"

  local entry name resource mapping
  for entry in "${FORWARDS[@]}"; do
    IFS='|' read -r name resource mapping <<<"$entry"
    start_one "$name" "$resource" "$mapping"
  done
}

stop_one() {
  local name="$1" pid_file_path pid
  pid_file_path=$(pid_file "$name")

  if ! is_running "$pid_file_path"; then
    rm -f "$pid_file_path"
    printf '%-12s not running\n' "$name"
    return 0
  fi

  pid=$(<"$pid_file_path")
  kill "$pid" 2>/dev/null || true
  rm -f "$pid_file_path"
  printf '%-12s stopped (PID %s)\n' "$name" "$pid"
}

stop_all() {
  local entry name resource mapping
  for entry in "${FORWARDS[@]}"; do
    IFS='|' read -r name resource mapping <<<"$entry"
    stop_one "$name"
  done
}

status_all() {
  local entry name resource mapping pid_file_path
  printf '%-12s %-10s %s\n' "FORWARD" "STATUS" "LOCAL URL"

  for entry in "${FORWARDS[@]}"; do
    IFS='|' read -r name resource mapping <<<"$entry"
    pid_file_path=$(pid_file "$name")

    if is_running "$pid_file_path"; then
      printf '%-12s %-10s http://localhost:%s\n' \
        "$name" "running" "${mapping%%:*}"
    else
      printf '%-12s %-10s http://localhost:%s\n' \
        "$name" "stopped" "${mapping%%:*}"
    fi
  done
}

case "${1:-}" in
  start)
    start_all
    ;;
  stop)
    stop_all
    ;;
  status)
    status_all
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac
