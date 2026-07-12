"""Tests for the Goal state-machine substrate (Tier 3 gate-blocker #3, Piece A).

Covers: fingerprint purity, all three debounce guards, approve happy-path,
approve-expired, approve-conflict, reject, reconcile_running, record_goal_result,
and the HTTP API layer.

Pattern: in-memory StaticPool engine monkeypatched onto backend.database.engine,
matching test_governor.py.  Worker-pool enqueue is always patched so tests never
actually run a durable task.
"""
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

# Register all table metadata (including Goal) before any test runs.
import backend.database  # noqa: F401


# ---------------------------------------------------------------------------
# Shared engine fixture
# ---------------------------------------------------------------------------

def make_engine():
    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(eng)
    return eng


@pytest.fixture
def eng(monkeypatch):
    e = make_engine()
    monkeypatch.setattr("backend.database.engine", e)
    return e


# ---------------------------------------------------------------------------
# Pool patch helper (prevents real task execution)
# ---------------------------------------------------------------------------

def _mock_pool():
    pool = MagicMock()
    pool.enqueue = AsyncMock()
    return pool


# ---------------------------------------------------------------------------
# 1. propose — creates a "proposed" goal with fingerprint + expires_at
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_propose_creates_proposed_goal(eng):
    from backend.agents import goals
    from backend.database import Goal

    result = await goals.propose(
        "Buy milk",
        "Go to the store and buy two litres of whole milk.",
        ttl_seconds=3600,
    )

    assert result["status"] == "proposed"
    g = result["goal"]
    assert g["status"] == "proposed"
    assert len(g["fingerprint"]) == 16  # sha256[:16]
    assert g["expires_at"] is not None   # TTL was passed

    # Exactly one row in the DB.
    with Session(eng) as s:
        rows = s.exec(select(Goal)).all()
    assert len(rows) == 1
    assert rows[0].fingerprint == g["fingerprint"]


# ---------------------------------------------------------------------------
# 1b. fingerprint — embedded sensor readings dedup on device+category, not the
# literal value (a fluctuating metric must not bypass dedup just because its
# reading changed between ticks)
# ---------------------------------------------------------------------------

def test_fingerprint_ignores_embedded_reading():
    from backend.agents import goals

    fp_a = goals._fingerprint(
        "Investigate Switch A temperature (111°F)",
        "Switch has been running hot at 111°F, check for airflow issues.",
    )
    fp_b = goals._fingerprint(
        "Investigate Switch A temperature (108°F)",
        "Switch has been running hot at 108°F, check for airflow issues.",
    )
    assert fp_a == fp_b

    fp_c = goals._fingerprint(
        "Investigate Device X WiFi signal strength (-89dBm)",
        "Signal has dropped to -89dBm, investigate interference.",
    )
    fp_d = goals._fingerprint(
        "Investigate Device X WiFi signal strength (-93dBm)",
        "Signal has dropped to -93dBm, investigate interference.",
    )
    assert fp_c == fp_d

    # Different devices must still fingerprint differently -- a bare model
    # number isn't a reading and must survive the strip.
    fp_e = goals._fingerprint(
        "Investigate Switch B temperature (111°F)",
        "Switch has been running hot at 111°F, check for airflow issues.",
    )
    assert fp_e != fp_a


# ---------------------------------------------------------------------------
# 2. debounce — duplicate active
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_debounce_duplicate_active(eng):
    from backend.agents import goals
    from backend.database import Goal

    title = "Restart server"
    desc = "Reboot the Unraid NAS."

    r1 = await goals.propose(title, desc)
    assert r1["status"] == "proposed"

    r2 = await goals.propose(title, desc)
    assert r2["status"] == "debounced"
    assert r2["reason"] == "duplicate_active"

    # Only ONE goal row should exist.
    with Session(eng) as s:
        count = len(s.exec(select(Goal)).all())
    assert count == 1


