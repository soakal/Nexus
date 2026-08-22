"""Tests for the Outcome Tracker foundation (docs/outcome-tracker-spec.md,
rollout step 1) — backend/database.py's `OutcomeFlag` table + index shim, and
backend/agents/outcomes.py's write/dedup/resolve/read surface.

Covers AC1-AC12, AC28, AC29, AC32, AC33 from the spec's §6 acceptance
criteria — every AC scoped to step 1 ("Table + index shim + config +
outcomes.py + tests/test_outcome_flags.py. Nothing writes yet.", §7.5).
AC13-AC27, AC30, AC31, AC34 belong to later rollout steps' own test files
(telegram_poller/telegram_commands/api_endpoints/homelab_watch/watchdog/
briefing/chat_memory/backup), per that same ordering — out of scope here.

Pattern: in-memory StaticPool engine monkeypatched onto backend.database.engine,
matching test_goals.py / test_secrets_fallback.py.
"""
import asyncio
from datetime import datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

# Register all table metadata (including OutcomeFlag) before any test runs.
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


def _create_open_index(engine):
    """Create ux_outcomeflag_open directly, matching how test_goals.py's
    test_debounce_survives_concurrent_propose_race builds ux_goal_fingerprint_active:
    the shared `eng` fixture only runs SQLModel.metadata.create_all(), not the
    create_db_and_tables()-only index shim."""
    with Session(engine) as s:
        s.exec(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_outcomeflag_open "
            "ON outcomeflag(fingerprint) WHERE status = 'open' AND fingerprint != ''"
        ))
        s.commit()


class _BoomSession:
    """Stand-in for sqlmodel.Session that fails on construction — simulates
    the DB engine being genuinely unavailable, same technique as
    test_secrets_fallback.py's _BoomSession."""

    def __init__(self, *a, **k):
        raise RuntimeError("db unavailable")


# ---------------------------------------------------------------------------
# 6.1 Data model / migration
# ---------------------------------------------------------------------------

def test_ac1_create_db_and_tables_creates_outcomeflag_with_all_columns(monkeypatch):
    """AC1: create_db_and_tables() on a fresh :memory: engine creates
    `outcomeflag` with all declared columns."""
    from backend.database import OutcomeFlag, create_db_and_tables

    e = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    monkeypatch.setattr("backend.database.engine", e)

    create_db_and_tables()

    with e.connect() as conn:
        cols = {row[1] for row in conn.execute(text("PRAGMA table_info(outcomeflag)"))}
    expected = set(OutcomeFlag.model_fields.keys())
    assert expected.issubset(cols)


def test_ac2_create_db_and_tables_is_idempotent(monkeypatch):
    """AC2: calling create_db_and_tables() twice raises nothing and creates
    no duplicate index."""
    from backend.database import create_db_and_tables

    e = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    monkeypatch.setattr("backend.database.engine", e)

    create_db_and_tables()
    create_db_and_tables()  # must not raise

    with e.connect() as conn:
        rows = conn.execute(text(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='ux_outcomeflag_open'"
        )).fetchall()
    assert len(rows) == 1


def test_ac3_partial_unique_index_rejects_second_open_same_fingerprint(eng):
    """AC3: the partial unique index rejects a second status="open" row with
    the same fingerprint, and permits one when the first is resolved."""
    from backend.database import OutcomeFlag

    _create_open_index(eng)

    with Session(eng) as session:
        session.add(OutcomeFlag(
            source="homelab_watch", check="garage_open",
            fingerprint="homelab_watch:garage_open", summary="door open",
        ))
        session.commit()

    with Session(eng) as session:
        session.add(OutcomeFlag(
            source="homelab_watch", check="garage_open",
            fingerprint="homelab_watch:garage_open", summary="door open again",
        ))
        with pytest.raises(IntegrityError):
            session.commit()

    # Resolve the first row; a second open insert with the same fingerprint
    # must now be permitted.
    with Session(eng) as session:
        row = session.exec(select(OutcomeFlag)).first()
        row.status = "resolved"
        session.add(row)
        session.commit()

    with Session(eng) as session:
        session.add(OutcomeFlag(
            source="homelab_watch", check="garage_open",
            fingerprint="homelab_watch:garage_open", summary="door open a third time",
        ))
        session.commit()  # must not raise
        rows = session.exec(select(OutcomeFlag)).all()
    assert len(rows) == 2


