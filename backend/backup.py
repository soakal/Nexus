"""Vault backup to Unraid SMB share.

Copies nexus.vault + nexus.vault.meta (+ .vault.key if configured) plus a
consistent nexus.db snapshot to the UNC path in settings.unraid_backup_path.
Keeps a dated history/ subfolder capped at 14 copies. Never raises — backup
failures must never block a secret save or crash the scheduler.

Restore path (manual):
  1. Stop NEXUS (systemctl stop nexus-backend nexus-frontend)
  2. Copy nexus.vault (and .vault.key if backed up) AND nexus.db from the
     share to the project root, overwriting the current files. Delete any
     stale nexus.db-wal / nexus.db-shm sidecars.
  3. Start NEXUS (systemctl start nexus-backend nexus-frontend)
  To restore a specific point-in-time: copy from history/<timestamp>/ instead.
"""
import logging
import os
import pathlib
import shutil
from datetime import datetime

logger = logging.getLogger(__name__)

_HISTORY_KEEP = 14  # max dated copies retained in history/

# backup_vault() (daily) and backup_knowledge() (every 30 min) both run via
# asyncio.to_thread and both rclone-sync into the same remote root -- without
# this lock, two overlapping runs could race writes into the same directory.
# Held only around the actual rclone subprocess call, not the local
# copy/history/prune work each function does first.
import threading as _threading
_RCLONE_LOCK = _threading.Lock()

# POSIX only: local staging root mirroring exactly what should exist on the
# Unraid share -- backup_vault()'s existing shutil-based copy/history/prune
# logic runs against this local path completely unchanged, then a single
# `rclone sync` at the end mirrors it to the real remote share (uploads new
# files, deletes anything pruned locally -- one command instead of separate
# remote-prune logic). Kernel `mount.cifs` was tried first and rejected
# (mount error(13) Permission denied against this specific Unraid SMB
# server, even with several protocol/auth variants) while `smbclient` and
# `rclone` (both userspace SMB clients, no kernel mount, no elevated
# unprivileged-LXC capability needed) connect fine -- rclone was chosen over
# raw smbclient scripting because `rclone sync` already does exactly the
# copy+mirror-delete this function needs, matching Unraid backup patterns
# already documented as a rejected-mount / working-userspace-client split.
_STAGING_ROOT = pathlib.Path("/var/lib/nexus/.unraid_staging")


def _smb_share_and_subpath(unc_path: str) -> tuple[str, str]:
    """Split a \\\\host\\share\\sub\\path UNC string into (share, subpath)."""
    parts = unc_path.lstrip("\\").split("\\")
    return parts[1], "/".join(parts[2:])


def _rclone_remote(unc_path: str, subpath: str = "") -> str:
    """Build the `nexus-unraid:{share}/{sub}` remote string for an rclone
    command. Extracted so tests can point _run_rclone's captured command at
    a real local tmp dir instead (rclone treats a plain filesystem path as
    local, not remote, so a test can swap this and exercise real rclone
    sync semantics without ever touching the network)."""
    share, base = _smb_share_and_subpath(unc_path)
    full = f"{share}/{base}" if not subpath else f"{share}/{base}/{subpath}"
    return f"nexus-unraid:{full}"


def _run_rclone(args: list, timeout: int) -> "subprocess.CompletedProcess":
    """The one subprocess seam every rclone invocation in this module goes
    through -- test isolation (conftest.py's autouse guard) patches THIS
    function, never subprocess.run directly, so a test that forgets to mock
    it fails loudly instead of silently reaching a real share.

    Serialized via _RCLONE_LOCK: backup_vault() (daily) and
    backup_knowledge() (every 30 min) both reach this via asyncio.to_thread
    and both sync into the same remote root -- the lock keeps two overlapping
    runs from racing writes into the same directory.
    """
    import subprocess

    with _RCLONE_LOCK:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout)


