---
name: nexus-lxc-health-audit
description: Top-to-bottom health audit of the nexus-lxc host (Proxmox LXC 207, 192.168.1.62 / Tailscale nexus-lxc) — services, scheduler job registration, Syncthing vault sync, Unraid knowledge backup, watchdog/deploy-drift via /api/safety/status. Use when something on the LXC seems off, after a deploy/restart, or as a periodic check. Post-2026-08-15 cutover this host owns ALL jobs.
---

# nexus-lxc Health Audit

Run from devbox, cheapest checks first. Code at `/opt/nexus` (git checkout, `main`), runtime
state/`.env`/DB/vault at `/var/lib/nexus` (systemd `WorkingDirectory` — the recurring
cwd-relative-config trap: anything reading `.env` or a relative DB path MUST run from
`/var/lib/nexus`, not `/opt/nexus`).

## 0. SSH access

```
ssh -i ~/.ssh/id_nexus_lxc root@nexus-lxc hostname   # expect: nexus
```

If this fails, that IS the first finding — stop and fix it (key is devbox-local at
`~/.ssh/id_nexus_lxc`; access was only established 2026-08-16, don't assume it works). All checks
below run on the host via this SSH.

## 1. Services + API health

```
systemctl status nexus-backend nexus-frontend   # both: active (running)
curl -s http://127.0.0.1:8000/api/health         # {"status":"ok"}
```

`/api/health` is the only unauthenticated endpoint. If backend is up but behaving stale, remember:
pydantic-settings reads `.env` ONCE at boot — a flag edited in `/var/lib/nexus/.env` does nothing
until `systemctl restart nexus-backend` (exact root cause of the "goal_proposer silently off"
incident, resolved 2026-08-16: flag was right, process was old).

## 2. Scheduler job registration (30 jobs expected)

After a restart, `journalctl -u nexus-backend --since "10 min ago" | grep -i apscheduler` shows
jobs running; boot log has per-flag lines like `Goal proposer enabled: every 6h (suggest-only)`.
But not every disabled flag logs (e.g. `mail_autodraft` skips silently), so the authoritative
check is a fresh-interpreter `scheduler.get_jobs()` from `/var/lib/nexus` (NOT `/opt/nexus` — the
cwd trap above; wrong cwd reads the wrong/no `.env` and lies about gating):

```
cd /var/lib/nexus && /opt/nexus/venv/bin/python - <<'EOF'
import sys; sys.path.insert(0, "/opt/nexus")
from backend.config import get_settings
from backend.scheduler import setup_scheduler, scheduler
s = get_settings()
setup_scheduler(s.briefing_time, s.briefing_timezone)
jobs = sorted(j.id for j in scheduler.get_jobs())
print(len(jobs), jobs)
EOF
```

Healthy (verified live 2026-08-16): **30 jobs**, including `goal_proposer`, `goal_recurrence`,
`morning_briefing`, `brain_organizer`, `mail_autodraft`, `knowledge_backup`, `watchdog`,
`trial_report` (registered but trial harness deliberately OFF pending Brian's go-ahead), the four
`state_refresh_*`, `vault_backup`, `db_backup`. Missing job → check its `_ENABLED` flag in
`/var/lib/nexus/.env`, then whether the service restarted since the flag changed. (The old
goal-id-overlap-with-Windows reason for disabling jobs is dead — Windows was decommissioned
2026-08-15, all 11 ownership flags are `true` here now.)

## 3. Syncthing vault sync (obsidian-vault folder)

```
curl -s -H "X-API-Key: $(grep -oP '(?<=<apikey>)[^<]+' /root/.local/state/syncthing/config.xml)" \
  "http://127.0.0.1:8384/rest/db/status?folder=obsidian-vault"
```

Healthy: `"state":"idle"`, `"needBytes":0`, `"errors":0`, `"pullErrors":0`. This checks the vault
replication chain to Brian's other devices — a third host can be down even when nexus-lxc itself
is fine. Nonzero `needBytes`/`sync-preparing` stuck for long → peer offline or conflict; check
Syncthing GUI.

## 4. Unraid knowledge backup (30-min rclone job)

```
journalctl -u "nexus*" --since "1 hour ago" | grep -i "knowledge backup"
```

Healthy: ~2 lines/hour, each `Knowledge backup ok: nexus-unraid:Computer
Backup/Nexus_backup-lxc/knowledge`. Silence or failures → `rclone` remote `nexus-unraid` broken or
Unraid share unreachable (rclone is the userspace SMB path; kernel mount.cifs never worked in this
unprivileged LXC). Note `backup_vault()` sync failures used to be swallowed silently — fixed
2026-08-14, so absence of "ok" lines is now a real signal.

## 5. Watchdog / deploy drift / spend — `/api/safety/status`

Bearer-auth'd. `NEXUS_API_KEY` is NOT in `/var/lib/nexus/.env` — it lives in the vault; fetch it
via the backend's own secrets manager (from `/var/lib/nexus`, per the cwd trap):

```
cd /var/lib/nexus
KEY=$(/opt/nexus/venv/bin/python -c 'import sys; sys.path.insert(0,"/opt/nexus");
from backend.secrets.manager import get_secret; print(get_secret("NEXUS_API_KEY"))' | tail -1)
curl -s -H "Authorization: Bearer $KEY" http://127.0.0.1:8000/api/safety/status
```

Healthy (verified live 2026-08-16): `autonomy_enabled: true`, `today_spend_usd` <
`daily_budget_usd` (2.0), `scheduler_running: true`, `notify_channel.pending_count: 0` /
`dead_lettered_count: 0`, and `running_sha` == `git -C /opt/nexus rev-parse HEAD`. SHA mismatch =
deploy drift: a `git pull` landed without a restart — `systemctl restart nexus-backend` clears it.
`secret_fallback` listing keys is informational (infisical→vault fallback counts), not a failure
by itself.

## 6. Claude usage statusline — reads nexus-lxc's OWN copy, pushed from devbox

`backend/integrations/claude_usage.py` reads `Path.home()/.claude/rate-limits.json` — since the
backend runs as root on nexus-lxc, that's `/root/.claude/rate-limits.json` **on nexus-lxc itself**,
not on devbox. Fixed 2026-08-17 (previously this file never existed anywhere — see
`devbox-setup/claude/statusline.sh`): devbox's live `~/.claude/statusline.sh` persists the
`rate_limits` payload locally on every render, then throttle-pushes it (max once/60s, backgrounded)
via `scp` over the existing devbox→nexus-lxc SSH key (`~/.ssh/id_ed25519` on devbox; nexus-lxc has
NO reverse trust to devbox, so this can only be a devbox-initiated push, never a nexus-lxc pull).

Check:
```
ssh -i ~/.ssh/id_ed25519 root@nexus-lxc cat /root/.claude/rate-limits.json
```
Healthy: recent `captured_at` (within the last few minutes if a Claude Code session was recently
active on devbox — deliberately goes stale, not wrong, whenever no session is running; that's
still correct, don't chase it as a server outage). If the file is missing entirely, check devbox's
`~/.claude/statusline.sh` actually has the persist+push block (diff against
`~/repos/devbox-setup/claude/statusline.sh`) and that `~/.ssh/id_ed25519` still exists/authenticates
to nexus-lxc.