# ---------------------------------------------------------------------------
# 6.2 Write path / dedup — the core of the feature
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ac4_record_flag_inserts_open_row(eng):
    """AC4: record_flag on an empty DB inserts one row, status="open",
    surfaced_count=1, and returns its id."""
    from backend.agents import outcomes
    from backend.database import OutcomeFlag

    _create_open_index(eng)

    flag_id = await outcomes.record_flag("homelab_watch", "garage_open", "Garage door open 35 min")
    assert flag_id is not None

    with Session(eng) as session:
        rows = session.exec(select(OutcomeFlag)).all()
    assert len(rows) == 1
    assert rows[0].id == flag_id
    assert rows[0].status == "open"
    assert rows[0].surfaced_count == 1


@pytest.mark.asyncio
async def test_ac5_record_flag_dedups_same_source_check(eng):
    """AC5: a second record_flag with the same (source, check) returns the
    SAME id, creates NO second row, and increments surfaced_count to 2 and
    advances last_surfaced_at."""
    from backend.agents import outcomes
    from backend.database import OutcomeFlag

    _create_open_index(eng)

    id1 = await outcomes.record_flag("homelab_watch", "garage_open", "Garage door open 35 min")
    with Session(eng) as session:
        first_surfaced_at = session.get(OutcomeFlag, id1).last_surfaced_at

    id2 = await outcomes.record_flag("homelab_watch", "garage_open", "Garage door open 40 min")
    assert id2 == id1

    with Session(eng) as session:
        rows = session.exec(select(OutcomeFlag)).all()
        row = session.get(OutcomeFlag, id1)
        assert row.surfaced_count == 2
        assert row.last_surfaced_at >= first_surfaced_at
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_ac6_record_flag_after_resolved_creates_new_row(eng):
    """AC6: after resolve_flag(id, "resolved"), a subsequent record_flag with
    the same fingerprint creates a NEW row (a genuinely recurring condition
    re-raises)."""
    from backend.agents import outcomes
    from backend.database import OutcomeFlag

    _create_open_index(eng)

    id1 = await outcomes.record_flag("homelab_watch", "garage_open", "Garage door open 35 min")
    result = await outcomes.resolve_flag(id1, "resolved")
    assert result == "resolved"

    id2 = await outcomes.record_flag("homelab_watch", "garage_open", "Garage door open again")
    assert id2 != id1

    with Session(eng) as session:
        rows = session.exec(select(OutcomeFlag)).all()
    assert len(rows) == 2


@pytest.mark.asyncio
async def test_ac7_record_flag_false_positive_cooldown(eng, monkeypatch):
    """AC7: after resolve_flag(id, "false_positive"), a record_flag with the
    same fingerprint within the cooldown returns None and creates no row;
    past the cooldown it creates one."""
    from backend.agents import outcomes
    from backend.database import OutcomeFlag

    _create_open_index(eng)

    id1 = await outcomes.record_flag("homelab_watch", "garage_open", "Garage door open 35 min")
    assert await outcomes.resolve_flag(id1, "false_positive") == "false_positive"

    # Still within the (default 30-day) cooldown.
    suppressed = await outcomes.record_flag("homelab_watch", "garage_open", "Garage door open again")
    assert suppressed is None
    with Session(eng) as session:
        assert len(session.exec(select(OutcomeFlag)).all()) == 1

    # Backdate resolved_at past the cooldown window.
    with Session(eng) as session:
        row = session.get(OutcomeFlag, id1)
        row.resolved_at = datetime.utcnow() - timedelta(days=31)
        session.add(row)
        session.commit()

    id2 = await outcomes.record_flag("homelab_watch", "garage_open", "Garage door open, third time")
    assert id2 is not None
    assert id2 != id1
    with Session(eng) as session:
        assert len(session.exec(select(OutcomeFlag)).all()) == 2


