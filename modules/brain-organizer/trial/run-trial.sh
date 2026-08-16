#!/usr/bin/env bash
# Brain Organizer trial (Trial B) runner. Two modes, both invoked by cron:
#   run-trial.sh snapshot   -- 01:55, before the real 02:00 run deletes raws
#   run-trial.sh run        -- 03:00, well after the real run has finished
#
# See modules/brain-organizer/trial/README.md (this repo) for the full design.
set -euo pipefail

TRIAL_ROOT=/var/lib/nexus/brain-trial
REAL_VAULT=/var/lib/nexus/knowledge/Brain
REPO_MODULE=/opt/nexus/modules/brain-organizer
NEXUS_VENV_PY=/opt/nexus/venv/bin/python

mode="${1:-}"
if [[ "$mode" != "snapshot" && "$mode" != "run" ]]; then
    echo "usage: $0 {snapshot|run}" >&2
    exit 2
fi

if [[ "$mode" == "snapshot" ]]; then
    echo "[$(date -Is)] snapshot: copying real vault into isolated trial dir"
    rm -rf "$TRIAL_ROOT/vault"
    mkdir -p "$TRIAL_ROOT/vault/Brain"
    # Explicitly NOT .stversions (Syncthing history, ~591MB, irrelevant here).
    for d in raw wiki _meta; do
        [[ -d "$REAL_VAULT/$d" ]] && cp -a "$REAL_VAULT/$d" "$TRIAL_ROOT/vault/Brain/$d"
    done
    [[ -f "$REAL_VAULT/_Index.md" ]] && cp -a "$REAL_VAULT/_Index.md" "$TRIAL_ROOT/vault/Brain/_Index.md"
    [[ -f "$REPO_MODULE/processed.json" ]] && cp -a "$REPO_MODULE/processed.json" "$TRIAL_ROOT/processed.json"

    # A separate FROZEN copy of wiki/, untouched by the trial run itself --
    # $TRIAL_ROOT/vault/Brain/wiki above is the trial's own working copy and
    # gets mutated in place. Diffing the trial's output against the LIVE real
    # vault (as this script used to) mixes in every unrelated write the real
    # vault picks up between snapshot and diff time (goal completions, digest
    # entries, etc.) -- diffing against this frozen baseline instead isolates
    # exactly what the trial run itself changed.
    rm -rf "$TRIAL_ROOT/wiki-baseline"
    [[ -d "$REAL_VAULT/wiki" ]] && cp -a "$REAL_VAULT/wiki" "$TRIAL_ROOT/wiki-baseline"

    echo "[$(date -Is)] snapshot done: $(du -sh "$TRIAL_ROOT/vault" | cut -f1)"
    exit 0
fi

# mode == run
night="$(date -d '-1 day' +%F 2>/dev/null || date -v-1d +%F)"   # GNU vs BSD date
night_dir="$TRIAL_ROOT/nights/$night"
mkdir -p "$night_dir"

if [[ ! -d "$TRIAL_ROOT/vault/Brain/raw" ]]; then
    echo "[$(date -Is)] run: no snapshot found at $TRIAL_ROOT/vault -- the 01:55 snapshot cron didn't run or failed" | tee "$night_dir/organizer.log" >&2
    echo 1 > "$night_dir/rc"
    exit 1
fi

echo "[$(date -Is)] run: night $night starting"

# Isolate __file__-relative state (_USAGE_LOG, .organizer.lock) by physically
# copying the trial script into its own directory -- running it in place from
# $REPO_MODULE would share those paths with the concurrently-scheduled real run.
rm -rf "$TRIAL_ROOT/module"
mkdir -p "$TRIAL_ROOT/module"
cp "$REPO_MODULE/brain_organizer_trial.py" "$TRIAL_ROOT/module/brain_organizer.py"

# Pull both keys through NEXUS's own live Settings -- OPENROUTER_API_KEY is
# what actually gets used (every trial model id contains "/"); ANTHROPIC_API_KEY
# is only required because the script's own startup unconditionally builds an
# anthropic.Anthropic client before ever checking which model it'll call.
# cwd MUST be /var/lib/nexus (the real .env lives there, matching systemd's
# WorkingDirectory) -- /opt/nexus is just the git checkout and has no .env of
# its own, so get_settings() there can't reach Infisical and raises KeyError.
export OPENROUTER_API_KEY="$(cd /var/lib/nexus && PYTHONPATH=/opt/nexus "$NEXUS_VENV_PY" -c \
    'from backend.config import get_settings; print(get_settings().openrouter_api_key)')"
export ANTHROPIC_API_KEY="$(cd /var/lib/nexus && PYTHONPATH=/opt/nexus "$NEXUS_VENV_PY" -c \
    'from backend.config import get_settings; print(get_settings().anthropic_api_key)')"
# No duplicate "Run complete" Telegram ping from the trial run.
unset TELEGRAM_BOT_TOKEN TELEGRAM_CHAT_ID

set +e
"$REPO_MODULE/venv/bin/python" "$TRIAL_ROOT/module/brain_organizer.py" \
    --config "$TRIAL_ROOT/config.json" \
    > "$night_dir/organizer.log" 2>&1
run_rc=$?
set -e
echo "[$(date -Is)] run: brain_organizer.py exited $run_rc" >> "$night_dir/organizer.log"
echo "$run_rc" > "$night_dir/rc"

# Clean signal: exactly what the trial run itself changed, isolated from any
# unrelated live-vault noise (see the snapshot step's wiki-baseline comment).
diff -ru "$TRIAL_ROOT/wiki-baseline" "$TRIAL_ROOT/vault/Brain/wiki" > "$night_dir/diff-trial.txt" || true
# Reference only: what the real run changed over the same window. May still
# include some unrelated background writes -- the real vault stays live and
# shared the whole time, unlike the isolated trial copy.
diff -ru "$TRIAL_ROOT/wiki-baseline" "$REAL_VAULT/wiki" > "$night_dir/diff-real.txt" || true

"$REPO_MODULE/venv/bin/python" "$REPO_MODULE/wikilink_census.py" \
    --before "$REAL_VAULT" \
    --after "$TRIAL_ROOT/vault/Brain" \
    --out "$night_dir/census.md" || echo "census failed" >> "$night_dir/organizer.log"

# Preserve the night's real token usage BEFORE the next run's module wipe --
# trial_report.py's cost criterion has no other source for Trial B spend
# (this usage.jsonl is deliberately never ingested by brain_spend.py, see
# brain_organizer_trial.py's own docstring).
cp "$TRIAL_ROOT/module/logs/usage.jsonl" "$night_dir/usage.jsonl" 2>/dev/null || true

echo "[$(date -Is)] run: night $night done, rc=$run_rc"
