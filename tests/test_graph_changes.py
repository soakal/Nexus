"""G3a change recording (plan g3/plan-v2 §4): to_utc_iso, the source-scoped diff, store helpers, collect + vault wiring.

Fixtures use example names and RFC 5737 addresses only (public repo).
"""
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from sqlalchemy import event, text
from sqlmodel import SQLModel, create_engine
from sqlmodel.pool import StaticPool
from test_graph_topology import CONFIGS, CONTAINERS, JOBS, TOML, cfg, collect, prox_with, vm
from test_graph_vault_index import bump, put

from backend.graph import changes, store, to_utc_iso
from backend.graph.store import Edge, Node
from backend.graph.vault_index import refresh

T = "2026-01-01T00:00:00"


@pytest.fixture
def file_eng(tmp_path, monkeypatch):
    import backend.database as bd

    db_path = tmp_path / "nexus.db"
    monkeypatch.setattr(bd, "DB_PATH", db_path)
    e = create_engine(f"sqlite:///{db_path}", echo=False, connect_args={"check_same_thread": False, "timeout": 30})
    event.listen(e, "connect", bd._set_sqlite_pragmas)
    monkeypatch.setattr(bd, "engine", e)
    SQLModel.metadata.create_all(e)
    return e


@pytest.fixture
def eng(monkeypatch):
    import backend.database  # noqa: F401 - register models

    e = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(e)
    monkeypatch.setattr("backend.database.engine", e)
    return e


def rows(where: str = "", **p) -> list[tuple]:
    with store._read() as c:
        return [tuple(r) for r in c.execute(
            text("SELECT node_id, field, old, new, source FROM graphchange" + (f" WHERE {where}" if where else "") + " ORDER BY id"), p)]


# ---------------------------------------------------------------- to_utc_iso

def test_to_utc_iso():
    assert to_utc_iso("2026-11-01T10:00:00+02:00") == "2026-11-01T08:00:00"
    assert to_utc_iso("2026-11-01T10:00:00Z") == "2026-11-01T10:00:00"
    assert to_utc_iso("2026-11-01T10:00:00.123456") == "2026-11-01T10:00:00"  # naive = already UTC
    assert to_utc_iso(datetime(2026, 11, 1, 5, tzinfo=UTC) + timedelta(hours=1)) == "2026-11-01T06:00:00"
    got = datetime.fromisoformat(to_utc_iso("24h"))
    want = datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=24)
    assert abs((got - want).total_seconds()) < 5
    assert to_utc_iso(" 7d ") < to_utc_iso("30m")
    for bad in ("yesterday", "24x", "", "2026-13-01", "999999999999999999999999h", "0001-01-01T00:00:00+01:00"):
        with pytest.raises(ValueError):
            to_utc_iso(bad)


# ---------------------------------------------------------------- pure diff

def test_diff_nodes_tracked_fields_only():
    old = {"status": "running", "cores": 2, "tags": ["b", "a"], "name": "lab-vm",
           "volumes": [{"key": "rootfs", "volume": "VM:disk-0"}], "net": [{"key": "net0", "ip": "198.51.100.5/24"}]}
    same = {**old, "tags": ["a", "b"], "name": "renamed", "last_seen": "x"}  # order + untracked keys ignored
    assert changes.diff_nodes("guest", old, same) == []
    new = {**old, "status": "stopped", "cores": 4,
           "volumes": old["volumes"] + [{"key": "mp0", "volume": "Media:sub-1"}],
           "net": [{"key": "net0", "ip": "198.51.100.6/24"}]}
    got = {f: (o, n) for f, o, n in changes.diff_nodes("guest", old, new)}
    assert set(got) == {"status", "cores", "volumes", "net.ip"}
    assert got["status"] == ("running", "stopped") and got["cores"] == ("2", "4")
    assert got["net.ip"] == ('["198.51.100.5/24"]', '["198.51.100.6/24"]')
    assert changes.diff_nodes("endpoint", {"url": "a"}, {"url": "b"}) == []  # untracked type
    assert changes.diff_nodes("storage", {"disabled": False}, {"disabled": True}) == [("disabled", "false", "true")]