@pytest.mark.asyncio
async def test_ac8_record_flag_deferred_suppresses_until_deadline(eng):
    """AC8: after resolve_flag(id, "deferred", defer_days=7), record_flag
    returns None while deferred_until is future."""
    from backend.agents import outcomes
    from backend.database import OutcomeFlag

    _create_open_index(eng)

    id1 = await outcomes.record_flag("homelab_watch", "garage_open", "Garage door open 35 min")
    result = await outcomes.resolve_flag(id1, "deferred", defer_days=7)
    assert result == "deferred"

    suppressed = await outcomes.record_flag("homelab_watch", "garage_open", "Garage door open again")
    assert suppressed is None
    with Session(eng) as session:
        assert len(session.exec(select(OutcomeFlag)).all()) == 1


@pytest.mark.asyncio
async def test_ac9_record_flag_never_raises_when_db_unavailable(eng, monkeypatch):
    """AC9: record_flag never raises: with the DB engine patched to raise, it
    returns None (and logs). Mirrors test_secrets_fallback.py's
    never-raises assertions."""
    from backend.agents import outcomes

    monkeypatch.setattr("sqlmodel.Session", _BoomSession)

    result = await outcomes.record_flag("homelab_watch", "garage_open", "Garage door open 35 min")
    assert result is None


@pytest.mark.asyncio
async def test_ac10_record_flag_noop_when_disabled(eng, monkeypatch):
    """AC10: record_flag is a no-op returning None when
    outcome_flags_enabled=False."""
    from backend.agents import outcomes
    from backend.config import get_settings
    from backend.database import OutcomeFlag

    _create_open_index(eng)

    monkeypatch.setattr(get_settings(), "outcome_flags_enabled", False)

    result = await outcomes.record_flag("homelab_watch", "garage_open", "Garage door open 35 min")
    assert result is None
    with Session(eng) as session:
        assert session.exec(select(OutcomeFlag)).all() == []


@pytest.mark.asyncio
async def test_record_flag_integrity_error_reselects_real_winner(eng, monkeypatch):
    """Spec §2.1 step 4 / outcomes.py's actual feature: record_flag's INSERT
    (branch 4) can lose a genuine TOCTOU race against a concurrent caller for
    the same fingerprint -- ux_outcomeflag_open then raises a real
    IntegrityError, which _db_insert_flag must catch (not propagate), after
    which record_flag re-SELECTs and returns the winner's actual id rather
    than a new row's id or None.

    None of AC4-AC10 exercise this: they only call record_flag() once per
    fingerprint per branch, so _db_insert_flag's `except IntegrityError`
    (outcomes.py) never fires anywhere else in this file -- AC3 only proves
    the raw SQL-level constraint exists, via direct ORM inserts, never
    through record_flag()'s own catch/reselect code path.

    Simulated the same way test_goals.py's
    test_debounce_survives_concurrent_propose_race simulates goals.propose()'s
    equivalent race: force the pre-insert dedup check
    (_db_find_active_by_fingerprint) to report "no active row" on its first
    call -- what a genuinely concurrent caller would see -- while a real
    'winner' row already exists under the fingerprint. Unlike that goals.py
    test (whose forced-None patch stays in effect for the post-race re-fetch
    too, so it never checks the re-fetched winner is correct), this restores
    the real lookup for record_flag's post-IntegrityError re-SELECT, so the
    returned id is verified to be the actual winner, not just "some non-None
    value" or "no duplicate row"."""
    from backend.agents import outcomes
    from backend.database import OutcomeFlag

    _create_open_index(eng)

    # A winning row already committed under this fingerprint -- simulates a
    # concurrent record_flag() call that inserted first.
    with Session(eng) as session:
        winner = OutcomeFlag(
            source="homelab_watch", check="garage_open",
            fingerprint="homelab_watch:garage_open", summary="winner (already committed)",
        )
        session.add(winner)
        session.commit()
        session.refresh(winner)
        winner_id = winner.id

    real_find_active = outcomes._db_find_active_by_fingerprint
    calls = {"n": 0}

    def stale_then_real(fingerprint):
        calls["n"] += 1
        if calls["n"] == 1:
            return None  # branch-1 pre-check misses the winner (the race window)
        return real_find_active(fingerprint)  # post-IntegrityError re-SELECT: real lookup

    monkeypatch.setattr(outcomes, "_db_find_active_by_fingerprint", stale_then_real)

    flag_id = await outcomes.record_flag("homelab_watch", "garage_open", "loser (raced)")

    assert calls["n"] == 2, "expected exactly one pre-check call and one post-IntegrityError re-SELECT"
    assert flag_id == winner_id, "record_flag must return the real winner's id after losing the race, not None or a new id"

    with Session(eng) as session:
        rows = session.exec(select(OutcomeFlag)).all()
    assert len(rows) == 1, "the losing insert must not leave a duplicate row behind"
    assert rows[0].summary == "winner (already committed)"


