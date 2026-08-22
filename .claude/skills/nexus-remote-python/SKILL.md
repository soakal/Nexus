---
name: nexus-remote-python
description: Run a one-off Python diagnostic against nexus-lxc's LIVE venv and LIVE database without quote-escaping hell — the scp-a-script pattern, the heredoc shortcut, and the cwd trap that silently returns plausible zeros instead of erroring. Use whenever you need to inspect real NEXUS state (spend, settings, scheduler jobs, DB rows) rather than reason about it from the source.
---

# Running a one-off Python diagnostic on nexus-lxc

devbox has no NEXUS venv (see `nexus-test-without-venv` — the deps include torch/whisper).
nexus-lxc has the real one at `/opt/nexus/venv`, pointed at the real database. This skill is for
*asking the live system a question*, not for running tests.

## THE TRAP, before anything else: cwd must be `/var/lib/nexus`

**Never run these from `/opt/nexus`.** Code lives at `/opt/nexus`; runtime state — `.env`, the
vault, and the database — lives at `/var/lib/nexus`, which is the systemd unit's
`WorkingDirectory`. `backend/config.py` resolves the DB path *relative to cwd*, so the wrong cwd
does not error. It finds this instead:

```
-rw-r--r-- 1 root root     4096 Aug 14 21:43 /opt/nexus/nexus.db     <-- DECOY, empty schema
-rw-r--r-- 1 root root 24473600 Aug 22 09:41 /var/lib/nexus/nexus.db  <-- the real one, ~24 MB
```

A 4096-byte SQLite file is an empty-but-valid database: every query succeeds and returns zero
rows. You get "0 goals, $0.00 spend, no ActionLog entries" and no error anywhere. That is the
single most expensive way to be wrong about this system — it reads as a finding.

**Sanity check before you trust any number:** `ls -la /var/lib/nexus/nexus.db` and confirm it is
tens of megabytes, or have the script print `sqlite3.connect(...)`'s resolved path.

There are **126** files named `nexus.db` on this host. About twenty are real, and none of them is
the live DB:

- `/opt/nexus/nexus.db` — the 4096-byte decoy above, created 2026-08-14 by a script run from the
  wrong cwd. It has never been deleted because deleting it would just let the next wrong-cwd run
  create a fresh one silently.
- `/var/lib/nexus/backups/YYYYMMDD-033000/nexus.db` — the nightly local backups (7 kept).
- `/var/lib/nexus/.unraid_staging/nexus.db` and `.unraid_staging/history/YYYYMMDD-033500/nexus.db`
  — the staging tree rclone mirrors to Unraid.
- `/var/lib/nexus/db-pre-cutover-20260815/nexus.db` — the pre-cutover rollback DB. **Not a routine
  backup.** See `nexus-restore-drill` before touching it.
- `/tmp/pytest-of-root/**/nexus.db` — ~100 throwaway files from pytest runs. Harmless, ignorable,
  and the reason a bare `find / -name nexus.db` looks alarming.

## The working pattern: write locally, scp, run

Inline `ssh host python3 -c "..."` breaks as soon as the snippet contains quotes, and NEXUS
diagnostics always do — dict literals, f-strings, `select(...)` calls. The shell eats one layer of
quoting, ssh eats another, and you end up debugging your escaping instead of the system. Don't.

```bash
cat > /tmp/diag.py <<'PY'
import sys; sys.path.insert(0, "/opt/nexus")
# ... your query ...
PY
scp -i ~/.ssh/id_ed25519 /tmp/diag.py root@100.84.21.43:/tmp/diag.py
ssh -i ~/.ssh/id_ed25519 root@100.84.21.43 'cd /var/lib/nexus && /opt/nexus/venv/bin/python /tmp/diag.py'
ssh -i ~/.ssh/id_ed25519 root@100.84.21.43 'rm -f /tmp/diag.py'     # always clean up
```

The quoted `<<'PY'` heredoc (quoted delimiter = no local expansion) means you write ordinary
Python with ordinary quotes and nothing mangles it.

`sys.path.insert(0, "/opt/nexus")` is required: cwd is `/var/lib/nexus`, so `import backend`
would otherwise fail. `PYTHONPATH=/opt/nexus` in the env works equally well and is shorter for
one-liners.

## The heredoc shortcut, for genuinely short snippets

For a few lines with no tricky quoting, skip the scp round-trip and pipe the heredoc straight
through ssh:

```bash
ssh -i ~/.ssh/id_ed25519 root@100.84.21.43 'cd /var/lib/nexus && PYTHONPATH=/opt/nexus /opt/nexus/venv/bin/python -' <<'PY'
from backend.config import get_settings
s = get_settings()
print(s.action_judge_mode, s.calibration_suppression_enabled)
PY
```

The trailing `-` tells python to read the program from stdin. Note the heredoc is consumed by
**ssh's** stdin, so this cannot be combined with anything else that wants stdin. If the snippet
grows past ~10 lines or needs to be re-run with edits, go back to scp — it is easier to iterate on
a file.

Healthy looks like: your printed output and nothing else. A `ModuleNotFoundError: backend` means
the path insert is missing; a traceback mentioning `.env` or a missing secret usually means the
wrong cwd.

## Recipe 1 — spend by label (which model/feature is costing money)

```bash
ssh -i ~/.ssh/id_ed25519 root@100.84.21.43 'cd /var/lib/nexus && PYTHONPATH=/opt/nexus /opt/nexus/venv/bin/python -' <<'PY'
from backend.safety import governor
r = governor.spend_report(30)
print(f"total 30d: ${r.get('total_usd', 0):.4f}")
for e in sorted(r.get("by_label", []), key=lambda e: -e["cost_usd"])[:15]:
    print(f"  {e['label']:28s} ${e['cost_usd']:.4f}  n={e.get('calls', '?')}")
PY
```

Healthy: a handful of dollars over 30 days, `briefing`/`chat` near the top. Labels prefixed
`shadow:` are the Trial A shadow calls (see `nexus-trial-status`). A label you don't recognise
costing real money is the finding.

## Recipe 2 — settings diff against `config.py`'s defaults

The single most useful question after "did it restart": which settings does this box actually
override? pydantic-settings reads `.env` **once at boot**, so a `.env` edit does nothing until
`systemctl restart nexus-backend` — this diff shows what the *running config would be*, which you
then compare against the running process.

```bash
ssh -i ~/.ssh/id_ed25519 root@100.84.21.43 'cd /var/lib/nexus && PYTHONPATH=/opt/nexus /opt/nexus/venv/bin/python -' <<'PY'
from backend.config import Settings, get_settings
live, defaults = get_settings(), Settings.model_construct()
for name, field in sorted(Settings.model_fields.items()):
    if any(k in name for k in ("key", "token", "password", "secret")):
        continue                      # never print credentials
    cur, dflt = getattr(live, name, None), field.default
    if cur != dflt:
        print(f"{name}: {dflt!r} -> {cur!r}")
PY
```

Healthy: the eleven `*_enabled` ownership flags flipped `true` (2026-08-15 cutover), the host/URL
settings, and little else. The credential skip is deliberate — this output gets pasted into
transcripts.

## Recipe 3 — what the scheduler would actually register

A fresh interpreter is the authoritative answer, because not every gated-off job logs a line at
boot (`mail_autodraft` skips silently). This is the same technique the `nexus-lxc-health-audit`
skill uses at step 2.

```bash
ssh -i ~/.ssh/id_ed25519 root@100.84.21.43 'cd /var/lib/nexus && PYTHONPATH=/opt/nexus /opt/nexus/venv/bin/python -' <<'PY'
from backend.config import get_settings
from backend.scheduler import setup_scheduler, scheduler
s = get_settings()
setup_scheduler(s.briefing_time, s.briefing_timezone)
jobs = sorted(scheduler.get_jobs(), key=lambda j: j.id)
print(len(jobs), "jobs")
for j in jobs:
    print(f"  {j.id:32s} {j.trigger}")
PY
```

Healthy: ~30 jobs. Two are one-off `DateTrigger`s that **disappear on their own once their gate
date passes** — `infisical_soak_reminder` (2026-09-21) and `calibration_soak_reminder`
(2026-09-05). If either is missing, its date is in the past and the reminder will now never fire;
that is a real bug with a history (the Infisical one was dead from 2026-08-05 to 2026-08-22), not
a quirk. Bump the constant at the top of `backend/scheduler.py`.

## Cleanup

`rm -f /tmp/diag.py` on nexus-lxc when you're done, and remove the local copy. `/tmp` on this host
already collects pytest debris; don't add to it. If you ran a full test suite, also
`rm -rf /tmp/nexus-test /tmp/nexus.bundle`.
