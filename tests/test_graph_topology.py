from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import event, text
from sqlmodel import SQLModel, create_engine

from backend.graph import store, topology, vault_index
from backend.graph.store import Node
from backend.integrations.proxmox import ProxmoxData

TOML = """
[[node]]
id = "host:pve"
type = "host"
name = "pve"
aliases = ["proxmox", "pve"]
attrs = { ip = "192.168.1.60" }

[[node]]
id = "host:unraid"
type = "host"
name = "Unraid"
aliases = ["unraid"]
attrs = { ip = "192.168.1.50" }

[[node]]
id = "service:immich"
type = "service"
name = "Immich"
aliases = ["immich", "photos"]

[[edge]]
src = "service:immich"
dst = "guest:209"
type = "runs_on"
"""


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


def manual(tmp_path, toml=TOML):
    p = tmp_path / "m.toml"
    p.write_text(toml)
    return topology.load_manual(p)


def vm(vmid, name, kind="lxc"):
    return {"vmid": vmid, "name": name, "status": "running", "type": kind, "node": "pve"}


def cfg(*volumes):
    return {"name": "x", "volumes": [{"volume": v} for v in volumes], "net": []}


PROX = ProxmoxData(node="pve", node_status="online", vms=[vm(209, "immich"), vm(101, "win", "qemu")])
CONFIGS = {209: cfg("VM:subvol-209-disk-0", "/mnt/unraid/immich"), 101: cfg("local-lvm:vm-101-disk-0")}
STORAGES = [
    {"storage": "VM", "type": "zfspool", "server": None, "export": None, "content": "rootdir", "disabled": False},
    {"storage": "local-lvm", "type": "lvmthin", "server": None, "export": None, "content": "", "disabled": False},
    {"storage": "VM_BU_NFS", "type": "nfs", "server": "192.168.1.50", "export": "/x", "content": "backup", "disabled": False},
    {"storage": "Odd", "type": "nfs", "server": "10.9.9.9", "export": None, "content": "", "disabled": True},
]
JOBS = [{"id": "j1", "storage": "VM_BU_NFS", "schedule": "21:00", "all": True, "vmids": [209, 101, 999]}]
CONTAINERS = [{"id": "c1", "name": "AdGuard-Home", "state": "RUNNING", "status": "Up"}]


def prox_with(*vms):
    return ProxmoxData(node="pve", node_status="online", vms=list(vms))


async def collect(tmp_path, *, prox=PROX, configs=CONFIGS, storages=STORAGES, jobs=JOBS, containers=CONTAINERS,
                  toml=TOML, flag=None):
    """One collect() run; a value that is an Exception instance makes that fetcher raise."""
    def mock(v):
        return AsyncMock(side_effect=v) if isinstance(v, Exception) else AsyncMock(return_value=v)

    flag = {} if flag is None else flag
    with ExitStack() as st:
        st.enter_context(patch("backend.integrations.proxmox.fetch", mock(prox)))
        st.enter_context(patch("backend.integrations.proxmox.fetch_guest_configs", mock(configs)))
        st.enter_context(patch("backend.integrations.proxmox.fetch_storages", mock(storages)))
        st.enter_context(patch("backend.integrations.proxmox.fetch_backup_jobs", mock(jobs)))
        st.enter_context(patch("backend.integrations.unraid.fetch_containers", mock(containers)))
        st.enter_context(patch("backend.scheduler._parse_uptime_targets", return_value=[]))
        st.enter_context(patch("backend.graph.topology.load_manual", return_value=manual(tmp_path, toml)))
        flag["rec"] = st.enter_context(patch("backend.agents.outcomes.record_flag_ex", new_callable=AsyncMock))
        flag["clr"] = st.enter_context(patch("backend.agents.outcomes.clear_flag", new_callable=AsyncMock))
        return await topology.collect()


def edge_set(source=None, type=None, status=None):
    with store._read() as c:
        rows = c.execute(text("SELECT src, dst, type, source, status FROM graphedge")).all()
    return {(r.src, r.dst, r.type) for r in rows
            if (source is None or r.source == source) and (type is None or r.type == type) and (status is None or r.status == status)}


def node_status(nid):
    return (store.get_node(nid) or {}).get("status")


# ---------------------------------------------------------------- pure

def test_storage_for_volume():
    s = {"VM", "Movies"}
    assert topology.storage_for_volume("VM:subvol-103-disk-0", s) == "VM"
    assert topology.storage_for_volume("/mnt/pve/Movies/x", s) == "Movies"
    assert topology.storage_for_volume("/mnt/unraid/immich", s) is None
    assert topology.storage_for_volume("other:vol", s) is None


@pytest.mark.parametrize("toml,frag", [
    ('[[node]]\nid="host:pve"\ntype="bogus"\nname="x"', "unknown type"),
    ('[[node]]\nid="guest:pve"\ntype="host"\nname="x"', "does not match"),
    (TOML + '[[node]]\nid="service:immich"\ntype="service"\nname="x"', "duplicate"),
    ('[[node]]\nid="host:unraid"\ntype="host"\nname="x"', "host:pve"),
    (TOML + "\n[backup]\nx = 1", "unknown top-level"),
])
def test_load_manual_rejects(tmp_path, toml, frag):
    with pytest.raises(ValueError, match=frag):
        manual(tmp_path, toml)


