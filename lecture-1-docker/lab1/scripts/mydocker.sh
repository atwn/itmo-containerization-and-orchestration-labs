#!/usr/bin/env bash

set -Eeuo pipefail

UNIT_NAME="lab1-api"
API_BIN="${API_BIN:-/tmp/api}"

usage() {
    cat <<EOF
Usage: $0 {start|stop|status}

Environment:
  API_BIN  Path to the api executable (default: /tmp/api)
EOF
}

require_commands() {
    local command_name

    for command_name in systemd-run systemctl unshare ip; do
        if ! command -v "$command_name" >/dev/null 2>&1; then
            echo "required command not found: $command_name" >&2
            exit 1
        fi
    done
}

start() {
    require_commands

    if [[ ! -x "$API_BIN" ]]; then
        echo "API executable is not executable: $API_BIN" >&2
        exit 1
    fi

    if sudo systemctl is-active --quiet "$UNIT_NAME"; then
        echo "unit is already running: $UNIT_NAME" >&2
        exit 1
    fi

    sudo systemd-run \
        --unit="$UNIT_NAME" \
        --collect \
        --no-block \
        -p MemoryMax=128M \
        -p MemorySwapMax=0 \
        -p CPUQuota=50% \
        -p TasksMax=64 \
        -p NoNewPrivileges=yes \
        -p 'SystemCallFilter=~mkdir mkdirat' \
        -p SystemCallErrorNumber=EPERM \
        -- \
        unshare \
            --pid --mount --net --uts --ipc \
            --user --map-root-user \
            --fork --mount-proc \
            bash -c '
                ip link set lo up
                exec capsh --drop=cap_sys_time -- -c "exec \"$1\""
            ' bash "$API_BIN"

    echo "started transient service: $UNIT_NAME"
    echo "check status: sudo systemctl status $UNIT_NAME"
    echo "find API PID: pgrep -n -f '$API_BIN'"

}

stop() {
    sudo systemctl stop "$UNIT_NAME" 2>/dev/null || true
    echo "stopped transient service: $UNIT_NAME"
}

status() {
    sudo systemctl status "$UNIT_NAME" --no-pager
}

case "${1:-}" in
    start)
        start
        ;;
    stop)
        stop
        ;;
    status)
        status
        ;;
    -h|--help|help)
        usage
        ;;
    *)
        usage >&2
        exit 2
        ;;
esac
