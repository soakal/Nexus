"""Tests for the Tier 3 autonomous goal proposer with narrow auto-approve.

Pattern: in-memory StaticPool engine monkeypatched onto backend.database.engine,
matching test_governor.py / test_goals.py.

SAFETY CONTRACT assertions are spread across every test:
  - router.haiku is the ONLY LLM function called.
  - The proposer MAY call goals.approve(), but ONLY for low-risk reversible autonomous
    goals when auto_approve_low_risk is True. All other goals stay 'proposed'.
  - The proposer NEVER calls execute_action, run_task, or get_pool directly.
"""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

# Register all table metadata before any test runs.
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
# Helpers
# ---------------------------------------------------------------------------

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


def _all_goals(eng):
    from backend.database import Goal
    with Session(eng) as s:
        return s.exec(select(Goal)).all()


def _all_tasks(eng):
    from backend.database import Task
    with Session(eng) as s:
        return s.exec(select(Task)).all()


# Minimal fake fetch results that _build_snapshot can handle.
def _fake_fetch():
    obj = SimpleNamespace(
        entities=[],
        alerts=[],
        docker_containers=[],
        array_status="started",
        storage_used_gb=1.0,
        storage_total_gb=10.0,
        recording_now=[],
        blocked_today=0,
        blocked_pct=0.0,
        filtering_enabled=True,
        summary="Clear, 70°F",
    )
    return obj


def _mock_integrations(monkeypatch):
    """Patch all five integration fetch() calls to return a fake object."""
    fake = _fake_fetch()

    async def _fetch(*a, **k):
        return fake

    for mod_path in (
        "backend.integrations.homeassistant.fetch",
        "backend.integrations.unraid.fetch",
        "backend.integrations.channels_dvr.fetch",
        "backend.integrations.adguard.fetch",
        "backend.integrations.weather.fetch",
    ):
        monkeypatch.setattr(mod_path, _fetch)


# ---------------------------------------------------------------------------
# Test 1 — Kill switch: autonomy disabled → tick skips; no Opus call; no DB rows
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_kill_switch_skips_everything(eng, monkeypatch):
    """When autonomy_enabled is False the tick must return 'skipped' without
    calling Opus and without inserting any Goal or Task rows."""
    _seed_state(eng, autonomy=False)
    _mock_integrations(monkeypatch)

    opus_mock = AsyncMock(return_value="[]")
    with patch("backend.agents.router.haiku", new=opus_mock):
        from backend.agents.proposer import propose_goals_tick
        result = await propose_goals_tick()

    assert result["status"] == "skipped"
    assert result.get("reason") == "autonomy_disabled"
    # SAFETY: Opus must NEVER be called when autonomy is off.
    opus_mock.assert_not_awaited()
    # SAFETY: no Goal or Task rows created.
    assert _all_goals(eng) == []
    assert _all_tasks(eng) == []


# ---------------------------------------------------------------------------
# Test 2 — Happy path: 2 valid proposals → 2 'proposed' Goal rows, actor='autonomous'
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_happy_path_two_proposals(eng, monkeypatch):
    """With autonomy on and auto_approve_low_risk=False, both proposals stay
    status='proposed' with actor='autonomous'. No Task rows created."""
    _seed_state(eng, autonomy=True)
    _mock_integrations(monkeypatch)

    opus_response = json.dumps([
        {
            "title": "Clean up old Docker images",
            "description": "Run docker system prune to free disk space on Unraid.",
            "success_criteria": "docker system df shows reclaimable space under 1 GB.",
            "risk": "low",
            "reversibility": "reversible",
            "confidence": 0.85,
        },
        {
            "title": "Review stale GitHub PRs",
            "description": "Check open PRs older than 48 hours and leave review comments.",
            "success_criteria": "Every PR older than 48h has at least one review comment.",
            "risk": "low",
            "reversibility": "reversible",
            "confidence": 0.75,
        },
    ])

    with patch("backend.agents.router.haiku", new=AsyncMock(return_value=opus_response)):
        # Also patch config so cap doesn't interfere; disable auto-approve so
        # goals stay proposed (auto-approve is tested separately in test 7 and
        # test_auto_approve.py).
        with patch("backend.config.get_settings") as mock_settings:
            s = MagicMock()
            s.proposer_max_per_tick = 3
            s.goal_ttl_seconds = 86400
            s.goal_debounce_seconds = 3600
            s.auto_approve_low_risk = False
            mock_settings.return_value = s

            from backend.agents.proposer import propose_goals_tick
            result = await propose_goals_tick()

    assert result["status"] == "ok"
    assert result["count_proposed"] == 2

    goals = _all_goals(eng)
    assert len(goals) == 2
    for g in goals:
        assert g.status == "proposed"
        assert g.actor == "autonomous"

    # SAFETY: no Task rows (auto-approve disabled).
    assert _all_tasks(eng) == []


