"""Tests for the pre-fix Fact-table duplicate-subject cleanup
(backend/agents/facts_cleanup.py).

Covers:
  1. _normalize_subject_key — pure, case/separator-insensitive.
  2. plan_subject_renames — majority-wins case/separator merge; alias fix;
     leaves distinct-but-similar subjects alone; identity for clean data.
  3. plan_predicate_merges — canonical-key collisions merge with newest-wins;
     unique keys are untouched.
  4. run_cleanup — dry_run makes no DB changes; a real run renames + merges,
     never deletes a row, and is idempotent (a second run is a no-op).
"""
from datetime import datetime, timedelta

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


def _all_facts(engine):
    from backend.database import Fact
    with Session(engine) as s:
        return list(s.exec(select(Fact)).all())


def _active_facts(engine):
    from backend.database import Fact
    with Session(engine) as s:
        stmt = (
            select(Fact)
            .where(Fact.superseded_by == None)  # noqa: E711
            .where(Fact.dismissed_at == None)   # noqa: E711
        )
        return list(s.exec(stmt).all())


# ---------------------------------------------------------------------------
# 1. _normalize_subject_key — pure
# ---------------------------------------------------------------------------

def test_normalize_subject_key_case_and_separator_insensitive():
    from backend.agents.facts_cleanup import _normalize_subject_key

    assert _normalize_subject_key("Unraid Array") == "unraid array"
    assert _normalize_subject_key("Unraid array") == "unraid array"
    assert _normalize_subject_key("Unraid_array") == "unraid array"
    assert _normalize_subject_key("  Unraid   Array  ") == "unraid array"


def test_normalize_subject_key_does_not_reorder_words():
    from backend.agents.facts_cleanup import _normalize_subject_key

    # Different word order must NOT collide -- only formatting variants should.
    assert _normalize_subject_key("Network device") != _normalize_subject_key("device Network")


# ---------------------------------------------------------------------------
# 2. plan_subject_renames — pure
# ---------------------------------------------------------------------------

def _row(subject, created_at):
    return {"subject": subject, "created_at": created_at}


def test_plan_subject_renames_majority_spelling_wins():
    from backend.agents.facts_cleanup import plan_subject_renames

    now = datetime.utcnow()
    rows = [
        _row("Unraid Array", now),
        _row("Unraid array", now),
        _row("Unraid array", now),
        _row("Unraid_array", now),
    ]
    plan = plan_subject_renames(rows)
    assert plan["Unraid Array"] == "Unraid array"
    assert plan["Unraid array"] == "Unraid array"
    assert plan["Unraid_array"] == "Unraid array"


def test_plan_subject_renames_applies_known_alias():
    from backend.agents.facts_cleanup import plan_subject_renames

    now = datetime.utcnow()
    rows = [_row("Charlie", now), _row("Charlee", now + timedelta(milliseconds=6))]
    plan = plan_subject_renames(rows)
    assert plan["Charlie"] == "Charlie"
    assert plan["Charlee"] == "Charlie"


def test_plan_subject_renames_leaves_distinct_subjects_alone():
    """'Unraid' and 'Unraid storage' are plausibly different sub-topics --
    the mechanical case/separator pass must NOT merge them."""
    from backend.agents.facts_cleanup import plan_subject_renames

    now = datetime.utcnow()
    rows = [_row("Unraid", now), _row("Unraid storage", now), _row("Unraid system", now)]
    plan = plan_subject_renames(rows)
    assert plan["Unraid"] == "Unraid"
    assert plan["Unraid storage"] == "Unraid storage"
    assert plan["Unraid system"] == "Unraid system"


def test_plan_subject_renames_identity_when_already_clean():
    from backend.agents.facts_cleanup import plan_subject_renames

    now = datetime.utcnow()
    rows = [_row("Charlie", now), _row("Unraid", now)]
    plan = plan_subject_renames(rows)
    assert plan == {"Charlie": "Charlie", "Unraid": "Unraid"}


def test_plan_subject_renames_tie_break_is_deterministic():
    """Equal counts on both sides must resolve the same way every time
    (recency, then alphabetical) -- required for idempotency."""
    from backend.agents.facts_cleanup import plan_subject_renames

    now = datetime.utcnow()
    rows = [_row("Foo Bar", now), _row("foo bar", now + timedelta(seconds=1))]
    plan1 = plan_subject_renames(rows)
    plan2 = plan_subject_renames(rows)
    assert plan1 == plan2
    # The more recently created spelling wins the tie.
    assert plan1["Foo Bar"] == "foo bar"


