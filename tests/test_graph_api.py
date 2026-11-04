from unittest.mock import AsyncMock, patch

import pytest

AUTH = {"Authorization": "Bearer test-nexus-key"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    (tmp_path / ".vault.key").write_bytes(b"A" * 32)
    (tmp_path / "nexus.vault").write_text("{}")
    monkeypatch.chdir(tmp_path)
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


def test_health_requires_auth(client):
    assert client.get("/api/graph/health").status_code in (401, 403)


def test_health_disabled_by_default(client):
    r = client.get("/api/graph/health", headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] is False
    assert "jobs" in body and "nodes" in body


@pytest.fixture
def gclient(client, monkeypatch):
    import backend.config as config_mod

    monkeypatch.setenv("GRAPH_ENABLED", "true")
    monkeypatch.setattr(config_mod, "_settings_instance", None)
    yield client
    monkeypatch.setattr(config_mod, "_settings_instance", None)


def _seed(n_extra=0):
    from backend.graph import now_iso, store
    from backend.graph.store import Edge, Node

    at = now_iso()
    nodes = [Node("host:pve", "host", "pve"), Node("guest:1", "guest", "web"), Node("service:web", "service", "web")]
    edges = [Edge("guest:1", "host:pve", "runs_on", "proxmox_api")]
    for i in range(n_extra):
        nodes.append(Node(f"guest:{1000 + i}", "guest", f"g{i}"))
        edges.append(Edge(f"guest:{1000 + i}", "guest:1", "runs_on", "manual"))
    store.upsert_nodes(nodes, at)
    store.upsert_edges(edges, at)


def _get(c, **params):
    with patch("backend.agents.outcomes.open_flags", new=AsyncMock(return_value=[])):
        return c.get("/api/graph/topology", params=params, headers=AUTH)


def test_topology_503_when_disabled(client):
    assert _get(client, node="x").status_code == 503


def test_topology_requires_auth(gclient):
    assert gclient.get("/api/graph/topology", params={"node": "x"}).status_code in (401, 403)


def test_topology_ok_404_409_422(gclient):
    _seed()
    r = _get(gclient, node="guest:1")
    assert r.status_code == 200
    body = r.json()
    assert body["node"]["id"] == "guest:1" and body["edges"][0]["source"] == "proxmox_api"
    assert body["warnings"] == [] and [x["id"] for x in body["up"]] == ["host:pve"]
    assert _get(gclient, node="nope").status_code == 404
    assert _get(gclient, node="web").status_code == 409  # guest + service share the name
    assert _get(gclient, node="guest:1", direction="sideways").status_code == 422


def test_topology_depth_clamp_and_truncation(gclient):
    _seed(n_extra=201)
    body = _get(gclient, node="guest:1", direction="down", depth=99).json()
    assert body["truncated"] is True and len(body["down"]) == 200
    assert [x["id"] for x in body["down"]] == sorted(x["id"] for x in body["down"])


def test_topology_flags_truncated(gclient):
    _seed()
    rows = [{"id": i, "severity": "low", "summary": "s", "source": "vault_signals", "check": "x"} for i in range(1000)]
    with patch("backend.agents.outcomes.open_flags", new=AsyncMock(return_value=rows)):
        r = gclient.get("/api/graph/topology", params={"node": "guest:1"}, headers=AUTH)
    assert r.json()["flags_truncated"] is True


# ---------------------------------------------------------------- /changes (G3a)

def _seed_changes():
    from backend.graph import store
    from backend.graph.store import Edge, Node

    store.upsert_nodes([Node("host:pve", "host", "pve"), Node("guest:3101", "guest", "lab-vm", ("CT 3101",)),
                        Node("guest:3102", "guest", "dup"), Node("guest:3103", "guest", "dup")], "2026-01-01T00:00:00")
    store.upsert_edges([Edge("guest:3101", "host:pve", "runs_on", "proxmox_api")], "2026-01-01T00:00:00")
    store.insert_change("2099-01-01T00:00:00", "host:pve", "status", "online", "offline", "manual")
    store.insert_change("2099-01-02T00:00:00", "guest:3101", "cores", "2", "4", "proxmox_api")


def test_changes_503_when_disabled(client):
    assert client.get("/api/graph/changes", params={"node": "x"}, headers=AUTH).status_code == 503
    assert client.post("/api/graph/changes", json={"node_id": "x", "field": "f", "source": "kithos"}, headers=AUTH).status_code == 503


def test_changes_requires_auth(gclient):
    assert gclient.get("/api/graph/changes", params={"node": "x"}).status_code in (401, 403)
    assert gclient.post("/api/graph/changes", json={}).status_code in (401, 403)


def test_changes_get(gclient):
    _seed_changes()
    r = gclient.get("/api/graph/changes", params={"node": "CT 3101", "since": "2098-12-31T00:00:00Z"}, headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["node"] == "guest:3101" and body["nodes"] == ["guest:3101", "host:pve"]
    assert [c["field"] for c in body["changes"]] == ["cores", "status"]  # ancestors included, newest first
    r = gclient.get("/api/graph/changes", params={"node": "guest:3101", "since": "2098-12-31T00:00:00", "ancestors": "false"}, headers=AUTH)
    assert [c["node_id"] for c in r.json()["changes"]] == ["guest:3101"]
    assert gclient.get("/api/graph/changes", params={"node": "nope"}, headers=AUTH).status_code == 404
    assert gclient.get("/api/graph/changes", params={"node": "dup"}, headers=AUTH).status_code == 409
    for bad in ("soon", "999999999999999999999999h", "0001-01-01T00:00:00+01:00"):
        assert gclient.get("/api/graph/changes", params={"node": "guest:3101", "since": bad}, headers=AUTH).status_code == 422
    for lim in (0, 501):
        assert gclient.get("/api/graph/changes", params={"node": "guest:3101", "limit": lim}, headers=AUTH).status_code == 422


def test_changes_post(gclient):
    from backend.graph import store

    _seed_changes()
    r = gclient.post("/api/graph/changes", headers=AUTH, json={
        "machine": "lab-vm", "field": "pack:docker", "new": "installed", "source": "kithos", "ts": "2026-11-01T10:00:00+02:00"})
    assert r.status_code == 201 and r.json()["node_id"] == "guest:3101"
    got = store.changes_for(["guest:3101"], since="2026-11-01T00:00:00", before="2026-11-02T00:00:00")
    assert [(c["ts"], c["field"], c["source"]) for c in got] == [("2026-11-01T08:00:00", "pack:docker", "kithos")]
    assert gclient.post("/api/graph/changes", headers=AUTH, json={"node_id": "guest:3101", "field": "x", "source": "kithos"}).status_code == 201
    bad = [
        ({"machine": "ghost-box", "field": "x", "source": "kithos"}, 404),
        ({"node_id": "guest:9999999", "field": "x", "source": "kithos"}, 404),
        ({"machine": "dup", "field": "x", "source": "kithos"}, 409),
        ({"node_id": "guest:3101", "machine": "lab-vm", "field": "x", "source": "kithos"}, 422),
        ({"field": "x", "source": "kithos"}, 422),
        ({"node_id": "guest:3101", "field": "x", "source": "proxmox_api"}, 422),
        ({"node_id": "guest:3101", "field": "Bad Field", "source": "kithos"}, 422),
        ({"node_id": "guest:3101", "field": "x", "source": "kithos", "ts": "later"}, 422),
        ({"node_id": "guest:3101", "field": "x", "source": "kithos", "ts": "0001-01-01T00:00:00+01:00"}, 422),
        ({"node_id": "guest:3101", "field": "x", "source": "kithos", "new": "v" * 501}, 422),
    ]
    for body, code in bad:
        assert gclient.post("/api/graph/changes", headers=AUTH, json=body).status_code == code, body
    assert store.get_node("guest:9999999") is None and store.resolve_node("ghost-box") == []  # never creates nodes