# ---------------------------------------------------------------------------
# 2b. debounce — TOCTOU race closed by ux_goal_fingerprint_active (2026-07-09)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_debounce_survives_concurrent_propose_race(eng, monkeypatch):
    """propose()'s pre-check (SELECT for an active duplicate) and the insert are
    separate round-trips -- two concurrent calls with the same fingerprint could
    both pass "no duplicate" before either commits. Simulate that race directly
    (patch the pre-check to always say "no duplicate found", bypassing DEBOUNCE
    #1 entirely) and confirm the DB-level unique index is the backstop: exactly
    one row lands, and the loser gets the same debounced/duplicate_active
    response DEBOUNCE #1 would normally give."""
    from backend.agents import goals
    from backend.database import Goal
    from sqlalchemy import text

    # The shared `eng` fixture only runs SQLModel.metadata.create_all(), not
    # create_db_and_tables() -- so the partial unique index (added via
    # _ensure_goal_columns(), a create_db_and_tables()-only shim) doesn't exist
    # yet on this engine. Create it explicitly, matching what happens at real
    # NEXUS startup.
    with Session(eng) as s:
        s.exec(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_goal_fingerprint_active "
            "ON goal(fingerprint) WHERE status IN ('proposed','approved','running') "
            "AND fingerprint != ''"
        ))
        s.commit()

    title = "Restart server"
    desc = "Reboot the Unraid NAS."

    # Force DEBOUNCE #1's pre-check to always report "no active duplicate" --
    # this is what "both calls race past the check before either inserts"
    # looks like from propose()'s perspective.
    monkeypatch.setattr(goals, "_db_active_by_fingerprint", lambda fp: None)

    r1 = await goals.propose(title, desc)
    r2 = await goals.propose(title, desc)

    statuses = {r1["status"], r2["status"]}
    assert statuses == {"proposed", "debounced"}, f"expected one proposed + one debounced, got {r1['status']!r}/{r2['status']!r}"
    loser = r1 if r1["status"] == "debounced" else r2
    assert loser["reason"] == "duplicate_active"

    # The DB-level constraint must have kept this to exactly ONE row, not two.
    with Session(eng) as s:
        rows = s.exec(select(Goal).where(Goal.title == title)).all()
    assert len(rows) == 1


# ---------------------------------------------------------------------------
# 3. debounce — cooldown (same fingerprint, terminal goal, proposed recently)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_debounce_cooldown(eng):
    from backend.agents import goals
    from backend.database import Goal

    title = "Send report"
    desc = "Email the weekly summary to the team."
    fp = goals._fingerprint(title, desc)

    # Seed a terminal (abandoned) goal with proposal_at = just now.
    recent_proposal = datetime.utcnow() - timedelta(seconds=30)
    with Session(eng) as s:
        s.add(Goal(
            title=title,
            description=desc,
            status="abandoned",
            fingerprint=fp,
            proposal_at=recent_proposal,
        ))
        s.commit()

    # debounce_seconds=3600 means 30s ago is still within the window.
    r = await goals.propose(title, desc, debounce_seconds=3600)
    assert r["status"] == "debounced"
    assert r["reason"] == "cooldown"

    # Still only one row.
    with Session(eng) as s:
        count = len(s.exec(select(Goal)).all())
    assert count == 1


# ---------------------------------------------------------------------------
# 4. debounce — backoff
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_debounce_backoff(eng):
    from backend.agents import goals
    from backend.database import Goal

    title = "Fetch weather"
    desc = "Pull the latest weather data."
    fp = goals._fingerprint(title, desc)

    # Seed a failed goal with backoff_until in the future.
    future_backoff = datetime.utcnow() + timedelta(hours=1)
    with Session(eng) as s:
        s.add(Goal(
            title=title,
            description=desc,
            status="failed",
            fingerprint=fp,
            proposal_at=datetime.utcnow() - timedelta(hours=2),
            backoff_until=future_backoff,
        ))
        s.commit()

    r = await goals.propose(title, desc)
    assert r["status"] == "debounced"
    assert r["reason"] == "backoff"

    with Session(eng) as s:
        count = len(s.exec(select(Goal)).all())
    assert count == 1


