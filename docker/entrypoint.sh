#!/bin/sh
set -eu

export REPRYNTT_DATA_DIR="${REPRYNTT_DATA_DIR:-/data}"
export REPRYNTT_PROVIDER="${REPRYNTT_PROVIDER:-local}"
export PYTHONUTF8="${PYTHONUTF8:-1}"

child_pid=""

cleanup() {
    if [ -n "$child_pid" ]; then
        kill "$child_pid" 2>/dev/null || true
        wait "$child_pid" 2>/dev/null || true
    fi
    repryntt stop >/dev/null 2>&1 || true
    repryntt chain stop >/dev/null 2>&1 || true
}

trap cleanup INT TERM

if [ "${REPRYNTT_START_CHAIN:-0}" = "1" ]; then
    repryntt chain start || echo "warning: Rust chain did not start; check /data/logs/rust-chain.log" >&2
fi

"$@" &
child_pid="$!"
set +e
wait "$child_pid"
status="$?"
set -e
child_pid=""
cleanup
exit "$status"
