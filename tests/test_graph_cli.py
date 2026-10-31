import json
import runpy
import sqlite3
import sys
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event, text
from sqlmodel import SQLModel, create_engine

from backend.graph import store


@pytest.fixture
def file_eng(tmp_path, monkeypatch):
    import backend.database as bd

    db_path = tmp_path / "nexus.db"
    monkeypatch.setattr(bd, "DB_PATH", db_path)
    e = create_engine(f"sqlite:///{db_path}", echo=False, connect_args={"check_same_thread": False, "timeout": 30})
    event.listen(e, "connect", bd._set_sqlite_pragmas)
    monkeypatch.setattr(bd, "engine", e)
    SQLModel.metadata.create_all(e)
    monkeypatch.setenv("NEXUS_GRAPH_MIN_DB_BYTES", "0")
    return e


def run(*args):
    sys.argv = ["backend.graph", *args]
    with pytest.raises(SystemExit) as ei:
        runpy.run_module("backend.graph", run_name="__main__")
    return ei.value.code


def _ago(minutes: int) -> str:
    return (datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=minutes)).replace(microsecond=0).isoformat()


def test_health_stdout_is_json_only(file_eng, capsys):
    assert run("health") == 0
    out, err = capsys.readouterr()
    body = json.loads(out)
    assert {"nodes", "edges", "jobs"} <= body.keys()
    assert "db=" in err and "size=" in err


def test_index_stdout_is_json(file_eng, capsys, tmp_path, monkeypatch):
    brain = tmp_path / "Brain"
    brain.mkdir()
    (brain / "A.md").write_text("[[B]]")
    (brain / "B.md").write_text("hi")
    monkeypatch.setattr("backend.api.vault._brain_root", lambda: brain)
    assert run("index", "--full") == 0
    assert json.loads(capsys.readouterr().out)["scanned"] == 2


def test_drop_requires_yes(file_eng):
    assert run("drop") == 2
    with file_eng.connect() as c:
        c.execute(text("SELECT COUNT(*) FROM graphnode"))
    assert run("drop", "--yes") == 0
    with file_eng.connect() as c:
        assert c.execute(text("SELECT name FROM sqlite_master WHERE name='graphnode'")).first() is None


def test_guard_small_db_untouched(file_eng, monkeypatch):
    import backend.database as bd

    monkeypatch.delenv("NEXUS_GRAPH_MIN_DB_BYTES")
    before = bd.DB_PATH.read_bytes()
    assert run("health") == 2
    assert bd.DB_PATH.read_bytes() == before


def test_guard_no_outcomeflag_untouched(tmp_path, monkeypatch):
    import backend.database as bd

    p = tmp_path / "other.db"
    con = sqlite3.connect(p)
    con.execute("CREATE TABLE task (id INTEGER, pad TEXT)")
    con.execute("INSERT INTO task VALUES (1, ?)", ("x" * 1_200_000,))
    con.commit()
    con.close()
    monkeypatch.setattr(bd, "DB_PATH", p)
    before = p.read_bytes()
    assert run("health") == 2
    assert p.read_bytes() == before
    con = sqlite3.connect(p)
    assert con.execute("SELECT 1 FROM sqlite_master WHERE name='outcomeflag'").fetchone() is None
    con.close()


def test_guard_missing_file(tmp_path, monkeypatch):
    import backend.database as bd

    p = tmp_path / "nope.db"
    monkeypatch.setattr(bd, "DB_PATH", p)
    assert run("health") == 2
    assert not p.exists()


def test_doctor_empty_graph(file_eng, capsys):
    assert run("doctor") == 1
    out, err = capsys.readouterr()
    assert "✗ graph_enabled=false" in out
    assert "✓ tables present" in out
    assert "✗ vault index fresh: vault_index never ran" in out
    assert "✓ links resolved n/a" in out
    assert "Traceback" not in err


@pytest.mark.parametrize("age,mark", [(31, "✗"), (1, "✓")])
def test_doctor_freshness(file_eng, capsys, age, mark):
    store.record_job("vault_index", ok=True, stats={}, at=_ago(age))
    run("doctor")
    assert f"{mark} vault index fresh" in capsys.readouterr().out