def test_diff_source_scoping_and_empty_before():
    n = [Node("guest:1", "guest", "a", attrs={"status": "running"}, source="proxmox_api")]
    assert changes.diff_source(source="proxmox_api", ts=T, new_nodes=n, new_edges=[], stored_nodes=[], stored_edges={}) == []
    stored = [{"id": "guest:1", "type": "guest", "attrs": {"status": "running", "cores": 2}, "status": "active"}]
    # merge semantics: a key the source no longer sends is kept; an explicit None deletes it
    assert changes.diff_source(source="proxmox_api", ts=T, new_nodes=n, new_edges=[], stored_nodes=stored, stored_edges={}) == []
    gone = [Node("guest:1", "guest", "a", attrs={"status": "running", "cores": None}, source="proxmox_api")]
    r = changes.diff_source(source="proxmox_api", ts=T, new_nodes=gone, new_edges=[], stored_nodes=stored, stored_edges={})
    assert [(x["field"], x["old"], x["new"]) for x in r] == [("cores", "2", None)]


def test_duplicate_edges_one_row():
    e = Edge("guest:1", "storage:BU", "backs_up_to", "proxmox_api")  # two backup jobs, same guest + storage
    stored = [{"id": "guest:1", "type": "guest", "attrs": {}, "status": "active"}]
    r = changes.diff_source(source="proxmox_api", ts=T, new_nodes=[], new_edges=[e, e], stored_nodes=stored, stored_edges={})
    assert [x["field"] for x in r] == ["edge:backs_up_to:storage:BU"]
    r = changes.diff_source(source="proxmox_api", ts=T, new_nodes=[], new_edges=[e, e], stored_nodes=stored,
                            stored_edges={("guest:1", "storage:BU", "backs_up_to"): "stale"})
    assert [(x["old"], x["new"]) for x in r] == [("stale", "active")]


def test_record_truncates(eng):
    many = [{"ts": T, "node_id": f"guest:{i}", "field": "status", "old": "a", "new": "b", "source": "proxmox_api"}
            for i in range(changes.MAX_PER_RUN + 7)]
    with store.batch() as c:
        assert changes.record(c, T, many) == changes.MAX_PER_RUN + 1
    assert rows("field = 'truncated'") == [("graph:changes", "truncated", None, "7", "graph")]


# ---------------------------------------------------------------- store

def test_store_change_helpers(eng):
    store.upsert_edges([Edge("a", "b", "runs_on", "proxmox_api"), Edge("a", "c", "runs_on", "unraid_api")], T)
    assert store.edge_keys(source="proxmox_api") == {("a", "b", "runs_on"): "active"}
    for i, ts in enumerate(("2026-01-01T00:00:00", "2026-01-02T00:00:00", "2026-01-03T00:00:00")):
        store.insert_change(ts, "guest:1" if i < 2 else "guest:2", "status", "x", str(i), "proxmox_api")
    got = store.changes_for(["guest:1", "guest:2", "guest:1"], since="2026-01-01T00:00:00")
    assert [r["new"] for r in got] == ["2", "1", "0"]  # newest first, duplicate id harmless
    assert [r["new"] for r in store.changes_for(["guest:1", "guest:2"], since=T, before="2026-01-03T00:00:00")] == ["1", "0"]
    assert len(store.changes_for(["guest:1"], since=T, limit=1)) == 1
    assert store.changes_for([], since=T) == []
    assert store.health()["changes"] == {"rows": 3, "oldest": "2026-01-01T00:00:00"}
    assert store.prune_changes("2026-01-02T00:00:00") == 1
    assert store.health()["changes"]["rows"] == 2


# ---------------------------------------------------------------- collect wiring

async def test_collect_records_real_changes_only(file_eng, tmp_path):
    await collect(tmp_path)
    assert rows() == []  # first run on an empty graph: nothing
    await collect(tmp_path)
    assert rows() == []  # nothing changed across any source

    stopped = prox_with(dict(vm(209, "immich"), status="stopped"), vm(101, "win", "qemu"))
    r = await collect(tmp_path, prox=stopped)
    assert rows() == [("guest:209", "status", "running", "stopped", "proxmox_api")]
    assert store.jobs()["collect"]["stats"]["changes"] == 1 and "nodes" in r


async def test_failed_source_writes_nothing_for_it(file_eng, tmp_path):
    await collect(tmp_path)
    moved = [dict(CONTAINERS[0], state="EXITED")]
    await collect(tmp_path, prox=RuntimeError("pve down"), containers=moved)
    assert rows() == [("container:AdGuard-Home", "state", "RUNNING", "EXITED", "unraid_api")]