# ---------------------------------------------------------------------------
# Test 3 — Empty proposal array → zero goals, status 'ok'
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_empty_proposal_creates_no_goals(eng, monkeypatch):
    _seed_state(eng, autonomy=True)
    _mock_integrations(monkeypatch)

    with patch("backend.agents.router.haiku", new=AsyncMock(return_value="[]")):
        with patch("backend.config.get_settings") as mock_settings:
            s = MagicMock()
            s.proposer_max_per_tick = 3
            s.goal_ttl_seconds = 86400
            s.goal_debounce_seconds = 3600
            mock_settings.return_value = s

            from backend.agents.proposer import propose_goals_tick
            result = await propose_goals_tick()

    assert result["status"] == "ok"
    assert result["count_proposed"] == 0
    assert _all_goals(eng) == []


# ---------------------------------------------------------------------------
# Test 4 — Cap: Opus returns 5 items but max_per_tick=3 → at most 3 created
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cap_limits_proposals(eng, monkeypatch):
    _seed_state(eng, autonomy=True)
    _mock_integrations(monkeypatch)

    five_goals = json.dumps([
        {
            "title": f"Goal {i}",
            "description": f"Description for goal {i}.",
            "success_criteria": f"Goal {i} verifiably done.",
            "risk": "low",
            "reversibility": "reversible",
            "confidence": 0.7,
        }
        for i in range(5)
    ])

    with patch("backend.agents.router.haiku", new=AsyncMock(return_value=five_goals)):
        with patch("backend.config.get_settings") as mock_settings:
            s = MagicMock()
            s.proposer_max_per_tick = 3
            s.goal_ttl_seconds = 86400
            s.goal_debounce_seconds = 3600
            s.auto_approve_low_risk = False  # isolate cap behavior, not auto-approve
            mock_settings.return_value = s

            from backend.agents.proposer import propose_goals_tick
            result = await propose_goals_tick()

    assert result["status"] == "ok"
    # At most 3 goals should exist.
    goals = _all_goals(eng)
    assert len(goals) <= 3
    assert result["count_proposed"] <= 3


# ---------------------------------------------------------------------------
# Test 5 — Dedup: same opus output across two ticks → 2nd tick debounced
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dedup_second_tick_debounced(eng, monkeypatch):
    """Proposing the same title+description twice produces only ONE Goal row.
    The second tick returns status='debounced' for that item."""
    _seed_state(eng, autonomy=True)
    _mock_integrations(monkeypatch)

    single_goal = json.dumps([
        {
            "title": "Clean up old Docker images",
            "description": "Run docker system prune to free disk space on Unraid.",
            "success_criteria": "docker system df shows reclaimable space under 1 GB.",
            "risk": "low",
            "reversibility": "reversible",
            "confidence": 0.85,
        }
    ])

    def _make_settings():
        s = MagicMock()
        s.proposer_max_per_tick = 3
        s.goal_ttl_seconds = 86400
        # Zero debounce_seconds so ONLY the duplicate_active guard fires.
        # (The active goal from tick 1 is still 'proposed', so tick 2 hits
        # the duplicate_active debounce regardless of cooldown.)
        s.goal_debounce_seconds = 0
        s.auto_approve_low_risk = False  # isolate dedup behavior
        return s

    with patch("backend.agents.router.haiku", new=AsyncMock(return_value=single_goal)):
        with patch("backend.config.get_settings", side_effect=_make_settings):
            from backend.agents.proposer import propose_goals_tick

            # Tick 1: should create the goal.
            result1 = await propose_goals_tick()
            assert result1["status"] == "ok"
            assert result1["count_proposed"] == 1

            # Tick 2: same output → duplicate_active debounce.
            result2 = await propose_goals_tick()
            assert result2["status"] == "ok"
            # The item should be debounced, not proposed again.
            assert result2["count_proposed"] == 0
            debounced_items = [r for r in result2["results"] if r["status"] == "debounced"]
            assert len(debounced_items) == 1

    # Only ONE goal row should exist.
    goals = _all_goals(eng)
    assert len(goals) == 1
    assert goals[0].status == "proposed"


