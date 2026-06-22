"""Tests for the enriched autonomous proposer (Tier 3 — w33gixx93 gap closure).

Covers:
  1. Goal.rejection_reason field: created and read back via SQLModel.
  2. goals.reject(reason=...) persists rejection_reason.
  3. _db_recent_abandoned returns abandoned goals with rejection_reason, newest first.
  4. propose_goals_tick injects RECENT TRENDS, UPTIME ANOMALIES, DO NOT RE-PROPOSE
     into the Opus prompt when seed data is present.
  5. Empty case: no trends/anomalies/abandoned → prompt sections show "(none)" but
     the tick still completes ok.

Pattern: in-memory StaticPool engine monkeypatched onto backend.database.engine,
matching test_proposer.py / test_goals.py.
"""
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

# Register all table metadata (including Goal, TrendSnapshot, UptimeSample) before tests.
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
# Helpers shared with test_proposer.py pattern
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


def _make_settings(auto_approve: bool = False, max_per_tick: int = 3):
    s = MagicMock()
    s.proposer_max_per_tick = max_per_tick
    s.goal_ttl_seconds = 86400
    s.goal_debounce_seconds = 3600
    s.auto_approve_low_risk = auto_approve
    return s


# ---------------------------------------------------------------------------
# Test 1 — Goal.rejection_reason: field present, can be set and read back
# ---------------------------------------------------------------------------

def test_goal_rejection_reason_field(eng):
    """Goal rows can be created with rejection_reason=None and later updated."""
    from backend.database import Goal

    with Session(eng) as s:
        g = Goal(
            title="Test goal",
            description="Some description.",
            status="proposed",
            fingerprint="aabbccdd11223344",
        )
        s.add(g)
        s.commit()
        s.refresh(g)
        goal_id = g.id
        # Default is None
        assert g.rejection_reason is None

    # Set rejection_reason
    with Session(eng) as s:
        g = s.get(Goal, goal_id)
        g.rejection_reason = "not useful"
        g.status = "abandoned"
        s.add(g)
        s.commit()
        s.refresh(g)
        assert g.rejection_reason == "not useful"
        assert g.status == "abandoned"


# ---------------------------------------------------------------------------
# Test 2 — goals.reject(reason=...) persists rejection_reason + status=abandoned
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_reject_with_reason_persists(eng):
    """reject(goal_id, reason='not useful') sets status=abandoned and rejection_reason."""
    from backend.agents import goals
    from backend.database import Goal

    result = await goals.propose(
        "Restart plex",
        "Restart the Plex Docker container.",
    )
    assert result["status"] == "proposed"
    goal_id = result["goal"]["id"]

    r = await goals.reject(goal_id, reason="not useful")
    assert r["status"] == "abandoned"

    with Session(eng) as s:
        g = s.get(Goal, goal_id)
    assert g.status == "abandoned"
    assert g.rejection_reason == "not useful"


@pytest.mark.asyncio
async def test_reject_without_reason_still_abandons(eng):
    """reject(goal_id) with no reason leaves rejection_reason as None."""
    from backend.agents import goals
    from backend.database import Goal

    result = await goals.propose("Buy coffee", "Get coffee beans.")
    goal_id = result["goal"]["id"]

    r = await goals.reject(goal_id)
    assert r["status"] == "abandoned"

    with Session(eng) as s:
        g = s.get(Goal, goal_id)
    assert g.status == "abandoned"
    assert g.rejection_reason is None


# ---------------------------------------------------------------------------
# Test 3 — _db_recent_abandoned returns abandoned goals with rejection_reason
# ---------------------------------------------------------------------------

def test_db_recent_abandoned(eng):
    """_db_recent_abandoned returns abandoned goals newest-updated-first with rejection_reason."""
    from backend.agents.goals import _db_recent_abandoned
    from backend.database import Goal

    now = datetime.utcnow()
    with Session(eng) as s:
        g1 = Goal(
            title="Restart plex",
            description="Restart Plex.",
            status="abandoned",
            fingerprint="fp001",
            rejection_reason="manual",
            updated_at=now - timedelta(minutes=5),
        )
        g2 = Goal(
            title="Check logs",
            description="Review logs.",
            status="abandoned",
            fingerprint="fp002",
            rejection_reason=None,
            updated_at=now - timedelta(minutes=2),
        )
        g3 = Goal(
            title="Archive backups",
            description="Archive old backups.",
            status="proposed",  # not abandoned — must NOT appear
            fingerprint="fp003",
            updated_at=now,
        )
        s.add_all([g1, g2, g3])
        s.commit()

    rows = _db_recent_abandoned(limit=8)

    # Only abandoned goals returned, newest first
    assert len(rows) == 2
    titles = [r["title"] for r in rows]
    assert "Archive backups" not in titles
    # newest first: g2 was updated more recently
    assert rows[0]["title"] == "Check logs"
    assert rows[1]["title"] == "Restart plex"
    assert rows[1]["rejection_reason"] == "manual"
    assert rows[0]["rejection_reason"] is None