async def test_disappear_only_after_two_cycles_then_reappear(file_eng, tmp_path):
    await collect(tmp_path)
    only_101 = prox_with(vm(101, "win", "qemu"))
    await collect(tmp_path, prox=only_101)
    assert rows() == []  # absent once: still active in the store, nothing written
    await collect(tmp_path, prox=only_101)
    assert rows("node_id = 'guest:209' AND field = 'status'") == [("guest:209", "status", "active", "stale", "proxmox_api")]
    assert {f for _, f, *_ in rows("node_id = 'guest:209' AND field LIKE 'edge:%'")} == {
        "edge:runs_on:host:pve", "edge:depends_on:storage:VM", "edge:backs_up_to:storage:VM_BU_NFS"}
    n = len(rows())
    await collect(tmp_path)
    back = rows("id > :n", n=n)
    assert ("guest:209", "status", "stale", "active", "proxmox_api") in back
    assert ("guest:209", "edge:runs_on:host:pve", "stale", "active", "proxmox_api") in back


async def test_backup_edge_goes_stale_once(file_eng, tmp_path):
    await collect(tmp_path)
    jobs = [dict(JOBS[0], vmids=[101])]
    await collect(tmp_path, jobs=jobs)
    await collect(tmp_path, jobs=jobs)
    assert rows() == [("guest:209", "edge:backs_up_to:storage:VM_BU_NFS", "active", "stale", "proxmox_api")]
    await collect(tmp_path, jobs=jobs)
    assert len(rows()) == 1  # already stale: mark_stale returns nothing new


async def test_new_guest_and_edge_appear(file_eng, tmp_path):
    await collect(tmp_path)
    more = prox_with(vm(209, "immich"), vm(101, "win", "qemu"), vm(150, "lab-vm"))
    await collect(tmp_path, prox=more, configs={**CONFIGS, 150: cfg("VM:subvol-150-disk-0")})
    got = rows()
    assert ("guest:150", "node", None, "guest", "proxmox_api") in got
    assert ("guest:150", "edge:depends_on:storage:VM", None, "active", "proxmox_api") in got


async def test_nexus_sha_recorded_on_deploy(file_eng, tmp_path):
    toml = TOML + '\n[[node]]\nid = "service:nexus"\ntype = "service"\nname = "NEXUS"\n'
    with patch("backend.version.running_sha", return_value="a" * 40):
        await collect(tmp_path, toml=toml)
        await collect(tmp_path, toml=toml)
    assert rows() == []
    with patch("backend.version.running_sha", return_value="b" * 40):
        await collect(tmp_path, toml=toml, prox=RuntimeError("pve down"))  # still caught on a partial tick
    assert rows() == [("service:nexus", "sha", "a" * 40, "b" * 40, "manual")]
    with patch("backend.version.running_sha", return_value=None):
        await collect(tmp_path, toml=toml)
    assert len(rows()) == 1  # unknown sha keeps the stored one, no row


# ---------------------------------------------------------------- vault wiring

def test_vault_note_changes(eng, tmp_path, monkeypatch):
    root = tmp_path / "Brain"
    root.mkdir()
    monkeypatch.setattr("backend.api.vault._brain_root", lambda: root)
    a = put(root, "wiki/Alpha.md", "---\ntitle: Alpha\nstatus: draft\n---\nbody\n")
    put(root, "wiki/Beta.md", "beta\n")
    refresh()
    assert rows() == []  # first index
    a.write_text("---\ntitle: Alpha\nstatus: draft\n---\nedited body only\n")
    bump(a)
    assert refresh()["changes"] == 0
    a.write_text("---\ntitle: Alpha Two\nstatus: done\n---\nedited body only\n")
    bump(a)
    refresh()
    assert rows() == [("note:wiki/Alpha", "title", "Alpha", "Alpha Two", "vault"),
                      ("note:wiki/Alpha", "status", "draft", "done", "vault")]
    (root / "wiki/Beta.md").unlink()
    put(root, "wiki/Gamma.md", "gamma\n")
    refresh()
    assert rows("id > 2") == [("note:wiki/Gamma", "node", None, "note", "vault"),
                              ("note:wiki/Beta", "node", "note", None, "vault")]


