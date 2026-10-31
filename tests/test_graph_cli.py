import json
import runpy
import sqlite3
import sys
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event, text
from sqlmodel import SQLModel, create_engine

from backend.graph import store
from backend.graph.store import Edge, Node


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


@pytest.mark.parametrize("unresolved,mark", [(1, "✓"), (9, "✗")])  # 10 resolved: 91% vs 53%
def test_doctor_links_resolved_threshold(file_eng, capsys, unresolved, mark):
    store.upsert_nodes([Node("note:a", "note", "a", attrs={"unresolved": unresolved})], _ago(0))
    store.upsert_nodes([Node(f"note:t{i}", "note", f"t{i}") for i in range(10)], _ago(0))
    store.upsert_edges([Edge("note:a", f"note:t{i}", "links_to", "wikilink") for i in range(10)], _ago(0))
    run("doctor")
    assert f"{mark} links resolved" in capsys.readouterr().out


def test_drop_unconfirmed_leaves_schema_alone(file_eng):
    store.drop_tables()
    assert run("drop") == 2
    with file_eng.connect() as c:
        assert c.execute(text("SELECT name FROM sqlite_master WHERE name LIKE 'graph%'")).first() is None


def _seed_fresh(generation=1):
    for n in ("vault_index", "collect", "source:proxmox_api", "source:unraid_api", "source:uptime_targets"):
        store.record_job(n, ok=True, stats={}, at=_ago(1))
    with store.batch() as c:
        c.execute(text("UPDATE graphjob SET generation = :g WHERE name LIKE 'source:%'"), {"g": generation})


def test_doctor_topology_lines_never_ran(file_eng, capsys):
    run("doctor")
    out = capsys.readouterr().out
    assert "✗ topology fresh: collect never ran" in out
    assert "✗ sources: proxmox_api never ran · unraid_api never ran · uptime_targets never ran" in out
    assert "✓ dangling edges: 0" in out


def test_doctor_exit_zero(file_eng, capsys, monkeypatch):
    import backend.config as config_mod

    monkeypatch.setenv("GRAPH_ENABLED", "true")
    monkeypatch.setattr(config_mod, "_settings_instance", None)
    _seed_fresh()
    store.upsert_nodes([Node("note:a", "note", "a"), Node("note:b", "note", "b")], _ago(0))
    store.upsert_edges([Edge("note:a", "note:b", "links_to", "wikilink")], _ago(0))
    assert run("doctor") == 0
    out = capsys.readouterr().out
    assert "✗" not in out and "✓ sources: proxmox_api ok" in out
    monkeypatch.setattr(config_mod, "_settings_instance", None)


def test_doctor_flags_error_stale_and_dangling(file_eng, capsys):
    _seed_fresh()
    store.record_job("source:proxmox_api", ok=False, error="pve down", at=_ago(0))
    store.upsert_nodes([Node("guest:1", "guest", "g", source="proxmox_api")], _ago(0))
    store.upsert_edges([Edge("guest:1", "host:pve", "runs_on", "proxmox_api")], _ago(0))
    with store.batch() as c:
        c.execute(text("UPDATE graphnode SET status='stale'"))
    run("doctor")
    out = capsys.readouterr().out
    assert "✗ sources: proxmox_api error: pve down" in out
    assert "✗ stale: nodes=1 edges=0" in out
    assert "✗ dangling edges: 1 (e.g. guest:1 -runs_on-> host:pve)" in out


def _patch_fetchers(proxmox_error=None):
    from contextlib import ExitStack
    from unittest.mock import AsyncMock, patch

    from backend.integrations.proxmox import ProxmoxData

    st = ExitStack()
    prox = AsyncMock(side_effect=proxmox_error) if proxmox_error else AsyncMock(return_value=ProxmoxData(node="pve", node_status="online"))
    st.enter_context(patch("backend.integrations.proxmox.fetch", prox))
    st.enter_context(patch("backend.integrations.proxmox.fetch_guest_configs", AsyncMock(return_value={})))
    st.enter_context(patch("backend.integrations.proxmox.fetch_storages", AsyncMock(return_value=[{"storage": "local"}])))
    st.enter_context(patch("backend.integrations.proxmox.fetch_backup_jobs", AsyncMock(return_value=[])))
    st.enter_context(patch("backend.integrations.unraid.fetch_containers", AsyncMock(return_value=[])))
    st.enter_context(patch("backend.scheduler._parse_uptime_targets", return_value=[]))
    st.enter_context(patch("backend.agents.outcomes.clear_flag", AsyncMock()))
    return st


def test_collect_stdout_json_and_exit(file_eng, capsys):
    with _patch_fetchers():
        assert run("collect") == 0
    body = json.loads(capsys.readouterr().out)
    assert set(body["sources"].values()) == {"ok"}


def test_collect_skipped_source_exits_1(file_eng, capsys):
    with _patch_fetchers(RuntimeError("pve down")):
        assert run("collect") == 1
    body = json.loads(capsys.readouterr().out)
    assert body["sources"]["proxmox_api"].startswith("skipped:")


def test_rebuild_clears_and_repopulates(file_eng, capsys, tmp_path, monkeypatch):
    brain = tmp_path / "Brain"
    brain.mkdir()
    (brain / "A.md").write_text("hi")
    monkeypatch.setattr("backend.api.vault._brain_root", lambda: brain)
    store.upsert_nodes([Node("note:old", "note", "old")], _ago(0))
    with _patch_fetchers():
        assert run("rebuild") == 0
    body = json.loads(capsys.readouterr().out)
    assert body["index"]["scanned"] == 1 and "collect" in body
    assert store.get_node("note:old") is None and store.get_node("note:A")


def test_audit_unavailable_exits_1(file_eng, capsys):
    from unittest.mock import AsyncMock, patch

    store.upsert_nodes([Node("guest:5", "guest", "g", source="proxmox_api"), Node("host:pve", "host", "pve")], _ago(0))
    store.upsert_edges([Edge("guest:5", "host:pve", "runs_on", "proxmox_api")], _ago(0))
    boom = AsyncMock(side_effect=RuntimeError("pve down"))
    with patch("backend.integrations.proxmox.fetch", boom):
        assert run("audit") == 1
    assert "? guest:5 runs_on host:pve (source unavailable: pve down)" in capsys.readouterr().out


def test_audit_confirms_live(file_eng, capsys):
    from unittest.mock import AsyncMock, patch

    from backend.integrations.proxmox import ProxmoxData

    store.upsert_nodes([Node("guest:5", "guest", "g", source="proxmox_api"), Node("host:pve", "host", "pve")], _ago(0))
    store.upsert_edges([Edge("guest:5", "host:pve", "runs_on", "proxmox_api")], _ago(0))
    ok = AsyncMock(return_value=ProxmoxData(vms=[{"vmid": 5}]))
    with patch("backend.integrations.proxmox.fetch", ok):
        assert run("audit") == 0
    assert "\u2713 guest:5 runs_on host:pve" in capsys.readouterr().out
    with patch("backend.integrations.proxmox.fetch", AsyncMock(return_value=ProxmoxData(vms=[]))):
        assert run("audit") == 1


def test_audit_empty_graph_no_network(file_eng, capsys):
    from unittest.mock import AsyncMock, patch

    m = AsyncMock()
    with patch("backend.integrations.proxmox.fetch", m), patch("backend.integrations.unraid.fetch_containers", m):
        assert run("audit") == 0
        run("doctor")
    assert not m.called
    assert "\u2713 audit 0/0" in capsys.readouterr().out
