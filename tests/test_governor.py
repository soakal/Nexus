"""Tests for the Tier 1.5 cost governor + global kill switch.

Covers: pricing/metering (router), spend logging best-effort guarantees, budget
windowing, the daily + per-task brakes, chat graceful degrade, the broker kill
switch, SystemState seeding, and the /api/safety pause/resume/status/budget API.
"""
import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

# Ensure all tables (incl. SpendLog, SystemState) are registered on metadata.
import backend.database  # noqa: F401,E402


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


def _seed_state(eng, autonomy=True, daily=25.0, per_task=5.0):
    from backend.database import SystemState

    with Session(eng) as s:
        row = s.get(SystemState, 1)
        if row is None:
            row = SystemState(id=1)
            s.add(row)
        row.autonomy_enabled = autonomy
        row.daily_budget_usd = daily
        row.per_task_budget_usd = per_task
        s.commit()


def _seed_spend(eng, cost, created_at=None, model="claude-sonnet-4-6", task_id=None):
    from backend.database import SpendLog

    with Session(eng) as s:
        row = SpendLog(model=model, cost_usd=cost, task_id=task_id)
        if created_at is not None:
            row.created_at = created_at
        s.add(row)
        s.commit()


def _all_spend(eng):
    from backend.database import SpendLog

    with Session(eng) as s:
        return s.exec(select(SpendLog)).all()


def _usage_resp(text="hi", input_tokens=1000, output_tokens=500, cache_creation=0, cache_read=0):
    """A Messages-API-shaped response with a real (int) usage namespace."""
    resp = SimpleNamespace()
    resp.content = [SimpleNamespace(type="text", text=text)]
    resp.usage = SimpleNamespace(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_creation_input_tokens=cache_creation,
        cache_read_input_tokens=cache_read,
    )
    return resp


# ---------------------------------------------------------------------------
# Pricing — _compute_cost
# ---------------------------------------------------------------------------

def test_compute_cost_per_model():
    from backend.agents import router

    for model, price in router._PRICE_PER_MTOK.items():
        cost = router._compute_cost(model, 1_000_000, 1_000_000)
        # 1M input + 1M output at the table's rates (no hardcoded dollars here).
        assert cost == pytest.approx(price["input"] + price["output"])


def test_compute_cost_unknown_model_zero():
    from backend.agents import router

    assert router._compute_cost("not-a-real-model", 1000, 500) == 0.0


def test_compute_cost_includes_cache_tokens():
    from backend.agents import router

    model = router.SONNET_MODEL
    price = router._PRICE_PER_MTOK[model]
    base = router._compute_cost(model, 1000, 500)
    with_cache = router._compute_cost(model, 1000, 500, cache_creation=2000, cache_read=3000)
    # Cache tokens are priced as fractions of the input rate: cache_creation at
    # 1.25x input, cache_read at 0.1x input.
    expected_delta = (
        2000 / 1e6 * (price["input"] * 1.25)
        + 3000 / 1e6 * (price["input"] * 0.1)
    )
    assert with_cache - base == pytest.approx(expected_delta)
    assert with_cache > base


# ---------------------------------------------------------------------------
# Spend logging — best effort
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_spendlog_written_per_call(eng):
    resp = _usage_resp()
    with patch("anthropic.Anthropic") as mock_anthropic:
        mock_client = MagicMock()
        mock_client.messages.create.return_value = resp
        mock_anthropic.return_value = mock_client

        from backend.agents import router
        out = await router.sonnet("hi", label="unit")
        assert out == "hi"

    rows = _all_spend(eng)
    assert len(rows) == 1
    assert rows[0].model == router.SONNET_MODEL
    assert rows[0].input_tokens == 1000
    assert rows[0].output_tokens == 500
    assert rows[0].label == "unit"
    assert rows[0].cost_usd > 0


@pytest.mark.asyncio
async def test_no_usage_writes_no_row(eng):
    """A plain MagicMock response (usage is a MagicMock -> int() raises) writes
    ZERO SpendLog rows — protecting the existing test_router.py suite."""
    mock_resp = MagicMock()
    mock_resp.content = [MagicMock(type="text", text="x")]
    with patch("anthropic.Anthropic") as mock_anthropic:
        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_resp
        mock_anthropic.return_value = mock_client

        from backend.agents import router
        await router.sonnet("hi")

    assert len(_all_spend(eng)) == 0


@pytest.mark.asyncio
async def test_spend_logging_failure_does_not_break_call(eng):
    resp = _usage_resp(text="answer")
    with patch("anthropic.Anthropic") as mock_anthropic, \
         patch("backend.agents.router._record_spend", side_effect=RuntimeError("db down")):
        mock_client = MagicMock()
        mock_client.messages.create.return_value = resp
        mock_anthropic.return_value = mock_client

        from backend.agents import router
        out = await router.sonnet("hi")

    assert out == "answer"  # response still returned despite logging failure