def test_shipped_toml_loads():
    nodes, edges = topology.load_manual()
    ids = {n.id for n in nodes}
    assert "host:pve" in ids and all(e.src.split(":")[0] in {"service", "guest"} for e in edges)
    assert all(e.type != "backs_up_to" for e in edges)


def test_build_expected(tmp_path):
    nodes, edges = topology.build(PROX, CONFIGS, STORAGES, JOBS, CONTAINERS, [("immich", "http://x", 200)], manual(tmp_path))
    ids = {n.id for n in nodes}
    assert {"guest:209", "guest:101", "storage:VM", "storage:Odd", "external:10.9.9.9", "container:AdGuard-Home",
            "endpoint:immich", "host:pve"} <= ids
    assert len([n for n in nodes if n.id == "host:pve"]) == 1
    got = {(e.src, e.dst, e.type, e.source) for e in edges}
    assert ("guest:209", "storage:VM", "depends_on", "proxmox_api") in got
    assert ("guest:209", "storage:VM_BU_NFS", "backs_up_to", "proxmox_api") in got
    assert ("guest:101", "storage:VM_BU_NFS", "backs_up_to", "proxmox_api") in got
    assert not any("999" in e.src for e in edges)  # job lists a guest that is not in this snapshot
    assert ("storage:VM_BU_NFS", "host:unraid", "depends_on", "proxmox_api") in got
    assert ("storage:Odd", "external:10.9.9.9", "depends_on", "proxmox_api") in got
    assert ("container:AdGuard-Home", "host:unraid", "runs_on", "unraid_api") in got
    assert ("endpoint:immich", "service:immich", "resolves_via", "uptime_targets") in got
    assert ("service:immich", "guest:209", "runs_on", "manual") in got
    assert not any(e.dst == "storage:Odd" and e.type == "depends_on" and e.src == "guest:209" for e in edges)
    storage = next(n for n in nodes if n.id == "storage:VM")
    assert "status" not in storage.attrs


def test_build_unavailable_and_empty(tmp_path):
    m = manual(tmp_path)
    nodes, edges = topology.build(None, None, None, None, [], None, m)
    assert {n.id for n in nodes} == {"host:pve", "host:unraid", "service:immich"}
    assert [e.type for e in edges] == ["runs_on"]
    _, e2 = topology.build(PROX, CONFIGS, STORAGES, [], CONTAINERS, [], m)
    assert not any(e.type == "backs_up_to" for e in e2)
    _, e3 = topology.build(PROX, CONFIGS, STORAGES, [{"id": "j", "storage": None, "vmids": [209]}], [], [], m)
    assert not any(e.type == "backs_up_to" for e in e3)


# ---------------------------------------------------------------- collect

async def test_collect_populates_and_keeps_host_identity(file_eng, tmp_path):
    store.upsert_nodes([Node("note:wiki/Immich", "note", "Immich", (), {"title": "Immich"}, "vault")], "2026-01-01T00:00:00")
    flag = {}
    r = await collect(tmp_path, flag=flag)
    assert set(r["sources"].values()) == {"ok"}
    assert [n["id"] for n in store.resolve_node("proxmox")] == ["host:pve"]
    pve = store.get_node("host:pve")
    assert pve["attrs"] == {"ip": "192.168.1.60", "status": "online"} and pve["source"] == "manual"
    assert edge_set(type="documents") == {("note:wiki/Immich", "service:immich", "documents"),
                                          ("note:wiki/Immich", "guest:209", "documents")}  # first collect on empty infra sees them
    assert len(edge_set(type="backs_up_to")) == 2
    flag["clr"].assert_awaited_with("graph", "stale_edges")
    await collect(tmp_path)
    assert len(edge_set(type="documents")) == 2
    j = store.jobs()
    assert j["collect"]["last_success"] and j["source:proxmox_api"]["generation"] == 2


async def test_outage_is_not_staleness(file_eng, tmp_path):
    await collect(tmp_path)
    gen = store.jobs()["source:proxmox_api"]["generation"]
    flag = {}
    for _ in range(3):
        r = await collect(tmp_path, prox=RuntimeError("pve down"), flag=flag)
        assert r["sources"]["proxmox_api"].startswith("skipped") and r["sources"]["unraid_api"] == "ok"
    j = store.jobs()["source:proxmox_api"]
    assert j["generation"] == gen and "pve down" in j["last_error"]
    assert node_status("guest:209") == "active" and store.stale_counts() == {"nodes": 0, "edges": 0}
    flag["rec"].assert_not_awaited()
    await collect(tmp_path)
    j = store.jobs()["source:proxmox_api"]
    assert j["generation"] == gen + 1 and j["last_error"] is None


