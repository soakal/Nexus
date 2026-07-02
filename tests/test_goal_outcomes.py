"""Tier B2 — completed-goal outcomes flow into outcome_summary + digest + facts."""
import json
import pytest
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

from sqlmodel import Session, SQLModel, create_engine, select
from sqlalchemy.pool import StaticPool


@pytest.fixture
def eng(monkeypatch):
    import backend.database  # noqa: F401
    e = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(e)
    monkeypatch.setattr("backend.database.engine", e)
    return e


def _seed_running_goal_with_task(eng, result_json, criteria=None):
    from backend.database import Goal, Task
    with Session(eng) as s:
        task = Task(prompt="p", status="success", result_json=result_json)
        s.add(task)
        s.commit()
        s.refresh(task)
        goal = Goal(
            title="Check disk space",
            description="d",
            status="running",
            fingerprint="fp_b2",
            task_id=task.id,
            success_criteria=criteria,
        )
        s.add(goal)
        s.commit()
        s.refresh(goal)
        return goal.id


def _settings(distill=False, criteria_eval=False):
    s = MagicMock()
    s.goal_outcome_distill_llm = distill
    s.success_criteria_eval_enabled = criteria_eval
    return s


def test_summarize_outcome_prefers_summary_field():
    from backend.agents.goals import _summarize_outcome
    raw = json.dumps({"summary": "Cleaned 12 GB\nfrom cache"})
    assert _summarize_outcome("t", raw) == "Cleaned 12 GB from cache"
    assert _summarize_outcome("my goal", None) == "completed: my goal"
    assert _summarize_outcome("my goal", "not json{") == "completed: my goal"


@pytest.mark.asyncio
async def test_reconcile_completed_writes_outcome_summary(eng, monkeypatch):
    from backend.agents import goals
    from backend.database import Goal
    gid = _seed_running_goal_with_task(eng, json.dumps({"summary": "All disks healthy"}))
    monkeypatch.setattr("backend.config.get_settings", lambda: _settings())

    await goals.reconcile_running(backoff_base_seconds=60, max_attempts=5)

    with Session(eng) as s:
        g = s.get(Goal, gid)
    assert g.status == "completed"
    assert g.outcome_summary == "All disks healthy"


@pytest.mark.asyncio
async def test_distill_no_llm_when_flag_off(eng, monkeypatch):
    from backend.agents import goals
    _seed_running_goal_with_task(eng, "{}")
    monkeypatch.setattr("backend.config.get_settings", lambda: _settings(distill=False))

    extract = AsyncMock()
    with patch("backend.agents.facts.extract_and_store", new=extract):
        await goals.reconcile_running(backoff_base_seconds=60, max_attempts=5)
    extract.assert_not_awaited()


@pytest.mark.asyncio
async def test_distill_calls_facts_when_flag_on(eng, monkeypatch):
    from backend.agents import goals
    from backend.database import Goal
    gid = _seed_running_goal_with_task(eng, json.dumps({"summary": "porch light off"}))
    monkeypatch.setattr("backend.config.get_settings", lambda: _settings(distill=True))

    extract = AsyncMock()
    with patch("backend.agents.facts.extract_and_store", new=extract):
        await goals.reconcile_running(backoff_base_seconds=60, max_attempts=5)

    extract.assert_awaited_once()
    args, kwargs = extract.await_args
    assert "porch light off" in args[0]
    assert kwargs.get("source") == "task"
    with Session(eng) as s:
        assert s.get(Goal, gid).status == "completed"


@pytest.mark.asyncio
async def test_distill_never_blocks_completion(eng, monkeypatch):
    from backend.agents import goals
    from backend.database import Goal
    gid = _seed_running_goal_with_task(eng, "{}")
    monkeypatch.setattr("backend.config.get_settings", lambda: _settings(distill=True))

    async def _boom(*a, **k):
        raise RuntimeError("facts exploded")

    with patch("backend.agents.facts.extract_and_store", new=_boom):
        await goals.reconcile_running(backoff_base_seconds=60, max_attempts=5)

    with Session(eng) as s:
        assert s.get(Goal, gid).status == "completed"


@pytest.mark.asyncio
async def test_digest_includes_completed_goals(eng, monkeypatch):
    from backend.database import Goal
    with Session(eng) as s:
        s.add(Goal(title="Tidy DVR", description="d", status="completed",
                   fingerprint="fp_dig", outcome_summary="freed 40 GB",
                   updated_at=datetime.utcnow()))
        s.commit()

    from backend.agents import digest
    text = await digest.build_autonomy_digest()
    assert "Completed (24h): 1" in text
    assert "Tidy DVR: freed 40 GB" in text


# ---------------------------------------------------------------------------
# Tier B1 (NEXUS side) — approval buttons ride the notify chain
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_notify_phone_passes_buttons_through(monkeypatch):
    from backend import events
    s = MagicMock()
    s.phone_notifications_enabled = True
    s.phone_suppressed_kinds = set()
    s.app_base_url = ""
    monkeypatch.setattr("backend.config.get_settings", lambda: s)

    sent = {}

    async def _fake_notify(payload):
        sent.update(payload)
        return True

    with patch("backend.integrations.hermes.notify", new=_fake_notify):
        ok = await events.notify_phone(
            "msg", kind="goal_proposed",
            buttons=[{"text": "✓", "callback_data": "goal:approve:7"}],
        )
    assert ok is True
    assert sent["buttons"] == [{"text": "✓", "callback_data": "goal:approve:7"}]


@pytest.mark.asyncio
async def test_notify_phone_no_buttons_key_when_absent(monkeypatch):
    from backend import events
    s = MagicMock()
    s.phone_notifications_enabled = True
    s.phone_suppressed_kinds = set()
    s.app_base_url = ""
    monkeypatch.setattr("backend.config.get_settings", lambda: s)

    sent = {}

    async def _fake_notify(payload):
        sent.update(payload)
        return True

    with patch("backend.integrations.hermes.notify", new=_fake_notify):
        await events.notify_phone("msg", kind="autonomy_digest")
    assert "buttons" not in sent