# ---------------------------------------------------------------------------
# 5. approve — happy path
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_approve_happy_path(eng, monkeypatch):
    from backend.agents import goals
    from backend.database import Goal, Task

    pool = _mock_pool()
    monkeypatch.setattr("backend.agents.goals.get_pool", lambda: pool)

    # Propose a goal first.
    r_propose = await goals.propose("Organise files", "Sort downloads folder by type.")
    goal_id = r_propose["goal"]["id"]

    r_approve = await goals.approve(goal_id)
    assert r_approve["status"] == "approved"
    assert r_approve["task_id"] is not None
    assert r_approve["goal"]["status"] == "running"
    assert r_approve["goal"]["task_id"] == r_approve["task_id"]

    # A Task row was created with prompt == "Goal: {title}\n\n{description}" (Fix 5).
    with Session(eng) as s:
        task = s.get(Task, r_approve["task_id"])
    assert task is not None
    title = r_propose["goal"]["title"]
    desc = r_propose["goal"]["description"]
    assert task.prompt == f"Goal: {title}\n\n{desc}"

    # pool.enqueue was called exactly once.
    pool.enqueue.assert_awaited_once_with(r_approve["task_id"])

    # Goal row in DB is "running".
    with Session(eng) as s:
        g = s.get(Goal, goal_id)
    assert g.status == "running"
    assert g.task_id == r_approve["task_id"]


# ---------------------------------------------------------------------------
# 6. approve — expired
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_approve_expired(eng, monkeypatch):
    from backend.agents import goals
    from backend.database import Goal, Task

    pool = _mock_pool()
    monkeypatch.setattr("backend.agents.goals.get_pool", lambda: pool)

    fp = goals._fingerprint("Old task", "A task that expired.")
    # Insert directly with expires_at already in the past.
    with Session(eng) as s:
        g = Goal(
            title="Old task",
            description="A task that expired.",
            status="proposed",
            fingerprint=fp,
            proposal_at=datetime.utcnow() - timedelta(hours=2),
            expires_at=datetime.utcnow() - timedelta(hours=1),
        )
        s.add(g)
        s.commit()
        s.refresh(g)
        goal_id = g.id

    r = await goals.approve(goal_id)
    assert r["status"] == "expired"

    # Goal should now be "abandoned", no Task created.
    with Session(eng) as s:
        g = s.get(Goal, goal_id)
    assert g.status == "abandoned"

    pool.enqueue.assert_not_awaited()
    with Session(eng) as s:
        tasks = s.exec(select(Task)).all()
    assert len(tasks) == 0


# ---------------------------------------------------------------------------
# 7. approve — conflict (already running / approved)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_approve_conflict(eng, monkeypatch):
    from backend.agents import goals

    pool = _mock_pool()
    monkeypatch.setattr("backend.agents.goals.get_pool", lambda: pool)

    r_propose = await goals.propose("Check logs", "Review system logs for errors.")
    goal_id = r_propose["goal"]["id"]

    # First approve succeeds.
    await goals.approve(goal_id)

    # Second approve on a now-running goal → conflict.
    r2 = await goals.approve(goal_id)
    assert r2["status"] == "conflict"
    assert r2["current"] == "running"


# ---------------------------------------------------------------------------
# 8. reject
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_reject_proposed_goal(eng):
    from backend.agents import goals
    from backend.database import Goal

    r_propose = await goals.propose("Buy groceries", "Pick up items from the grocery list.")
    goal_id = r_propose["goal"]["id"]

    r_reject = await goals.reject(goal_id)
    assert r_reject["status"] == "abandoned"

    with Session(eng) as s:
        g = s.get(Goal, goal_id)
    assert g.status == "abandoned"


# ---------------------------------------------------------------------------
# 8b. delete + edit
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_delete_goal(eng):
    from backend.agents import goals
    from backend.database import Goal

    r = await goals.propose("Delete me", "A goal to remove.")
    goal_id = r["goal"]["id"]

    r_del = await goals.delete(goal_id)
    assert r_del["status"] == "deleted"

    with Session(eng) as s:
        assert s.get(Goal, goal_id) is None


@pytest.mark.asyncio
async def test_delete_missing_goal(eng):
    from backend.agents import goals

    r = await goals.delete(999999)
    assert r["status"] == "not_found"


@pytest.mark.asyncio
async def test_edit_proposed_goal(eng):
    from backend.agents import goals
    from backend.database import Goal

    r = await goals.propose("Old title", "Old description.", risk="low", category="other")
    goal_id = r["goal"]["id"]
    old_fp = r["goal"]["fingerprint"]

    r_edit = await goals.edit(goal_id, {
        "title": "New title",
        "description": "New description.",
        "risk": "high",
        "category": "storage",
    })
    assert r_edit["status"] == "updated"
    assert r_edit["goal"]["title"] == "New title"
    assert r_edit["goal"]["risk"] == "high"
    assert r_edit["goal"]["category"] == "storage"
    # Fingerprint must change when title/description change (debounce consistency).
    assert r_edit["goal"]["fingerprint"] != old_fp

    with Session(eng) as s:
        g = s.get(Goal, goal_id)
    assert g.title == "New title"
    assert g.description == "New description."


