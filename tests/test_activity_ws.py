"""Tests for /ws/agent-activity and GET /api/activity -- the Pulse page's
transport layer. Mirrors tests/test_lan_edge.py's fixture/pattern for the
existing /ws/logs and /ws/state sockets.
"""
import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch, AsyncMock
from sqlmodel import SQLModel, create_engine, Session
from sqlmodel.pool import StaticPool

from backend import activity


def make_test_engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return engine


@pytest.fixture(autouse=True)
def _reset_activity():
    activity.reset_registry()
    yield
    activity.reset_registry()


@pytest.fixture
def lan_client(tmp_path, monkeypatch):
    """Same wiring as test_lan_edge.py's lan_client fixture."""
    vault_key = tmp_path / ".vault.key"
    vault_file = tmp_path / "nexus.vault"
    vault_key.write_bytes(b"A" * 32)
    vault_file.write_text("{}")
    monkeypatch.chdir(tmp_path)

    test_engine = make_test_engine()
    monkeypatch.setattr("backend.database.engine", test_engine)

    def override_session():
        with Session(test_engine) as session:
            yield session

    with patch("backend.database.create_db_and_tables"), \
         patch("backend.scheduler.setup_scheduler"), \
         patch("backend.scheduler.scheduler") as sched, \
         patch("backend.agents.memo_watcher.start_watcher_blocking"), \
         patch("backend.agents.memo_watcher.stop_watcher", new_callable=AsyncMock), \
         patch("backend.state_workers.prime_state_workers", new_callable=AsyncMock):
        sched.running = False
        from backend.main import app
        from backend.database import get_session
        app.dependency_overrides[get_session] = override_session
        with TestClient(app, raise_server_exceptions=False) as c:
            yield c
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Auth handshake — same contract as /ws/logs and /ws/state
# ---------------------------------------------------------------------------

def test_activity_ws_rejects_no_key(lan_client):
    from starlette.websockets import WebSocketDisconnect
    with pytest.raises((WebSocketDisconnect, Exception)):
        with lan_client.websocket_connect("/ws/agent-activity"):
            pass


def test_activity_ws_rejects_wrong_key(lan_client):
    from starlette.websockets import WebSocketDisconnect
    with pytest.raises((WebSocketDisconnect, Exception)):
        with lan_client.websocket_connect("/ws/agent-activity?key=WRONG_KEY"):
            pass


def test_activity_ws_accepts_correct_key(lan_client):
    from backend.config import get_settings
    real_key = get_settings().nexus_api_key
    with lan_client.websocket_connect(f"/ws/agent-activity?key={real_key}") as ws:
        assert ws is not None


def test_activity_ws_accepts_key_via_subprotocol(lan_client):
    from backend.config import get_settings
    real_key = get_settings().nexus_api_key
    with lan_client.websocket_connect(
        "/ws/agent-activity", subprotocols=["nexus-api-key", real_key]
    ) as ws:
        assert ws is not None


# ---------------------------------------------------------------------------
# Snapshot on connect
# ---------------------------------------------------------------------------

def test_activity_ws_sends_snapshot_on_connect(lan_client):
    from backend.config import get_settings
    real_key = get_settings().nexus_api_key

    activity.begin("job:homelab_watch", "job", "homelab_watch")
    activity.pulse("broker", "action", "ha_service allowed")

    with lan_client.websocket_connect(f"/ws/agent-activity?key={real_key}") as ws:
        msg = ws.receive_json()

    assert msg["type"] == "activity.snapshot"
    ids = {e["actor_id"] for e in msg["entries"]}
    assert "job:homelab_watch" in ids
    assert len(msg["events"]) == 1
    assert msg["events"][0]["summary"] == "ha_service allowed"


def test_activity_ws_snapshot_empty_when_no_activity(lan_client):
    from backend.config import get_settings
    real_key = get_settings().nexus_api_key
    with lan_client.websocket_connect(f"/ws/agent-activity?key={real_key}") as ws:
        msg = ws.receive_json()
    assert msg == {"type": "activity.snapshot", "entries": [], "events": []}


# ---------------------------------------------------------------------------
# REST fallback
# ---------------------------------------------------------------------------

def test_get_activity_requires_auth(lan_client):
    resp = lan_client.get("/api/activity")
    assert resp.status_code in (401, 403)


