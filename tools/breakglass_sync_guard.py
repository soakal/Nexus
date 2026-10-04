#!/usr/bin/env python3
"""Cron guard for the weekly Infisical -> Proton Pass Break-Glass sync.

  run    run sync_infisical_to_protonpass.py, append its output to the log,
         write a heartbeat, and page NEXUS if it failed (the 2026-09-13 and
         2026-09-20 failures were silent for two weeks).
  check  page NEXUS if the heartbeat is missing, older than MAX_AGE_HOURS
         (covers "cron stopped running"), or the last run failed.

Add --dry-run to either mode to print what would be paged without posting.

Paging reuses NEXUS's POST /api/safety/flags (page_now), the same channel as
tools/check_cron_heartbeats.py. The API key comes from NEXUS's own settings
(the same vault path the server uses), so no key is copied to this host.
The sync prints only counts, never secret values, so its last line is safe to
put in the page.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path

NEXUS_ROOT = Path("/opt/nexus")
WORKDIR = Path("/var/lib/nexus")
SYNC = NEXUS_ROOT / "tools" / "sync_infisical_to_protonpass.py"
LOG = Path("/var/log/infisical-protonpass-sync.log")
HEARTBEAT = Path(os.environ.get("BG_HEARTBEAT", WORKDIR / "heartbeat" / "breakglass_sync.json"))
BASE_URL = os.environ.get("NEXUS_BASE_URL", "http://127.0.0.1:8000")
MAX_AGE_HOURS = 8 * 24  # weekly job + one day of slack
JOB = "breakglass_sync"

# nexus-breakglass-sync-2026-10 PAT: created 2026-10-04 with --expiration 1y. `pass-cli
# personal-access-token list` can't be run from inside a PAT session to read this
# back, so it's hardcoded -- confirm the exact date in Proton Pass's web settings
# if precision matters. No code path can renew it automatically (that's an
# interactive pass-cli command), so check() pages ahead of time instead of
# waiting for the "it broke" page.
PAT_EXPIRES = date(2027, 10, 4)
PAT_WARN_DAYS = 30  # visible in `check` output, no page yet
PAT_PAGE_DAYS = 7  # actually pages once this close


def _api_key() -> str:
    sys.path.insert(0, str(NEXUS_ROOT))
    os.chdir(WORKDIR)  # Settings reads .env relative to cwd
    from backend.config import get_settings
    return get_settings().nexus_api_key


def _page(check: str, summary: str, dry_run: bool) -> bool:
    if dry_run:
        print(f"[dry-run] would page: {check}: {summary}")
        return True
    body = json.dumps({"source": "cron_heartbeat", "check": check, "summary": summary,
                       "severity": "high", "page_now": True}).encode()
    try:
        req = urllib.request.Request(
            f"{BASE_URL}/api/safety/flags", data=body, method="POST",
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {_api_key()}"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            return 200 <= resp.status < 300
    except Exception as e:  # never raise from the alert path; cron will still see the exit code
        print(f"page failed for {check}: {type(e).__name__}: {e}", file=sys.stderr)
        return False


def _write_heartbeat(rc: int, last_line: str) -> None:
    HEARTBEAT.parent.mkdir(parents=True, exist_ok=True)
    tmp = HEARTBEAT.with_name(f".{HEARTBEAT.name}.tmp")
    tmp.write_text(json.dumps({
        "job": JOB, "finished_at": datetime.now(timezone.utc).isoformat(),
        "exit_code": rc, "last_line": last_line[:200]}) + "\n")
    tmp.replace(HEARTBEAT)


def run(dry_run: bool) -> int:
    r = subprocess.run([str(NEXUS_ROOT / "venv" / "bin" / "python"), str(SYNC)],
                       capture_output=True, text=True)
    out = (r.stdout + r.stderr).strip()
    with LOG.open("a") as f:
        f.write(f"=== {datetime.now().astimezone().isoformat()} guard: exit {r.returncode} ===\n{out}\n")
    last = out.splitlines()[-1] if out else "(no output)"
    _write_heartbeat(r.returncode, last)
    if r.returncode != 0:
        _page(f"failed:{JOB}", f"Break-Glass sync failed (exit {r.returncode}): {last}", dry_run)
    return r.returncode


def check(dry_run: bool) -> int:
    try:
        d = json.loads(HEARTBEAT.read_text())
        finished = datetime.fromisoformat(d["finished_at"])
        rc = int(d["exit_code"])
    except FileNotFoundError:
        return 0 if _page(f"missing_heartbeat:{JOB}", "Break-Glass sync has never written a heartbeat.", dry_run) else 1
    except Exception as e:
        return 0 if _page(f"unreadable_heartbeat:{JOB}", f"Break-Glass sync heartbeat unreadable: {e}", dry_run) else 1
    age_h = (datetime.now(timezone.utc) - finished).total_seconds() / 3600
    if age_h > MAX_AGE_HOURS:
        _page(f"overdue:{JOB}", f"Break-Glass sync last ran {age_h:.0f}h ago (expected weekly); the cron has likely stopped.", dry_run)
    elif rc != 0:
        _page(f"failed:{JOB}", f"Break-Glass sync's last run exited {rc}: {d.get('last_line', '')}", dry_run)
    else:
        session_check = subprocess.run(
            [str(NEXUS_ROOT / "venv" / "bin" / "python"), str(SYNC), "--ensure-session"],
            capture_output=True, text=True,
            env={**os.environ, "PROTON_PASS_AGENT_REASON": "break-glass session check"})
        if session_check.returncode != 0:
            # The session doesn't survive a host reboot (lost 2026-09-27 09:40) -- ensure-session
            # auto-logs-in with the Break-Glass-scoped PAT (PROTON_PASS_BREAKGLASS_PAT in
            # Infisical) when that happens. ensure_session()'s own error strings are fixed
            # text (never include the token), so it's safe to put the last line in the page --
            # it also lets this distinguish "PAT is bad" from e.g. "Infisical unreachable".
            output = (session_check.stderr or session_check.stdout or "").strip()
            last_err = output.splitlines()[-1] if output else "(no output)"
            _page(f"no_session:{JOB}", "Break-Glass sync: auto-login from PROTON_PASS_BREAKGLASS_PAT failed: "
                  f"{last_err}. If the PAT itself is the problem (missing/expired/revoked), generate a new "
                  "one (role: editor, vault: Break-Glass) and update Infisical.", dry_run)
        else:
            days_left = (PAT_EXPIRES - datetime.now(timezone.utc).date()).days
            if days_left <= PAT_PAGE_DAYS:
                _page(f"pat_expiring:{JOB}",
                      f"Break-Glass sync PAT expires in {days_left}d ({PAT_EXPIRES}). Run: "
                      "pass-cli personal-access-token renew --personal-access-token-name "
                      "nexus-breakglass-sync, then update PROTON_PASS_BREAKGLASS_PAT in Infisical.", dry_run)
            elif days_left <= PAT_WARN_DAYS:
                print(f"ok: last run {age_h:.1f}h ago, exit 0 (PAT expires in {days_left}d, no page yet)")
            else:
                print(f"ok: last run {age_h:.1f}h ago, exit 0")
    return 0


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--dry-run"]
    dry = "--dry-run" in sys.argv[1:]
    if len(args) != 1 or args[0] not in ("run", "check"):
        raise SystemExit("usage: breakglass_sync_guard.py run|check [--dry-run]")
    raise SystemExit(run(dry) if args[0] == "run" else check(dry))
