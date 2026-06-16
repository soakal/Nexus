"""Tests for Tier 2.3c — Entity/Fact store (backend/agents/facts.py).

Covers:
  1. effective_confidence — at age 0, at half-life, monotonic decreasing.
  2. _db_upsert_fact INSERT — empty table → one active fact.
  3. _db_upsert_fact REINFORCE — same subject/predicate/value → one fact, bumped conf.
  4. _db_upsert_fact SUPERSEDE — same subject/predicate, new value → two rows, old superseded.
  5. facts_recall — stale fact excluded; keyword ranking; "" when all below floor.
  6. extract_and_store — haiku mock inserts a fact; "[]" inserts nothing; raise is swallowed.
  7. memory.assemble injection — facts_str appears under [KNOWN FACTS]; all-empty returns "".
"""
import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_engine():
    """In-memory SQLite engine with all tables created."""
    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(eng)
    return eng


def _count_active(engine) -> int:
    """Count rows in the fact table where superseded_by IS NULL."""
    from sqlmodel import Session, select
    from backend.database import Fact
    with Session(engine) as s:
        rows = s.exec(select(Fact).where(Fact.superseded_by == None)).all()  # noqa: E711
        return len(rows)


def _all_facts(engine):
    """Return all Fact rows (active + superseded) as a list."""
    from sqlmodel import Session, select
    from backend.database import Fact
    with Session(engine) as s:
        return list(s.exec(select(Fact)).all())


# ---------------------------------------------------------------------------
# 1. effective_confidence — pure function
# ---------------------------------------------------------------------------

def test_effective_confidence_at_age_zero():
    from backend.agents.facts import effective_confidence
    assert effective_confidence(0.8, 0.0) == pytest.approx(0.8, rel=1e-6)
    assert effective_confidence(0.6, 0.0) == pytest.approx(0.6, rel=1e-6)


def test_effective_confidence_at_half_life():
    from backend.agents.facts import HALF_LIFE_DAYS, effective_confidence
    conf = 0.8
    result = effective_confidence(conf, HALF_LIFE_DAYS)
    assert result == pytest.approx(conf / 2, rel=1e-6)


def test_effective_confidence_monotonic_decreasing():
    from backend.agents.facts import effective_confidence
    conf = 0.9
    values = [effective_confidence(conf, d) for d in [0, 10, 30, 60, 90, 180]]
    for i in range(len(values) - 1):
        assert values[i] > values[i + 1], (
            f"Expected monotonic decrease but values[{i}]={values[i]} "
            f"<= values[{i+1}]={values[i+1]}"
        )


def test_effective_confidence_negative_age_clamped_to_zero():
    """Future-dated rows (negative age) should not gain confidence."""
    from backend.agents.facts import effective_confidence
    assert effective_confidence(0.5, -10.0) == pytest.approx(0.5, rel=1e-6)


# ---------------------------------------------------------------------------
# 2. _db_upsert_fact INSERT — empty table → one active fact
# ---------------------------------------------------------------------------

def test_db_upsert_fact_insert(monkeypatch):
    from backend.agents.facts import _db_upsert_fact
    from backend.database import Fact

    eng = _make_engine()
    monkeypatch.setattr("backend.database.engine", eng)

    _db_upsert_fact("user", "prefers", "dark mode", 0.7, "chat", None)

    facts = _all_facts(eng)
    assert len(facts) == 1
    f = facts[0]
    assert f.subject == "user"
    assert f.predicate == "prefers"
    assert f.value == "dark mode"
    assert f.confidence == pytest.approx(0.7, rel=1e-6)
    assert f.superseded_by is None


# ---------------------------------------------------------------------------
# 3. _db_upsert_fact REINFORCE — same subject/predicate/value → one fact, bumped
# ---------------------------------------------------------------------------

def test_db_upsert_fact_reinforce(monkeypatch):
    from backend.agents.facts import CONFIRM_BUMP, _db_upsert_fact
    from backend.database import Fact

    eng = _make_engine()
    monkeypatch.setattr("backend.database.engine", eng)

    # First insert
    _db_upsert_fact("user", "prefers", "dark mode", 0.7, "chat", None)
    # Reinforce with same value
    _db_upsert_fact("user", "prefers", "dark mode", 0.7, "chat", None)

    facts = _all_facts(eng)
    assert len(facts) == 1, "REINFORCE must not create a second row"
    f = facts[0]
    assert f.superseded_by is None
    # Confidence should be bumped by CONFIRM_BUMP (capped at 1.0)
    expected = min(1.0, 0.7 + CONFIRM_BUMP)
    assert f.confidence == pytest.approx(expected, rel=1e-6)


def test_db_upsert_fact_reinforce_caps_at_one(monkeypatch):
    from backend.agents.facts import CONFIRM_BUMP, _db_upsert_fact

    eng = _make_engine()
    monkeypatch.setattr("backend.database.engine", eng)

    # Start very high so the bump would exceed 1.0
    _db_upsert_fact("user", "name", "Brian", 0.99, "manual", None)
    _db_upsert_fact("user", "name", "Brian", 0.99, "manual", None)

    facts = _all_facts(eng)
    assert len(facts) == 1
    assert facts[0].confidence <= 1.0


