"""Local backup agent for NEXUS durability pipeline.

Provides:
  - WAL checkpoint (hourly)
  - Daily backup of nexus.db + secrets into a gitignored backups/<timestamp>/
  - SQLite integrity check
  - Phone alert on any failure

All backups are LOCAL ONLY — no network, no Hermes relay, no SFTP.
The backups/ directory is gitignored so secrets are never committed.
"""
import asyncio
import logging
import os
import re
import shutil
from datetime import datetime

from sqlalchemy import text

logger = logging.getLogger(__name__)

# Regex that matches NEXUS's own backup timestamp directory names (YYYYMMDD-HHMMSS)
_BACKUP_DIR_RE = re.compile(r"^\d{8}-\d{6}$")


def _db_path() -> str:
    """Derive the SQLite file path from the engine URL.

    Falls back to 'nexus.db' in cwd if the engine URL doesn't yield a usable path.
    """
    try:
        from backend.database import engine
        db = engine.url.database  # e.g. "nexus.db" or an absolute path
        if db and db not in (":memory:", ""):
            return db
    except Exception as e:
        logger.debug(f"_db_path: could not read engine URL: {e}")
    return "nexus.db"


def checkpoint_db() -> None:
    """Run a WAL TRUNCATE checkpoint to flush the WAL into the main db file.

    Best-effort: logs on error, never raises.
    """
    try:
        from backend.database import engine
        with engine.connect() as conn:
            conn.execute(text("PRAGMA wal_checkpoint(TRUNCATE)"))
            conn.commit()
        logger.debug("WAL checkpoint completed")
    except Exception as e:
        logger.warning(f"checkpoint_db failed: {e}")


def integrity_check() -> str:
    """Run PRAGMA integrity_check and return the result string.

    Returns "ok" when the db is healthy.  Returns the error string on db
    corruption.  Returns the exception message if the PRAGMA itself fails.
    """
    try:
        from backend.database import engine
        with engine.connect() as conn:
            row = conn.execute(text("PRAGMA integrity_check")).fetchone()
        result = row[0] if row else "no result"
        return result
    except Exception as e:
        return str(e)


def make_backup() -> dict:
    """Checkpoint, copy db + secrets to a timestamped dir, run integrity check.

    Returns a dict:
      {"dir": str, "files": [str, ...], "integrity": str, "ok": bool}
    On exception:
      {"ok": False, "error": str}

    'ok' is True only when integrity == "ok" AND nexus.db was copied.
    """
    try:
        from backend.config import get_settings
        s = get_settings()
        backup_dir = getattr(s, "backup_dir", "backups")

        # Checkpoint WAL before snapshotting the file
        checkpoint_db()

        # Create timestamped subdirectory
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        dest = os.path.join(backup_dir, ts)
        os.makedirs(dest, exist_ok=True)

        # Files to back up — skip any that don't exist
        sources = [
            _db_path(),
            ".vault.key",
            "nexus.vault",
            "nexus.vault.meta",
        ]
        copied = []
        for src in sources:
            if os.path.exists(src):
                shutil.copy2(src, dest)
                copied.append(os.path.basename(src))
            else:
                logger.debug(f"make_backup: {src!r} not found, skipping")

        integrity = integrity_check()
        ok = integrity == "ok" and "nexus.db" in copied

        logger.info(
            f"Backup to {dest!r}: files={copied}, integrity={integrity!r}, ok={ok}"
        )
        return {"dir": dest, "files": copied, "integrity": integrity, "ok": ok}

    except Exception as e:
        logger.error(f"make_backup failed: {e}")
        return {"ok": False, "error": str(e)}


def prune_old_backups() -> int:
    """Delete stale NEXUS backup directories inside backup_dir.

    Only touches subdirectories whose name matches the timestamp pattern
    (YYYYMMDD-HHMMSS) AND whose mtime is older than backup_retention_days.
    Never touches:
      - backup_dir itself
      - Non-matching names (e.g. 'keepme', README, etc.)
      - Directories newer than the retention window

    Returns the count of directories pruned.
    """
    try:
        from backend.config import get_settings
        s = get_settings()
        backup_dir = getattr(s, "backup_dir", "backups")
        retention_days = int(getattr(s, "backup_retention_days", 7))

        if not os.path.isdir(backup_dir):
            return 0

        cutoff = datetime.now().timestamp() - (retention_days * 86400)
        pruned = 0

        for entry in os.scandir(backup_dir):
            if not entry.is_dir():
                continue
            if not _BACKUP_DIR_RE.match(entry.name):
                # Not a NEXUS backup dir — never touch it
                continue
            if entry.stat().st_mtime < cutoff:
                shutil.rmtree(entry.path, ignore_errors=True)
                logger.info(f"Pruned old backup: {entry.path!r}")
                pruned += 1

        return pruned

    except Exception as e:
        logger.warning(f"prune_old_backups failed: {e}")
        return 0


async def run_backup_job() -> dict:
    """Best-effort daily backup job.  NEVER raises.

    Returns {"skipped": True} when backup_enabled is False.
    Sends a phone alert on failure (integrity != "ok" or any copy error).
    """
    try:
        from backend.config import get_settings
        if not getattr(get_settings(), "backup_enabled", True):
            return {"skipped": True}

        result = await asyncio.to_thread(make_backup)
        await asyncio.to_thread(prune_old_backups)

        if not result.get("ok"):
            from backend import events
            msg = result.get("integrity") or result.get("error") or "unknown error"
            await events.notify_phone(
                f"NEXUS BACKUP FAILED: {msg}",
                kind="backup_failed",
            )

        return result

    except Exception as e:
        logger.error(f"run_backup_job unexpected error: {e}")
        return {"ok": False, "error": str(e)}


async def run_checkpoint_job() -> None:
    """Best-effort hourly WAL checkpoint job.  NEVER raises."""
    try:
        await asyncio.to_thread(checkpoint_db)
    except Exception as e:
        logger.error(f"run_checkpoint_job unexpected error: {e}")
