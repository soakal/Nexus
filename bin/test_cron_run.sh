#!/usr/bin/env bash
# Smallest possible check that bin/cron_run.sh actually does its one job:
# a failing wrapped command still produces a log file AND a heartbeat with
# a nonzero exit_code, plus the CRON_RUN_NO_SECRETS / CRON_RUN_PRIVATE_LOG
# flags. Run manually (`bash bin/test_cron_run.sh`); not wired into pytest
# since this repo's Python test suite doesn't run shell scripts.
#
# cron_run.sh does `git checkout main` + `git pull` in its own repo, so it
# runs against a throwaway sandbox clone, never the real repo.
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/cron_run.sh"
SB="$(mktemp -d)"
trap 'rm -rf "$SB"' EXIT
unset CLAUDE_CODE_OAUTH_TOKEN CRON_RUN_NO_SECRETS CRON_RUN_PRIVATE_LOG

G=(git -c user.name=t -c user.email=t@t)
"${G[@]}" init -q --bare -b main "$SB/origin.git"
"${G[@]}" init -q -b main "$SB/seed"
"${G[@]}" -C "$SB/seed" commit -q --allow-empty -m init
"${G[@]}" -C "$SB/seed" push -q "$SB/origin.git" main
"${G[@]}" clone -q "$SB/origin.git" "$SB/repo"
mkdir -p "$SB/repo/bin"
cp "$SRC" "$SB/repo/bin/cron_run.sh"
cat > "$SB/repo/bin/infisical_secret.py" <<'EOF'
import os
open(os.environ["MARKER"], "w").close()
print("tok")
EOF
cd "$SB/repo"
export MARKER="$SB/marker"

fail() { echo "FAIL: $1"; exit 1; }
run() { ./bin/cron_run.sh "$@" || true; }
mode() { stat -c %a "logs/$1.log"; }

# 1. failing command: log + heartbeat with exit_code 1
run j1 -- false
[[ -f logs/j1.log ]] || fail "no log file written"
[[ -f state/heartbeat/j1.json ]] || fail "no heartbeat file written"
grep -q '"exit_code":1' state/heartbeat/j1.json || fail "heartbeat did not record exit_code 1"
echo "PASS: cron_run.sh writes a log and a nonzero-exit heartbeat for a failing command"

# 2. NO_SECRETS skips the token fetch; default does the fetch
rm -f "$MARKER"
CRON_RUN_NO_SECRETS=1 run j2 -- true
[[ ! -e "$MARKER" ]] || fail "NO_SECRETS=1 still ran infisical_secret.py"
run j3 -- true
[[ -e "$MARKER" ]] || fail "default run did not call infisical_secret.py"
echo "PASS: CRON_RUN_NO_SECRETS=1 skips the secret fetch"

# 3. PRIVATE_LOG: new log 600, pre-existing 644 log becomes 600
CRON_RUN_PRIVATE_LOG=1 run j4 -- true
[[ "$(mode j4)" == 600 ]] || fail "new private log mode $(mode j4), want 600"
: > logs/j5.log; chmod 644 logs/j5.log
CRON_RUN_PRIVATE_LOG=1 run j5 -- true
[[ "$(mode j5)" == 600 ]] || fail "existing private log mode $(mode j5), want 600"
echo "PASS: CRON_RUN_PRIVATE_LOG=1 forces log mode 600"

# 4. default: new log not forced to 600; existing 644 stays 644
(umask 022; run j6 -- true)
[[ "$(mode j6)" == 644 ]] || fail "default new log mode $(mode j6), want 644 under umask 022"
: > logs/j7.log; chmod 644 logs/j7.log
run j7 -- true
[[ "$(mode j7)" == 644 ]] || fail "default existing log mode $(mode j7), want 644"
echo "PASS: default leaves log modes alone"
