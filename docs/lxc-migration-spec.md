# NEXUS Windows→LXC Migration — Implementation Spec

**Planner:** Fable · **Writer:** Sonnet, one step per pass · **Verifier:** checks every AC literally
**Date:** 2026-08-14 · Powered by CwiAI

Companion to the approved proposal at `C:\Users\Brian\.claude\plans\for-nexus-and-brain-ticklish-grove.md`
(current-architecture facts, design reasoning, and all locked decisions — read that first).

## Rules for the writer (apply to every step)

- **R1 — Windows NEXUS is never stopped, restarted, reconfigured, or git-pulled** before Phase 6. Any step needing a file *from* Windows copies it read-only.
- **R2 — All code changes are platform-gated** (`os.name`/`sys.platform`) so `master` stays runnable on Windows unchanged. Rollback after cutover requires it.
- **R3 — Repo is PUBLIC** (`soakal/Nexus`). Before any push that adds deploy scripts/unit files/docs, run the `public-repo-infra-scrubber` agent. Never commit `nexus.vault`, `.vault.key`, `.env`, credentials files, or SMB passwords. LAN/Tailscale IPs are allowlisted-public per `.gitleaks.toml` — leave that file alone.
- **R4 — Read existing tests before touching any function** (`tests/test_*` for backup, broker, wiki_ingest, scheduler). Run full `pytest` after each code step; baseline is **3 known pre-existing failures** (two scheduler-job-count tests, one time-of-day-flaky proposer test) — zero *new* failures allowed.
- **R5 — One step per pass.** Each step ends with its AC verified and stated (or explicitly "untestable this session because X").
- **R6 — Proxmox host access**: root SSH to `192.168.1.60` via `~/.ssh/proxmox_deploy_ed25519` (already set up). LXC shell via `pct exec <CTID>` or SSH once installed.
- **R7 — Shadow-mode invariant (Phases 1–5):** the LXC instance must never share the production Telegram bot token, never auto-trash mail, never dispatch autonomous writes, and never write to Windows's Unraid backup path. Steps 1.4/1.6 implement this; every later step preserves it.

---

## Phase 0 — Provision the LXC

**0.1 — Create the container.**
On the Proxmox host: create an **Ubuntu 24.04** unprivileged CT, storage **local-lvm**, suggested CT ID **207** ⚠ *(confirm ID + final hostname with Brian)*, hostname `nexus`, rootfs **48 GB** (torch/whisper + node_modules are heavy), **4 vCPU / 8 GB RAM / 1 GB swap** ⚠ *(defaults, Brian may resize)*, `features: nesting=1`, DHCP on vmbr0, onboot=1.
**AC:** `pct status 207` → `running`; `pct config 207` shows `unprivileged: 1`, `onboot: 1`, rootfs on `local-lvm`; from inside, `lsb_release -rs` → `24.04`; note the assigned LAN IP in the step report.

**0.2 — Base OS setup.**
`apt install` git, curl, build-essential, rsync, cifs-utils, ffmpeg, sqlite3. Set timezone: `timedatectl set-timezone America/Detroit`. Locale `en_US.UTF-8`.
**AC:** `timedatectl` shows `America/Detroit`; `ffmpeg -version`, `rsync --version`, `mount.cifs -V` all succeed; `locale` shows UTF-8.

**0.3 — Python 3.13 + Node.**
Windows runs Python 3.13 (requirements were specifically adjusted for it — whisper/torch pins). Install `python3.13 python3.13-venv python3.13-dev` via deadsnakes PPA; Node 22 LTS via NodeSource.
**AC:** `python3.13 --version` reports 3.13.x matching the Windows minor version (check with `C:\...\nexus\venv\Scripts\python.exe --version` from the Windows repo, read-only); `node --version` ≥ 22; `npm --version` works.

**0.4 — Tailscale.**
Unprivileged CTs lack `/dev/net/tun` — replicate LXC 206's pattern: add the `lxc.cgroup2.devices.allow: c 10:200 rwm` + `lxc.mount.entry` lines to `/etc/pve/lxc/207.conf`, restart CT, install tailscale, `tailscale up`. **Do NOT configure `tailscale serve` yet** (Phase 6).
**AC:** `tailscale status` shows the node logged in with a `100.x` address and MagicDNS name; `tailscale ping` from Brian's PC succeeds.