# ---------------------------------------------------------------------------
# 6.3 Close-the-loop
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ac11_resolve_flag_status_strings(eng):
    """AC11: resolve_flag returns "not_found" for an unknown id,
    "already_closed" for a non-open row, "invalid_status" for a status
    outside the five."""
    from backend.agents import outcomes

    _create_open_index(eng)

    assert await outcomes.resolve_flag(999, "resolved") == "not_found"

    flag_id = await outcomes.record_flag("homelab_watch", "garage_open", "Garage door open")
    assert await outcomes.resolve_flag(flag_id, "bogus_status") == "invalid_status"

    assert await outcomes.resolve_flag(flag_id, "resolved") == "resolved"
    assert await outcomes.resolve_flag(flag_id, "resolved") == "already_closed"


@pytest.mark.asyncio
async def test_ac11_resolve_flag_allows_deferred_and_needs_follow_up_rows(eng):
    """Pins the OTHER direction of AC11's "already_closed for a non-open row"
    wording. Per spec Section 3.5, `deferred` and `needs_follow_up` rows are
    NOT "closed" and must stay resolvable -- the sweeper flips
    deferred -> needs_follow_up and Telegram's `flag:resolved:<id>` button
    must still work on the result. `_CLOSED_STATUSES` (outcomes.py) is
    deliberately `{"resolved", "false_positive"}` only; a future engineer
    "fixing" it to match AC11's literal "non-open" wording would silently
    break this flow while the rest of the suite stayed green.
    """
    from backend.agents import outcomes
    from backend.database import OutcomeFlag

    _create_open_index(eng)

    # Direction 1: resolve_flag() called directly on a `deferred` row.
    flag_id = await outcomes.record_flag("homelab_watch", "garage_open", "Garage door open")
    assert await outcomes.resolve_flag(flag_id, "deferred", defer_days=7) == "deferred"

    result = await outcomes.resolve_flag(flag_id, "resolved")
    assert result == "resolved"
    with Session(eng) as session:
        assert session.get(OutcomeFlag, flag_id).status == "resolved"

    # Direction 2: the real sweep_deferred() path -- record -> defer ->
    # backdate deferred_until -> sweep (flips to needs_follow_up) -> resolve.
    swept_id = await outcomes.record_flag("watchdog", "dead_letters", "Dead letters piling up")
    await outcomes.resolve_flag(swept_id, "deferred", defer_days=7)

    with Session(eng) as session:
        row = session.get(OutcomeFlag, swept_id)
        row.deferred_until = datetime.utcnow() - timedelta(days=1)
        session.add(row)
        session.commit()

    flipped = await outcomes.sweep_deferred()
    assert swept_id in flipped
    with Session(eng) as session:
        assert session.get(OutcomeFlag, swept_id).status == "needs_follow_up"

    swept_result = await outcomes.resolve_flag(swept_id, "resolved")
    assert swept_result == "resolved"
    with Session(eng) as session:
        assert session.get(OutcomeFlag, swept_id).status == "resolved"


@pytest.mark.asyncio
async def test_ac12_resolve_flag_sets_fields_leaves_others_untouched(eng):
    """AC12: a successful resolve_flag sets resolved_at, resolved_by,
    resolution_note, and status, and leaves created_at/summary untouched."""
    from backend.agents import outcomes
    from backend.database import OutcomeFlag

    _create_open_index(eng)

    flag_id = await outcomes.record_flag("homelab_watch", "garage_open", "Garage door open 35 min")
    with Session(eng) as session:
        before = session.get(OutcomeFlag, flag_id)
        created_at_before = before.created_at
        summary_before = before.summary

    result = await outcomes.resolve_flag(flag_id, "resolved", note="closed it myself", by="telegram")
    assert result == "resolved"

    with Session(eng) as session:
        row = session.get(OutcomeFlag, flag_id)
    assert row.status == "resolved"
    assert row.resolved_at is not None
    assert row.resolved_by == "telegram"
    assert row.resolution_note == "closed it myself"
    assert row.created_at == created_at_before
    assert row.summary == summary_before