def _rclone_sync(local_dir: pathlib.Path, unc_path: str) -> bool:
    """Mirror local_dir to the Unraid share via the pre-configured
    `nexus-unraid` rclone remote (rclone config create, one-time host setup --
    not done here). Root-anchored --exclude keeps backup_vault()'s sync from
    ever touching backup_knowledge()'s separate knowledge/ subtree living in
    the same remote root (both write under the same Nexus_backup-lxc/ path;
    without this, backup_vault's mirror-delete semantics wipe the knowledge
    mirror on every run -- reproduced live 2026-08-14).

    Returns True on success, False on failure -- the caller MUST fold this
    into its own `ok` result; a swallowed failure here previously meant
    backup_vault() reported ok=True even when nothing actually reached
    Unraid (found in code review, 2026-08-14: backup_knowledge() already
    checks its own returncode correctly, this was the one place that
    didn't). Never raises.
    """
    try:
        remote = _rclone_remote(unc_path)
        result = _run_rclone(
            ["rclone", "sync", str(local_dir), remote, "--fast-list", "--exclude", "/knowledge/**"],
            timeout=300,
        )
        if result.returncode != 0:
            logger.warning("rclone sync to %s failed: %s", remote, result.stderr[:500])
            return False
        return True
    except Exception as e:
        logger.warning("rclone sync attempt failed (non-fatal): %s", e)
        return False


def backup_vault() -> dict:
    """Copy nexus.vault (+ meta + optionally .vault.key) to the Unraid share.

    Returns {"ok": bool, "dest": str, "error": str | None}.
    Never raises.
    """
    try:
        from backend.config import get_settings
        from backend.secrets.vault import VAULT_PATH, KEY_PATH, META_PATH

        s = get_settings()
        dest_root = s.unraid_backup_path.strip()
        if not dest_root:
            return {"ok": False, "dest": "", "error": "unraid_backup_path not configured"}

        is_unc = dest_root.startswith("\\\\")

        if is_unc:
            # No kernel mount available in this environment (see
            # _STAGING_ROOT's docstring) -- write to a local staging mirror,
            # rclone-sync it to the real share at the end of this function.
            dest = _STAGING_ROOT
        else:
            dest = pathlib.Path(dest_root)

        history = dest / "history"
        dest.mkdir(parents=True, exist_ok=True)
        history.mkdir(parents=True, exist_ok=True)

        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        files: list[tuple[pathlib.Path, str]] = []
        if VAULT_PATH.exists():
            files.append((VAULT_PATH, VAULT_PATH.name))
        if META_PATH.exists():
            files.append((META_PATH, META_PATH.name))
        if s.unraid_backup_include_key and KEY_PATH.exists():
            files.append((KEY_PATH, KEY_PATH.name))

        if not files:
            return {"ok": False, "dest": str(dest), "error": "no vault files found to back up"}

        # Latest copy (overwrites previous)
        for src, name in files:
            dst = dest / name
            shutil.copy2(src, dst)

        # Dated history copy
        hist_dir = history / ts
        hist_dir.mkdir(parents=True, exist_ok=True)
        for src, name in files:
            dst = hist_dir / name
            shutil.copy2(src, dst)

        # Also ship a consistent nexus.db snapshot so the off-VM bundle is a
        # RESTORABLE set, not just secrets. Snapshot to a local temp first
        # (VACUUM INTO straight onto SMB risks a half-written db on a network
        # hiccup), then copy. Best-effort: a db failure never breaks the
        # vault half of the backup.
        try:
            import tempfile
            from backend.agents.backup import snapshot_db_to, integrity_check_file, _db_path
            db_name = os.path.basename(_db_path())
            tmp_db = os.path.join(tempfile.gettempdir(), f"nexus-db-snapshot-{ts}.db")
            try:
                snapshot_db_to(tmp_db)
                if integrity_check_file(tmp_db) == "ok":
                    for target in (dest / db_name, hist_dir / db_name):
                        shutil.copy2(tmp_db, target)
                else:
                    logger.warning("db snapshot failed integrity check; not shipped to Unraid")
            finally:
                if os.path.exists(tmp_db):
                    os.remove(tmp_db)
        except Exception as e:
            logger.warning("db snapshot for Unraid backup failed (non-fatal): %s", e)

        # Prune history to _HISTORY_KEEP most recent entries
        entries = sorted(history.iterdir(), key=lambda p: p.name)
        for old in entries[:-_HISTORY_KEEP]:
            try:
                shutil.rmtree(old)
            except Exception:
                pass

        if is_unc and os.name != "nt":
            synced = _rclone_sync(dest, dest_root)
            if not synced:
                # Local staging copy succeeded, but nothing actually reached
                # Unraid -- this MUST read as a failure (the whole point of
                # the off-VM backup is the off-VM part), not silently ok=True.
                return {"ok": False, "dest": str(dest_root), "error": "rclone sync to Unraid failed"}

        logger.info("Vault backed up to %s (%s)", dest, ts)
        return {"ok": True, "dest": str(dest_root if is_unc else dest), "error": None}

    except Exception as e:
        logger.warning("Vault backup failed (non-fatal): %s", e)
        return {"ok": False, "dest": "", "error": str(e)}