# ---------------------------------------------------------------------------
# 4. _db_upsert_fact SUPERSEDE — same (subject, predicate), different value
# ---------------------------------------------------------------------------

def test_db_upsert_fact_supersede(monkeypatch):
    from backend.agents.facts import _db_upsert_fact

    eng = _make_engine()
    monkeypatch.setattr("backend.database.engine", eng)

    # First insert
    _db_upsert_fact("unraid", "named", "Tower", 0.8, "chat", None)
    # Supersede with a new value
    _db_upsert_fact("unraid", "named", "NAS-Prime", 0.9, "chat", None)

    all_rows = _all_facts(eng)
    assert len(all_rows) == 2, "SUPERSEDE must produce exactly two rows"

    active = [f for f in all_rows if f.superseded_by is None]
    superseded = [f for f in all_rows if f.superseded_by is not None]
    assert len(active) == 1, "Exactly ONE active fact must remain after supersede"
    assert len(superseded) == 1

    # Active row is the new value
    assert active[0].value == "NAS-Prime"
    # Superseded row points to the active row's id
    assert superseded[0].superseded_by == active[0].id
    # Old value is preserved in the superseded row
    assert superseded[0].value == "Tower"


def test_db_upsert_fact_supersede_case_insensitive_match(monkeypatch):
    """(subject, predicate) matching must be case-insensitive."""
    from backend.agents.facts import _db_upsert_fact

    eng = _make_engine()
    monkeypatch.setattr("backend.database.engine", eng)

    _db_upsert_fact("User", "Prefers", "light mode", 0.6, "chat", None)
    # Different case for subject/predicate but same semantics → SUPERSEDE
    _db_upsert_fact("user", "prefers", "dark mode", 0.8, "chat", None)

    active = [f for f in _all_facts(eng) if f.superseded_by is None]
    assert len(active) == 1
    assert active[0].value == "dark mode"


# ---------------------------------------------------------------------------
# 5. facts_recall — floor filtering, keyword ranking, empty result
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_facts_recall_excludes_stale_facts(monkeypatch):
    """A fact with effective_confidence < EFFECTIVE_FLOOR must be excluded."""
    from backend.agents.facts import EFFECTIVE_FLOOR, _db_upsert_fact, facts_recall
    from backend.database import Fact

    eng = _make_engine()
    monkeypatch.setattr("backend.database.engine", eng)

    # Insert a fact and manually back-date created_at so it decays below floor.
    # At conf=0.6, floor=0.2: need 0.6 * 0.5^(age/30) < 0.2
    # → 0.5^(age/30) < 0.333 → age/30 > log2(3) ≈ 1.585 → age > 47.6 days
    _db_upsert_fact("garage", "located_at", "north side", 0.6, "chat", None)
    # Back-date to 90 days ago (well past the decay floor)
    with Session(eng) as s:
        from sqlmodel import select
        row = s.exec(select(Fact)).first()
        row.created_at = datetime.utcnow() - timedelta(days=90)
        s.add(row)
        s.commit()

    result = await facts_recall("garage location")
    assert result == "", f"Expected empty string but got: {result!r}"


@pytest.mark.asyncio
async def test_facts_recall_keyword_ranking(monkeypatch):
    """The fact matching query keywords should rank higher than unrelated facts."""
    from backend.agents.facts import _db_upsert_fact, facts_recall

    eng = _make_engine()
    monkeypatch.setattr("backend.database.engine", eng)

    # Insert two recent high-confidence facts
    _db_upsert_fact("user", "prefers", "dark theme", 0.9, "chat", None)
    _db_upsert_fact("unraid", "storage_total", "100TB", 0.9, "chat", None)

    result = await facts_recall("dark theme preference")
    assert result != ""
    lines = result.strip().split("\n")
    # The dark theme fact should appear first (better keyword overlap)
    assert "dark theme" in lines[0]


@pytest.mark.asyncio
async def test_facts_recall_returns_empty_when_all_stale(monkeypatch):
    """Returns '' when all active facts are below the effective floor."""
    from backend.agents.facts import _db_upsert_fact, facts_recall
    from backend.database import Fact

    eng = _make_engine()
    monkeypatch.setattr("backend.database.engine", eng)

    _db_upsert_fact("user", "name", "Brian", 0.5, "chat", None)
    # Back-date to 200 days ago — well past decay floor for conf=0.5
    with Session(eng) as s:
        from sqlmodel import select
        row = s.exec(select(Fact)).first()
        row.created_at = datetime.utcnow() - timedelta(days=200)
        s.add(row)
        s.commit()

    result = await facts_recall("anything")
    assert result == ""


@pytest.mark.asyncio
async def test_facts_recall_returns_empty_on_no_facts(monkeypatch):
    """Returns '' when the fact table is empty."""
    from backend.agents.facts import facts_recall

    eng = _make_engine()
    monkeypatch.setattr("backend.database.engine", eng)

    result = await facts_recall("query")
    assert result == ""


