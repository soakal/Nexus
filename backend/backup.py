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


def _rclone_sync(local_dir: pathlib.Path, unc_path: str) -> None:
    """Best-effort: mirror local_dir to the Unraid share via the pre-configured
    `nexus-unraid` rclone remote (rclone config create, one-time host setup --
    not done here). Never raises; a sync failure leaves the local staging
    copy intact for the next scheduled run to retry."""
    import subprocess

    try:
        share, subpath = _smb_share_and_subpath(unc_path)
        remote = f"nexus-unraid:{share}/{subpath}"
        result = subprocess.run(
            ["rclone", "sync", str(local_dir), remote, "--fast-list"],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode != 0:
            logger.warning("rclone sync to %s failed: %s", remote, result.stderr[:500])
    except Exception as e:
        logger.warning("rclone sync attempt failed (non-fatal): %s", e)


def _mount_unc(unc_path: str, settings) -> None:
    """Best-effort: use PowerShell's New-SmbMapping to authenticate the UNC
    share before copying.

    Credential lookup order:
      1. Vault keys UNRAID_BACKUP_USER / UNRAID_BACKUP_PASSWORD (explicit override)
      2. cred:unraid:user / cred:unraid:password (from Credentials & Passwords section)
    Silently skips if no credentials found or PowerShell is unavailable.

    The password is NEVER placed on the child process's argv (a plaintext
    credential there is visible, for the process's whole lifetime, to any
    co-resident process/user that can enumerate command lines). Instead the
    entire mapping script -- including the credentials -- is piped over the
    child's STDIN to `powershell -NoProfile -NonInteractive -Command -`.
    """
    pw = ""
    try:
        user = getattr(settings, "unraid_backup_user", "").strip()
        pw = getattr(settings, "unraid_backup_password", "").strip()

        # Fall back to the general credential store under service "unraid" (case-insensitive)
        if not user or not pw:
            try:
                from backend.secrets.manager import get_credential, list_credentials
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

        import base64
        import subprocess

        def _b64(s: str) -> str:
            # Pure-ASCII by construction (no quote char in the alphabet), so
            # this also makes script injection impossible without a separate
            # escaping helper.
            return base64.b64encode(s.encode("utf-8")).decode("ascii")

        share_b64 = _b64(share)
        pw_b64 = _b64(pw)

        # The ENTIRE script is built as ONE semicolon-joined line -- a
        # multi-line script piped to `-Command -` can silently truncate
        # execution after the first network call with no error. Credentials
        # are base64 on the Python side and decoded INSIDE PowerShell so the
        # piped text is pure ASCII: Windows PowerShell 5.1 decodes redirected
        # stdin using the console's OEM code page (not UTF-8), which would
        # otherwise silently corrupt a non-ASCII credential embedded directly
        # in the script. The catch block avoids Write-Error, which renders a
        # full ErrorRecord that echoes the invoking script line (credentials
        # included) into stderr -- [Console]::Error.WriteLine prints only the
        # .NET exception's own message text.
        stmts = [
            f"$s=[System.Text.Encoding]::UTF8.GetString([System.Convert]::FromBase64String('{share_b64}'))",
            f"$p=[System.Text.Encoding]::UTF8.GetString([System.Convert]::FromBase64String('{pw_b64}'))",
        ]
        if user:
            user_b64 = _b64(user)
            stmts.append(
                f"$u=[System.Text.Encoding]::UTF8.GetString([System.Convert]::FromBase64String('{user_b64}'))"
            )
            stmts.append(
                "try { New-SmbMapping -RemotePath $s -UserName $u -Password $p "
                "-Persistent $false -ErrorAction Stop | Out-Null } "
                "catch { [Console]::Error.WriteLine($_.Exception.Message); exit 1 }"
            )
        else:
            stmts.append(
                "try { New-SmbMapping -RemotePath $s -Password $p "
                "-Persistent $false -ErrorAction Stop | Out-Null } "
                "catch { [Console]::Error.WriteLine($_.Exception.Message); exit 1 }"
            )
        script = ";".join(stmts)

        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", "-"],
            input=script.encode("ascii"),
            capture_output=True,
            timeout=20,
        )
        if result.returncode != 0:
            out = result.stdout.decode(errors="replace").replace(pw, "[REDACTED]")
            err = result.stderr.decode(errors="replace").replace(pw, "[REDACTED]")
            logger.debug(
                "SMB mount returned %d: stdout=%s stderr=%s", result.returncode, out, err
            )
    except Exception as e:
        msg = str(e)
        if pw:
            msg = msg.replace(pw, "[REDACTED]")
        logger.debug("SMB mount attempt: %s", msg)


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

        if is_unc and os.name == "nt":
            dest = pathlib.Path(dest_root)
            # Mount it via net use first. No-op if the share is already
            # accessible (guest/already-mapped).
            _mount_unc(dest_root, s)
        elif is_unc:
            # POSIX: no kernel mount available in this environment (see
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

        # Strip Hidden attribute from backup copies so they're visible in Explorer.
        # Exception: the vault key. setup.ps1 deliberately marks the LOCAL
        # .vault.key Hidden (attrib +H) as an extra obscurity layer -- clearing
        # it here would make the remote copy MORE discoverable than the local
        # one, for no functional reason (nothing depends on it being visible).
        if os.name == "nt":
            import stat as _stat
            for p in copied_paths:
                try:
                    p.chmod(p.stat().st_mode | _stat.S_IRUSR | _stat.S_IWUSR)
                    if p.name == KEY_PATH.name:
                        continue
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

        if is_unc and os.name != "nt":
            _rclone_sync(dest, dest_root)

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

        share, subpath = _smb_share_and_subpath(dest_root)
        remote = f"nexus-unraid:{share}/{subpath}/knowledge"

        import subprocess
        result = subprocess.run(
            ["rclone", "sync", vault_path, remote, "--fast-list"],
            capture_output=True, text=True, timeout=300,
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
