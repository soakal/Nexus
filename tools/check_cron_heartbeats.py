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
until it's fixed. Once the condition clears, the flag is auto-resolved on
the next run (see _check_one / _resolve_flag) -- a recovered job must not
leave a page-worthy flag sitting open, which it did for a solid week before
this was added (flag 431, 2026-09-15..22).

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
import urllib.error
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


# Every check kind this script can raise, per job. _check_one clears the
# ones that DON'T currently apply, so a recovered job's flag closes itself
# instead of sitting open forever -- flag 431 (failed:vault_signals_routine)
# sat open for a week after the job started exiting 0 again, because this
# script only ever posted, never cleared.
_CHECK_KINDS = ("missing_heartbeat", "unreadable_heartbeat", "overdue", "failed")
_LIVE_STATUSES = {"open", "needs_follow_up", "deferred"}


def _open_flags(base_url: str, key: str) -> dict[str, int]:
    """check -> flag id for every still-live cron_heartbeat flag, via the
    existing GET /api/safety/flags. There is no clear-by-fingerprint HTTP
    endpoint, and outcomes.clear_flag is in-process only (this script runs
    on devbox, the API on nexus-lxc) -- so recovery is a GET + a resolve
    POST on the two routes that already exist, not a new channel.
    Returns {} on any failure -- never raises: a dead API means "cleared
    nothing this run", never a skipped check."""
    req = urllib.request.Request(
        f"{base_url}/api/safety/flags?source=cron_heartbeat&limit=200",
        headers={"Authorization": f"Bearer {key}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            rows = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"GET /api/safety/flags failed, clearing nothing this run: {e}")
        return {}
    if not isinstance(rows, list):
        return {}
    return {
        r["check"]: r["id"]
        for r in rows
        if isinstance(r, dict)
        and r.get("status") in _LIVE_STATUSES
        and r.get("check")
        and r.get("id") is not None
    }


def _resolve_flag(base_url: str, key: str, flag_id: int, check: str) -> bool:
    """Close one recovered flag via POST /api/safety/flags/{id}/resolve.
    A 409 (a human resolved it between our GET and this POST) is a success,
    not an error -- the flag is closed either way, which is all we wanted."""
    body = json.dumps(
        {"status": "resolved",
         "note": "auto-cleared by check_cron_heartbeats: condition no longer present"}
    ).encode("utf-8")
    req = urllib.request.Request(
        f"{base_url}/api/safety/flags/{flag_id}/resolve",
        data=body,
        method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            ok = 200 <= resp.status < 300
    except urllib.error.HTTPError as e:
        if e.code == 409:
            return True
        print(f"resolve failed for flag {flag_id} ({check}): {e}")
        return False
    except Exception as e:
        print(f"resolve failed for flag {flag_id} ({check}): {e}")
        return False
    if ok:
        print(f"cleared recovered flag {flag_id} ({check})")
    return ok


def _diagnose(job: str, expected_hours: int) -> tuple[str, str] | None:
    """The ONE problem (check_kind, summary) this job currently has, or None
    if it's healthy. Split out of _check_one so the caller can both raise
    the live problem and clear every OTHER kind's stale flag."""
    path = STATE_DIR / f"{job}.json"
    now = datetime.now(timezone.utc)

    if not path.exists():
        return ("missing_heartbeat",
                f"Cron job '{job}' has never written a heartbeat file ({path}). "
                f"Either it has never run via bin/cron_run.sh, or it predates this check.")

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        finished_at = datetime.fromisoformat(data["finished_at"])
        if finished_at.tzinfo is None:
            finished_at = finished_at.replace(tzinfo=timezone.utc)
        exit_code = int(data["exit_code"])
    except Exception as e:
        return ("unreadable_heartbeat",
                f"Cron job '{job}'s heartbeat file is unreadable/malformed: {e}")

    age_hours = (now - finished_at).total_seconds() / 3600
    if age_hours > expected_hours * 2:
        return ("overdue",
                f"Cron job '{job}' last finished {age_hours:.1f}h ago "
                f"(expected every ~{expected_hours}h) -- it has likely stopped running.")
    if exit_code != 0:
        return ("failed",
                f"Cron job '{job}'s last run (finished {finished_at.isoformat()}) "
                f"exited with code {exit_code}.")
    return None


def _check_one(job: str, expected_hours: int, base_url: str, key: str,
               open_flags: dict[str, int]) -> None:
    problem = _diagnose(job, expected_hours)
    if problem is not None:
        kind, summary = problem
        _post_flag(base_url, key, f"{kind}:{job}", summary)
    for kind in _CHECK_KINDS:
        if problem is not None and kind == problem[0]:
            continue  # still true -- record_flag_ex's own dedup owns that row
        flag_id = open_flags.get(f"{kind}:{job}")
        if flag_id is not None:
            _resolve_flag(base_url, key, flag_id, f"{kind}:{job}")


def main() -> None:
    key = _api_key()
    if not key:
        print("check_cron_heartbeats: no NEXUS_API_KEY available, cannot report -- exiting 1")
        raise SystemExit(1)
    base_url = os.environ.get("NEXUS_BASE_URL", _DEFAULT_BASE_URL)
    open_flags = _open_flags(base_url, key)
    for job, hours in EXPECTED_INTERVAL_HOURS.items():
        _check_one(job, hours, base_url, key, open_flags)


if __name__ == "__main__":
    main()
