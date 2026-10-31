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