def test_get_activity_returns_snapshot(lan_client):
    from backend.config import get_settings
    real_key = get_settings().nexus_api_key
    activity.begin("worker:0", "worker", "task 7")

    resp = lan_client.get("/api/activity", headers={"Authorization": f"Bearer {real_key}"})
    assert resp.status_code == 200
    body = resp.json()
    ids = {e["actor_id"] for e in body["entries"]}
    assert "worker:0" in ids


def test_get_activity_includes_registered_jobs(lan_client):
    """The real-world state this exists for: goal_proposer is registered on
    the live scheduler but hasn't fired since the last restart -- it must be
    representable in `jobs` without a matching `entries` row."""
    from types import SimpleNamespace
    from backend.config import get_settings
    import backend.scheduler as sched_mod

    real_key = get_settings().nexus_api_key
    fake_job = SimpleNamespace(id="goal_proposer", next_run_time=None)
    with patch.object(sched_mod.scheduler, "get_jobs", return_value=[fake_job]):
        resp = lan_client.get("/api/activity", headers={"Authorization": f"Bearer {real_key}"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["jobs"] == [{"id": "goal_proposer", "next_run_time": None}]
    assert "job:goal_proposer" not in {e["actor_id"] for e in body["entries"]}


def test_get_activity_jobs_empty_when_scheduler_unavailable(lan_client):
    """A scheduler hiccup must degrade `jobs` to [] without breaking the rest
    of the page's paint -- entries/events unaffected."""
    from backend.config import get_settings
    real_key = get_settings().nexus_api_key
    activity.begin("worker:0", "worker", "task 7")

    resp = lan_client.get("/api/activity", headers={"Authorization": f"Bearer {real_key}"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["jobs"] == []
    assert "worker:0" in {e["actor_id"] for e in body["entries"]}


# ---------------------------------------------------------------------------
# Broadcaster: runs the REAL run_activity_broadcaster() coroutine, not a
# reimplementation of its logic in the test -- an Opus verify pass on this
# feature found the original version of these two tests never actually
# executed the broadcaster function at all (it was the one real gap that let
# a genuine blocking bug -- _pending_events growing unbounded whenever no
# client was connected -- ship past 47 passing tests undetected).
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_broadcaster_sends_real_delta_on_change():
    from backend.api.agents import activity_ws_manager
    import asyncio

    sent = []

    class _FakeWs:
        pass

    activity_ws_manager.active.append(_FakeWs())
    try:
        with patch.object(activity_ws_manager, "broadcast", new=AsyncMock(side_effect=lambda m: sent.append(m))):
            task = asyncio.create_task(activity.run_activity_broadcaster())
            await asyncio.sleep(0.05)  # let the loop start and reach its first sleep
            activity.begin("job:x", "job", "x")
            await asyncio.sleep(0.4)  # cover at least one real 250ms tick
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
    finally:
        activity_ws_manager.active.clear()

    assert len(sent) >= 1
    assert '"type": "activity.delta"' in sent[0]
    assert "job:x" in sent[0]


@pytest.mark.asyncio
async def test_broadcaster_real_loop_skips_send_when_no_clients_connected():
    from backend.api.agents import activity_ws_manager
    import asyncio

    assert activity_ws_manager.active == []
    broadcast_mock = AsyncMock()
    with patch.object(activity_ws_manager, "broadcast", broadcast_mock):
        task = asyncio.create_task(activity.run_activity_broadcaster())
        activity.begin("job:x", "job", "x")
        activity.begin("job:z", "job", "z")
        activity.remove("job:z")  # exercises _removed without wiping the entry job:x's later assertion needs
        activity.pulse("job:y", "note", "hello")
        await asyncio.sleep(0.4)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    # Regression: a real follow-up Opus verify pass found _removed leaking
    # unbounded because the broadcaster's no-clients branch used to be a
    # bare `continue` -- confirm the REAL loop actually calls
    # discard_undelivered() each tick nobody's connected, not just that the
    # function exists in isolation.
    assert activity._removed == set()
    assert len(activity._pending_events) == 0

    broadcast_mock.assert_not_awaited()
    # Dirty state still accumulates even with nobody connected -- the next
    # connecting client gets it via the snapshot, not a missed delta.
    assert activity.snapshot()["entries"]
