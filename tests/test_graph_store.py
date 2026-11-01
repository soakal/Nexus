import pytest
from sqlalchemy import event, text
from sqlalchemy.exc import IntegrityError
from sqlmodel import SQLModel, create_engine
from sqlmodel.pool import StaticPool

from backend.graph import INFRA_TYPES, store
from backend.graph.store import Edge, Node

T = "2026-01-01T00:00:00"
T2 = "2026-01-02T00:00:00"


@pytest.fixture
def eng(monkeypatch):
    import backend.database  # noqa: F401 - register models

    e = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(e)
    monkeypatch.setattr("backend.database.engine", e)
    return e


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


def N(i, t="guest", **kw):
    return Node(i, t, kw.pop("name", i), **kw)


def E(s, d, t="runs_on", src="proxmox_api"):
    return Edge(s, d, t, src)


def test_tables_and_pk(eng):
    with eng.connect() as c:
        names = {r[0] for r in c.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))}
        assert {"graphnode", "graphedge", "graphchange", "graphjob"} <= names
        pk = [r for r in c.execute(text("PRAGMA table_info(graphedge)")) if r[5] > 0]
        assert len(pk) == 4


def test_upsert_nodes_merge_and_first_seen(eng):
    store.upsert_nodes([N("guest:1", attrs={"ip": "x", "owner": "manual"}, source="manual")], T)
    store.upsert_nodes([N("guest:1", attrs={"status": "up", "ip": "y"}, source="proxmox_api")], T2, gen=3)
    n = store.get_node("guest:1")
    assert n["attrs"] == {"ip": "y", "owner": "manual", "status": "up"}
    assert n["source"] == "manual"
    with eng.connect() as c:
        r = c.execute(text("SELECT first_seen, last_seen, gen FROM graphnode")).one()
    assert tuple(r) == (T, T2, 3)


def test_attrs_null_cases(eng):
    store.upsert_nodes([N("a:1", attrs=None)], T)
    store.upsert_nodes([N("a:1", attrs={"a": 1})], T)
    assert store.get_node("a:1")["attrs"] == {"a": 1}
    store.upsert_nodes([N("a:1", attrs=None)], T)
    assert store.get_node("a:1")["attrs"] == {"a": 1}


def test_replace_attrs(eng):
    store.upsert_nodes([N("note:x", "note", attrs={"a": 1, "service": "x"})], T, merge_attrs=False)
    store.upsert_nodes([N("note:x", "note", attrs={"a": 2})], T2, merge_attrs=False)
    assert store.get_node("note:x")["attrs"] == {"a": 2}


def test_check_rejects_bogus_type(eng):
    with pytest.raises(IntegrityError):
        store.upsert_nodes([N("x:1", "bogus")], T)


def test_traversal_cycle_depth_stale(eng):
    store.upsert_edges([E("a", "b"), E("b", "c"), E("c", "a"), E("c", "d", "depends_on")], T)
    anc = dict(store.ancestors("a", 3))
    assert anc == {"b": 1, "c": 2, "d": 3}
    assert dict(store.ancestors("a", 1)) == {"b": 1}
    assert dict(store.descendants("a", 3)) == {"c": 1, "b": 2}
    with eng.begin() as c:
        c.execute(text("UPDATE graphedge SET status='stale' WHERE src='b'"))
    assert dict(store.ancestors("a", 3)) == {"b": 1}


def test_neighbors_and_edges_of(eng):
    store.upsert_nodes([N(f"note:{i}", "note") for i in range(60)], T)
    store.upsert_edges([Edge("note:0", f"note:{i}", "links_to", "wikilink") for i in range(1, 60)], T)
    assert len(store.neighbors("note:0")) == 50
    both = store.neighbors("note:5")
    assert [(x["id"], x["direction"]) for x in both] == [("note:0", "in")]
    assert {x["id"] for x in store.neighbors("note:5", depth=2, limit=100)} == {f"note:{i}" for i in range(60)} - {"note:5"}
    store.upsert_edges([E("note:5", "note:0", "mentions", "manual")], T)
    ed = store.edges_of("note:5")
    assert [(e["type"], e["direction"], e["other"], e["source"], e["status"]) for e in ed] == [
        ("links_to", "in", "note:0", "wikilink", "active"),
        ("mentions", "out", "note:0", "manual", "active"),
    ]


def test_generation_and_mark_stale(eng):
    with store.batch() as c:
        assert [store.bump_generation("proxmox_api", c) for _ in range(3)] == [1, 2, 3]
    assert store.jobs()["source:proxmox_api"]["generation"] == 3
    with pytest.raises(RuntimeError):
        with store.batch() as c:
            store.bump_generation("proxmox_api", c)
            raise RuntimeError
    assert store.jobs()["source:proxmox_api"]["generation"] == 3
    store.upsert_nodes([N("g:1", source="proxmox_api")], T, gen=2)
    store.upsert_nodes([N("g:2", source="proxmox_api")], T, gen=3)
    store.upsert_nodes([N("g:3", source="manual")], T)
    store.upsert_nodes([N("g:4", source="unraid_api")], T, gen=1)
    store.upsert_nodes([N("g:5", source="proxmox_api")], T, gen=None)  # NULL gen never flips
    store.upsert_edges([E("g:1", "g:2")], T, gen=2)
    assert store.mark_stale("proxmox_api", 2) == (1, 1)
    assert {n["id"]: n["status"] for n in store.list_nodes()} == {
        "g:1": "stale", "g:2": "active", "g:3": "active", "g:4": "active", "g:5": "active"}
    assert store.stale_counts() == {"nodes": 1, "edges": 1}