@pytest.mark.asyncio
async def test_resolve_flag_syncs_expected_resource_baseline(eng):
    """2026-08-21: resolving a homelab_watch 'expected:{kind}:{identifier}'
    flag (the shape check_expected_resources writes, with a {kind,
    identifier, observed_state} JSON detail blob) as "resolved" must sync
    ExpectedResource.desired_state to the observed state that was flagged --
    otherwise the exact same mismatch re-flags forever after a human already
    said "this is fine now." A resolve to a non-"resolved" status (e.g.
    false_positive/deferred) must NOT touch the baseline."""
    import json

    from backend.agents import expected_resources, outcomes
    from backend.database import ExpectedResource

    with Session(eng) as session:
        session.add(ExpectedResource(kind="docker", identifier="sonarr", key="docker:sonarr", desired_state="running"))
        session.commit()

    detail = json.dumps({"kind": "docker", "identifier": "sonarr", "observed_state": "stopped"})
    flag_id = await outcomes.record_flag(
        "homelab_watch", "expected:docker:sonarr", "sonarr is stopped but expected running", detail=detail,
    )

    result = await outcomes.resolve_flag(flag_id, "resolved", by="telegram")
    assert result == "resolved"

    rows = await expected_resources.list_expected()
    row = next(r for r in rows if r["kind"] == "docker" and r["identifier"] == "sonarr")
    assert row["desired_state"] == "stopped"

    # A false_positive resolution must NOT touch the baseline.
    with Session(eng) as session:
        session.exec(select(ExpectedResource)).first()  # sanity: table still queryable
    flag_id2 = await outcomes.record_flag(
        "homelab_watch", "expected:docker:radarr", "radarr mismatch",
        detail=json.dumps({"kind": "docker", "identifier": "radarr", "observed_state": "stopped"}),
    )
    with Session(eng) as session:
        session.add(ExpectedResource(kind="docker", identifier="radarr", key="docker:radarr", desired_state="running"))
        session.commit()
    await outcomes.resolve_flag(flag_id2, "false_positive")
    rows2 = await expected_resources.list_expected()
    row2 = next(r for r in rows2 if r["kind"] == "docker" and r["identifier"] == "radarr")
    assert row2["desired_state"] == "running"  # untouched


@pytest.mark.asyncio
async def test_resolve_flag_ignores_non_expected_resource_flags(eng):
    """A plain homelab_watch flag (e.g. "garage_open", not the
    "expected:{kind}:{identifier}" shape) resolves normally with no
    ExpectedResource side effect and no error -- _maybe_sync_expected_resource
    must be a no-op for every existing flag shape."""
    from backend.agents import outcomes

    flag_id = await outcomes.record_flag("homelab_watch", "garage_open", "Garage door open 35 min")
    result = await outcomes.resolve_flag(flag_id, "resolved")
    assert result == "resolved"


# ---------------------------------------------------------------------------
# 6.5 Read path (calibration only — the rest of §6.5 belongs to briefing's
# own test file, later rollout steps)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ac28_calibration_summary_groups_by_source_check(eng):
    """AC28: calibration_summary(days=30) groups by source:check with correct
    per-status counts and returns {} on an empty table."""
    from backend.agents import outcomes

    _create_open_index(eng)

    assert await outcomes.calibration_summary(days=30) == {}

    from backend.database import OutcomeFlag

    id1 = await outcomes.record_flag("homelab_watch", "garage_open", "Garage door open")
    await outcomes.resolve_flag(id1, "false_positive")
    # Backdate resolved_at past the cooldown so the next record_flag call for
    # this fingerprint isn't itself suppressed (AC7) -- this test is only
    # about calibration_summary's grouping/counting, not the cooldown.
    with Session(eng) as session:
        row = session.get(OutcomeFlag, id1)
        row.resolved_at = datetime.utcnow() - timedelta(days=31)
        session.add(row)
        session.commit()
    await outcomes.record_flag("homelab_watch", "garage_open", "Garage door open again")

    id3 = await outcomes.record_flag("watchdog", "dead_letters", "Dead letters piling up")
    await outcomes.resolve_flag(id3, "resolved")

    summary = await outcomes.calibration_summary(days=30)
    assert summary["homelab_watch:garage_open"] == {"false_positive": 1, "open": 1}
    assert summary["watchdog:dead_letters"] == {"resolved": 1}