**0.5 — Directory skeleton.**
Create `/opt/nexus` (code), `/var/lib/nexus` (runtime data/secrets — will be the systemd WorkingDirectory), `/var/lib/nexus/knowledge` (canonical knowledge store), `/mnt/unraid-backup` (empty CIFS mountpoint). Services run as **root inside the unprivileged CT** (isolation comes from the CT boundary; matches this homelab's single-purpose-CT pattern).
**AC:** all four directories exist; `stat -c %U /var/lib/nexus` → root.

---

## Phase 1 — Deploy NEXUS + verify parity (shadow mode)

**1.1 — Clone + venvs + frontend build.**
`git clone https://github.com/soakal/Nexus /opt/nexus` (⚠ confirm exact remote URL via `git remote -v` on Windows, read-only). `python3.13 -m venv /opt/nexus/venv`, `pip install -r requirements.txt` (respect the `starlette==0.38.6` re-pin; document whatever torch/whisper install path is actually used on Linux). `cd frontend && npm ci && npm run build`. Also provision the Brain Organizer module's own venv.
**AC:** `pip check` clean in both venvs; `npm run build` exits 0 with a `dist/` produced; `git log -1` SHA matches current `origin/master`.

**1.2 — Seed secrets + DB from the Unraid backup (this doubles as a live restore drill).**
Copy `nexus.vault`, `nexus.vault.meta`, `.vault.key` (if present in backup; else read-only from Windows repo root), and the `nexus.db` snapshot from `\\192.168.1.50\Computer Backup\Nexus_backup` into **`/var/lib/nexus/`** — these paths are cwd-relative in code, and the systemd unit's `WorkingDirectory=/var/lib/nexus` satisfies "secrets outside the repo tree" with zero code changes. Copy `.env` read-only from Windows to `/var/lib/nexus/.env`. Delete any stale `nexus.db-wal`/`nexus.db-shm`.
**AC:** all four files present in `/var/lib/nexus`; `sqlite3 /var/lib/nexus/nexus.db "PRAGMA integrity_check;"` → `ok`; a one-off python check can decrypt the vault and list secret *names* (never print values). Report that the Unraid backup was genuinely restorable — that's the drill result.

**1.3 — Initial knowledge-store copy.**
Copy the **entire Obsidian vault** from Windows (`C:\Users\Brian\iCloudDrive\iCloud~md~obsidian`) to `/var/lib/nexus/knowledge/` (tar-over-SSH or SMB read, one-way, Windows untouched). Whole vault, not just `Brain/`. ⚠ First inventory the vault root and report what exists besides `Brain/`; flag anything that looks like non-vault iCloud noise before copying.
**AC:** `Brain/raw`, `Brain/wiki`, `Brain/wiki/daily`, `Brain/_meta` all exist under `/var/lib/nexus/knowledge/`; file count within ±1% of the Windows source (iCloud eviction placeholders must be materialized on Windows first if mismatched ⚠).

**1.4 — Shadow-mode configuration (must complete BEFORE first backend start).**

> **Rewritten 2026-08-14 to match what was actually deployed** — the staging-bot design
> below was never built; the real shadow-mode mechanism is a poll-off flag on the SAME
> real bot/token, not a second bot. Kept here (struck through in spirit, not deleted) so a
> future migration doesn't have to re-derive why the simpler design won.

1. **Telegram: same real bot/token as Windows, via Infisical — the LXC just doesn't poll.**
   The originally-specced "create a staging bot, override the token locally, keep Infisical
   off" approach was never built. The real mechanism is simpler: both instances share the
   one real `TELEGRAM_BOT_TOKEN` (both read it from the same Infisical project), but
   `TELEGRAM_POLL_ENABLED=false` in the LXC's `.env` stops `telegram_poller.py`'s
   `getUpdates` long-poll loop from ever starting there (`backend/config.py`'s
   `telegram_poll_enabled` flag, checked in `telegram_poller.py`'s startup). That's the
   entire fix for the 409-war risk the staging-bot design existed to avoid — only one
   process (Windows) ever calls `getUpdates`, so there's nothing to collide with. The LXC
   can still legitimately *send* (its own scheduled jobs, watchdog alerts, etc. all still
   fire through the same bot) — that's intentional, not a gap; it's part of proving parity.
2. **Infisical: fully configured from day one, not deliberately withheld.** `INFISICAL_URL`/
   `INFISICAL_CLIENT_ID`/`INFISICAL_CLIENT_SECRET`/`INFISICAL_PROJECT_ID`/`SECRETS_BACKEND`
   are all set in `/var/lib/nexus/.env`, and `get_secret` resolves the real prod secrets the
   same way Windows does — this is also just simpler than standing up a parallel secret set,
   and per the original migration proposal's own Decision #4, secrets were never meant to be
   a Windows-vs-Linux distinction in the first place (Infisical primary + local vault
   fallback is platform-agnostic already).
3. **Paths:** `OBSIDIAN_VAULT_PATH=/var/lib/nexus/knowledge`, `UNRAID_BACKUP_PATH=` a
   UNC-style string, e.g. `\\192.168.1.50\Computer Backup\Nexus_backup-lxc` — **not** a real
   Linux mountpoint like `/mnt/unraid-backup/...` as originally specced. `backend/backup.py`'s
   POSIX path never mounts anything; it parses this string into an rclone remote
   (`_smb_share_and_subpath`) and stages locally first (see the fix-plan entry above this
   one in the CLAUDE.md history for the full rclone design). Distinct from Windows's path —
   two instances must never write one history rotation (confirmed: the `-lxc` suffix is
   what keeps the two shares separate).
4. **Autonomy:** confirm the LXC's actual `autonomy_enabled` state on `SystemState` before
   relying on this doc's framing — this section describes the intended STARTING state
   (autonomy off, scheduled jobs run, agent/autonomous broker dispatches FORBIDDEN+logged,
   user-actor drills still execute), not necessarily where burn-in has progressed to by the
   time you're reading this.
5. **Mail writes off:** `MAIL_AUTOTRASH_ENABLED=false`; check for any autodraft-enable flag and disable it too.
6. **Windows-path audit:** grep `backend/config.py` defaults for `C:\\`/drive-letter/UNC values; override every one that matters; report any beyond the known two (`obsidian_vault_path`, `unraid_backup_path`).
**AC:** written checklist of all 6 items done; Infisical resolves real secrets (confirm via
`SecretFallback` table staying EMPTY, the inverse of the original spec's expectation);
`TELEGRAM_POLL_ENABLED=false` confirmed in `.env` and `telegram_poller` logs "disabled" at
boot; `sqlite3` SELECT reports the current `autonomy_enabled` value (whatever it is) rather
than asserting it must be 0; grep output included.

**1.5 — systemd units.**
Create (commit templates under `deploy/`, scrubbed per R3):
- `nexus-backend.service`: `WorkingDirectory=/var/lib/nexus`, `ExecStart=/opt/nexus/venv/bin/python /opt/nexus/run.py`, `Restart=on-failure`, `RestartSec=10`, `StartLimitIntervalSec=600`, `StartLimitBurst=5`, `After=network-online.target`. Default `KillMode=control-group` is load-bearing — it kills the spawned `:8765` Flask child too, fixing the documented Windows orphan-`mcp_server` bug for free.
- `nexus-frontend.service`: `WorkingDirectory=/opt/nexus/frontend`, vite preview on `0.0.0.0:3000`, `Restart=on-failure`.
This wholesale replaces `tray.py`/`tray_supervisor.ps1`/Task Scheduler/auto-logon — none of it is ported.
**AC:** `systemctl enable --now` both; `active (running)`; `curl http://127.0.0.1:8000/api/health` → ok (needs 1.6's fix for full health); `curl` on `:3000` → 200.

**1.6 — Fix the Windows-hardcoded MCP-server spawn path** *(new finding — blocks the whole `:8765` write surface on Linux)*.
`backend/main.py:172` builds `venv / "Scripts" / "python.exe"`; on Linux that never exists, so the Brain MCP server silently never spawns and every vault write fails. Fix platform-aware (`"Scripts/python.exe" if os.name == "nt" else "bin/python"`). Then `grep -rn "Scripts" backend/ modules/ tools/` and fix every other venv-path assumption the same way — `scheduler.py::_run_brain_organizer`'s subprocess launch of `brain_organizer.py` is the expected second hit. Set the brain-organizer module's own `vault_path` config to `/var/lib/nexus/knowledge`.
**AC:** journal shows `Brain Organizer MCP server started (PID …)`; `curl -X POST http://127.0.0.1:8765/raw ...` → 2xx and the file appears under `Brain/raw/`; grep shows zero remaining un-gated `Scripts` paths; pytest green (R4); Windows path byte-identical (R2).

**1.7 — Parity test pass.**
Full `pytest` on the LXC. Manual parity sweep against `http://<lxc-ip>:3000`: Dashboard cards live, Today page, Chat, HA tab reads state (do NOT toggle devices yet), Traces/Pulse render, Settings shows secret names.
**AC:** pytest same 3 (or fewer) known failures, zero new; each sweep item pass/fail; `/api/safety/status` shows `autonomy_enabled: false` and `running_sha` matches `git rev-parse HEAD`.

---

## Phase 2 — The two real code ports (both platform-gated per R2)

**2.1 — `backend/safety/broker.py::_dispatch_system_restart` — Linux branch.**
Keep the Windows branch verbatim. On Linux: `subprocess.Popen(["systemd-run", "--on-active=3", "systemctl", "restart", "nexus-backend", "nexus-frontend"])` — survives the backend's own death mid-restart, same as the PowerShell version's detached-process property. Read existing broker/system_restart tests first (R4).
**AC:** drill via `/restart` from the staging bot → response arrives, then within ~15s `systemctl show nexus-backend -p ExecMainStartTimestamp` shows a fresh start, health ok, `ActionLog` row `executed`. pytest green; Windows branch byte-identical.

**2.2 — `backend/backup.py` — POSIX backup target.**
CIFS via fstab automount — `backup_vault()`'s copy/history/prune logic is path-agnostic `shutil`, needs zero changes; only `_mount_unc` (PowerShell) becomes dead code, already gated by the `\\\\` prefix check.
1. `/etc/fstab`: CIFS entry with `x-systemd.automount,_netdev,noauto`, credentials file mode 0600.
2. New optional setting `unraid_backup_mountpoint` — on POSIX, `backup_vault()` returns `{"ok": False, "error": "backup mountpoint not mounted"}` unless `os.path.ismount()` passes, so a failed automount can't silently write to the local rootfs. Never raises (preserve contract; read tests first).
3. LXC `.env`: `UNRAID_BACKUP_MOUNTPOINT=/mnt/unraid-backup`.
**AC:** trigger once → `{"ok": True}`; share shows vault/meta/DB snapshot + dated `history/` entry; negative test (unmount, retrigger) → `ok: False`, nothing written locally; restore mount. pytest green.

**2.3 — Fix `backend/agents/wiki_ingest.py` direct vault writes.**
Bypasses `:8765` with raw `pathlib` writes at ~lines 195, 208–218, 304, 350, 487, 539, 607–613 (the live Sunday `weekly_fragmentation_report` → `Brain/wiki/Inbox.md`), 688. Daily cron already removed; only the fragmentation report still runs.
1. Delete the dead ingest write-paths nothing calls.
2. Route the live report's output through `obsidian.py`'s `:8765 /raw` surface as a dated raw note. ⚠ **Behavior change to flag Brian:** arrives via raw→wiki pipeline instead of a direct `Inbox.md` append — don't build a dedicated append endpoint unless he asks for byte-identical semantics.
3. Add a regression guard test scanning `backend/agents/`+`backend/integrations/` asserting no module except `obsidian.py` writes the vault path directly.
**AC:** grep shows zero vault-path writes outside `obsidian.py`; guard test passes; manual invoke → note lands via `:8765`, `Inbox.md` untouched; pytest green; update CLAUDE.md's "all direct pathlib writes gone" claim to actually be true.

---

## Phase 3 — Knowledge-store backup extension + snapshots

**3.1 — `backup_knowledge()` — frequent knowledge sync to Unraid.**
Extend `backend/backup.py`: `rsync -a --delete /var/lib/nexus/knowledge/ <unraid_backup_path>/knowledge/`. One mirrored copy, no 14-deep history (point-in-time history comes from nightly Proxmox snapshots + Syncthing versioning). New scheduler job, every 30 min. Two tests hardcode a job count — update them, note which of the 3 known failures this touches.
**AC:** canary file created → appears on share; deleted → gone from share (`--delete` verified); job registered on a 30-min trigger; pytest green.

**3.2 — Nightly Proxmox vzdump backup of the CT.**
Add CT 207 to the existing vzdump schedule if one exists, else create one on whatever storage the other CTs use ⚠ *(read the existing job config first, match it)*.
**AC:** backup config includes CT 207; after one cycle a fresh vzdump archive exists; the Dashboard's `proxmox_backups` badge reflects it.

**3.3 — Restore drill.**
Rename `knowledge/` aside, restore from the Unraid mirror; restore `nexus.db` from the newest backup into a scratch path and integrity-check.
**AC:** restored file count matches live at drill time; `PRAGMA integrity_check` → `ok`; NEXUS untouched/healthy throughout.

---

## Phase 4 — Syncthing to Obsidian clients

**4.1 — Syncthing daemon on the LXC.**
`apt install syncthing`, system service, GUI on LAN/tailnet with a password. Share `/var/lib/nexus/knowledge` as `obsidian-vault` (Send & Receive), **staggered file versioning ON** (e.g. 30 days) — the conflict safety net for a headless write-peer.
**AC:** service active; GUI password-protected; folder Up to Date.

**4.2 — Windows client, against a STAGING folder (not the live vault).**
Install on Windows; accept into `C:\Users\Brian\ObsidianSync\` — **never** the live iCloud vault (double-syncing one folder is the exact forking failure the proposal forbids). Repoints to the real vault only at cutover (6.4).
**AC:** initial sync completes, counts match; round-trip ≤60s each direction.

**4.3 — iPhone client, staging.**
⚠ No official iOS client — Möbius Sync or Synctrain, **Brian picks** (foreground-only, accepted). Staging location, not the live vault yet.
**AC:** foregrounded app reaches Up to Date; round-trip works; foreground-only behavior demonstrated and noted.

**4.4 — `.stignore` + conflict behavior check.**
Add `.stignore` (`.obsidian/workspace*.json`, backup/lock artifacts). Force a same-file conflict while Windows is offline, reconnect.
**AC:** a `.sync-conflict-*` file appears (no silent data loss) on both peers.

---

## Phase 5 — Burn-in + verification drills (minimum 14 days side-by-side)

**5.1 — Uptime Kuma monitors for the LXC.**
Clone the two NEXUS monitors on Kuma (LXC 206) → LXC backend (Keyword `"status":"ok"`) + frontend, same settings.
**AC:** both green; a 4-min stop/start produces a DOWN then UP alert.

**5.2 — Supervision drills (the headline fix).**
(a) `kill -9` the backend with no session open afterward → systemd restarts unattended. (b) `pct reboot 207` → both services return with nobody logging in. (c) confirm the `:8765` child died with the backend, no orphan.
**AC:** (a) restart count increments, health ok ≤60s; (b) both units active post-reboot with zero logged-in users; (c) exactly one process on 8765, PID postdates the restart.

**5.3 — Nightly/weekly job-cycle verification (real days, don't simulate).**
Confirm with evidence: `brain_organizer` digestion, Sunday `wiki_fragmentation_report`, morning briefing to the staging bot, `backup_vault`+`backup_knowledge` landing, `retention_prune`/`record_uptime`/`record_speedtest`, Sunday `facts_digest`.
**AC:** dated evidence table, one row per job. Any job that never fired blocks Phase 6.

**5.4 — Interactive parity drills via the staging bot + UI.**
`/status`, `/calendar`, `/mail`, `/task` (completes), a voice note, `/flags`, a goal-approve tap (must come back FORBIDDEN — correct shadow-mode result). One user-actor HA toggle drill on a harmless light (confirm the specific device with Brian first ⚠).
**AC:** each drill pass/fail with observed output; forbidden goal dispatch logged as `autonomy_disabled`.

**5.5 — Manual deploy drill (confirmed-manual, per locked decision 5).**
`deploy/update.sh` (git pull → deps if changed → build if changed → restart), committed under `deploy/` (scrubbed). Run once by hand.
**AC:** `running_sha` matches `origin/master`; no deploy-drift alert; health ok.

**5.6 — Burn-in exit review.**
Assemble go/no-go evidence (Kuma uptime, restart causes, job table across ≥2 Sundays, backup streak, Syncthing health, open flags).
**AC:** written summary; **Brian explicitly says "go" before any Phase 6 step** — a generic earlier "yes" does not carry forward.

---

## Phase 6 — Deliberate cutover (each step individually confirmed with Brian; one sitting, this order)

**6.1 — Final data re-sync.**
Stop LXC services only. Fresh `nexus.db` from the newest Unraid snapshot, fresh vault files, a final one-way vault copy Windows→LXC overwriting burn-in wiki writes. Delete stale WAL sidecars.
**AC:** DB integrity ok; knowledge counts match; LXC down, Windows still serving.

**6.2 — Telegram token swap (the moment of cutover).**
Run `stop.ps1` on Windows, disable the "NEXUS Tray" task (**first pre-decommission touch of Windows — confirmed specifically**; rollback = re-enable + `start.ps1`, ≤5 min). LXC: write the production Telegram token, start services.
**AC:** Windows ports refused; poller shows zero 409s over 10 min; `/status` from the real chat answers from the LXC.

**6.3 — Tailscale serve + APP_BASE_URL.**
Rebuild `/api`/`/ws`/`/` serve mounts on the LXC per the `nexus-https-setup` skill (adapted to Linux `tailscale serve`); set `APP_BASE_URL`; remove Windows's serve config.
**AC:** HTTPS tailnet URL loads the app; WS-dependent page (Pulse) live-updates; an alert link points at the new URL.

**6.4 — Obsidian devices onto Syncthing (vault leaves iCloud).**
⚠ User-visible, Brian does this: Windows repoints to the real vault location (iCloud folder left frozen, not deleted); iPhone points Syncthing at the local Obsidian vault storage.
**AC:** cross-device notes propagate; iCloud vault mtime stops advancing; Brian confirms both devices open the new vault.

**6.5 — Repoint `:8765` write-clients + MCP_Obsidian's vault path.**
Update `C:\Users\Brian\CLAUDE.md`'s `/save` flow to the LXC URL + Bearer token (loopback exemption no longer applies remotely). Update MCP_Obsidian's ("Carl," stays on Windows) vault-path config to the new Syncthing vault location only.
**AC:** test POST with Bearer token lands and syncs back; `vault_search_notes` finds a note that only exists in the new location.

**6.6 — Un-shadow: Infisical + autonomy + mail + monitors.**
Add Infisical creds to the LXC; `autonomy_enabled=1`; mail-autotrash back to default; repoint Kuma monitors to the LXC IP; note the VM-101 failure-correlation change in CLAUDE.md.
**AC:** `autonomy_enabled: true`; no new `SecretFallback` rows; zero Kuma monitors on `192.168.1.119`; CLAUDE.md updated.

**6.7 — Documentation cutover.**
Update both CLAUDE.md files (run/deploy/layout facts; tray/PS1 sections marked historical) and `/save` a session note through the new brain URL (also tests 6.5 live).
**AC:** both files committed (project one scrubbed+pushed); session note visible in `Brain/raw/` on the LXC.

---

## Phase 7 — Post-cutover soak, deploy graduation, Windows decommission

**7.1 — Post-cutover soak (≥14 days).**
Same evidence table as 5.3/5.6, production config, real everything.
**AC:** the table, zero Windows fallback incidents.

**7.2 — Graduate deploy to fully-automatic (locked decision 5's end state).**
`nexus-update.timer` running `deploy/update.sh` every 30 min, only acts on HEAD change.
**AC:** a trivial push reflects in `running_sha` within 35 min with no human action; no-change interval performs no restart.

**7.3 — Decommission Windows NEXUS (each item individually confirmed).**
In order: delete the "NEXUS Tray" task; disable auto-logon (⚠ only if nothing else on that VM needs it — Brian's own Claude Code sessions run there too; the VM itself is not decommissioned); revert WU hardening if desired; archive-then-remove Windows runtime data; retire the old Unraid share folder to `archive/`; optionally delete the frozen iCloud vault copy only after weeks of clean Syncthing operation.
**AC:** scheduled task gone; no NEXUS process on Windows after reboot; dated checklist with per-item confirmations; final `/save` note recording completion.

---

## Flagged unknowns (writer: resolve with Brian or on-host, never guess)

1. CT ID/hostname/resources (0.1).
2. Exact GitHub remote URL for the clone (1.1).
3. Vault-root contents beyond `Brain/` + iCloud-evicted placeholders (1.3).
4. Staging bot token creation (1.4) — human step.
5. iPhone Syncthing client choice (4.3) — Brian picks.
6. Existing vzdump job config/target on the Proxmox host (3.2).
7. Inbox.md semantics change for the fragmentation report (2.3) — flag, default to `/raw`.
8. Infisical machine-credential mechanics for the LXC (6.6) — writer must read `backend/secrets/manager.py`'s auth config before 1.4; if Infisical config lives in the copied `.env`, it must be **stripped** there instead of merely "not added."
9. Known post-cutover regression, accepted: the Dashboard's Claude-usage card reads `~/.claude/rate-limits.json` on the NEXUS host — stays on Windows, so the card goes permanently stale on the LXC. Note in CLAUDE.md at 6.7, don't fix.

*Skipped (YAGNI, revisit only if asked): MCP-server consolidation, a wiki-append endpoint on mcp_server.py, containerizing anything, HA/floating-IP tricks, migrating MCP_Obsidian.*
