"""Tests for the weekly Facts -> Brain digest (backend/agents/facts_digest.py).

Covers:
  1. _format_digest — pure formatter: groups by subject, has the CwiAI footer.
  2. build_facts_digest — first run (no watermark) includes all above-floor facts;
     excludes pre-watermark facts; includes a fact reinforced after the watermark;
     excludes below-floor facts; empty case.
  3. run_facts_digest — skips the write when nothing's new; writes + advances the
     watermark when something is; swallows a write failure without advancing it.
"""
import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select


def _make_engine():
    """In-memory SQLite engine with all tables created."""
    import backend.database  # noqa: F401 -- registers all tables on SQLModel.metadata
    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(eng)
    return eng


def _get_system_state(engine):
    from backend.database import SystemState
    with Session(engine) as s:
        return s.get(SystemState, 1)


def test_facts_digest_enabled_by_default():
    """The digest ships ENABLED (flipped 2026-07-29): the pre-fix duplicate-
    subject noise (Charlie/Charlee, multiple Unraid/UniFi/Calendar-event/
    On-call-schedule spellings) was cleaned up via
    backend/agents/facts_cleanup.py (see tests/test_facts_cleanup.py) and
    verified against the live nexus.db (backup: nexus.db.bak-2026-07-29).
    This is the regression guard against a silent, unnoticed flip in either
    direction -- if this default ever needs to go back to False (e.g. new
    unreconciled noise is found), update this assertion deliberately, don't
    just delete it."""
    from backend.config import Settings
    assert Settings().facts_digest_enabled is True


# ---------------------------------------------------------------------------
# 1. _format_digest — pure, no I/O
# ---------------------------------------------------------------------------

def test_format_digest_groups_by_subject_and_has_cwiai_footer():
    from backend.agents.facts_digest import _format_digest

    facts = [
        {"subject": "Charlie", "predicate": "birthday", "value": "2026-07-23", "effective_confidence": 0.95},
        {"subject": "Unraid", "predicate": "storage used", "value": "34%", "effective_confidence": 0.9},
        {"subject": "Charlie", "predicate": "location", "value": "Detroit", "effective_confidence": 0.8},
    ]
    text = _format_digest(facts, since=None)

    assert text.endswith("Powered by CwiAI")
    # Both subjects appear as their own section, each fact nested under it.
    charlie_idx = text.index("## Charlie")
    unraid_idx = text.index("## Unraid")
    assert "birthday: 2026-07-23 (confidence 0.95)" in text
    assert "location: Detroit (confidence 0.80)" in text
    assert "storage used: 34% (confidence 0.90)" in text
    # Both Charlie facts land after the Charlie header and before the next one.
    assert charlie_idx < text.index("birthday: 2026-07-23") < text.index("location: Detroit")
    assert unraid_idx < text.index("storage used: 34%")


def test_format_digest_first_run_note_when_no_watermark():
    from backend.agents.facts_digest import _format_digest

    text = _format_digest(
        [{"subject": "X", "predicate": "p", "value": "v", "effective_confidence": 0.9}],
        since=None,
    )
    assert "First digest" in text


# ---------------------------------------------------------------------------
# 2. build_facts_digest
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_build_facts_digest_first_run_no_watermark_includes_all_above_floor(monkeypatch):
    from backend.agents.facts import _db_upsert_fact
    from backend.agents.facts_digest import build_facts_digest

    eng = _make_engine()
    monkeypatch.setattr("backend.database.engine", eng)
    _db_upsert_fact("Charlie", "birthday", "2026-07-23", 0.9, "chat", None)

    text, selected = await build_facts_digest()

    assert text != ""
    assert len(selected) == 1
    assert selected[0]["subject"] == "Charlie"


@pytest.mark.asyncio
async def test_build_facts_digest_excludes_facts_before_watermark(monkeypatch):
    from backend.agents.facts import _db_upsert_fact
    from backend.agents.facts_digest import build_facts_digest
    from backend.database import Fact, SystemState

    eng = _make_engine()
    monkeypatch.setattr("backend.database.engine", eng)

    _db_upsert_fact("Charlie", "birthday", "2026-07-23", 0.9, "chat", None)
    watermark = datetime.utcnow()
    with Session(eng) as s:
        row = s.exec(select(Fact)).first()
        row.last_seen_at = watermark - timedelta(days=1)
        row.created_at = watermark - timedelta(days=1)
        s.add(row)
        s.add(SystemState(id=1, last_facts_digest_at=watermark))
        s.commit()

    text, selected = await build_facts_digest()

    assert text == ""
    assert selected == []


@pytest.mark.asyncio
async def test_build_facts_digest_includes_reinforced_fact_after_watermark(monkeypatch):
    from backend.agents.facts import _db_upsert_fact
    from backend.agents.facts_digest import build_facts_digest
    from backend.database import Fact, SystemState

    eng = _make_engine()
    monkeypatch.setattr("backend.database.engine", eng)

    _db_upsert_fact("Charlie", "birthday", "2026-07-23", 0.9, "chat", None)
    watermark = datetime.utcnow()
    with Session(eng) as s:
        row = s.exec(select(Fact)).first()
        row.last_seen_at = watermark - timedelta(days=1)
        row.created_at = watermark - timedelta(days=1)
        s.add(row)
        s.add(SystemState(id=1, last_facts_digest_at=watermark))
        s.commit()

    # Reinforce (same value) -- bumps last_seen_at to "now", after the watermark.
    _db_upsert_fact("Charlie", "birthday", "2026-07-23", 0.9, "chat", None)

    text, selected = await build_facts_digest()

    assert text != ""
    assert len(selected) == 1
    assert selected[0]["subject"] == "Charlie"


