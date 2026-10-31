import os
import time
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlmodel import SQLModel, create_engine
from sqlmodel.pool import StaticPool

from backend.graph import store
from backend.graph.vault_index import Resolver, parse_frontmatter, parse_tags, refresh


@pytest.fixture
def eng(monkeypatch):
    import backend.database  # noqa: F401 - register models

    e = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(e)
    monkeypatch.setattr("backend.database.engine", e)
    return e


@pytest.fixture
def brain(tmp_path, monkeypatch):
    root = tmp_path / "Brain"
    root.mkdir()
    monkeypatch.setattr("backend.api.vault._brain_root", lambda: root)
    return root


def put(root: Path, rel: str, body: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    return p


def bump(p: Path) -> None:
    st = p.stat()
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))


HUB = (
    "---\ntitle: Hub Page\ntags: [Infra, homelab]\nservice: immich\nhost: pve\nvmid: 109\n---\n"
    "# Heading\nSee [[Target]] and [[Target#Heading]] and [[Target|label]], [[Dup]], [[Alias Name]],\n"
    "[[Hub Page]], [[Missing]], ![[img.png]] #inline-tag https://x.test/a#frag\n"
)


@pytest.fixture
def vault(brain):
    put(brain, "wiki/Hub.md", HUB)
    put(brain, "wiki/Target.md", "target body\n")
    put(brain, "wiki/Aliased.md", "---\naliases:\n  - Alias Name\n---\nbody\n")
    put(brain, "a/Dup.md", "dup a\n")
    put(brain, "b/Dup.md", "dup b\n")
    put(brain, "backups/Skip.md", "[[Target]]\n")
    put(brain, "big.md", "x" * 201_000)
    return brain


def edges(eng, type_=None):
    q = "SELECT src, dst, type, source FROM graphedge" + (" WHERE type = :t" if type_ else "") + " ORDER BY 1, 2, 3"
    with eng.connect() as c:
        return [tuple(r) for r in c.execute(text(q), {"t": type_} if type_ else {})]


def test_first_index_counts(eng, vault):
    r = refresh()
    assert (r["scanned"], r["changed"], r["links"], r["unresolved"], r["ambiguous"]) == (5, 5, 3, 1, 1)
    assert r["skipped_oversize"] == 1 and r["unreadable"] == 0 and r["removed"] == 0
    assert edges(eng, "links_to") == [
        ("note:wiki/Hub", "note:a/Dup", "links_to", "wikilink"),  # equal path length -> lexicographic winner
        ("note:wiki/Hub", "note:wiki/Aliased", "links_to", "wikilink"),
        ("note:wiki/Hub", "note:wiki/Target", "links_to", "wikilink"),
    ]
    assert [e[1] for e in edges(eng, "tagged")] == ["tag:homelab", "tag:infra", "tag:inline-tag"]
    assert not edges(eng, "documents")
    a = store.get_node("note:wiki/Hub")["attrs"]
    assert a["service"] == "immich" and a["host"] == "pve" and a["vmid"] == "109"
    assert (a["unresolved"], a["ambiguous"]) == (1, 1)
    assert store.get_node("note:backups/Skip") is None and store.get_node("note:big") is None
    assert store.jobs()["vault_index"]["stats"]["skipped_oversize"] == 1


def test_idempotent_second_run(eng, vault):
    refresh()
    store.record_job("vault_index", ok=True, at="2000-01-01T00:00:00")
    with eng.connect() as c:
        before = c.execute(text("SELECT count(*), max(last_seen) FROM graphedge")).one()
    r = refresh()
    assert r["changed"] == 0 and r["removed"] == 0 and r["links"] == 0
    with eng.connect() as c:
        assert c.execute(text("SELECT count(*), max(last_seen) FROM graphedge")).one() == before
    assert store.jobs()["vault_index"]["last_success"] > "2000-01-01T00:00:00"


def test_edit_and_delete(eng, vault):
    refresh()
    p = put(vault, "wiki/Hub.md", "---\ntitle: Hub Page\n---\n[[Target]]\n")
    bump(p)
    refresh()
    assert edges(eng, "links_to") == [("note:wiki/Hub", "note:wiki/Target", "links_to", "wikilink")]
    assert not edges(eng, "tagged") and store.get_node("tag:infra") is None
    assert "service" not in store.get_node("note:wiki/Hub")["attrs"]
    (vault / "wiki/Target.md").unlink()
    r = refresh()
    assert r["removed"] == 1 and store.get_node("note:wiki/Target") is None
    assert not edges(eng, "links_to")


def test_target_edits_reresolve_unchanged_sources(eng, brain):
    put(brain, "B.md", "[[Foo]]\n[[Old Title]]\n")
    a = put(brain, "A.md", "---\ntitle: Old Title\n---\n")
    refresh()
    assert edges(eng, "links_to") == [("note:B", "note:A", "links_to", "wikilink")]
    b_before = store.get_node("note:B")["attrs"]["sha256"]
    a.write_text("---\ntitle: New Title\naliases: [Foo]\n---\n")
    bump(a)  # B itself is untouched
    r = refresh()
    assert r["changed"] == 1
    assert store.get_node("note:B")["attrs"]["sha256"] == b_before
    assert store.get_node("note:B")["attrs"]["unresolved"] == 1  # Old Title now broken, Foo resolves
    assert edges(eng, "links_to") == [("note:B", "note:A", "links_to", "wikilink")]