async def test_cap_is_per_run_not_per_source(file_eng, tmp_path):
    vms = [vm(5000 + i, f"g{i}") for i in range(300)]
    ctrs = [{"id": f"c{i}", "name": f"demo-{i}", "state": "RUNNING", "status": "Up"} for i in range(300)]
    await collect(tmp_path, prox=prox_with(*vms), configs={}, containers=ctrs, jobs=[])
    await collect(tmp_path, prox=prox_with(*[dict(v, status="stopped") for v in vms]), configs={},
                  containers=[dict(c, state="EXITED") for c in ctrs], jobs=[])
    assert len(rows()) == changes.MAX_PER_RUN + 1
    assert rows("field = 'truncated'") == [("graph:changes", "truncated", None, "100", "graph")]


async def test_manual_removal_recorded_when_pruned(file_eng, tmp_path):
    extra = '\n[[edge]]\nsrc = "service:immich"\ndst = "host:unraid"\ntype = "depends_on"\n'
    await collect(tmp_path, toml=TOML + extra)
    await collect(tmp_path, toml=TOML + extra)
    assert rows() == []
    await collect(tmp_path, prox=RuntimeError("pve down"))  # edge removed from TOML, but prune is skipped
    assert rows() == []
    await collect(tmp_path)
    assert rows() == [("service:immich", "edge:depends_on:host:unraid", "active", None, "manual")]
    lean = TOML.replace('[[node]]\nid = "service:immich"\ntype = "service"\nname = "Immich"\naliases = ["immich", "photos"]\n', "")
    lean = lean[: lean.index("[[edge]]")]
    await collect(tmp_path, toml=lean)
    assert ("service:immich", "node", "service", None, "manual") in rows()
    assert ("service:immich", "edge:runs_on:guest:209", "active", None, "manual") in rows()


async def _collect_at(tmp_path, when: str, **kw):
    with patch("backend.graph.topology.now_iso", return_value=when):
        return await collect(tmp_path, **kw)


async def test_stale_rows_deleted_after_seven_days(file_eng, tmp_path):
    only_101 = prox_with(vm(101, "win", "qemu"))
    await _collect_at(tmp_path, "2026-11-01T00:00:00")
    await _collect_at(tmp_path, "2026-11-01T01:00:00", prox=only_101)
    await _collect_at(tmp_path, "2026-11-01T02:00:00", prox=only_101)  # guest:209 goes stale (last seen 11-01 00:00)
    assert store.get_node("guest:209")["status"] == "stale"
    flag = {}
    r = await _collect_at(tmp_path, "2026-11-07T23:00:00", prox=only_101, flag=flag)  # 6d23h: kept
    assert r["purged_stale"] == 0 and store.get_node("guest:209") is not None
    r = await _collect_at(tmp_path, "2026-11-08T00:00:01", prox=only_101, flag=flag)
    assert store.get_node("guest:209") is None
    assert not [e for e in store.edges_of("guest:209") if e["source"] == "proxmox_api"]
    assert r["stale"] == {"nodes": 0, "edges": 0} and r["purged_stale"] == 4  # node + runs_on/depends_on/backs_up_to
    flag["clr"].assert_awaited_with("graph", "stale_edges")  # the open stale-edges flag closes itself
    removed = rows("ts = '2026-11-08T00:00:01'")
    assert ("guest:209", "node", "guest", None, "proxmox_api") in removed
    assert ("guest:209", "edge:runs_on:host:pve", "stale", None, "proxmox_api") in removed
    assert store.get_node("guest:101")["status"] == "active"  # untouched


async def test_outage_never_purges(file_eng, tmp_path):
    await _collect_at(tmp_path, "2026-11-01T00:00:00")
    for day in range(2, 20):  # proxmox down for weeks: nothing goes stale, so nothing is deleted
        r = await _collect_at(tmp_path, f"2026-11-{day:02d}T00:00:00", prox=RuntimeError("pve down"))
        assert r["purged_stale"] == 0
    assert store.get_node("guest:209")["status"] == "active"


async def test_stale_guest_that_returns_is_not_purged(file_eng, tmp_path):
    only_101 = prox_with(vm(101, "win", "qemu"))
    await _collect_at(tmp_path, "2026-11-01T00:00:00")
    await _collect_at(tmp_path, "2026-11-01T01:00:00", prox=only_101)
    await _collect_at(tmp_path, "2026-11-01T02:00:00", prox=only_101)
    await _collect_at(tmp_path, "2026-11-05T00:00:00")  # back before the 7 days are up
    r = await _collect_at(tmp_path, "2026-11-20T00:00:00")
    assert r["purged_stale"] == 0 and store.get_node("guest:209")["status"] == "active"