@pytest.mark.asyncio
async def test_build_facts_digest_excludes_below_floor_facts(monkeypatch):
    from backend.agents.facts import _db_upsert_fact
    from backend.agents.facts_digest import build_facts_digest
    from backend.database import Fact

    eng = _make_engine()
    monkeypatch.setattr("backend.database.engine", eng)

    _db_upsert_fact("garage", "located_at", "north side", 0.6, "chat", None)
    # Back-date created_at (not last_seen_at) well past the decay floor --
    # isolates that the exclusion is confidence-decay, not the watermark.
    with Session(eng) as s:
        row = s.exec(select(Fact)).first()
        row.created_at = datetime.utcnow() - timedelta(days=90)
        s.add(row)
        s.commit()

    text, selected = await build_facts_digest()

    assert text == ""
    assert selected == []


@pytest.mark.asyncio
async def test_build_facts_digest_empty_returns_empty_string_and_no_facts(monkeypatch):
    from backend.agents.facts_digest import build_facts_digest

    eng = _make_engine()
    monkeypatch.setattr("backend.database.engine", eng)

    text, selected = await build_facts_digest()

    assert text == ""
    assert selected == []


# ---------------------------------------------------------------------------
# 3. run_facts_digest
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_facts_digest_skips_write_when_nothing_new(monkeypatch):
    from backend.agents.facts_digest import run_facts_digest

    eng = _make_engine()
    monkeypatch.setattr("backend.database.engine", eng)

    with patch(
        "backend.integrations.obsidian.write_facts_digest", new_callable=AsyncMock
    ) as mock_write:
        result = await run_facts_digest()

    mock_write.assert_not_called()
    assert result == {"written": False, "count": 0, "watermark_advanced": False}


@pytest.mark.asyncio
async def test_run_facts_digest_writes_and_advances_watermark(monkeypatch):
    from backend.agents.facts import _db_upsert_fact
    from backend.agents.facts_digest import run_facts_digest

    eng = _make_engine()
    monkeypatch.setattr("backend.database.engine", eng)
    _db_upsert_fact("Charlie", "birthday", "2026-07-23", 0.9, "chat", None)

    assert _get_system_state(eng) is None

    with patch(
        "backend.integrations.obsidian.write_facts_digest", new_callable=AsyncMock
    ) as mock_write:
        result = await run_facts_digest()

    mock_write.assert_called_once()
    written_text = mock_write.call_args[0][0]
    assert "Charlie" in written_text
    assert result == {"written": True, "count": 1, "watermark_advanced": True}

    state = _get_system_state(eng)
    assert state is not None
    assert state.last_facts_digest_at is not None


@pytest.mark.asyncio
async def test_run_facts_digest_swallows_write_failure(monkeypatch):
    from backend.agents.facts import _db_upsert_fact
    from backend.agents.facts_digest import run_facts_digest

    eng = _make_engine()
    monkeypatch.setattr("backend.database.engine", eng)
    _db_upsert_fact("Charlie", "birthday", "2026-07-23", 0.9, "chat", None)

    with patch(
        "backend.integrations.obsidian.write_facts_digest",
        new_callable=AsyncMock,
        side_effect=RuntimeError("brain unreachable"),
    ):
        result = await run_facts_digest()  # must not raise

    assert result == {"written": False, "count": 0, "watermark_advanced": False}
    assert _get_system_state(eng) is None


@pytest.mark.asyncio
async def test_run_facts_digest_watermark_not_after_run_start(monkeypatch):
    """The persisted watermark must be <= the instant the run started, not a
    later timestamp captured after the write -- otherwise a fact reinforced
    during the write's network round-trip would be skipped forever."""
    from backend.agents.facts import _db_upsert_fact
    from backend.agents.facts_digest import run_facts_digest

    eng = _make_engine()
    monkeypatch.setattr("backend.database.engine", eng)
    _db_upsert_fact("Charlie", "birthday", "2026-07-23", 0.9, "chat", None)

    recorded_at = {}

    async def slow_write(text):
        recorded_at["during_write"] = datetime.utcnow()

    with patch(
        "backend.integrations.obsidian.write_facts_digest", side_effect=slow_write
    ):
        await run_facts_digest()

    state = _get_system_state(eng)
    assert state.last_facts_digest_at <= recorded_at["during_write"]


@pytest.mark.asyncio
async def test_run_facts_digest_reports_failed_watermark_write(monkeypatch):
    """If the watermark DB write itself fails, the note is still reported as
    written (it's already in the vault) but watermark_advanced must be False
    so the caller can observe the drift instead of it being silently lost."""
    from backend.agents.facts import _db_upsert_fact
    from backend.agents import facts_digest
    from backend.agents.facts_digest import run_facts_digest

    eng = _make_engine()
    monkeypatch.setattr("backend.database.engine", eng)
    _db_upsert_fact("Charlie", "birthday", "2026-07-23", 0.9, "chat", None)
    monkeypatch.setattr(facts_digest, "_db_set_last_digest_at", lambda ts: False)

    with patch(
        "backend.integrations.obsidian.write_facts_digest", new_callable=AsyncMock
    ):
        result = await run_facts_digest()

    assert result == {"written": True, "count": 1, "watermark_advanced": False}