# ---------------------------------------------------------------------------
# 3. plan_predicate_merges — pure
# ---------------------------------------------------------------------------

def _fact_row(id_, subject, predicate, created_at, last_seen_at=None):
    return {
        "id": id_,
        "subject": subject,
        "predicate": predicate,
        "created_at": created_at,
        "last_seen_at": last_seen_at or created_at,
    }


def test_plan_predicate_merges_collapses_wording_variant_and_picks_newest():
    from backend.agents.facts_cleanup import plan_predicate_merges

    older = datetime.utcnow() - timedelta(days=1)
    newer = datetime.utcnow()
    rows = [
        _fact_row(1, "Unraid", "storage_used", older),
        _fact_row(2, "Unraid storage", "currently used", newer),
    ]
    merges = plan_predicate_merges(rows)
    assert len(merges) == 1
    assert merges[0] == {"winner_id": 2, "loser_ids": [1]}


def test_plan_predicate_merges_no_merge_for_distinct_keys():
    from backend.agents.facts_cleanup import plan_predicate_merges

    now = datetime.utcnow()
    rows = [
        _fact_row(1, "Unraid", "storage_used", now),
        _fact_row(2, "Unraid", "storage_total", now),
    ]
    assert plan_predicate_merges(rows) == []


def test_plan_predicate_merges_three_way_collision():
    from backend.agents.facts_cleanup import plan_predicate_merges

    t0 = datetime.utcnow() - timedelta(days=2)
    t1 = datetime.utcnow() - timedelta(days=1)
    t2 = datetime.utcnow()
    rows = [
        _fact_row(1, "AdGuard", "filtering_status", t0),
        _fact_row(2, "AdGuard", "filtering status", t1),
        _fact_row(3, "AdGuard", "filtering-status", t2),
    ]
    merges = plan_predicate_merges(rows)
    assert len(merges) == 1
    assert merges[0]["winner_id"] == 3
    assert set(merges[0]["loser_ids"]) == {1, 2}


# ---------------------------------------------------------------------------
# 4. run_cleanup — DB-backed
#
# Seeding uses a direct Fact insert (_seed_fact), NOT facts._db_upsert_fact.
# _db_upsert_fact already runs its own canonical-key fallback at insert time
# (the "extraction-prompt fix" mentioned in CLAUDE.md/config.py), so feeding
# it two wording-variant facts back to back would dedup them on the spot and
# never reproduce the pre-fix legacy-duplicate rows this module exists to
# clean up. _seed_fact bypasses that path on purpose, exactly as the real
# rows already sitting in nexus.db did (inserted before that fallback shipped).
# ---------------------------------------------------------------------------

def _seed_fact(engine, subject, predicate, value, confidence, created_at, last_seen_at=None, dismissed_at=None):
    from backend.database import Fact
    with Session(engine) as s:
        f = Fact(
            subject=subject,
            predicate=predicate,
            value=value,
            confidence=confidence,
            source="briefing",
            created_at=created_at,
            updated_at=created_at,
            last_seen_at=last_seen_at or created_at,
            dismissed_at=dismissed_at,
        )
        s.add(f)
        s.commit()
        s.refresh(f)
        return f.id


def test_run_cleanup_dry_run_makes_no_db_changes(monkeypatch):
    from backend.agents.facts_cleanup import run_cleanup

    eng = _make_engine()
    monkeypatch.setattr("backend.database.engine", eng)
    now = datetime.utcnow()
    _seed_fact(eng, "Unraid Array", "storage_total", "13041 GB", 0.9, now - timedelta(days=1))
    _seed_fact(eng, "Unraid array", "storage total", "13041 GB", 0.9, now)

    before = [(f.id, f.subject, f.superseded_by) for f in _all_facts(eng)]
    report = run_cleanup(dry_run=True)
    after = [(f.id, f.subject, f.superseded_by) for f in _all_facts(eng)]

    assert before == after  # nothing written
    assert report["dry_run"] is True
    assert len(report["subject_renames"]) == 1
    assert len(report["merges"]) == 1


