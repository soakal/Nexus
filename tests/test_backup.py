"""Tests for backend/agents/backup.py — local durability pipeline.

All tests use tmp_path so no real secrets or db files are touched.
A monkeypatched engine + settings keep everything hermetic.
"""
import asyncio
import os
import time
import pathlib
import pytest
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy import text
from sqlmodel import create_engine


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_tmp_engine(db_path: pathlib.Path):
    """Create a real file-based SQLite engine in WAL mode for a tmp db."""
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )
    # Ensure WAL mode so checkpoint has a real WAL to truncate
    with engine.connect() as conn:
        conn.execute(text("PRAGMA journal_mode=WAL"))
        conn.execute(text("CREATE TABLE IF NOT EXISTS _test (id INTEGER PRIMARY KEY)"))
        conn.commit()
    return engine


def _fake_settings(backup_dir: str, backup_enabled: bool = True, retention_days: int = 7):
    s = MagicMock()
    s.backup_enabled = backup_enabled
    s.backup_dir = backup_dir
    s.backup_retention_days = retention_days
    s.backup_time = "03:30"
    s.phone_notifications_enabled = True
    return s


# ---------------------------------------------------------------------------
# 1. checkpoint_db — runs without error on a real WAL db
# ---------------------------------------------------------------------------

def test_checkpoint_db_succeeds(tmp_path, monkeypatch):
    db_file = tmp_path / "nexus.db"
    tmp_engine = _make_tmp_engine(db_file)

    # Monkeypatch backend.database.engine so _db_path and checkpoint_db use our tmp engine
    monkeypatch.setattr("backend.database.engine", tmp_engine)

    from backend.agents.backup import checkpoint_db
    # Should not raise — TRUNCATE checkpoint on a WAL db is idempotent
    checkpoint_db()


# ---------------------------------------------------------------------------
# 2. integrity_check — returns "ok" on a healthy db
# ---------------------------------------------------------------------------

def test_integrity_check_healthy(tmp_path, monkeypatch):
    db_file = tmp_path / "nexus.db"
    tmp_engine = _make_tmp_engine(db_file)
    monkeypatch.setattr("backend.database.engine", tmp_engine)

    from backend.agents.backup import integrity_check
    result = integrity_check()
    assert result == "ok"


# ---------------------------------------------------------------------------
# 3. make_backup — creates timestamped dir with db copy; returns ok=True
# ---------------------------------------------------------------------------

def test_make_backup_creates_dir_and_copies_files(tmp_path, monkeypatch):
    # Set up real tmp db + fake secret files
    db_file = tmp_path / "nexus.db"
    tmp_engine = _make_tmp_engine(db_file)
    monkeypatch.setattr("backend.database.engine", tmp_engine)

    # Fake secrets in cwd (monkeypatched to tmp_path)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".vault.key").write_bytes(b"fakekey" * 8)
    (tmp_path / "nexus.vault").write_text('{"encrypted": "data"}')
    (tmp_path / "nexus.vault.meta").write_text('{"names": []}')

    backup_dir = str(tmp_path / "backups")
    fake_s = _fake_settings(backup_dir)
    monkeypatch.setattr("backend.config.get_settings", lambda: fake_s)
    # Also patch inside backup module
    monkeypatch.setattr("backend.agents.backup._db_path", lambda: str(db_file))

    from backend.agents.backup import make_backup
    result = make_backup()

    assert result["ok"] is True, f"Expected ok=True, got: {result}"
    assert result["integrity"] == "ok"
    assert "nexus.db" in result["files"]
    assert ".vault.key" in result["files"]
    assert "nexus.vault" in result["files"]
    assert "nexus.vault.meta" in result["files"]

    # Verify the timestamped directory was created
    dest = result["dir"]
    assert os.path.isdir(dest)
    assert os.path.exists(os.path.join(dest, "nexus.db"))


# ---------------------------------------------------------------------------
# 4. prune_old_backups — deletes only old timestamp-named dirs
# ---------------------------------------------------------------------------

def test_prune_old_backups_precision(tmp_path, monkeypatch):
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()

    # A "fresh" backup — mtime now (should be kept)
    fresh_dir = backup_dir / "20990101-000000"
    fresh_dir.mkdir()

    # An "old" backup — mtime set 10 days ago (should be pruned)
    old_dir = backup_dir / "20200101-000000"
    old_dir.mkdir()
    old_time = time.time() - (10 * 86400)
    os.utime(old_dir, (old_time, old_time))

    # A non-matching name — should never be touched regardless of age
    keepme = backup_dir / "keepme"
    keepme.mkdir()
    os.utime(keepme, (old_time, old_time))

    fake_s = _fake_settings(str(backup_dir), retention_days=7)
    monkeypatch.setattr("backend.config.get_settings", lambda: fake_s)

    from backend.agents.backup import prune_old_backups
    count = prune_old_backups()

    assert count == 1
    assert fresh_dir.exists(), "Fresh backup should NOT be pruned"
    assert not old_dir.exists(), "Old backup SHOULD be pruned"
    assert keepme.exists(), "Non-matching dir must NEVER be touched"