def backup_knowledge() -> dict:
    """Mirror the knowledge store (obsidian_vault_path) to Unraid, frequently
    (every 30 min, per the scheduler job) -- a different freshness need than
    backup_vault()'s once-daily secrets/DB snapshot.

    Deliberately ONE mirrored copy, no 14-deep dated history like
    backup_vault() keeps: point-in-time history for the knowledge store
    comes from nightly Proxmox LXC snapshots + Syncthing's own file
    versioning, not from this function duplicating the whole vault every 30
    minutes. `rclone sync` both uploads new/changed files AND deletes
    anything removed locally, matching the local canonical copy exactly.

    Only meaningful on POSIX today: the knowledge store is a Linux-only
    concept for this migration (Windows's vault is the separate,
    iCloud-synced original, backed up nowhere by this function). Never
    raises.
    """
    try:
        from backend.config import get_settings

        if os.name == "nt":
            return {"ok": False, "error": "backup_knowledge is POSIX-only (no Windows knowledge store)"}

        s = get_settings()
        vault_path = s.obsidian_vault_path.strip()
        dest_root = s.unraid_backup_path.strip()
        if not vault_path or not pathlib.Path(vault_path).is_dir():
            return {"ok": False, "error": f"obsidian_vault_path not found: {vault_path!r}"}
        if not dest_root.startswith("\\\\"):
            return {"ok": False, "error": "unraid_backup_path is not a UNC path"}

        remote = _rclone_remote(dest_root, "knowledge")
        result = _run_rclone(
            ["rclone", "sync", vault_path, remote, "--fast-list"],
            timeout=300,
        )
        if result.returncode != 0:
            err = result.stderr[:500]
            logger.warning("backup_knowledge rclone sync failed: %s", err)
            return {"ok": False, "error": err}

        logger.info("Knowledge store synced to %s", remote)
        return {"ok": True, "dest": remote, "error": None}

    except Exception as e:
        logger.warning("Knowledge backup failed (non-fatal): %s", e)
        return {"ok": False, "error": str(e)}


def restore_vault(timestamp: str | None = None) -> dict:
    """Copy vault files from the Unraid share back to the project root.

    timestamp: a history/<ts> folder name (e.g. "20260623-033000"). If None,
    restores from the latest (root) copy. STOP NEXUS before calling this.
    Returns {"ok": bool, "src": str, "error": str | None}.
    Never raises.
    """
    try:
        from backend.config import get_settings
        from backend.secrets.vault import VAULT_PATH, KEY_PATH, META_PATH

        s = get_settings()
        dest_root = s.unraid_backup_path.strip()
        if not dest_root:
            return {"ok": False, "src": "", "error": "unraid_backup_path not configured"}

        if dest_root.startswith("\\\\") and os.name != "nt":
            # No POSIX restore path is built (YAGNI -- the 2026-08-14 restore
            # drill used a manual `rclone copy` from nexus-unraid: just fine).
            # Fail loud and say what to do instead of silently mis-resolving
            # the UNC string as a local path with backslash-literal filenames
            # (what used to happen here, which "worked" only by accident --
            # nothing ever matched, so it degraded to the same ok:False below
            # but with a misleading "no vault files found" message).
            return {
                "ok": False, "src": "",
                "error": "restore_vault is not supported on POSIX -- restore "
                         "manually via `rclone copy nexus-unraid:<share>/<path> "
                         "<dest>` (see CLAUDE.md's backup section for the exact "
                         "remote layout)",
            }

        dest = pathlib.Path(dest_root)
        src_dir = dest / "history" / timestamp if timestamp else dest

        from backend.agents.backup import _db_path
        db_name = os.path.basename(_db_path())
        copied = []
        for name, local in [
            (VAULT_PATH.name, VAULT_PATH),
            (META_PATH.name, META_PATH),
            (KEY_PATH.name, KEY_PATH),
            (db_name, pathlib.Path(_db_path())),
        ]:
            src_file = src_dir / name
            if src_file.exists():
                shutil.copy2(src_file, local)
                copied.append(name)

        if not copied:
            return {"ok": False, "src": str(src_dir), "error": "no vault files found in backup"}

        logger.info("Vault restored from %s (%s)", src_dir, copied)
        return {"ok": True, "src": str(src_dir), "error": None}

    except Exception as e:
        logger.warning("Vault restore failed: %s", e)
        return {"ok": False, "src": "", "error": str(e)}