# ---------------------------------------------------------------------------
# Budget windowing
# ---------------------------------------------------------------------------

def test_today_spend_sums_only_today(eng):
    from backend.safety import governor

    now = datetime.utcnow()
    _seed_spend(eng, 3.0, created_at=now)
    _seed_spend(eng, 2.0, created_at=now)
    # Yesterday (well before any local midnight) must NOT count.
    _seed_spend(eng, 99.0, created_at=now - timedelta(days=1, hours=12))

    total = governor.today_spend_usd()
    assert total == pytest.approx(5.0)


# ---------------------------------------------------------------------------
# check_budget
# ---------------------------------------------------------------------------

def test_check_budget_daily_raises(eng):
    from backend.safety import governor

    _seed_state(eng, daily=10.0)
    _seed_spend(eng, 12.0, created_at=datetime.utcnow())
    with pytest.raises(governor.BudgetExceeded) as ei:
        governor.check_budget()
    assert ei.value.scope == "daily"


def test_check_budget_under_ok(eng):
    from backend.safety import governor

    _seed_state(eng, daily=10.0)
    _seed_spend(eng, 4.0, created_at=datetime.utcnow())
    assert governor.check_budget() is None


def test_check_budget_per_task_raises(eng):
    from backend.safety import governor

    _seed_state(eng, daily=1000.0, per_task=5.0)
    start = datetime.utcnow()
    # Spend tagged with task 42 — the per-task brake now scopes spend by task_id.
    _seed_spend(eng, 6.0, created_at=start + timedelta(seconds=1), task_id=42)
    with pytest.raises(governor.BudgetExceeded) as ei:
        governor.check_budget(task_id=42, task_start=start)
    assert ei.value.scope == "per_task"
    assert ei.value.task_id == 42


# ---------------------------------------------------------------------------
# Router daily brake
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_router_blocks_before_create_when_over_budget(eng):
    from backend.safety import governor

    _seed_state(eng, daily=1.0)
    _seed_spend(eng, 5.0, created_at=datetime.utcnow())

    with patch("anthropic.Anthropic") as mock_anthropic:
        mock_client = MagicMock()
        mock_anthropic.return_value = mock_client

        from backend.agents import router
        with pytest.raises(governor.BudgetExceeded):
            await router.sonnet("hi")

        # The brake fires BEFORE the API call — messages.create never runs.
        mock_client.messages.create.assert_not_called()


# ---------------------------------------------------------------------------
# Orchestrator per-task brake
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_task_budget_exceeded_finalizes_failed(eng):
    from backend.database import Task

    _seed_state(eng, daily=1000.0, per_task=0.01)
    with Session(eng) as s:
        t = Task(prompt="do a thing", status="pending")
        s.add(t)
        s.commit()
        s.refresh(t)
        task_id = t.id

    # Plan one step; during planning, accrue spend over the per-task cap, stamped
    # in the (near) future so it is unambiguously >= task_start when the per-task
    # brake checks it just before step 1 runs.
    from backend.agents.orchestrator import Plan, Step

    async def fake_plan(_prompt, **kwargs):
        # Spend tagged with this task's id so the per-task brake (now scoped by
        # task_id) attributes it to the task and trips before step 1's LLM call.
        _seed_spend(eng, 1.0, created_at=datetime.utcnow() + timedelta(hours=1), task_id=task_id)
        p = Plan(task_prompt="do a thing")
        p.steps.append(Step(index=1, prompt="step one"))
        return p

    with patch("backend.agents.orchestrator._opus_plan", new=fake_plan):
        from backend.agents.orchestrator import run_task
        result = await run_task("do a thing", task_id=task_id)

    assert result.success is False
    assert result.reason == "budget_exceeded"

    with Session(eng) as s:
        t = s.get(Task, task_id)
        assert t.status == "failed"
        payload = json.loads(t.result_json)
        assert payload["error"] == "budget_exceeded"
        assert payload["scope"] == "per_task"


# ---------------------------------------------------------------------------
# Chat graceful degrade
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_chat_degrades_on_budget(eng):
    from backend.agents import chat as chat_mod
    from backend.safety.governor import BudgetExceeded

    async def boom_haiku(*a, **k):
        raise BudgetExceeded("daily", 99.0, 25.0)

    with patch("backend.agents.router.haiku", new=boom_haiku):
        out = await chat_mod.chat(None, "hello there")

    assert out["reply"] == chat_mod._BUDGET_REACHED_REPLY
    # Reply persisted normally; no exception escaped.
    from backend.database import ChatMessage

    with Session(eng) as s:
        msgs = s.exec(select(ChatMessage)).all()
        assert any(m.role == "assistant" and m.content == chat_mod._BUDGET_REACHED_REPLY for m in msgs)


