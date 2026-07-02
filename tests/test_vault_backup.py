"""Tests for backend/backup.py (vault→Unraid) + the scheduler failure alert.

Tier A3.2/A3.3: the off-VM bundle must include a restorable nexus.db and a
failed off-VM backup must page the phone.
"""
import os
import pathlib
import sqlite3
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy import text
from sqlmodel import create_engine


def _make_tmp_engine(db_path: pathlib.Path):
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )
    with engine.connect() as conn:
        conn.execute(text("PRAGMA journal_mode=WAL"))
        conn.execute(text("CREATE TABLE IF NOT EXISTS _test (id INTEGER PRIMARY KEY)"))
        conn.execute(text("INSERT INTO _test (id) VALUES (42)"))
        conn.commit()
    return engine


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Tmp project root with vault files, tmp engine, tmp Unraid 'share'."""
    db_file = tmp_path / "nexus.db"
    engine = _make_tmp_engine(db_file)
    monkeypatch.setattr("backend.database.engine", engine)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("backend.agents.backup._db_path", lambda: str(db_file))

    vault = tmp_path / "nexus.vault"
    meta = tmp_path / "nexus.vault.meta"
    key = tmp_path / ".vault.key"
    vault.write_text('{"encrypted": "data"}')
    meta.write_text('{"names": []}')
    key.write_bytes(b"k" * 32)

    share = tmp_path / "share"

    s = MagicMock()
    s.unraid_backup_path = str(share)
    s.unraid_backup_include_key = True
    s.unraid_backup_user = ""
    s.unraid_backup_password = ""
    monkeypatch.setattr("backend.config.get_settings", lambda: s)

    monkeypatch.setattr("backend.secrets.vault.VAULT_PATH", vault)
    monkeypatch.setattr("backend.secrets.vault.META_PATH", meta)
    monkeypatch.setattr("backend.secrets.vault.KEY_PATH", key)

    return {"tmp": tmp_path, "share": share, "db": db_file,
            "vault": vault, "meta": meta, "key": key}


def test_backup_vault_includes_db(env):
    from backend.backup import backup_vault
    result = backup_vault()
    assert result["ok"] is True

    share = env["share"]
    assert (share / "nexus.db").exists(), "db snapshot missing from share root"
    hist = list((share / "history").iterdir())
    assert len(hist) == 1
    assert (hist[0] / "nexus.db").exists(), "db snapshot missing from history"

    # The shipped snapshot must be a real, consistent database
    con = sqlite3.connect(str(share / "nexus.db"))
    try:
        assert con.execute("SELECT id FROM _test").fetchone() == (42,)
        assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        con.close()


def test_backup_vault_db_snapshot_failure_is_nonfatal(env, monkeypatch):
    """A db-snapshot failure must not break the vault half of the backup."""
    def _boom(_dest):
        raise RuntimeError("disk on fire")
    monkeypatch.setattr("backend.agents.backup.snapshot_db_to", _boom)

    from backend.backup import backup_vault
    result = backup_vault()
    assert result["ok"] is True
    assert (env["share"] / "nexus.vault").exists()
    assert not (env["share"] / "nexus.db").exists()


def test_restore_vault_restores_db(env):
    from backend.backup import backup_vault, restore_vault
    assert backup_vault()["ok"] is True

    # Wipe the local files, then restore from the share. Dispose the engine
    # first — on Windows an open handle blocks unlink.
    import backend.database as dbmod
    dbmod.engine.dispose()
    env["db"].unlink()
    env["vault"].unlink()
    result = restore_vault()
    assert result["ok"] is True
    assert env["db"].exists(), "nexus.db not restored"
    assert env["vault"].exists(), "nexus.vault not restored"


@pytest.mark.asyncio
async def test_vault_backup_failure_notifies_phone(monkeypatch):
    """scheduler._vault_backup must page the phone when backup_vault fails."""
    notify = AsyncMock()
    monkeypatch.setattr(
        "backend.backup.backup_vault",
        lambda: {"ok": False, "dest": "", "error": "share unreachable"},
    )
    with patch("backend.events.notify_phone", new=notify):
        from backend.scheduler import _vault_backup
        await _vault_backup()

    notify.assert_awaited_once()
    args, kwargs = notify.await_args
    assert "BACKUP FAILED" in args[0]
    assert kwargs.get("kind") == "backup_failed"
