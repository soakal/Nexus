---
name: nexus-restore-drill
description: Restore the NEXUS database, vault, or knowledge store from backup — the three backup sources and what's actually in each, the ordered stop/checkpoint/clear-sidecars/swap/verify procedure, and the two files that must never be mistaken for routine backups. Use when recovering from corruption or a bad deploy, or when running a periodic drill. Read before touching /var/lib/nexus/nexus.db.
---

# Restoring NEXUS from backup

> **Do not build a restore script.** One exists for the DB (`backup.restore_from`, below) and it
> is enough. `backend/backup.py::restore_vault`'s POSIX branch (`:257-282`) deliberately refuses
> rather than growing a second code path — the 2026-08-14 restore drill used a manual
> `rclone copy` and that stands as the confirmed decision. **This skill is the documentation that
> replaces that code.** The refusal is loud on purpose: the previous behaviour silently
> mis-resolved a UNC string as a local path with backslash-literal filenames, matched nothing, and
> degraded to a misleading "no vault files found".

A backup that has never been restored is a hope. `tests/test_restore_drill.py` exists for exactly
this reason; so does this skill.

## The three backup sources

**1. Daily local — `/var/lib/nexus/backups/YYYYMMDD-HHMMSS/`**
Written by the `db_backup` scheduler job (03:30). Contains a checkpointed `nexus.db` snapshot plus
secrets. Retention `backup_retention_days`, default **7**; pruning only touches directories
matching the `YYYYMMDD-HHMMSS` pattern, so anything you drop in there by hand with a different
name survives. **Local only, no network** — this is your fastest restore and your least durable
one. It dies with the LXC.

**2. Unraid staging tree — `/var/lib/nexus/.unraid_staging/`**
A local mirror of exactly what should be on the Unraid share: the current copy at the root, plus
`history/YYYYMMDD-HHMMSS/` capped at **14** dated copies (`_HISTORY_KEEP`). `backup_vault()` does
all its copy/history/prune work here with plain `shutil`, then one `rclone sync` mirrors it
off-box. Restoring from staging is just a local file copy — no network needed.

**3. Off-box rclone target — `nexus-unraid:<share>/Nexus_backup-lxc/`**
The real durability. **Kernel `mount.cifs` has never worked in this unprivileged LXC** — it fails
`mount error(13) Permission denied` against this specific Unraid SMB server, with every protocol
and auth variant tried. Don't burn an hour rediscovering that. `rclone` and `smbclient` (userspace
SMB, no kernel mount, no elevated capability) connect fine; `rclone` was chosen because
`rclone sync` already does the copy+mirror-delete this needs.

```bash
rclone lsd nexus-unraid:                                   # remote reachable?
rclone ls "nexus-unraid:Computer Backup/Nexus_backup-lxc/history" | head
rclone copy "nexus-unraid:Computer Backup/Nexus_backup-lxc/history/<ts>" /tmp/restore-src
```

Note the mirror-delete semantics: `rclone sync` **deletes remotely anything pruned locally**. A
`backup_vault()` run with a broken local staging tree can therefore propagate emptiness to the
remote. (This is not hypothetical — an unrelated test-suite misconfiguration once mirror-deleted
the real share's dated history, which is why `conftest.py` now force-blanks
`UNRAID_BACKUP_PATH` before any backend import.) Copy *from* the remote by hand; never "fix" a
remote by running a sync toward it.

## The two files that are NOT routine backups