# ---------------------------------------------------------------------------
# Test 6 — Best-effort: Opus raises RuntimeError → tick returns 'error', doesn't raise
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_best_effort_on_opus_error(eng, monkeypatch):
    _seed_state(eng, autonomy=True)
    _mock_integrations(monkeypatch)

    with patch("backend.agents.router.haiku", new=AsyncMock(side_effect=RuntimeError("network error"))):
        with patch("backend.config.get_settings") as mock_settings:
            s = MagicMock()
            s.proposer_max_per_tick = 3
            s.goal_ttl_seconds = 86400
            s.goal_debounce_seconds = 3600
            mock_settings.return_value = s

            from backend.agents.proposer import propose_goals_tick
            # Must NOT raise.
            result = await propose_goals_tick()

    assert result["status"] == "error"
    assert "error" in result
    # No Goal rows created.
    assert _all_goals(eng) == []


# ---------------------------------------------------------------------------
# Test 7 — Selective auto-approve: medium-risk goal stays 'proposed'; low+reversible
#           goal is auto-approved; proposer module never calls forbidden names directly.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_selective_auto_approve_safety(eng, monkeypatch):
    """Updated safety assertion: with auto_approve_low_risk=True, a MEDIUM-risk goal
    must stay 'proposed' (no task created for it), while a LOW+reversible autonomous
    goal is auto-approved (task created). The proposer must NEVER call
    execute_action, run_task, or get_pool directly (verified via AST walk)."""
    _seed_state(eng, autonomy=True)
    _mock_integrations(monkeypatch)

    # Patch get_pool so approve() doesn't actually run a task.
    pool_mock = MagicMock()
    pool_mock.enqueue = AsyncMock()
    monkeypatch.setattr("backend.agents.goals.get_pool", lambda: pool_mock)

    opus_response = json.dumps([
        {
            "title": "Reboot Jellyfin container",
            "description": "Restart the Jellyfin Docker container on Unraid to clear a memory leak.",
            "success_criteria": "Jellyfin responds to requests after the restart.",
            "risk": "medium",
            "reversibility": "reversible_by_inverse",
            "confidence": 0.8,
        },
        {
            "title": "Archive old recordings",
            "description": "Move Channels DVR recordings older than 90 days to cold storage.",
            "success_criteria": "No recordings older than 90 days remain in the DVR library.",
            "risk": "low",
            "reversibility": "reversible",
            "confidence": 0.9,
        },
    ])

    with patch("backend.agents.router.haiku", new=AsyncMock(return_value=opus_response)):
        with patch("backend.config.get_settings") as mock_settings:
            s = MagicMock()
            s.proposer_max_per_tick = 3
            s.goal_ttl_seconds = 86400
            s.goal_debounce_seconds = 3600
            s.auto_approve_low_risk = True
            mock_settings.return_value = s

            from backend.agents.proposer import propose_goals_tick
            result = await propose_goals_tick()

    assert result["status"] == "ok"
    # count_auto_approved is now part of the return dict.
    assert result["count_auto_approved"] == 1  # only the low+reversible one
    assert result["count_proposed"] == 1       # medium-risk stays proposed

    all_g = _all_goals(eng)
    assert len(all_g) == 2
    statuses = {g.title: g.status for g in all_g}
    assert statuses["Reboot Jellyfin container"] == "proposed", (
        "Medium-risk goal must stay 'proposed' — proposer must not auto-approve medium+ risk"
    )
    assert statuses["Archive old recordings"] == "running", (
        "Low+reversible autonomous goal should be auto-approved (→ running)"
    )

    # Low+reversible goal produced a Task row; medium-risk goal did NOT.
    tasks = _all_tasks(eng)
    assert len(tasks) == 1, "Only the auto-approved low+reversible goal should create a Task"

    # SAFETY: verify at the AST level that proposer CALLS nothing forbidden.
    # approve() is now allowed; but execute_action/run_task/get_pool are still banned.
    import backend.agents.proposer as proposer_mod
    import ast, inspect, textwrap
    source = inspect.getsource(proposer_mod)
    tree = ast.parse(textwrap.dedent(source))
    called_attrs = set()
    called_names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute):
                called_attrs.add(func.attr)
            elif isinstance(func, ast.Name):
                called_names.add(func.id)
    all_calls = called_attrs | called_names
    # "approve" is now allowed (via goals.approve); only these three stay forbidden.
    forbidden_calls = {"execute_action", "run_task", "get_pool"}
    for name in forbidden_calls:
        assert name not in all_calls, (
            f"proposer.py must not CALL '{name}' directly (safety contract violation)"
        )