def test_deletes(eng):
    store.upsert_nodes([N("a"), N("b"), N("tag:foo", "tag"), N("tag:bar", "tag")], T)
    store.upsert_edges([E("a", "b"), Edge("a", "tag:foo", "tagged", "frontmatter"),
                        Edge("a", "b", "links_to", "wikilink"), E("b", "a", "depends_on", "manual"),
                        E("a", "b", "depends_on", "manual")], T)
    assert store.delete_edges(source="manual", not_in={("a", "b", "depends_on")}) == 1
    assert [e["type"] for e in store.edges_of("a") if e["source"] == "manual"] == ["depends_on"]
    assert store.delete_edges("a", source=("wikilink", "frontmatter")) == 2
    store.upsert_nodes([N("tag:x", "tag")], T)
    store.upsert_edges([Edge("b", "tag:x", "tagged", "frontmatter")], T)
    assert store.delete_orphan_tags() == 2  # tag:foo (edge removed) and tag:bar; tag:x kept
    assert {n["id"] for n in store.list_nodes(types=["tag"])} == {"tag:x"}
    assert store.delete_nodes(["a"]) == 1
    assert [e["other"] for e in store.edges_of("b")] == ["tag:x"]


def test_record_job(eng):
    store.record_job("collect", ok=True, stats={"n": 1}, at=T)
    store.record_job("collect", ok=False, error="boom", at=T2)
    j = store.jobs()["collect"]
    assert (j["last_success"], j["last_error"], j["stats"]) == (T, "boom", {"n": 1})
    store.record_job("collect", ok=True, at=T2)
    assert store.jobs()["collect"]["last_error"] is None
    with store.batch() as c:
        store.bump_generation("x", c)
    store.record_job("source:x", ok=True, at=T)
    assert store.jobs()["source:x"]["generation"] == 1


def test_list_nodes_sees_caller_transaction(file_eng):
    with store.batch() as conn:
        store.upsert_nodes([N("guest:9", source="proxmox_api")], T, conn)
        assert [n["id"] for n in store.list_nodes(types=INFRA_TYPES, conn=conn)] == ["guest:9"]
        assert store.list_nodes(types=INFRA_TYPES) == []
    assert len(store.list_nodes(types=INFRA_TYPES)) == 1


def test_list_nodes_filters_and(eng):
    store.upsert_nodes([N("a", source="manual"), N("b", source="manual"), N("c", source="vault")], T)
    with eng.begin() as c:
        c.execute(text("UPDATE graphnode SET status='stale' WHERE id='b'"))
    assert [n["id"] for n in store.list_nodes(source="manual", status="active")] == ["a"]


def test_batch_rollback(file_eng):
    with pytest.raises(RuntimeError):
        with store.batch() as c:
            store.upsert_nodes([N("a")], T, c)
            raise RuntimeError
    assert store.list_nodes() == []


def test_resolve_node(eng):
    store.upsert_nodes([N("guest:209", name="immich", aliases=("immich", "CT 209", "209")),
                        N("service:immich", "service", name="Immich", aliases=("immich",))], T)
    assert [n["id"] for n in store.resolve_node("guest:209")] == ["guest:209"]
    assert [n["id"] for n in store.resolve_node("immich")] == ["guest:209", "service:immich"]
    assert [n["id"] for n in store.resolve_node("ct 209")] == ["guest:209"]
    assert [n["id"] for n in store.resolve_node("VM 209")] == ["guest:209"]
    assert store.resolve_node("nope") == []


def test_documents_for(eng):
    store.upsert_edges([Edge("note:b", "service:x", "documents", "vault"), Edge("note:a", "service:x", "documents", "vault")], T)
    assert store.documents_for(["service:x", "service:y"]) == {"service:x": ["note:a", "note:b"]}


def test_health_and_drop(eng):
    store.upsert_nodes([N("note:a", "note", attrs={"unresolved": 2, "ambiguous": 1}, source="vault")], T)
    store.upsert_edges([E("note:a", "note:gone", "links_to", "wikilink")], T)
    store.record_job("vault_index", ok=True, stats={"skipped_oversize": 1, "unreadable": 0}, at=T)
    h = store.health()
    assert h["nodes"] == {"note": 1} and h["edges"] == {"links_to": 1}
    assert h["vault"] == {"notes": 1, "unresolved_links": 2, "ambiguous_stems": 1, "skipped_oversize": 1, "unreadable": 0}
    assert h["dangling_edges"] == 1 and h["sources"]["vault"]["nodes"] == 1
    store.drop_all()
    assert store.health()["nodes"] == {} and "vault_index" in store.jobs()
    store.drop_tables()
    assert store.health()["tables_missing"] is True
    assert store.jobs() == {}


def test_upsert_none_attr_deletes_key_and_is_never_stored(eng):
    store.upsert_nodes([Node("s:1", "storage", "s", attrs={"server": "x", "export": "/e", "x": None})], "2026-01-01T00:00:00")
    assert store.get_node("s:1")["attrs"] == {"export": "/e", "server": "x"}
    store.upsert_nodes([Node("s:1", "storage", "s", attrs={"owner": "manual"})], "2026-01-01T00:00:01")
    store.upsert_nodes([Node("s:1", "storage", "s", attrs={"server": "x", "export": None})], "2026-01-01T00:00:02")
    assert store.get_node("s:1")["attrs"] == {"owner": "manual", "server": "x"}
