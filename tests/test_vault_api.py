"""Vault browser API tests — catalog, note read + traversal guard, graph
resolution rungs, search delegation, auth."""

import json
from unittest.mock import patch, AsyncMock

import pytest

AUTH = {"Authorization": "Bearer test-nexus-key"}

CATALOG = {
    "built_at": "2026-08-22T03:00:00Z",
    "parser_version": 1,
    "pages": [
        {"title": "A", "filename": "A.md", "headers": "", "summary": "", "tags": []},
        {"title": "B", "filename": "B.md", "headers": "", "summary": "", "tags": []},
        # title differs from filename stem -- the documented namespace issue
        {"title": "C Title", "filename": "C-Page.md", "headers": "", "summary": "", "tags": []},
    ],
}


@pytest.fixture
def vault_client(tmp_path, monkeypatch):
    """TestClient wired to a fake Brain/ tree -- same app-boot patch stack as
    test_facts_audit's facts_client, minus the DB session override (this
    router never touches the DB)."""
    (tmp_path / ".vault.key").write_bytes(b"A" * 32)
    (tmp_path / "nexus.vault").write_text("{}")
    monkeypatch.chdir(tmp_path)

    brain = tmp_path / "Brain"
    (brain / "wiki").mkdir(parents=True)
    (brain / "_meta").mkdir()
    (brain / "_meta" / "wiki-catalog.json").write_text(json.dumps(CATALOG))
    # A links: stem-exact, title-exact (stem != title), ci-stem dup of B, unresolved, self
    (brain / "wiki" / "A.md").write_text("[[B]] and [[C Title]] and [[b]] and [[Nonexistent]] and [[A]]")
    (brain / "wiki" / "B.md").write_text("no links here")
    (brain / "wiki" / "C-Page.md").write_text("[[A]]")
    (tmp_path / "outside.md").write_text("secret")

    monkeypatch.setattr("backend.api.vault._brain_root", lambda: brain)

    with patch("backend.database.create_db_and_tables"), \
         patch("backend.scheduler.setup_scheduler"), \
         patch("backend.scheduler.scheduler") as sched, \
         patch("backend.agents.memo_watcher.start_watcher_blocking"), \
         patch("backend.agents.memo_watcher.stop_watcher", new_callable=AsyncMock), \
         patch("backend.state_workers.prime_state_workers", new_callable=AsyncMock):
        sched.running = False
        from fastapi.testclient import TestClient
        from backend.main import app
        with TestClient(app) as c:
            yield c


def test_requires_auth(vault_client):
    for path in ("/api/vault/catalog", "/api/vault/graph", "/api/vault/note?path=wiki/A.md", "/api/vault/search?q=x"):
        assert vault_client.get(path).status_code in (401, 403)


def test_catalog(vault_client):
    resp = vault_client.get("/api/vault/catalog", headers=AUTH)
    assert resp.status_code == 200
    data = resp.json()
    assert data["built_at"] == "2026-08-22T03:00:00Z"
    assert [p["filename"] for p in data["pages"]] == ["A.md", "B.md", "C-Page.md"]
    assert "path_str" not in data["pages"][0]


def test_note_read_and_guards(vault_client):
    ok = vault_client.get("/api/vault/note", params={"path": "wiki/A.md"}, headers=AUTH)
    assert ok.status_code == 200 and "[[B]]" in ok.json()["content"]
    assert vault_client.get("/api/vault/note", params={"path": "../outside.md"}, headers=AUTH).status_code == 400
    assert vault_client.get("/api/vault/note", params={"path": "wiki/A.txt"}, headers=AUTH).status_code == 400
    assert vault_client.get("/api/vault/note", params={"path": "wiki/nope.md"}, headers=AUTH).status_code == 404


def test_graph_resolution_rungs(vault_client):
    resp = vault_client.get("/api/vault/graph", headers=AUTH)
    assert resp.status_code == 200
    data = resp.json()
    assert {n["id"] for n in data["nodes"]} == {"A", "B", "C-Page"}
    links = {(l["source"], l["target"]) for l in data["links"]}
    # [[B]]+[[b]] dedupe to one edge; [[C Title]] resolves via title rung to C-Page;
    # [[Nonexistent]] dropped; [[A]] self-loop excluded; C-Page -> A via stem rung.
    assert links == {("A", "B"), ("A", "C-Page"), ("C-Page", "A")}


def test_search_delegates(vault_client):
    with patch("backend.api.vault.vault_search", new_callable=AsyncMock) as vs:
        vs.return_value = "**Brain/wiki/A.md**\ncontext line"
        resp = vault_client.get("/api/vault/search", params={"q": "hello world"}, headers=AUTH)
        assert resp.status_code == 200
        assert resp.json() == {"query": "hello world", "result": "**Brain/wiki/A.md**\ncontext line"}
        vs.assert_awaited_once_with("hello world", 20)


def test_catalog_missing_returns_503(vault_client, tmp_path):
    (tmp_path / "Brain" / "_meta" / "wiki-catalog.json").unlink()
    resp = vault_client.get("/api/vault/catalog", headers=AUTH)
    assert resp.status_code == 503