@pytest.mark.asyncio
async def test_cal32_open_flags_excludes_suppressed_by_default_and_includes_on_opt_in(monkeypatch):
    """CAL32 (docs/calibration-loop-spec.md §8): open_flags() omits a
    suppressed=True row by default and includes it when
    include_suppressed=True is passed explicitly. Also pins the falsiness
    handling `_db_open_flags` relies on instead of an `is False` check: a row
    whose `suppressed` column reads back as NULL must NOT be excluded by the
    default call — `not row.suppressed` treats None the same as False.

    Uses a hand-built table (mirrors test_cal3's legacy-table technique in
    test_calibration_loop.py) rather than the shared `eng`/
    SQLModel.metadata.create_all() fixture: that fixture declares `suppressed`
    NOT NULL (OutcomeFlag.suppressed: bool = False), which would make a NULL
    row impossible to construct at all. The real
    `_ensure_outcomeflag_columns()` shim adds the column via a plain
    `ALTER TABLE ... ADD COLUMN suppressed BOOLEAN` with no NOT NULL
    constraint, so a genuinely-NULL row is a real reachable shape in
    production, not a wrong test.
    """
    e = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    monkeypatch.setattr("backend.database.engine", e)

    with e.connect() as conn:
        conn.execute(text(
            "CREATE TABLE outcomeflag ("
            "id INTEGER PRIMARY KEY, source TEXT, \"check\" TEXT, fingerprint TEXT, "
            "summary TEXT, detail TEXT, severity TEXT, status TEXT, "
            "resolved_at TEXT, resolved_by TEXT, resolution_note TEXT, "
            "deferred_until TEXT, action_log_id INTEGER, surfaced_count INTEGER, "
            "last_surfaced_at TEXT, created_at TEXT, updated_at TEXT, "
            "suppressed BOOLEAN, suppressed_reason TEXT)"
        ))
        now = "2026-08-01T00:00:00"
        conn.execute(text(
            "INSERT INTO outcomeflag (id, source, \"check\", fingerprint, summary, "
            "severity, status, surfaced_count, last_surfaced_at, created_at, updated_at, suppressed) "
            "VALUES (1, 'homelab_watch', 'garage_open', 'homelab_watch:garage_open', "
            "'garage left open', 'medium', 'open', 1, :now, :now, :now, 0)"
        ).bindparams(now=now))
        conn.execute(text(
            "INSERT INTO outcomeflag (id, source, \"check\", fingerprint, summary, "
            "severity, status, surfaced_count, last_surfaced_at, created_at, updated_at, suppressed) "
            "VALUES (2, 'watchdog', 'dead_letters', 'watchdog:dead_letters', "
            "'dead letters piling up', 'medium', 'open', 1, :now, :now, :now, 1)"
        ).bindparams(now=now))
        # suppressed deliberately omitted -> column reads back NULL.
        conn.execute(text(
            "INSERT INTO outcomeflag (id, source, \"check\", fingerprint, summary, "
            "severity, status, surfaced_count, last_surfaced_at, created_at, updated_at) "
            "VALUES (3, 'briefing', 'stale_prs', 'briefing:stale_prs', "
            "'stale PRs piling up', 'medium', 'open', 1, :now, :now, :now)"
        ).bindparams(now=now))
        conn.commit()

    with e.connect() as conn:
        row = conn.execute(text("SELECT suppressed FROM outcomeflag WHERE id = 3")).fetchone()
    assert row[0] is None

    from backend.agents import outcomes

    default_ids = {f["id"] for f in await outcomes.open_flags()}
    assert default_ids == {1, 3}

    with_suppressed_ids = {f["id"] for f in await outcomes.open_flags(include_suppressed=True)}
    assert with_suppressed_ids == {1, 2, 3}


