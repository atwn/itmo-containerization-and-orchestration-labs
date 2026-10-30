#!/usr/bin/env bash

set -u

RPS="${1:-1}"
URL="${URL:-http://172.18.0.3:32590/health}"
DURATION_SECONDS="${DURATION_SECONDS:-0}"
export LC_ALL=C

if ! awk -v rps="$RPS" 'BEGIN { exit !(rps > 0) }' ||
   [[ ! "$DURATION_SECONDS" =~ ^[0-9]+$ ]]; then
  printf 'Usage: %s <rps>\n' "$0" >&2
  printf 'RPS must be positive; DURATION_SECONDS must be a nonnegative integer\n' >&2
  exit 2
fi

INTERVAL=$(awk -v rps="$RPS" 'BEGIN { printf "%.6f", 1 / rps }')

printf 'Starting load: rps=%s interval=%ss url=%s\n' \
  "$RPS" "$INTERVAL" "$URL" >&2

while true; do
  if ((DURATION_SECONDS > 0 && SECONDS >= DURATION_SECONDS)); then
    break
  fi
  ts=$(date -u +%Y-%m-%dT%H:%M:%S.%3NZ)
  if response=$(curl -sS -o /dev/null \
      -w '%{http_code} %{time_total}' \
      --connect-timeout 1 --max-time 2 "$URL"); then
    curl_exit=0
  else
    curl_exit=$?
  fi
  read -r status latency <<< "$response"
  result=failed
  if ((curl_exit == 0)) && [[ "$status" == 2?? ]]; then
    result=ok
  fi
  printf '{"ts":"%s","result":"%s","status":"%s","latency_s":%s,"curl_exit":%s}\n' \
    "$ts" "$result" "${status:-000}" "${latency:-0}" "$curl_exit"
  sleep "$INTERVAL"
done
