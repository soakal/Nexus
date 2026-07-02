"""Tests for Tier 3 narrow auto-approve policy.

Covers:
  1. is_auto_approvable() pure function — all boundary conditions.
  2. End-to-end proposer tick scenarios:
     a. Low+reversible+autonomous → auto-approved (status=running, Task created).
     b. High risk → stays proposed (no Task).
     c. Irreversible → stays proposed (no Task).
     d. Flag off → stays proposed even for low+reversible (no Task).
     e. Kill switch off → tick skips entirely (no propose, no approve).

Pattern: in-memory StaticPool engine monkeypatched onto backend.database.engine,
matching test_proposer.py / test_goals.py.
"""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

import backend.database  # noqa: F401  — registers all table metadata


# ---------------------------------------------------------------------------
# Shared fixtures / helpers
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


def _fake_fetch():
    return SimpleNamespace(
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


def _mock_integrations(monkeypatch):
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


def _mock_pool(monkeypatch):
    """Patch get_pool so goals.approve() doesn't actually run a worker task."""
    pool = MagicMock()
    pool.enqueue = AsyncMock()
    monkeypatch.setattr("backend.agents.goals.get_pool", lambda: pool)
    return pool


def _make_settings(auto_approve: bool, max_per_tick: int = 3):
    s = MagicMock()
    s.proposer_max_per_tick = max_per_tick
    s.goal_ttl_seconds = 86400
    s.goal_debounce_seconds = 3600
    s.auto_approve_low_risk = auto_approve
    return s


# ---------------------------------------------------------------------------
# Part 1 — is_auto_approvable() pure function tests
# ---------------------------------------------------------------------------

class TestIsAutoApprovable:
    """Pure policy: default-deny. True ONLY when ALL four conditions hold."""

    def _call(self, **kwargs):
        from backend.agents.goals import is_auto_approvable
        return is_auto_approvable(**kwargs)

    # Positive cases — the gate opens only when all four hold.

    def test_low_reversible_autonomous_enabled_true(self):
        """low + reversible + autonomous + enabled=True → True (the only True case)."""
        assert self._call(
            goal={"actor": "autonomous", "risk": "low", "reversibility": "reversible"},
            enabled=True,
        ) is True

    def test_low_reversible_by_inverse_autonomous_enabled_true(self):
        """reversible_by_inverse also counts as reversible."""
        assert self._call(
            goal={"actor": "autonomous", "risk": "low", "reversibility": "reversible_by_inverse"},
            enabled=True,
        ) is True

    # Flag-off — everything blocked regardless of other fields.

    def test_flag_off_blocks_low_reversible_autonomous(self):
        """enabled=False → False even when all other conditions are met."""
        assert self._call(
            goal={"actor": "autonomous", "risk": "low", "reversibility": "reversible"},
            enabled=False,
        ) is False

    # Actor guard — human-proposed goals must NEVER be auto-approved.

    def test_human_actor_low_reversible_enabled_false(self):
        """actor='user' (human) → False regardless of risk/reversibility/flag."""
        assert self._call(
            goal={"actor": "user", "risk": "low", "reversibility": "reversible"},
            enabled=True,
        ) is False

    def test_actor_user_medium_irreversible(self):
        """Sanity: human + medium + irreversible → False."""
        assert self._call(
            goal={"actor": "user", "risk": "medium", "reversibility": "irreversible"},
            enabled=True,
        ) is False

    # Risk guard — medium and high are always blocked.

    def test_medium_risk_autonomous_reversible(self):
        """risk='medium' → False (only 'low' is auto-approvable)."""
        assert self._call(
            goal={"actor": "autonomous", "risk": "medium", "reversibility": "reversible"},
            enabled=True,
        ) is False

    def test_high_risk_autonomous_reversible(self):
        """risk='high' → False."""
        assert self._call(
            goal={"actor": "autonomous", "risk": "high", "reversibility": "reversible"},
            enabled=True,
        ) is False

    # Reversibility guard — irreversible and unknown are always blocked.

    def test_irreversible_low_autonomous(self):
        """reversibility='irreversible' → False regardless of low risk."""
        assert self._call(
            goal={"actor": "autonomous", "risk": "low", "reversibility": "irreversible"},
            enabled=True,
        ) is False

    def test_unknown_reversibility_low_autonomous(self):
        """reversibility='unknown' → False (unknown is default-deny)."""
        assert self._call(
            goal={"actor": "autonomous", "risk": "low", "reversibility": "unknown"},
            enabled=True,
        ) is False

    # Missing fields (defensive — treats missing as unsafe).

    def test_missing_actor_field(self):
        """Missing actor → False (str(None) != 'autonomous')."""
        assert self._call(
            goal={"risk": "low", "reversibility": "reversible"},
            enabled=True,
        ) is False

    def test_missing_risk_field(self):
        """Missing risk → False."""
        assert self._call(
            goal={"actor": "autonomous", "reversibility": "reversible"},
            enabled=True,
        ) is False

    def test_missing_reversibility_field(self):
        """Missing reversibility → False."""
        assert self._call(
            goal={"actor": "autonomous", "risk": "low"},
            enabled=True,
        ) is False

    def test_empty_goal_dict(self):
        """Empty goal dict → False."""
        assert self._call(goal={}, enabled=True) is False

    # Physical-security / climate keyword guard (mirrors _HA_HIGH_DOMAINS in broker).
    # Uses word-boundary regex so "blocked", "discovery", "coverage" are NOT matched.

    def test_lock_keyword_in_title_blocked(self):
        """'lock' as a word in title → False (exact failing goal title)."""
        assert self._call(
            goal={"actor": "autonomous", "risk": "low", "reversibility": "reversible",
                  "title": "Lock the back door"},
            enabled=True,
        ) is False

    def test_unlock_keyword_in_description_blocked(self):
        """'unlock' as a word in description → False."""
        assert self._call(
            goal={"actor": "autonomous", "risk": "low", "reversibility": "reversible",
                  "title": "Secure entry", "description": "unlock the front door"},
            enabled=True,
        ) is False

    def test_alarm_keyword_blocked(self):
        """'alarm' as a word in title → False."""
        assert self._call(
            goal={"actor": "autonomous", "risk": "low", "reversibility": "reversible",
                  "title": "Arm the alarm system"},
            enabled=True,
        ) is False

    def test_cover_keyword_blocked(self):
        """'cover' as a word (garage door HA domain) → False."""
        assert self._call(
            goal={"actor": "autonomous", "risk": "low", "reversibility": "reversible",
                  "title": "Close the garage door",
                  "description": "cover.close on cover.garage_door_garage_door"},
            enabled=True,
        ) is False

    def test_climate_keyword_blocked(self):
        """'climate' as a word (thermostat HA domain) → False."""
        assert self._call(
            goal={"actor": "autonomous", "risk": "low", "reversibility": "reversible",
                  "title": "Set the climate to 70F"},
            enabled=True,
        ) is False

    def test_blocked_dns_not_false_positive(self):
        """'blocked' contains 'lock' as substring but NOT as a word — must NOT be blocked."""
        assert self._call(
            goal={"actor": "autonomous", "risk": "low", "reversibility": "reversible",
                  "title": "Investigate rising blocked DNS percentage"},
            enabled=True,
        ) is True

    def test_discovery_not_false_positive(self):
        """'discovery' contains 'cover' as substring but NOT as a word → not blocked."""
        assert self._call(
            goal={"actor": "autonomous", "risk": "low", "reversibility": "reversible",
                  "title": "Run service discovery on LAN"},
            enabled=True,
        ) is True

    def test_light_title_not_blocked(self):
        """Non-security goal (turn off light) is still auto-approvable."""
        assert self._call(
            goal={"actor": "autonomous", "risk": "low", "reversibility": "reversible",
                  "title": "Turn off porch light"},
            enabled=True,
        ) is True


# ---------------------------------------------------------------------------
# Part 2 — End-to-end proposer tick tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_e2e_low_reversible_autonomous_auto_approved(eng, monkeypatch):
    """E2E: Opus proposes one low+reversible autonomous goal with
    auto_approve_low_risk=True → tick auto-approves it → goal status='running',
    Task row created, count_auto_approved==1."""
    _seed_state(eng, autonomy=True)
    _mock_integrations(monkeypatch)
    pool = _mock_pool(monkeypatch)

    opus_response = json.dumps([
        {
            "title": "Prune stale Docker images",
            "description": "Run docker image prune to free disk on Unraid.",
            "risk": "low",
            "reversibility": "reversible",
            "confidence": 0.9,
        }
    ])

    with patch("backend.agents.router.sonnet", new=AsyncMock(return_value=opus_response)):
        with patch("backend.config.get_settings", return_value=_make_settings(auto_approve=True)):
            from backend.agents.proposer import propose_goals_tick
            result = await propose_goals_tick()

    assert result["status"] == "ok"
    assert result["count_auto_approved"] == 1
    # count_proposed counts goals still in 'proposed' state (not auto-approved ones).
    assert result["count_proposed"] == 0

    all_g = _all_goals(eng)
    assert len(all_g) == 1
    assert all_g[0].status == "running", (
        "Auto-approved goal should be in 'running' status"
    )
    assert all_g[0].actor == "autonomous"

    # A Task row must have been created.
    tasks = _all_tasks(eng)
    assert len(tasks) == 1, "Auto-approve must create a Task row"

    # pool.enqueue called exactly once.
    pool.enqueue.assert_awaited_once()


@pytest.mark.asyncio
async def test_e2e_reversible_by_inverse_also_auto_approved(eng, monkeypatch):
    """reversible_by_inverse is also auto-approvable."""
    _seed_state(eng, autonomy=True)
    _mock_integrations(monkeypatch)
    _mock_pool(monkeypatch)

    opus_response = json.dumps([
        {
            "title": "Toggle AdGuard filtering",
            "description": "Disable then re-enable AdGuard filtering to clear stale cache.",
            "risk": "low",
            "reversibility": "reversible_by_inverse",
            "confidence": 0.8,
        }
    ])

    with patch("backend.agents.router.sonnet", new=AsyncMock(return_value=opus_response)):
        with patch("backend.config.get_settings", return_value=_make_settings(auto_approve=True)):
            from backend.agents.proposer import propose_goals_tick
            result = await propose_goals_tick()

    assert result["status"] == "ok"
    assert result["count_auto_approved"] == 1
    all_g = _all_goals(eng)
    assert all_g[0].status == "running"


@pytest.mark.asyncio
async def test_e2e_high_risk_stays_proposed(eng, monkeypatch):
    """E2E: Opus proposes a HIGH-risk goal → stays 'proposed', no Task created,
    count_auto_approved==0 even when auto_approve_low_risk=True."""
    _seed_state(eng, autonomy=True)
    _mock_integrations(monkeypatch)
    _mock_pool(monkeypatch)

    opus_response = json.dumps([
        {
            "title": "Wipe Unraid cache drive",
            "description": "Format the Unraid cache drive to reclaim space.",
            "risk": "high",
            "reversibility": "irreversible",
            "confidence": 0.7,
        }
    ])

    with patch("backend.agents.router.sonnet", new=AsyncMock(return_value=opus_response)):
        with patch("backend.config.get_settings", return_value=_make_settings(auto_approve=True)):
            from backend.agents.proposer import propose_goals_tick
            result = await propose_goals_tick()

    assert result["status"] == "ok"
    assert result["count_auto_approved"] == 0
    assert result["count_proposed"] == 1  # stays proposed

    all_g = _all_goals(eng)
    assert len(all_g) == 1
    assert all_g[0].status == "proposed", "High-risk goal must stay 'proposed'"

    # No Task rows — nothing dispatched.
    assert _all_tasks(eng) == []


@pytest.mark.asyncio
async def test_e2e_irreversible_stays_proposed(eng, monkeypatch):
    """E2E: Opus proposes a low-risk but IRREVERSIBLE goal → stays 'proposed',
    no Task created. Irreversibility blocks auto-approve regardless of risk level."""
    _seed_state(eng, autonomy=True)
    _mock_integrations(monkeypatch)
    _mock_pool(monkeypatch)

    opus_response = json.dumps([
        {
            "title": "Delete old log files",
            "description": "Permanently delete log files older than 1 year.",
            "risk": "low",
            "reversibility": "irreversible",
            "confidence": 0.85,
        }
    ])

    with patch("backend.agents.router.sonnet", new=AsyncMock(return_value=opus_response)):
        with patch("backend.config.get_settings", return_value=_make_settings(auto_approve=True)):
            from backend.agents.proposer import propose_goals_tick
            result = await propose_goals_tick()

    assert result["status"] == "ok"
    assert result["count_auto_approved"] == 0
    assert result["count_proposed"] == 1

    all_g = _all_goals(eng)
    assert all_g[0].status == "proposed", (
        "Irreversible goal must stay 'proposed' regardless of low risk"
    )
    assert _all_tasks(eng) == []


@pytest.mark.asyncio
async def test_e2e_flag_off_low_reversible_stays_proposed(eng, monkeypatch):
    """E2E: auto_approve_low_risk=False → even a low+reversible autonomous goal
    stays 'proposed'. The flag is the master switch."""
    _seed_state(eng, autonomy=True)
    _mock_integrations(monkeypatch)
    _mock_pool(monkeypatch)

    opus_response = json.dumps([
        {
            "title": "Restart Plex container",
            "description": "Restart the Plex Docker container to apply a settings change.",
            "risk": "low",
            "reversibility": "reversible",
            "confidence": 0.88,
        }
    ])

    with patch("backend.agents.router.sonnet", new=AsyncMock(return_value=opus_response)):
        # auto_approve_low_risk = False
        with patch("backend.config.get_settings", return_value=_make_settings(auto_approve=False)):
            from backend.agents.proposer import propose_goals_tick
            result = await propose_goals_tick()

    assert result["status"] == "ok"
    assert result["count_auto_approved"] == 0
    assert result["count_proposed"] == 1  # stays proposed

    all_g = _all_goals(eng)
    assert all_g[0].status == "proposed", (
        "With auto_approve_low_risk=False, low+reversible goal must stay 'proposed'"
    )
    assert _all_tasks(eng) == []


@pytest.mark.asyncio
async def test_e2e_kill_switch_off_skips_no_approve(eng, monkeypatch):
    """E2E: autonomy_enabled=False → tick skips entirely; Opus never called,
    no Goals proposed, no approvals, no Tasks."""
    _seed_state(eng, autonomy=False)
    _mock_integrations(monkeypatch)

    opus_mock = AsyncMock(return_value="[]")
    with patch("backend.agents.router.sonnet", new=opus_mock):
        from backend.agents.proposer import propose_goals_tick
        result = await propose_goals_tick()

    assert result["status"] == "skipped"
    assert result.get("reason") == "autonomy_disabled"
    opus_mock.assert_not_awaited()
    assert _all_goals(eng) == []
    assert _all_tasks(eng) == []


@pytest.mark.asyncio
async def test_e2e_mixed_proposals_selective_auto_approve(eng, monkeypatch):
    """E2E: Opus proposes three goals — low+reversible, medium+reversible, low+irreversible.
    Only the low+reversible one is auto-approved; the others stay proposed."""
    _seed_state(eng, autonomy=True)
    _mock_integrations(monkeypatch)
    _mock_pool(monkeypatch)

    opus_response = json.dumps([
        {
            "title": "Refresh DNS cache",
            "description": "Flush the AdGuard DNS cache to pick up new rules.",
            "risk": "low",
            "reversibility": "reversible",
            "confidence": 0.9,
        },
        {
            "title": "Restart Unraid array",
            "description": "Stop and restart the Unraid storage array.",
            "risk": "medium",
            "reversibility": "reversible",
            "confidence": 0.7,
        },
        {
            "title": "Purge old backups",
            "description": "Delete Unraid backups older than 6 months.",
            "risk": "low",
            "reversibility": "irreversible",
            "confidence": 0.75,
        },
    ])

    with patch("backend.agents.router.sonnet", new=AsyncMock(return_value=opus_response)):
        with patch("backend.config.get_settings", return_value=_make_settings(auto_approve=True, max_per_tick=3)):
            from backend.agents.proposer import propose_goals_tick
            result = await propose_goals_tick()

    assert result["status"] == "ok"
    assert result["count_auto_approved"] == 1   # only "Refresh DNS cache"
    assert result["count_proposed"] == 2         # medium+rev and low+irrev stay proposed

    all_g = _all_goals(eng)
    assert len(all_g) == 3
    by_title = {g.title: g.status for g in all_g}
    assert by_title["Refresh DNS cache"] == "running", "low+reversible must be auto-approved"
    assert by_title["Restart Unraid array"] == "proposed", "medium-risk must stay proposed"
    assert by_title["Purge old backups"] == "proposed", "irreversible must stay proposed"

    # Exactly one Task row.
    tasks = _all_tasks(eng)
    assert len(tasks) == 1