# ---------------------------------------------------------------------------
# 6.6 Sweeper & retention (sweep_deferred only — run_watchdog wiring and
# prune_old_outcome_flags belong to later rollout steps' own test files)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ac29_sweep_deferred_flips_past_due_only(eng):
    """AC29: sweep_deferred() flips a row whose deferred_until is past to
    needs_follow_up and leaves a future-dated one alone."""
    from backend.agents import outcomes
    from backend.database import OutcomeFlag

    _create_open_index(eng)

    past_id = await outcomes.record_flag("homelab_watch", "garage_open", "Garage door open")
    await outcomes.resolve_flag(past_id, "deferred", defer_days=7)

    future_id = await outcomes.record_flag("watchdog", "dead_letters", "Dead letters piling up")
    await outcomes.resolve_flag(future_id, "deferred", defer_days=7)

    # Backdate only the first row's deferred_until into the past.
    with Session(eng) as session:
        row = session.get(OutcomeFlag, past_id)
        row.deferred_until = datetime.utcnow() - timedelta(days=1)
        session.add(row)
        session.commit()

    flipped = await outcomes.sweep_deferred()
    assert flipped == [past_id]

    with Session(eng) as session:
        assert session.get(OutcomeFlag, past_id).status == "needs_follow_up"
        assert session.get(OutcomeFlag, future_id).status == "deferred"


# ---------------------------------------------------------------------------
# 6.7 Invariants
# ---------------------------------------------------------------------------

def test_ac32_outcomes_module_does_not_import_broker():
    """AC32: backend/agents/outcomes.py does not import backend.safety.broker
    (or anything from the safety broker/websocket layer) — step 1 is the
    foundation only, nothing writes yet. Matches test_tools.py's existing
    test_no_dispatcher_references_a_write_function: only real `import`/`from`
    lines are checked, so the module docstring is free to NAME the broker in
    prose (as it does, to document the invariant) without tripping this."""
    import inspect

    from backend.agents import outcomes

    module_src = inspect.getsource(outcomes)
    import_lines = [
        ln for ln in module_src.splitlines()
        if ln.strip().startswith(("import ", "from "))
    ]
    for ln in import_lines:
        assert "broker" not in ln, f"outcomes.py imports the broker: {ln!r}"
        assert "websocket" not in ln, f"outcomes.py imports the websocket layer: {ln!r}"


def test_ac33_no_session_or_orm_crosses_await():
    """AC33: no Session or ORM object crosses an await in outcomes.py — every
    DB helper is sync (def, not async def) and every async wrapper (async
    def) calls it via asyncio.to_thread. Source-level check, since this is an
    invariant about the shape of the code, not its runtime behaviour."""
    import ast
    import pathlib

    src = pathlib.Path(__file__).parent.parent / "backend" / "agents" / "outcomes.py"
    tree = ast.parse(src.read_text(encoding="utf-8"))

    sync_helpers = []
    async_public = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name.startswith("_db_"):
            sync_helpers.append(node.name)
        if isinstance(node, ast.AsyncFunctionDef) and not node.name.startswith("_"):
            async_public.append(node)

    assert sync_helpers, "expected at least one sync _db_* helper"
    assert async_public, "expected at least one public async function"

    src_text = src.read_text(encoding="utf-8")
    for fn in async_public:
        fn_src = ast.get_source_segment(src_text, fn) or ""
        called = {n.func.attr for n in ast.walk(fn) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
        # Every _db_* name referenced inside a public async function must be
        # reached only via asyncio.to_thread(_db_x, ...), never called directly
        # (which would run it inline on the event loop thread, not necessarily
        # a violation by itself, but our contract is asyncio.to_thread only).
        direct_db_calls = [
            n for n in ast.walk(fn)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name)
            and n.func.id.startswith("_db_")
        ]
        assert not direct_db_calls, f"{fn.name} calls a _db_* helper directly instead of via asyncio.to_thread: {[n.func.id for n in direct_db_calls]}"
        assert "asyncio.to_thread" in fn_src or not any(
            isinstance(n, ast.Name) and n.id.startswith("_db_") for n in ast.walk(fn)
        ), f"{fn.name} references a _db_* helper but never calls asyncio.to_thread"