# ---------------------------------------------------------------------------
# 6. extract_and_store — haiku integration
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_extract_and_store_creates_fact(monkeypatch):
    """When haiku returns a JSON array with one fact, a Fact row must be created."""
    from backend.agents.facts import extract_and_store
    from backend.database import Fact

    eng = _make_engine()
    monkeypatch.setattr("backend.database.engine", eng)

    haiku_response = '[{"subject": "user", "predicate": "name", "value": "Brian", "confidence": 0.85}]'

    with patch("backend.agents.router.haiku", new_callable=AsyncMock) as mock_haiku:
        mock_haiku.return_value = haiku_response
        await extract_and_store("My name is Brian", conversation_id=1)

    facts = _all_facts(eng)
    assert len(facts) == 1
    f = facts[0]
    assert f.subject == "user"
    assert f.predicate == "name"
    assert f.value == "Brian"
    assert f.confidence == pytest.approx(0.85, rel=1e-6)
    assert f.source == "chat"
    assert f.conversation_id == 1


@pytest.mark.asyncio
async def test_extract_and_store_empty_array_creates_nothing(monkeypatch):
    """When haiku returns '[]', no Fact rows must be created."""
    from backend.agents.facts import extract_and_store

    eng = _make_engine()
    monkeypatch.setattr("backend.database.engine", eng)

    with patch("backend.agents.router.haiku", new_callable=AsyncMock) as mock_haiku:
        mock_haiku.return_value = "[]"
        await extract_and_store("Just browsing", conversation_id=None)

    assert len(_all_facts(eng)) == 0


@pytest.mark.asyncio
async def test_extract_and_store_haiku_raises_does_not_propagate(monkeypatch):
    """If haiku raises (including BudgetExceeded), extract_and_store must NOT raise."""
    from backend.agents.facts import extract_and_store

    eng = _make_engine()
    monkeypatch.setattr("backend.database.engine", eng)

    with patch("backend.agents.router.haiku", new_callable=AsyncMock) as mock_haiku:
        mock_haiku.side_effect = RuntimeError("haiku exploded")
        # Must not raise
        await extract_and_store("Some message", conversation_id=None)

    # No rows should have been created
    assert len(_all_facts(eng)) == 0


@pytest.mark.asyncio
async def test_extract_and_store_invalid_json_creates_nothing(monkeypatch):
    """If haiku returns non-JSON gibberish, extract_and_store must not raise or insert."""
    from backend.agents.facts import extract_and_store

    eng = _make_engine()
    monkeypatch.setattr("backend.database.engine", eng)

    with patch("backend.agents.router.haiku", new_callable=AsyncMock) as mock_haiku:
        mock_haiku.return_value = "Sorry, I cannot extract any facts from this."
        await extract_and_store("Just a message", conversation_id=None)

    assert len(_all_facts(eng)) == 0


# ---------------------------------------------------------------------------
# 7. memory.assemble — facts_str injection + backwards compat
# ---------------------------------------------------------------------------

def test_assemble_with_facts_str():
    """assemble with only facts_str must include [KNOWN FACTS] and the fact line."""
    from backend.agents.memory import assemble

    result = assemble("", "", "FACT_LINE_HERE")
    assert "[KNOWN FACTS]" in result
    assert "FACT_LINE_HERE" in result
    assert "RELEVANT MEMORY" in result
    # Precedence note must be present
    assert "prefer live data" in result


def test_assemble_all_empty_returns_empty_string():
    """assemble('', '', '') must return '' (no injection block)."""
    from backend.agents.memory import assemble

    assert assemble("", "", "") == ""


def test_assemble_none_facts_str_treated_as_empty():
    """assemble with facts_str=None must behave the same as facts_str=''."""
    from backend.agents.memory import assemble

    # All None → empty
    assert assemble(None, None, None) == ""
    # Only vault → no [KNOWN FACTS] section
    result = assemble("vault notes", None, None)
    assert "[KNOWN FACTS]" not in result
    assert "[VAULT NOTES]" in result


def test_assemble_all_three_sections():
    """assemble with all three non-empty must include all three sections."""
    from backend.agents.memory import assemble

    result = assemble("vault text", "briefing text", "fact text")
    assert "[VAULT NOTES]" in result
    assert "vault text" in result
    assert "[LATEST BRIEFING]" in result
    assert "briefing text" in result
    assert "[KNOWN FACTS]" in result
    assert "fact text" in result


def test_assemble_backwards_compat_two_arg_call():
    """Existing callers that pass only (vault_str, briefing_str) must still work."""
    from backend.agents.memory import assemble

    # Two-arg call — no facts_str
    result = assemble("vault data", "brief data")
    assert "[VAULT NOTES]" in result
    assert "[LATEST BRIEFING]" in result
    assert "[KNOWN FACTS]" not in result

    # Both empty two-arg call
    assert assemble("", "") == ""