# ---------------------------------------------------------------------------
# Test 8 — BudgetExceeded from Opus → returns skipped/budget, no goals
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_budget_exceeded_skips_gracefully(eng, monkeypatch):
    """If Opus raises BudgetExceeded (daily cap hit), the tick returns
    status='skipped' with reason='budget' and creates no Goal rows."""
    _seed_state(eng, autonomy=True)
    _mock_integrations(monkeypatch)

    from backend.safety.governor import BudgetExceeded

    with patch("backend.agents.router.haiku", new=AsyncMock(side_effect=BudgetExceeded("daily", 30.0, 25.0))):
        with patch("backend.config.get_settings") as mock_settings:
            s = MagicMock()
            s.proposer_max_per_tick = 3
            s.goal_ttl_seconds = 86400
            s.goal_debounce_seconds = 3600
            mock_settings.return_value = s

            from backend.agents.proposer import propose_goals_tick
            result = await propose_goals_tick()

    assert result["status"] == "skipped"
    assert result.get("reason") == "budget"
    assert _all_goals(eng) == []


# ---------------------------------------------------------------------------
# Test 9 — Goals without success_criteria are dropped (Tier A2.2)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_proposal_without_success_criteria_dropped(eng, monkeypatch):
    """One goal missing success_criteria + one with it -> only the complete
    one is created."""
    _seed_state(eng, autonomy=True)
    _mock_integrations(monkeypatch)

    response = json.dumps([
        {
            "title": "No criteria goal",
            "description": "This goal has no done-condition.",
            "risk": "low",
            "reversibility": "reversible",
            "confidence": 0.8,
        },
        {
            "title": "Complete goal",
            "description": "This goal has a checkable done-condition.",
            "success_criteria": "The thing is verifiably done.",
            "risk": "low",
            "reversibility": "reversible",
            "confidence": 0.8,
        },
    ])

    with patch("backend.agents.router.haiku", new=AsyncMock(return_value=response)):
        with patch("backend.config.get_settings") as mock_settings:
            s = MagicMock()
            s.proposer_max_per_tick = 3
            s.goal_ttl_seconds = 86400
            s.goal_debounce_seconds = 3600
            s.auto_approve_low_risk = False
            mock_settings.return_value = s

            from backend.agents.proposer import propose_goals_tick
            result = await propose_goals_tick()

    assert result["status"] == "ok"
    assert result["count_proposed"] == 1
    titles = [g.title for g in _all_goals(eng)]
    assert titles == ["Complete goal"]


# ---------------------------------------------------------------------------
# Test 10 — success_criteria is persisted onto the Goal row (Tier A2.2)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_success_criteria_persisted(eng, monkeypatch):
    _seed_state(eng, autonomy=True)
    _mock_integrations(monkeypatch)

    response = json.dumps([
        {
            "title": "Persisted criteria goal",
            "description": "A goal whose criteria must land on the row.",
            "success_criteria": "AdGuard filtering is re-enabled.",
            "risk": "low",
            "reversibility": "reversible",
            "confidence": 0.9,
        },
    ])

    with patch("backend.agents.router.haiku", new=AsyncMock(return_value=response)):
        with patch("backend.config.get_settings") as mock_settings:
            s = MagicMock()
            s.proposer_max_per_tick = 3
            s.goal_ttl_seconds = 86400
            s.goal_debounce_seconds = 3600
            s.auto_approve_low_risk = False
            mock_settings.return_value = s

            from backend.agents.proposer import propose_goals_tick
            await propose_goals_tick()

    goals_rows = _all_goals(eng)
    assert len(goals_rows) == 1
    assert goals_rows[0].success_criteria == "AdGuard filtering is re-enabled."


# ---------------------------------------------------------------------------
# Test 11 — Proposer bills Haiku, never Sonnet (Tier A2.3)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_uses_haiku_not_sonnet(eng, monkeypatch):
    _seed_state(eng, autonomy=True)
    _mock_integrations(monkeypatch)

    haiku_mock = AsyncMock(return_value="[]")
    sonnet_mock = AsyncMock(side_effect=AssertionError("proposer must not call sonnet"))

    with patch("backend.agents.router.haiku", new=haiku_mock), \
         patch("backend.agents.router.sonnet", new=sonnet_mock):
        with patch("backend.config.get_settings") as mock_settings:
            s = MagicMock()
            s.proposer_max_per_tick = 3
            s.goal_ttl_seconds = 86400
            s.goal_debounce_seconds = 3600
            mock_settings.return_value = s

            from backend.agents.proposer import propose_goals_tick
            result = await propose_goals_tick()

    assert result["status"] == "ok"
    haiku_mock.assert_awaited_once()
    sonnet_mock.assert_not_awaited()
