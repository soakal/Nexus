"""Vault backup to Unraid SMB share.

Copies nexus.vault + nexus.vault.meta (+ .vault.key if configured) plus a
consistent nexus.db snapshot to the UNC path in settings.unraid_backup_path.
Keeps a dated history/ subfolder capped at 14 copies. Never raises — backup
failures must never block a secret save or crash the scheduler.

Restore path (manual):
  1. Stop NEXUS (stop.ps1)
  2. Copy nexus.vault (and .vault.key if backed up) AND nexus.db from the
     share to the project root, overwriting the current files. Delete any
     stale nexus.db-wal / nexus.db-shm sidecars.
  3. Start NEXUS (start.ps1)
  To restore a specific point-in-time: copy from history/<timestamp>/ instead.
"""
import logging
import os
import pathlib
import shutil
from datetime import datetime

logger = logging.getLogger(__name__)

_HISTORY_KEEP = 14  # max dated copies retained in history/


def _mount_unc(unc_path: str, settings) -> None:
    """Best-effort: use 'net use' to authenticate the UNC share before copying.

    Credential lookup order:
      1. Vault keys UNRAID_BACKUP_USER / UNRAID_BACKUP_PASSWORD (explicit override)
      2. cred:unraid:user / cred:unraid:password (from Credentials & Passwords section)
    Silently skips if no credentials found or net use is unavailable.
    """
    try:
        user = getattr(settings, "unraid_backup_user", "").strip()
        pw = getattr(settings, "unraid_backup_password", "").strip()

        # Fall back to the general credential store under service "unraid" (case-insensitive)
        if not user or not pw:
            try:
                from backend.secrets.vault import get_credential, list_credentials
                creds_map = list_credentials()
                # find service key case-insensitively
                svc_key = next((k for k in creds_map if k.lower() == "unraid"), None)
                if svc_key:
                    cred = get_credential(svc_key)
                    if not user:
                        user = (cred.get("user") or "").strip()
                    if not pw:
                        pw = (cred.get("password") or "").strip()
            except Exception:
                pass

        if not pw:
            return  # nothing to authenticate with

        parts = unc_path.lstrip("\\").split("\\")
        if len(parts) < 2:
            return
        share = f"\\\\{parts[0]}\\{parts[1]}"
        import subprocess
        cmd = ["net", "use", share, pw]
        if user:
            cmd += [f"/user:{user}"]
        cmd.append("/persistent:no")
        result = subprocess.run(cmd, capture_output=True, timeout=10)
        if result.returncode != 0:
            logger.debug("net use returned %d: %s", result.returncode, result.stderr.decode(errors="replace"))
    except Exception as e:
        logger.debug("net use mount attempt: %s", e)


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

        dest = pathlib.Path(dest_root)

        # If it's a UNC path and credentials are configured, mount it via net use first.
        # This is a no-op if the share is already accessible (guest/already-mapped).
        if dest_root.startswith("\\\\"):
            _mount_unc(dest_root, s)

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
        copied_paths = []
        for src, name in files:
            dst = dest / name
            shutil.copy2(src, dst)
            copied_paths.append(dst)

        # Dated history copy
        hist_dir = history / ts
        hist_dir.mkdir(parents=True, exist_ok=True)
        for src, name in files:
            dst = hist_dir / name
            shutil.copy2(src, dst)
            copied_paths.append(dst)

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
                        copied_paths.append(target)
                else:
                    logger.warning("db snapshot failed integrity check; not shipped to Unraid")
            finally:
                if os.path.exists(tmp_db):
                    os.remove(tmp_db)
        except Exception as e:
            logger.warning("db snapshot for Unraid backup failed (non-fatal): %s", e)

        # Strip Hidden attribute from backup copies so they're visible in Explorer
        if os.name == "nt":
            import stat as _stat
            for p in copied_paths:
                try:
                    p.chmod(p.stat().st_mode | _stat.S_IRUSR | _stat.S_IWUSR)
                    # Clear Hidden via ctypes FILE_ATTRIBUTE_HIDDEN (0x2)
                    import ctypes
                    attrs = ctypes.windll.kernel32.GetFileAttributesW(str(p))
                    if attrs != -1 and (attrs & 0x2):
                        ctypes.windll.kernel32.SetFileAttributesW(str(p), attrs & ~0x2)
                except Exception:
                    pass

        # Prune history to _HISTORY_KEEP most recent entries
        entries = sorted(history.iterdir(), key=lambda p: p.name)
        for old in entries[:-_HISTORY_KEEP]:
            try:
                shutil.rmtree(old)
            except Exception:
                pass

        logger.info("Vault backed up to %s (%s)", dest, ts)
        return {"ok": True, "dest": str(dest), "error": None}

    except Exception as e:
        logger.warning("Vault backup failed (non-fatal): %s", e)
        return {"ok": False, "dest": "", "error": str(e)}


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