async def test_disappearance_counts_snapshots(file_eng, tmp_path):
    await collect(tmp_path)
    only_a = prox_with(vm(209, "immich"))
    jobs_a = [{"id": "j1", "storage": "VM_BU_NFS", "vmids": [209]}]
    await collect(tmp_path, prox=only_a, jobs=jobs_a)  # miss #1
    assert node_status("guest:101") == "active" and ("guest:101", "storage:VM_BU_NFS", "backs_up_to") in edge_set(status="active")
    flag = {}
    await collect(tmp_path, prox=only_a, jobs=jobs_a, flag=flag)  # miss #2
    assert node_status("guest:101") == "stale"
    assert ("guest:101", "storage:VM_BU_NFS", "backs_up_to") in edge_set(status="stale")
    assert node_status("guest:209") == "active"
    flag["rec"].assert_awaited_once()
    assert flag["rec"].await_args.args[:2] == ("graph", "stale_edges") and flag["rec"].await_args.kwargs["severity"] == "low"
    await collect(tmp_path, flag=flag)
    assert node_status("guest:101") == "active" and store.stale_counts()["edges"] == 0
    flag["clr"].assert_awaited_with("graph", "stale_edges")


async def test_backup_job_gone_stales_edges_only(file_eng, tmp_path):
    await collect(tmp_path)
    await collect(tmp_path, jobs=[])
    await collect(tmp_path, jobs=[])
    assert len(edge_set(type="backs_up_to", status="stale")) == 2
    assert node_status("guest:209") == "active"
    await collect(tmp_path)
    assert edge_set(type="backs_up_to", status="stale") == set()


async def test_unraid_incomplete_vs_authoritative_empty(file_eng, tmp_path):
    await collect(tmp_path)
    for _ in range(2):
        r = await collect(tmp_path, containers=RuntimeError("incomplete docker section"))
        assert r["sources"]["unraid_api"].startswith("skipped")
    assert node_status("container:AdGuard-Home") == "active"
    await collect(tmp_path, containers=[])
    assert node_status("container:AdGuard-Home") == "active"
    await collect(tmp_path, containers=[])
    assert node_status("container:AdGuard-Home") == "stale"


async def test_manual_prune(file_eng, tmp_path):
    await collect(tmp_path)
    assert ("service:immich", "guest:209", "runs_on") in edge_set(source="manual")
    # a skipped source -> prune skipped
    no_edge = TOML.split("[[edge]]")[0]
    await collect(tmp_path, toml=no_edge, prox=RuntimeError("down"))
    assert ("service:immich", "guest:209", "runs_on") in edge_set(source="manual")
    await collect(tmp_path, toml=no_edge)
    assert edge_set(source="manual") == set() and store.get_node("service:immich")
    no_node = no_edge.replace('[[node]]\nid = "service:immich"\ntype = "service"\nname = "Immich"\naliases = ["immich", "photos"]\n', "")
    r = await collect(tmp_path, toml=no_node)
    assert r["pruned_manual"] == 1 and store.get_node("service:immich") is None


async def test_reconcile_same_second_and_frontmatter(file_eng):
    note = Node("note:wiki/Runbook", "note", "Runbook", (), {"title": "Runbook", "service": "photos"}, "vault")
    store.upsert_nodes([note, Node("service:immich", "service", "Immich", ("photos",), None, "manual"),
                        Node("guest:5", "guest", "stale", (), None, "proxmox_api")], "2026-01-01T00:00:00")
    with store.batch() as c:
        assert vault_index.reconcile_documents("2026-01-01T00:00:00", c) == 1
    first = edge_set(type="documents")
    assert first == {("note:wiki/Runbook", "service:immich", "documents")}
    store.upsert_nodes([Node(note.id, "note", "Runbook", (), {"title": "Runbook"}, "vault")], "2026-01-01T00:00:00", merge_attrs=False)
    with store.batch() as c:
        assert vault_index.reconcile_documents("2026-01-01T00:00:00", c) == 0  # same second still removes
    assert edge_set(type="documents") == set()


async def test_stale_guest_gets_no_documents_edge(file_eng):
    store.upsert_nodes([Node("note:wiki/Old", "note", "Old", (), {"title": "Old"}, "vault"),
                        Node("guest:5", "guest", "Old", ("Old",), None, "proxmox_api")], "2026-01-01T00:00:00")
    with store.batch() as c:
        c.execute(text("UPDATE graphnode SET status='stale' WHERE id='guest:5'"))
        assert vault_index.reconcile_documents("2026-01-01T00:00:00", c) == 0


async def test_collect_rollback_leaves_graph_untouched(file_eng, tmp_path, monkeypatch):
    store.upsert_nodes([Node("note:wiki/Immich", "note", "Immich", (), {"title": "Immich"}, "vault")], "2026-01-01T00:00:00")

    def boom(*a, **k):
        raise RuntimeError("write failed")

    monkeypatch.setattr(store, "upsert_edges", boom)
    with pytest.raises(RuntimeError, match="write failed"):
        await collect(tmp_path)
    assert store.list_nodes(types=topology.INFRA_TYPES) == []
    assert edge_set() == set()
    j = store.jobs()
    assert j["collect"]["last_error"] == "write failed"
    assert "source:proxmox_api" not in j