def test_run_cleanup_applies_renames_and_merges_never_deletes(monkeypatch):
    from backend.agents.facts_cleanup import run_cleanup

    eng = _make_engine()
    monkeypatch.setattr("backend.database.engine", eng)
    now = datetime.utcnow()
    _seed_fact(eng, "Charlie", "birthday", "today", 1.0, now)
    _seed_fact(eng, "Charlee", "birthday", "tomorrow", 0.8, now + timedelta(milliseconds=6))
    _seed_fact(eng, "Unraid Array", "storage_total", "13041 GB", 0.9, now - timedelta(days=1))
    _seed_fact(eng, "Unraid array", "storage total", "13041 GB", 0.9, now)
    # A distinct, unrelated subject that must be left completely alone.
    _seed_fact(eng, "Home Assistant", "entity_count", "1335", 1.0, now)

    row_count_before = len(_all_facts(eng))

    report = run_cleanup(dry_run=False)

    row_count_after = len(_all_facts(eng))
    assert row_count_after == row_count_before  # never deletes

    active = _active_facts(eng)
    active_subjects = {f.subject for f in active}
    assert "Charlee" not in active_subjects
    assert "Unraid Array" not in active_subjects
    assert "Home Assistant" in active_subjects  # untouched

    # Charlie: two rows collapsed to one active row (newest value wins).
    charlie_active = [f for f in active if f.subject == "Charlie"]
    assert len(charlie_active) == 1
    assert charlie_active[0].value == "tomorrow"  # the later-created row won

    # Unraid array: two rows collapsed to one active row under the majority spelling.
    unraid_active = [f for f in active if f.subject == "Unraid array"]
    assert len(unraid_active) == 1

    assert report["dry_run"] is False
    assert report["active_after"] < report["active_before"]


def test_run_cleanup_leaves_dismissed_facts_alone(monkeypatch):
    """A dismissed fact is one Brian explicitly rejected -- run_cleanup's active
    filter (superseded_by IS NULL AND dismissed_at IS NULL) must exclude it
    from both passes. A dismissed row with a wording-variant spelling/subject
    must not get silently renamed or merged back to life."""
    from backend.agents.facts_cleanup import run_cleanup

    eng = _make_engine()
    monkeypatch.setattr("backend.database.engine", eng)
    now = datetime.utcnow()
    _seed_fact(eng, "Unraid Array", "storage_total", "13041 GB", 0.9, now - timedelta(days=1),
               dismissed_at=now)
    _seed_fact(eng, "Unraid array", "storage total", "13041 GB", 0.9, now)

    report = run_cleanup(dry_run=False)

    assert report["subject_renames"] == []
    assert report["merges"] == []

    all_facts = {f.id: f for f in _all_facts(eng)}
    dismissed = next(f for f in all_facts.values() if f.subject == "Unraid Array")
    assert dismissed.superseded_by is None  # untouched, not swept into a merge
    assert dismissed.dismissed_at is not None

    active = _active_facts(eng)
    assert len(active) == 1
    assert active[0].subject == "Unraid array"  # the live row, never renamed onto the dismissed one


def test_run_cleanup_is_idempotent(monkeypatch):
    from backend.agents.facts_cleanup import run_cleanup

    eng = _make_engine()
    monkeypatch.setattr("backend.database.engine", eng)
    now = datetime.utcnow()
    _seed_fact(eng, "Charlie", "birthday", "today", 1.0, now)
    _seed_fact(eng, "Charlee", "birthday", "tomorrow", 0.8, now + timedelta(milliseconds=6))
    _seed_fact(eng, "Unraid Array", "storage_total", "13041 GB", 0.9, now - timedelta(days=1))
    _seed_fact(eng, "Unraid array", "storage total", "13041 GB", 0.9, now)

    first = run_cleanup(dry_run=False)
    assert len(first["subject_renames"]) > 0
    assert len(first["merges"]) > 0

    state_after_first = [(f.id, f.subject, f.superseded_by) for f in _all_facts(eng)]

    second = run_cleanup(dry_run=False)
    state_after_second = [(f.id, f.subject, f.superseded_by) for f in _all_facts(eng)]

    assert second["subject_renames"] == []
    assert second["merges"] == []
    assert state_after_first == state_after_second  # no further mutation


def test_run_cleanup_empty_table_is_a_no_op(monkeypatch):
    from backend.agents.facts_cleanup import run_cleanup

    eng = _make_engine()
    monkeypatch.setattr("backend.database.engine", eng)

    report = run_cleanup(dry_run=False)
    assert report == {
        "subject_renames": [],
        "merges": [],
        "active_before": 0,
        "active_after": 0,
        "dry_run": False,
    }
