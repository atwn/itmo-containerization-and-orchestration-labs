#!/usr/bin/env bash

set -u

RPS="${1:-1}"
URL="${URL:-http://172.18.0.3:32590/health}"

if ! awk -v rps="$RPS" 'BEGIN { exit !(rps > 0) }'; then
  printf 'Usage: %s <rps>\n' "$0" >&2
  printf 'RPS must be a positive number\n' >&2
  exit 2
fi

INTERVAL=$(awk -v rps="$RPS" 'BEGIN { printf "%.6f", 1 / rps }')

printf 'Starting load: rps=%s interval=%ss url=%s\n' \
  "$RPS" "$INTERVAL" "$URL" >&2

while true; do
  ts=$(date -Is)
  if result=$(curl -sS -o /dev/null \
      -w '%{http_code} %{time_total}' \
      --max-time 2 "$URL"); then
    read -r status latency <<< "$result"
    printf '%s ok status=%s latency_s=%s\n' \
    "$ts" "$status" "$latency"
  else
    printf '%s failed\n' "$ts"
  fi
  sleep "$INTERVAL"
done