@pytest.mark.asyncio
async def test_edit_non_proposed_goal_allowed(eng):
    from backend.agents import goals
    from backend.database import Goal

    # Editing is now allowed from any status (e.g. a running/failed goal).
    with Session(eng) as s:
        g = Goal(title="Running", description="x", status="running", fingerprint="ffff0000ffff0000")
        s.add(g)
        s.commit()
        s.refresh(g)
        gid = g.id

    r = await goals.edit(gid, {"title": "edited running goal"})
    assert r["status"] == "updated"
    assert r["goal"]["title"] == "edited running goal"
    # Status is unchanged by an edit.
    assert r["goal"]["status"] == "running"


@pytest.mark.asyncio
async def test_edit_invalid_risk_rejected(eng):
    from backend.agents import goals

    r = await goals.propose("Risky", "desc")
    gid = r["goal"]["id"]
    r_edit = await goals.edit(gid, {"risk": "extreme"})
    assert r_edit["status"] == "conflict"
    assert r_edit["current"] == "invalid_risk"


# ---------------------------------------------------------------------------
# 8c. disable / enable
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_set_disabled_toggles(eng):
    from backend.agents import goals
    from backend.database import Goal

    r = await goals.propose("Pausable", "A goal to pause.")
    gid = r["goal"]["id"]
    assert r["goal"]["disabled"] is False

    r_dis = await goals.set_disabled(gid, True)
    assert r_dis["status"] == "updated"
    assert r_dis["goal"]["disabled"] is True

    r_en = await goals.set_disabled(gid, False)
    assert r_en["goal"]["disabled"] is False

    with Session(eng) as s:
        assert s.get(Goal, gid).disabled is False


@pytest.mark.asyncio
async def test_set_disabled_missing(eng):
    from backend.agents import goals

    r = await goals.set_disabled(999999, True)
    assert r["status"] == "not_found"


@pytest.mark.asyncio
async def test_disabled_recurring_goal_not_due(eng):
    from backend.agents import goals
    from backend.database import Goal

    # A due recurring goal that is disabled must NOT be returned by the scheduler.
    past = datetime.utcnow() - timedelta(hours=1)
    with Session(eng) as s:
        g = Goal(
            title="Recurring disabled",
            description="x",
            status="completed",
            fingerprint="cccc2222cccc2222",
            cadence="daily",
            next_eval_at=past,
            disabled=True,
        )
        s.add(g)
        s.commit()
        s.refresh(g)
        gid = g.id

    due = goals._db_due_recurring_goals(datetime.utcnow())
    assert all(d["id"] != gid for d in due), "disabled recurring goal must not be due"

    # Re-enable → now it should be due.
    await goals.set_disabled(gid, False)
    due2 = goals._db_due_recurring_goals(datetime.utcnow())
    assert any(d["id"] == gid for d in due2), "enabled recurring goal should be due"


# ---------------------------------------------------------------------------
# 9. reconcile_running
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_reconcile_running(eng):
    from backend.agents import goals
    from backend.database import Goal, Task

    # Create two running goals, each linked to a task.
    with Session(eng) as s:
        t_success = Task(prompt="do success thing", status="success")
        t_failed = Task(prompt="do failing thing", status="failed")
        s.add(t_success)
        s.add(t_failed)
        s.commit()
        s.refresh(t_success)
        s.refresh(t_failed)

        g_success = Goal(
            title="Succeed",
            description="A task that will succeed.",
            status="running",
            fingerprint="aaaa0000aaaa0000",
            task_id=t_success.id,
        )
        g_failed = Goal(
            title="Fail",
            description="A task that will fail.",
            status="running",
            fingerprint="bbbb1111bbbb1111",
            task_id=t_failed.id,
            attempts=0,
        )
        s.add(g_success)
        s.add(g_failed)
        s.commit()
        s.refresh(g_success)
        s.refresh(g_failed)
        g_success_id = g_success.id
        g_failed_id = g_failed.id

    # goal_outcome_distill_llm defaults True (2026-07-07) -- mock the Haiku
    # fact-extraction call so this reconcile test stays hermetic/offline.
    with patch("backend.agents.facts.extract_and_store", new_callable=AsyncMock):
        await goals.reconcile_running(backoff_base_seconds=300, max_attempts=5)

    with Session(eng) as s:
        gs = s.get(Goal, g_success_id)
        gf = s.get(Goal, g_failed_id)

    assert gs.status == "completed"
    assert gf.status == "failed"
    assert gf.attempts == 1
    assert gf.backoff_until is not None
    assert gf.backoff_until > datetime.utcnow()


