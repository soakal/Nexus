#!/usr/bin/env bash
# Smallest possible check that bin/cron_run.sh actually does its one job:
# a failing wrapped command still produces a log file AND a heartbeat with
# a nonzero exit_code. Run manually (`bash bin/test_cron_run.sh`); not wired
# into pytest since this repo's Python test suite doesn't run shell scripts.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."
JOB="test_cron_run_$$"

./bin/cron_run.sh "$JOB" -- false || true

LOG="logs/${JOB}.log"
HEARTBEAT="state/heartbeat/${JOB}.json"

fail() { echo "FAIL: $1"; rm -f "$LOG" "$HEARTBEAT"; exit 1; }

[[ -f "$LOG" ]] || fail "no log file written at $LOG"
[[ -f "$HEARTBEAT" ]] || fail "no heartbeat file written at $HEARTBEAT"
grep -q '"exit_code":1' "$HEARTBEAT" || fail "heartbeat did not record exit_code 1: $(cat "$HEARTBEAT")"

rm -f "$LOG" "$HEARTBEAT"
echo "PASS: cron_run.sh writes a log and a nonzero-exit heartbeat for a failing command"
