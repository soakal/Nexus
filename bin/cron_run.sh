#!/usr/bin/env bash
# cron_run.sh <job-name> -- <command> [args...]
#
# Every devbox crontab line for this repo must call ONLY this wrapper, never
# a raw `cd ... && git pull && cmd >> log 2>&1` line directly. That shape is
# exactly what caused a 6-day silent outage (2026-09-06): the `>> log 2>&1`
# redirect in shell syntax binds ONLY to the last command in an `&&` chain,
# not the whole line. When `git pull` failed first (a stray branch checkout
# left no upstream to pull from), the `&&` short-circuited before the
# redirected command -- and its own log write -- ever ran. Nothing was
# logged anywhere, for days, and nothing paged anyone. See
# tools/check_cron_heartbeats.py for the other half of this fix (a job that
# logs correctly but simply stops running still needs someone paged, not
# just a log to read).
#
# This wrapper's own `exec >> logfile 2>&1` below binds to the WHOLE rest of
# THIS PROCESS -- every subprocess it execs/forks inherits those fds, so no
# internal `&&` chain, however written, can ever again make output vanish.
# It also self-heals the stray-branch trigger (checks out main if the repo
# is ever left elsewhere) and always writes a heartbeat file, even on
# failure, via an EXIT trap.
set -uo pipefail

JOB_NAME="${1:?usage: cron_run.sh <job-name> -- <command> [args...]}"
shift
if [[ "${1:-}" != "--" ]]; then
    echo "cron_run.sh: expected '--' after job name, got: ${1:-<nothing>}" >&2
    exit 2
fi
shift

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_DIR="$REPO_DIR/logs"
STATE_DIR="$REPO_DIR/state/heartbeat"
mkdir -p "$LOG_DIR" "$STATE_DIR"

exec >> "$LOG_DIR/${JOB_NAME}.log" 2>&1

echo "=== $(date -Iseconds) cron_run: starting $JOB_NAME ==="

STARTED_AT="$(date -Iseconds)"
EXIT_CODE=0

write_heartbeat() {
    # Sibling-temp-then-rename: a reader (check_cron_heartbeats.py) must
    # never see a half-written file.
    local finished_at="$1"
    local exit_code="$2"
    local tmp
    tmp="$(mktemp "$STATE_DIR/.${JOB_NAME}.XXXXXX")"
    printf '{"job":"%s","started_at":"%s","finished_at":"%s","exit_code":%d}\n' \
        "$JOB_NAME" "$STARTED_AT" "$finished_at" "$exit_code" > "$tmp"
    mv -f "$tmp" "$STATE_DIR/${JOB_NAME}.json"
}
trap 'write_heartbeat "$(date -Iseconds)" "$EXIT_CODE"' EXIT

cd "$REPO_DIR" || { echo "cron_run: cannot cd to $REPO_DIR"; EXIT_CODE=1; exit 1; }

CURRENT_BRANCH="$(git branch --show-current 2>/dev/null || true)"
if [[ "$CURRENT_BRANCH" != "main" ]]; then
    echo "cron_run: WARNING -- repo was on branch '${CURRENT_BRANCH:-<detached>}', not main (this is exactly the 2026-09-06 trigger). Self-healing: checking out main."
    if ! git checkout main; then
        echo "cron_run: FATAL -- could not check out main, refusing to run $JOB_NAME"
        EXIT_CODE=1
        exit 1
    fi
fi

if ! git pull --quiet; then
    echo "cron_run: git pull failed for $JOB_NAME"
    EXIT_CODE=1
    exit 1
fi

"$@"
EXIT_CODE=$?

echo "=== $(date -Iseconds) cron_run: $JOB_NAME finished, exit $EXIT_CODE ==="
exit "$EXIT_CODE"