# ---------------------------------------------------------------------------
# 10. API layer (TestClient + Bearer)
# ---------------------------------------------------------------------------

@pytest.fixture
def goals_client(tmp_path, monkeypatch):
    vault_key = tmp_path / ".vault.key"
    vault_file = tmp_path / "nexus.vault"
    vault_key.write_bytes(b"A" * 32)
    vault_file.write_text("{}")
    monkeypatch.chdir(tmp_path)

    test_engine = make_engine()
    monkeypatch.setattr("backend.database.engine", test_engine)

    from backend.database import get_session

    def override_session():
        with Session(test_engine) as session:
            yield session

    with patch("backend.database.create_db_and_tables"), \
         patch("backend.scheduler.setup_scheduler"), \
         patch("backend.scheduler.scheduler") as sched, \
         patch("backend.agents.memo_watcher.start_watcher_blocking"), \
         patch("backend.agents.memo_watcher.stop_watcher", new_callable=AsyncMock):
        sched.running = False
        from backend.main import app
        app.dependency_overrides[get_session] = override_session
        with TestClient(app) as c:
            c._engine = test_engine
            yield c
        app.dependency_overrides.clear()


def test_api_propose_returns_200(goals_client, auth_headers, monkeypatch):
    pool = _mock_pool()
    monkeypatch.setattr("backend.agents.goals.get_pool", lambda: pool)

    resp = goals_client.post(
        "/api/goals/propose",
        headers=auth_headers,
        json={
            "title": "Update firmware",
            "description": "Flash the latest firmware on the router.",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "proposed"
    assert body["goal"]["status"] == "proposed"


def test_api_list_goals(goals_client, auth_headers, monkeypatch):
    pool = _mock_pool()
    monkeypatch.setattr("backend.agents.goals.get_pool", lambda: pool)

    goals_client.post(
        "/api/goals/propose",
        headers=auth_headers,
        json={"title": "Task A", "description": "Do something useful."},
    )
    resp = goals_client.get("/api/goals/", headers=auth_headers)
    assert resp.status_code == 200
    items = resp.json()
    assert len(items) >= 1
    assert items[0]["title"] == "Task A"


def test_api_approve_returns_200(goals_client, auth_headers, monkeypatch):
    pool = _mock_pool()
    monkeypatch.setattr("backend.agents.goals.get_pool", lambda: pool)

    r = goals_client.post(
        "/api/goals/propose",
        headers=auth_headers,
        json={"title": "Run diagnostics", "description": "Execute full system health check."},
    )
    goal_id = r.json()["goal"]["id"]

    resp = goals_client.post(f"/api/goals/{goal_id}/approve", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "approved"
    assert body["task_id"] is not None


def test_api_approve_missing_404(goals_client, auth_headers, monkeypatch):
    pool = _mock_pool()
    monkeypatch.setattr("backend.agents.goals.get_pool", lambda: pool)

    resp = goals_client.post("/api/goals/99999/approve", headers=auth_headers)
    assert resp.status_code == 404


def test_api_approve_same_id_twice_409(goals_client, auth_headers, monkeypatch):
    pool = _mock_pool()
    monkeypatch.setattr("backend.agents.goals.get_pool", lambda: pool)

    r = goals_client.post(
        "/api/goals/propose",
        headers=auth_headers,
        json={"title": "Reboot box", "description": "Restart the media server."},
    )
    goal_id = r.json()["goal"]["id"]

    goals_client.post(f"/api/goals/{goal_id}/approve", headers=auth_headers)
    resp = goals_client.post(f"/api/goals/{goal_id}/approve", headers=auth_headers)
    assert resp.status_code == 409