# ---------------------------------------------------------------------------
# Broker kill switch
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_broker_forbids_agent_when_autonomy_disabled(eng):
    from backend.safety.broker import Decision, execute_action

    _seed_state(eng, autonomy=False)
    with patch("backend.integrations.homeassistant.call_service", new_callable=AsyncMock) as cs:
        res = await execute_action(
            actor="agent", kind="ha_service", target="light.office",
            payload={"domain": "light", "service": "turn_on"},
        )
    assert res.decision == Decision.FORBIDDEN
    assert res.error == "autonomy_disabled"
    assert cs.call_count == 0

    from backend.database import ActionLog

    with Session(eng) as s:
        logs = s.exec(select(ActionLog)).all()
    assert len(logs) == 1
    assert logs[0].decision == "forbidden"
    assert json.loads(logs[0].result_json)["reason"] == "autonomy_disabled"


@pytest.mark.asyncio
async def test_broker_allows_user_when_autonomy_disabled(eng):
    from backend.safety.broker import Decision, execute_action

    _seed_state(eng, autonomy=False)
    with patch("backend.integrations.homeassistant.call_service", new_callable=AsyncMock, return_value={"ok": True}) as cs:
        res = await execute_action(
            actor="user", kind="ha_service", target="light.office",
            payload={"domain": "light", "service": "turn_on"},
        )
    assert res.decision == Decision.EXECUTED
    cs.assert_awaited_once()


@pytest.mark.asyncio
async def test_broker_agent_policy_unchanged_when_enabled(eng):
    """Regression: with autonomy ON, the existing agent policy still applies
    (a LOW/reversible agent action is allowed + dispatched)."""
    from backend.safety.broker import Decision, execute_action

    _seed_state(eng, autonomy=True)
    with patch("backend.integrations.homeassistant.call_service", new_callable=AsyncMock, return_value={"ok": True}) as cs:
        res = await execute_action(
            actor="agent", kind="ha_service", target="light.office",
            payload={"domain": "light", "service": "turn_on"},
        )
    assert res.decision == Decision.EXECUTED
    cs.assert_awaited_once()


# ---------------------------------------------------------------------------
# ITEM 1 — per-task spend (SpendLog.task_id)
# ---------------------------------------------------------------------------

def test_ensure_spendlog_columns_idempotent_on_migrated_db():
    """The ALTER-shim adds task_id to an old spendlog table that lacks it, and is
    safe to run repeatedly (idempotent)."""
    import backend.database as db
    from sqlalchemy import text
    from sqlmodel import create_engine
    from sqlmodel.pool import StaticPool

    # Build a legacy spendlog table WITHOUT the task_id column.
    eng = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    with eng.connect() as conn:
        conn.execute(text(
            "CREATE TABLE spendlog (id INTEGER PRIMARY KEY, model TEXT, "
            "input_tokens INTEGER, output_tokens INTEGER, "
            "cache_creation_input_tokens INTEGER, cache_read_input_tokens INTEGER, "
            "cost_usd REAL, label TEXT, created_at TEXT)"
        ))
        conn.commit()

    orig = db.engine
    db.engine = eng
    try:
        db._ensure_spendlog_columns()
        with eng.connect() as conn:
            cols = {r[1] for r in conn.execute(text("PRAGMA table_info(spendlog)"))}
        assert "task_id" in cols
        # Idempotent: a second call does not error.
        db._ensure_spendlog_columns()
        with eng.connect() as conn:
            cols2 = {r[1] for r in conn.execute(text("PRAGMA table_info(spendlog)"))}
        assert cols2 == cols
    finally:
        db.engine = orig


def test_task_spend_since_filters_by_task_id(eng):
    from backend.safety import governor

    start = datetime.utcnow()
    when = start + timedelta(seconds=1)
    _seed_spend(eng, 4.0, created_at=when, task_id=1)
    _seed_spend(eng, 7.0, created_at=when, task_id=2)
    _seed_spend(eng, 9.0, created_at=when, task_id=None)  # untagged

    # task_id given -> only that task's rows.
    assert governor.task_spend_since(start, task_id=1) == pytest.approx(4.0)
    assert governor.task_spend_since(start, task_id=2) == pytest.approx(7.0)
    # task_id None -> unchanged time-window total (all rows).
    assert governor.task_spend_since(start) == pytest.approx(20.0)