def test_failure_rolls_back_and_recovers(eng, vault, monkeypatch):
    refresh()
    before = edges(eng)
    bump(put(vault, "wiki/Target.md", "changed\n"))
    put(vault, "wiki/Hub.md", "[[Target]]\n")
    bump(vault / "wiki/Hub.md")
    with monkeypatch.context() as m:
        m.setattr(store, "upsert_edges", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        with pytest.raises(RuntimeError):
            refresh()
    assert edges(eng) == before
    assert store.jobs()["vault_index"]["last_error"] == "boom"
    refresh()
    assert store.jobs()["vault_index"]["last_error"] is None


def test_unreadable_note_left_alone_then_recovers(eng, brain, monkeypatch):
    put(brain, "B.md", "[[A]]\n")
    a = put(brain, "A.md", "---\ntitle: A\n---\nbody\n")
    refresh()
    snap = (edges(eng), store.get_node("note:A"))
    bump(a)
    orig = Path.read_bytes

    def fake(self):
        if self.name == "A.md":
            raise PermissionError("nope")
        return orig(self)

    with monkeypatch.context() as m:
        m.setattr(Path, "read_bytes", fake)
        r = refresh()
    assert r["unreadable"] == 1 and (edges(eng), store.get_node("note:A")) == snap
    assert store.jobs()["vault_index"]["last_error"] is None
    r = refresh()
    assert r["unreadable"] == 0 and r["changed"] >= 1 and store.get_node("note:A")["attrs"]["mtime_ns"] == a.stat().st_mtime_ns


def test_listing_failures_raise_and_keep_graph(eng, vault, monkeypatch):
    refresh()
    before = edges(eng)
    orig = os.scandir

    def fake(p):
        if str(p).endswith("wiki"):
            raise PermissionError("no listing")
        return orig(p)

    with monkeypatch.context() as m:
        m.setattr(os, "scandir", fake)
        with pytest.raises(PermissionError):
            refresh()
    assert edges(eng) == before and store.jobs()["vault_index"]["last_error"] == "no listing"
    monkeypatch.setattr("backend.api.vault._brain_root", lambda: vault / "gone")
    with pytest.raises(FileNotFoundError):
        refresh()
    assert edges(eng) == before


def test_parse_frontmatter():
    assert parse_frontmatter("---\ntags: [a, 'b']\naliases:\n  - x\n  - y\ntitle: \"T\"\nother: 1\n---\nbody") == {
        "tags": ["a", "b"], "aliases": ["x", "y"], "title": "T",
    }
    assert parse_frontmatter("no frontmatter") == {}
    assert parse_frontmatter("---\ntitle: x\nno close") == {}


def test_tag_regex_and_union():
    assert parse_tags({}, "# Heading\nsee http://x.test/a#frag and #Real/tag") == {"real/tag"}
    assert parse_tags({"tags": ["#One", "two"]}, "#two") == {"one", "two"}


def test_resolver_rungs():
    n = {
        "note:a/Dup": {"path": "a/Dup.md", "stem": "Dup"},
        "note:bb/x/Dup": {"path": "bb/x/Dup.md", "stem": "Dup"},
        "note:x/Foo": {"path": "x/Foo.md", "stem": "Foo", "title": "Nice Title", "aliases": ["Al"]},
    }
    r = Resolver(n)
    assert r.resolve("Dup") == ("note:a/Dup", True)  # shortest path wins
    assert r.resolve("bb/x/Dup") == ("note:bb/x/Dup", False)  # exact relpath rung first
    assert r.resolve("a/Dup.md") == ("note:a/Dup", False)
    assert r.resolve("Nice Title") == ("note:x/Foo", False)
    assert r.resolve("Al") == ("note:x/Foo", False)
    assert r.resolve("foo") == ("note:x/Foo", False)
    assert r.resolve("nice title") == ("note:x/Foo", False)
    assert r.resolve("nothing") == (None, False)


def test_speed_sanity(eng, brain):
    for i in range(60):
        put(brain, f"n{i}.md", f"[[n{(i + 1) % 60}]] #t{i % 5}\n")
    t = time.monotonic()
    assert refresh(full=True)["links"] == 60
    assert time.monotonic() - t < 5


def test_unchanged_unreadable_source_retried_after_target_rename(eng, brain, monkeypatch):
    b = put(brain, "B.md", "[[Old Title]]\n")
    a = put(brain, "A.md", "---\ntitle: Old Title\n---\nbody\n")
    refresh()
    assert ("note:B", "note:A") in [e[:2] for e in edges(eng, "links_to")]
    put(brain, "A.md", "---\ntitle: New Title\n---\nbody\n")  # forces a full re-resolve
    orig = Path.read_bytes

    def fake(self):
        if self.name == "B.md":
            raise PermissionError("nope")
        return orig(self)

    with monkeypatch.context() as m:
        m.setattr(Path, "read_bytes", fake)
        r = refresh()
    assert r["unreadable"] == 1
    assert ("note:B", "note:A") in [e[:2] for e in edges(eng, "links_to")]  # stale edge kept while B unreadable
    r = refresh()  # B unchanged on disk, now readable
    assert r["unreadable"] == 0
    assert ("note:B", "note:A") not in [e[:2] for e in edges(eng, "links_to")]
    assert store.get_node("note:B")["attrs"]["unresolved"] == 1


def test_root_level_stem_matches_in_incremental_run(eng, brain):
    put(brain, "_Index.md", "root\n")
    put(brain, "B.md", "body\n")
    refresh()
    put(brain, "B.md", "[[_index]]\n")
    refresh()
    inc = edges(eng)
    refresh(full=True)
    assert inc == edges(eng) and inc
