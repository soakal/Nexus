"""Dead-man's-switch checker for devbox cron jobs run via bin/cron_run.sh.

Incident this exists for (2026-09-06): a cron-triggered Claude routine's own
`git push` was blocked by Claude Code's auto-mode permission classifier (no
human available to confirm -- it runs non-interactively via `claude -p`). It
gave up gracefully but left the repo checked out on an orphan branch. Three
separate crontab lines all began `cd ... && git pull --quiet && <cmd> >>
logfile 2>&1` -- a redirect that in shell syntax binds ONLY to the last
command in an `&&` chain, not the whole line -- so every day for 6 days
`git pull` failed first, the `&&` short-circuited before the redirected
command (and its own log write) ever ran, and NOTHING was written to any log
file. Total silence, for days. The only reason it was ever noticed was an
unrelated dashboard flag, by a human who happened to ask about it.

bin/cron_run.sh (which every cron line now calls -- see crontab) makes that
specific silent-log failure structurally impossible: its own `exec >>
logfile 2>&1` binds to the whole wrapped process, and it writes a heartbeat
file even on failure. This script closes the other half: a job that logs
correctly but simply stops running (or keeps failing) still needs someone
paged -- a correct log nobody reads is not a notification.

For each job in EXPECTED_INTERVAL_HOURS, if its heartbeat file
(state/heartbeat/<job>.json) is missing, unreadable, more than 2x its
expected interval old, or its last exit_code != 0, this posts ONE
OutcomeFlag with page_now=true via NEXUS's existing POST /api/safety/flags
-- the same integration point tools/relay_vault_signals.py already uses, so
this reuses existing alert infrastructure (OutcomeFlag + notify_phone)
rather than inventing a new channel. record_flag_ex's own dedup (same
fingerprint, still open) means re-running this on an unresolved problem
bumps the existing flag's surfaced_count rather than spamming duplicates --
though page_now still re-pages on every run while the problem persists,
which is intentional escalation, not a bug: an unresolved "your automation
pipeline is dead" is exactly the kind of thing worth being re-annoyed about
until it's fixed.

Run every few hours via its own cron_run.sh-wrapped cron entry (see
crontab) -- deliberately not scheduled less often than that, since running
it more often only affects how quickly a newly-overdue job gets caught, not
whether false alarms fire before a job is genuinely overdue.

Known limitation, not solved here: this script is itself a cron job subject
to the same "cron stops running entirely" failure class it exists to catch.
Nothing currently watches the watcher. That's judged out of scope -- an
external ping (e.g. Uptime Kuma hitting a dead-man's-switch endpoint) would
close it, but wasn't asked for and adds a dependency this fix doesn't need.

Usage: python3 tools/check_cron_heartbeats.py
"""
from __future__ import annotations

import json
import os
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = REPO_ROOT / "state" / "heartbeat"
_DEFAULT_BASE_URL = "http://192.168.1.62:8000"

# job name -> expected interval in hours. Keep in sync with crontab -- a job
# registered here with no matching cron_run.sh-wrapped crontab line will
# always read as "missing_heartbeat" (correctly, if pessimistically).
EXPECTED_INTERVAL_HOURS = {
    "relay_claude_digest": 24,
    "vault_signals_routine": 24,
    "relay_vault_signals": 24,
}


def _api_key() -> str | None:
    """NEXUS_API_KEY env var, then ~/.config/nexus/api_key -- mirrors
    tools/relay_vault_signals.py's _api_key() exactly."""
    key = os.environ.get("NEXUS_API_KEY")
    if key:
        return key
    path = Path.home() / ".config" / "nexus" / "api_key"
    try:
        if path.exists():
            return path.read_text(encoding="utf-8").strip() or None
    except Exception as e:
        print(f"could not read {path}: {e}")
    return None


def _post_flag(base_url: str, key: str, check: str, summary: str) -> bool:
    """POST one finding to NEXUS's POST /api/safety/flags with page_now=true.
    Returns True on a 2xx response, False on any failure -- never raises.
    Mirrors tools/relay_vault_signals.py's _post_flag, plus page_now/severity."""
    body = json.dumps(
        {"source": "cron_heartbeat", "check": check, "summary": summary,
         "severity": "high", "page_now": True}
    ).encode("utf-8")
    req = urllib.request.Request(
        f"{base_url}/api/safety/flags",
        data=body,
        method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return 200 <= resp.status < 300
    except Exception as e:
        print(f"POST /api/safety/flags failed for {check}: {e}")
        return False


def _check_one(job: str, expected_hours: int, base_url: str, key: str) -> None:
    path = STATE_DIR / f"{job}.json"
    now = datetime.now(timezone.utc)

    if not path.exists():
        _post_flag(
            base_url, key, f"missing_heartbeat:{job}",
            f"Cron job '{job}' has never written a heartbeat file ({path}). "
            f"Either it has never run via bin/cron_run.sh, or it predates this check.",
        )
        return

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        finished_at = datetime.fromisoformat(data["finished_at"])
        if finished_at.tzinfo is None:
            finished_at = finished_at.replace(tzinfo=timezone.utc)
        exit_code = int(data["exit_code"])
    except Exception as e:
        _post_flag(
            base_url, key, f"unreadable_heartbeat:{job}",
            f"Cron job '{job}'s heartbeat file is unreadable/malformed: {e}",
        )
        return

    age_hours = (now - finished_at).total_seconds() / 3600
    if age_hours > expected_hours * 2:
        _post_flag(
            base_url, key, f"overdue:{job}",
            f"Cron job '{job}' last finished {age_hours:.1f}h ago "
            f"(expected every ~{expected_hours}h) -- it has likely stopped running.",
        )
    elif exit_code != 0:
        _post_flag(
            base_url, key, f"failed:{job}",
            f"Cron job '{job}'s last run (finished {finished_at.isoformat()}) "
            f"exited with code {exit_code}.",
        )


def main() -> None:
    key = _api_key()
    if not key:
        print("check_cron_heartbeats: no NEXUS_API_KEY available, cannot report -- exiting 1")
        raise SystemExit(1)
    base_url = os.environ.get("NEXUS_BASE_URL", _DEFAULT_BASE_URL)
    for job, hours in EXPECTED_INTERVAL_HOURS.items():
        _check_one(job, hours, base_url, key)


if __name__ == "__main__":
    main()