def test_task_a_overspend_does_not_trip_task_b(eng):
    """Task A blowing past the per-task cap must NOT trip task B's check_budget."""
    from backend.safety import governor

    _seed_state(eng, daily=1000.0, per_task=5.0)
    start = datetime.utcnow()
    # Task 101 is way over the per-task cap.
    _seed_spend(eng, 50.0, created_at=start + timedelta(seconds=1), task_id=101)
    # Task 202 has spent nothing.
    assert governor.check_budget(task_id=202, task_start=start) is None
    # Task 101 itself still trips.
    with pytest.raises(governor.BudgetExceeded) as ei:
        governor.check_budget(task_id=101, task_start=start)
    assert ei.value.scope == "per_task"


@pytest.mark.asyncio
async def test_spend_tagged_with_contextvar_task_id(eng):
    """A sonnet call made with the task contextvar SET writes task_id==N; with it
    unset writes NULL. The contextvar does NOT survive the run_in_executor hop;
    this passes because _run captures _current_task_id.get() ON THE LOOP and
    threads it into _record_spend via functools.partial (the documented fallback)."""
    from backend.agents import router

    resp = _usage_resp(text="hi")
    with patch("anthropic.Anthropic") as mock_anthropic:
        mock_client = MagicMock()
        mock_client.messages.create.return_value = resp
        mock_anthropic.return_value = mock_client

        # Set context -> spend tagged with 77.
        token = router.set_task_context(77)
        try:
            await router.sonnet("hi", label="ctx")
        finally:
            router.reset_task_context(token)

        # Unset (default None) -> spend tagged NULL.
        await router.sonnet("hi", label="noctx")

    rows = sorted(_all_spend(eng), key=lambda r: r.id)
    assert len(rows) == 2
    assert rows[0].label == "ctx" and rows[0].task_id == 77
    assert rows[1].label == "noctx" and rows[1].task_id is None


# ---------------------------------------------------------------------------
# SystemState seeding
# ---------------------------------------------------------------------------

def test_systemstate_seeded_once(eng):
    from backend.database import SystemState, _ensure_system_state

    # Row absent initially.
    with Session(eng) as s:
        assert s.get(SystemState, 1) is None

    _ensure_system_state()
    with Session(eng) as s:
        row = s.get(SystemState, 1)
        assert row is not None
        first_updated = row.updated_at

    # Idempotent — a second call does not insert a second row or overwrite.
    _ensure_system_state()
    with Session(eng) as s:
        rows = s.exec(select(SystemState)).all()
        assert len(rows) == 1
        assert rows[0].updated_at == first_updated


# ---------------------------------------------------------------------------
# /api/safety pause / resume / status / budget
# ---------------------------------------------------------------------------

@pytest.fixture
def safety_client(tmp_path, monkeypatch):
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
            c._sched = sched
            yield c
        app.dependency_overrides.clear()


def test_safety_pause_resume_status(safety_client, auth_headers):
    eng = safety_client._engine
    _seed_state(eng, autonomy=True, daily=25.0, per_task=5.0)

    # 401 without a key.
    assert safety_client.post("/api/safety/pause").status_code == 401
    assert safety_client.get("/api/safety/status").status_code == 401

    # pause -> flips flag.
    resp = safety_client.post("/api/safety/pause", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["autonomy_enabled"] is False
    from backend.database import SystemState

    with Session(eng) as s:
        assert s.get(SystemState, 1).autonomy_enabled is False

    # status returns the five documented keys.
    resp = safety_client.get("/api/safety/status", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    for key in ("autonomy_enabled", "today_spend_usd", "daily_budget_usd",
                "per_task_budget_usd", "scheduler_running"):
        assert key in body
    assert body["autonomy_enabled"] is False

    # resume -> flips back.
    resp = safety_client.post("/api/safety/resume", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["autonomy_enabled"] is True
    with Session(eng) as s:
        assert s.get(SystemState, 1).autonomy_enabled is True


def test_safety_pause_calls_scheduler_pause(safety_client, auth_headers):
    eng = safety_client._engine
    _seed_state(eng, autonomy=True)
    sched = safety_client._sched
    sched.running = True

    resp = safety_client.post("/api/safety/pause", headers=auth_headers)
    assert resp.status_code == 200
    sched.pause.assert_called_once()


def test_safety_budget_setter(safety_client, auth_headers):
    eng = safety_client._engine
    _seed_state(eng, autonomy=True, daily=25.0, per_task=5.0)

    resp = safety_client.post(
        "/api/safety/budget",
        headers=auth_headers,
        json={"daily_usd": 50.0, "per_task_usd": 8.0},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["daily_budget_usd"] == 50.0
    assert body["per_task_budget_usd"] == 8.0

    from backend.database import SystemState

    with Session(eng) as s:
        row = s.get(SystemState, 1)
        assert row.daily_budget_usd == 50.0
        assert row.per_task_budget_usd == 8.0