def test_db_recent_abandoned_limit(eng):
    """_db_recent_abandoned respects the limit parameter."""
    from backend.agents.goals import _db_recent_abandoned
    from backend.database import Goal

    now = datetime.utcnow()
    with Session(eng) as s:
        for i in range(10):
            s.add(Goal(
                title=f"Goal {i}",
                description=f"Description {i}.",
                status="abandoned",
                fingerprint=f"fp{i:04d}",
                updated_at=now - timedelta(minutes=i),
            ))
        s.commit()

    rows = _db_recent_abandoned(limit=3)
    assert len(rows) == 3


# ---------------------------------------------------------------------------
# Test 4 — proposer injects enrichment context into Opus prompt
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_proposer_injects_enrichment_context(eng, monkeypatch):
    """Seed TrendSnapshots (storage rising), UptimeSample (down event), and an
    abandoned goal. Run the proposer tick and capture the Opus prompt. Assert
    all three enrichment blocks are present with correct content."""
    _seed_state(eng, autonomy=True)
    _mock_integrations(monkeypatch)

    now = datetime.utcnow()
    from backend.database import TrendSnapshot, UptimeSample, Goal

    # Seed: unraid storage_used_gb rising 800 -> 900 over 7 days
    with Session(eng) as s:
        s.add(TrendSnapshot(
            source="unraid",
            metric="storage_used_gb",
            value=800.0,
            captured_at=now - timedelta(days=6),
        ))
        s.add(TrendSnapshot(
            source="unraid",
            metric="storage_used_gb",
            value=900.0,
            captured_at=now - timedelta(days=1),
        ))
        # Seed: a down event for "plex"
        s.add(UptimeSample(
            source="plex",
            ok=False,
            latency_ms=None,
            checked_at=now - timedelta(hours=3),
        ))
        # Seed: an abandoned goal with rejection_reason
        s.add(Goal(
            title="Restart plex",
            description="Restart the Plex Docker container.",
            status="abandoned",
            fingerprint="fp_plex_01",
            rejection_reason="manual",
            updated_at=now - timedelta(hours=2),
        ))
        s.commit()

    # Capture the prompt passed to Opus
    captured_prompts: list[str] = []

    async def _mock_opus(prompt, *, label=""):
        captured_prompts.append(prompt)
        return "[]"

    with patch("backend.agents.router.opus", new=_mock_opus):
        with patch("backend.config.get_settings", return_value=_make_settings()):
            from backend.agents.proposer import propose_goals_tick
            result = await propose_goals_tick()

    assert result["status"] == "ok", f"tick failed: {result}"
    assert len(captured_prompts) == 1, "Opus should have been called exactly once"

    prompt = captured_prompts[0]

    # Trends block
    assert "RECENT TRENDS" in prompt, "Prompt must contain RECENT TRENDS block"
    assert "storage_used_gb" in prompt, "Prompt must mention the storage_used_gb metric"
    assert "rising" in prompt, "Prompt must indicate rising trend direction"

    # Uptime anomalies block
    assert "UPTIME ANOMALIES" in prompt, "Prompt must contain UPTIME ANOMALIES block"
    assert "plex" in prompt, "Prompt must mention plex as a down source"

    # Do-not-re-propose block
    assert "DO NOT RE-PROPOSE" in prompt, "Prompt must contain DO NOT RE-PROPOSE block"
    assert "Restart plex" in prompt, "Prompt must include the rejected goal title"


# ---------------------------------------------------------------------------
# Test 5 — Empty case: no trends/anomalies/abandoned → sections show "(none)"
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_proposer_empty_enrichment_shows_none(eng, monkeypatch):
    """With no TrendSnapshots, UptimeSamples, or abandoned goals, all three
    enrichment sections show '(none)' and the tick completes with status='ok'."""
    _seed_state(eng, autonomy=True)
    _mock_integrations(monkeypatch)

    # No seed data at all — DB is empty except for SystemState

    captured_prompts: list[str] = []

    async def _mock_opus(prompt, *, label=""):
        captured_prompts.append(prompt)
        return "[]"

    with patch("backend.agents.router.opus", new=_mock_opus):
        with patch("backend.config.get_settings", return_value=_make_settings()):
            from backend.agents.proposer import propose_goals_tick
            result = await propose_goals_tick()

    assert result["status"] == "ok", f"tick failed: {result}"
    assert len(captured_prompts) == 1

    prompt = captured_prompts[0]

    # All three data block headers must be present
    assert "RECENT TRENDS (7d):" in prompt
    assert "UPTIME ANOMALIES (24h, outage incidents):" in prompt
    assert "DO NOT RE-PROPOSE (recently rejected" in prompt

    # Find the data blocks by their full header lines (unique enough)
    trends_idx = prompt.index("RECENT TRENDS (7d):")
    anoms_idx = prompt.index("UPTIME ANOMALIES (24h, outage incidents):")
    dnr_idx = prompt.index("DO NOT RE-PROPOSE (recently rejected")

    # Sections must appear in order
    assert trends_idx < anoms_idx < dnr_idx, "Sections must appear in expected order"

    # Extract the text for each section (up to the next block or end)
    trends_section = prompt[trends_idx:anoms_idx]
    anoms_section = prompt[anoms_idx:dnr_idx]
    dnr_section = prompt[dnr_idx:]

    assert "(none)" in trends_section, "Trends section must show (none) when empty"
    assert "(none)" in anoms_section, "Anomalies section must show (none) when empty"
    assert "(none)" in dnr_section, "Do-not-re-propose section must show (none) when empty"
