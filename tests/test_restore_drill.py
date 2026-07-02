"""Tier B8 — the restore drill: a backup that has never been restored is a hope."""
import os
import pathlib
import sqlite3
import pytest
from unittest.mock import MagicMock

from sqlalchemy import text
from sqlmodel import create_engine


def _make_db(db_path: pathlib.Path):
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    with engine.connect() as conn:
        conn.execute(text("PRAGMA journal_mode=WAL"))
        conn.execute(text("CREATE TABLE IF NOT EXISTS _drill (id INTEGER PRIMARY KEY, v TEXT)"))
        conn.execute(text("INSERT INTO _drill (id, v) VALUES (1, 'precious'), (2, 'data')"))
        conn.commit()
    return engine


@pytest.fixture
def env(tmp_path, monkeypatch):
    db_file = tmp_path / "nexus.db"
    engine = _make_db(db_file)
    monkeypatch.setattr("backend.database.engine", engine)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("backend.agents.backup._db_path", lambda: str(db_file))
    s = MagicMock()
    s.backup_enabled = True
    s.backup_dir = str(tmp_path / "backups")
    s.backup_retention_days = 7
    monkeypatch.setattr("backend.config.get_settings", lambda: s)
    return {"db": db_file, "engine": engine, "tmp": tmp_path}


def test_restore_drill_round_trips_rows(env):
    """backup -> disaster -> restore -> rows back."""
    from backend.agents.backup import make_backup, restore_from

    result = make_backup()
    assert result["ok"] is True

    # Disaster: live db destroyed (dispose engine first — Windows file lock).
    env["engine"].dispose()
    env["db"].write_bytes(b"corrupted garbage")

    restored = restore_from(result["dir"])
    assert restored["ok"] is True, restored

    con = sqlite3.connect(str(env["db"]))
    try:
        rows = con.execute("SELECT id, v FROM _drill ORDER BY id").fetchall()
        assert rows == [(1, "precious"), (2, "data")]
        assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        con.close()


def test_restore_from_deletes_stale_wal(env):
    from backend.agents.backup import make_backup, restore_from

    result = make_backup()
    env["engine"].dispose()
    wal = pathlib.Path(str(env["db"]) + "-wal")
    shm = pathlib.Path(str(env["db"]) + "-shm")
    wal.write_bytes(b"stale wal")
    shm.write_bytes(b"stale shm")

    assert restore_from(result["dir"])["ok"] is True
    assert not wal.exists()
    assert not shm.exists()


def test_restore_from_missing_backup_returns_not_ok(env, tmp_path):
    from backend.agents.backup import restore_from
    empty = tmp_path / "empty-dir"
    empty.mkdir()
    result = restore_from(str(empty))
    assert result["ok"] is False
    assert "no nexus.db" in result["error"]
    # live db untouched
    assert env["db"].exists()


def test_restore_from_refuses_corrupt_backup(env, tmp_path):
    from backend.agents.backup import restore_from
    bad_dir = tmp_path / "bad-backup"
    bad_dir.mkdir()
    (bad_dir / "nexus.db").write_bytes(b"not a database" * 50)
    result = restore_from(str(bad_dir))
    assert result["ok"] is False
    assert "integrity" in result["error"]