# ---------------------------------------------------------------------------
# 5. run_backup_job — forced failure alerts via notify_phone
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_backup_job_failure_calls_notify_phone(monkeypatch):
    fake_s = _fake_settings("backups", backup_enabled=True)
    monkeypatch.setattr("backend.config.get_settings", lambda: fake_s)

    # Force make_backup to return failure
    monkeypatch.setattr(
        "backend.agents.backup.make_backup",
        lambda: {"ok": False, "integrity": "corruption detected"},
    )
    monkeypatch.setattr(
        "backend.agents.backup.prune_old_backups",
        lambda: 0,
    )

    mock_notify = AsyncMock(return_value=True)
    monkeypatch.setattr("backend.events.notify_phone", mock_notify)

    from backend.agents.backup import run_backup_job
    result = await run_backup_job()

    assert result.get("ok") is False
    mock_notify.assert_awaited_once()
    call_kwargs = mock_notify.call_args
    assert call_kwargs.kwargs.get("kind") == "backup_failed"
    assert "NEXUS BACKUP FAILED" in call_kwargs.args[0]


# ---------------------------------------------------------------------------
# 6. backup_enabled=False → run_backup_job skips, returns {"skipped": True}
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_backup_job_disabled(monkeypatch, tmp_path):
    fake_s = _fake_settings(str(tmp_path / "backups"), backup_enabled=False)
    monkeypatch.setattr("backend.config.get_settings", lambda: fake_s)

    from backend.agents.backup import run_backup_job
    result = await run_backup_job()

    assert result == {"skipped": True}
    # No backup dir created
    assert not (tmp_path / "backups").exists()


# ---------------------------------------------------------------------------
# 7. Scheduler registers db_checkpoint + db_backup when backup_enabled=True
#    (also see test_coverage_boost.py test_setup_scheduler_adds_jobs)
# ---------------------------------------------------------------------------

def test_scheduler_backup_jobs_registered_when_enabled(monkeypatch):
    """When backup_enabled=True the scheduler adds db_checkpoint + db_backup."""
    from backend.scheduler import setup_scheduler, scheduler

    with patch.object(scheduler, "add_job") as mock_add:
        setup_scheduler("07:30", "America/New_York")

    ids = {c.kwargs.get("id") for c in mock_add.call_args_list}
    assert "db_checkpoint" in ids
    assert "db_backup" in ids


def test_scheduler_backup_jobs_absent_when_disabled(monkeypatch):
    """When backup_enabled=False no backup jobs are added."""
    from backend.scheduler import setup_scheduler, scheduler

    # Override backup_enabled to False on the settings object; the scheduler
    # calls get_settings() from backend.config at job-registration time.
    mock_s = MagicMock()
    mock_s.backup_enabled = False
    # Keep other feature flags off so only the 5 unconditional jobs are added
    mock_s.step_watchdog_enabled = False
    mock_s.proposer_enabled = False
    mock_s.autonomy_digest_enabled = False
    mock_s.watchdog_enabled = False
    mock_s.spend_report_enabled = False

    with patch("backend.config.get_settings", return_value=mock_s), \
         patch.object(scheduler, "add_job") as mock_add:
        setup_scheduler("07:30", "America/New_York")

    ids = {c.kwargs.get("id") for c in mock_add.call_args_list}
    assert "db_checkpoint" not in ids
    assert "db_backup" not in ids


# ---------------------------------------------------------------------------
# 8. .gitignore contains backups/
# ---------------------------------------------------------------------------

def test_gitignore_contains_backups_entry():
    """Verify backups/ is in .gitignore so secrets never get committed."""
    gitignore = pathlib.Path(__file__).parent.parent / ".gitignore"
    assert gitignore.exists(), ".gitignore not found at repo root"
    content = gitignore.read_text(encoding="utf-8")
    # Must have backups/ as a standalone entry (not buried inside another word)
    lines = [line.strip() for line in content.splitlines()]
    assert "backups/" in lines, f"backups/ not found in .gitignore lines: {lines}"