- **`/var/lib/nexus/db-pre-cutover-20260815/nexus.db`** — the pre-cutover rollback DB from the
  2026-08-15 Windows→LXC migration. It preserves this instance's own genuine audit trail
  (including real `ActionLog` rows from that night's incidents) and is the rollback path for the
  cutover itself. It is **not** in the retention rotation and must never be treated as "just an
  older backup" — restoring it silently reverts the entire cutover. It is also two days *older*
  than the data that replaced it.
- **`/opt/nexus/nexus.db`** — a 4096-byte empty decoy created by a script run from the wrong cwd.
  Restoring "from" it, or over it, does nothing useful. See `nexus-remote-python`'s cwd trap.

## `.vault.key` is in no backup, by design — and losing it is unrecoverable

Secrets live encrypted in `nexus.vault` (Fernet); the key is `.vault.key` (chmod 0600). The
encrypted vault is backed up. **The key is not, anywhere, deliberately** — a backup containing
both is a backup containing plaintext secrets.

There is no recovery path. Lose `.vault.key` and every secret must be re-entered by hand:
`ANTHROPIC_API_KEY`, `NEXUS_API_KEY`, `HASS_TOKEN`, `TELEGRAM_BOT_TOKEN`, the UniFi/Unraid/
ProtonMail credentials, `OPENROUTER_API_KEY`. `nexus.vault.meta` (names + timestamps, no values)
is tracked in git and tells you *which* secrets you need to reconstruct — that is its whole
purpose, so read it before you start guessing.

Confirm the key exists before you begin any restore, and keep your own copy somewhere that is not
this host:

```bash
ssh -i ~/.ssh/id_ed25519 root@100.84.21.43 'ls -la /var/lib/nexus/.vault.key /var/lib/nexus/nexus.vault'
```

## The ordered DB restore procedure

Every step matters. Step 3 is the one that actually bit during the real cutover.

### 1. Stop the backend — and confirm nothing is mid-run

```bash
ssh -i ~/.ssh/id_ed25519 root@100.84.21.43 'systemctl stop nexus-backend && systemctl is-active nexus-backend'
```

Expect `inactive`. Restoring under a live engine reads and writes a clobbered file. **Before
stopping, check the 02:00 `brain_organizer` run has genuinely finished** — watch for its own log
completion line, not the scheduler's "job started" line. Killing it mid-run destroys that night's
digestion of raw notes. This exact check was made during the 2026-08-15 cutover.

### 2. Checkpoint the WAL

```bash
ssh -i ~/.ssh/id_ed25519 root@100.84.21.43 'cd /var/lib/nexus && sqlite3 nexus.db "PRAGMA wal_checkpoint(TRUNCATE);"'
```

The DB runs in WAL mode. Uncheckpointed, committed data lives in `nexus.db-wal`, not `nexus.db` —
so a naive file copy of `nexus.db` alone can be missing the most recent writes. Also back up the
current file *before* overwriting it, even when you're sure:

```bash
ssh -i ~/.ssh/id_ed25519 root@100.84.21.43 'cd /var/lib/nexus && cp -a nexus.db /var/lib/nexus/pre-restore-$(date +%Y%m%d-%H%M%S).db'
```

### 3. Delete the stale `-wal`/`-shm` sidecars BEFORE swapping the file

```bash
ssh -i ~/.ssh/id_ed25519 root@100.84.21.43 'cd /var/lib/nexus && rm -f nexus.db-wal nexus.db-shm && ls -la nexus.db*'
```

**This is the step that bit during the real cutover.** The sidecars belong to the *old* database.
Leave them next to a freshly swapped-in `nexus.db` and SQLite reads torn state — a mix of the new
file and the old file's journal. It does not necessarily error; it can just be quietly wrong,
which is worse.

### 4. Swap in the backup

`backup.restore_from` does steps 3-4 correctly and refuses a missing or integrity-failing backup
*before* touching the live file. Prefer it over a hand `cp`:

```bash
ssh -i ~/.ssh/id_ed25519 root@100.84.21.43 'cd /var/lib/nexus && PYTHONPATH=/opt/nexus /opt/nexus/venv/bin/python -' <<'PY'
from backend.agents import backup
print(backup.restore_from("/var/lib/nexus/backups/20260822-033000"))
PY
```

Healthy: `{'ok': True, 'restored': '.../nexus.db', 'error': None}`. An `ok: False` here means it
refused and **the live DB is untouched** — that is a successful outcome of a bad backup, not a
failed restore. Note the cwd requirement (`/var/lib/nexus`); from `/opt/nexus` this resolves the
"live" DB to the 4096-byte decoy and cheerfully reports success having restored nothing.

For the vault or knowledge store there is no code path — `rclone copy` from the remote (or `cp -a`
from `.unraid_staging`) into `/var/lib/nexus/`, by hand, as the YAGNI guard says.

### 5. Verify BEFORE restarting

Do not restart and then check. A backend that boots against a corrupt DB starts writing to it.

```bash
ssh -i ~/.ssh/id_ed25519 root@100.84.21.43 'cd /var/lib/nexus && sqlite3 nexus.db "PRAGMA integrity_check;" &&
  sqlite3 nexus.db "SELECT (SELECT COUNT(*) FROM goal), (SELECT COUNT(*) FROM actionlog), (SELECT COUNT(*) FROM spendlog);" &&
  ls -la nexus.db'
```

Healthy: `integrity_check` prints exactly `ok`, the row counts are *plausible for the backup's
date* (not zero, not wildly larger than you expect), and the file is tens of megabytes — ~24 MB as
of 2026-08-22. **A 4096-byte file means you restored the decoy.** This is the same
integrity-plus-row-count check the cutover used before touching the live file.

### 6. Restart and confirm

```bash
ssh -i ~/.ssh/id_ed25519 root@100.84.21.43 'systemctl start nexus-backend && sleep 12 &&
  systemctl is-active nexus-backend &&
  curl -sf http://localhost:8000/api/health &&
  journalctl -u nexus-backend --since "1 minute ago" --no-pager | grep -iE "error|traceback" | head'
```

Healthy: `active`, `{"status":"ok"}`, no error lines. Then confirm the scheduler came up with its
full job set (`nexus-lxc-health-audit` step 2) — a DB swap can change which gated jobs register if
`SystemState` rows moved.

## Drill cadence

Run the whole procedure against a **copy** — restore a backup into `/tmp/drill/nexus.db` and run
steps 5's checks on it — without stopping the service. That exercises the parts that actually rot
(remote reachability, backup integrity, whether the file is the size you think) at zero risk. Do
the full stop-and-swap version only for a real recovery, or deliberately, with time booked for it.
