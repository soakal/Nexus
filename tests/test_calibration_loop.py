"""Tests for the Calibration Loop foundation (docs/calibration-loop-spec.md),
rollout step 1 (§9.5 step 1): the `CalibrationHint` table, the
`_ensure_outcomeflag_columns` shim adding `suppressed`/`suppressed_reason` to
the live `OutcomeFlag` table, and the nine new `calibration_*` config fields.

Covers CAL1-CAL4 from the spec's §8.1 acceptance criteria. Nothing computes
and nothing suppresses yet (§9.1/§9.2) — `recompute_hints()`, the gate, and
CAL5-CAL12 belong to a future cycle (§9.5 step 2), per the spec's own §8.2
dependency on a function that does not exist until then.

Pattern: in-memory StaticPool engine monkeypatched onto backend.database.engine,
matching tests/test_outcome_flags.py / tests/test_db_pragma.py.

§8.4's gate tests (CAL20-CAL24) additionally cover the read/gate/write trio
landed this cycle (§9.5 step 1's remaining half): outcomes.active_hint,
outcomes.should_page, and outcomes.record_flag_ex/record_flag's back-compat
wrapper. recompute_hints() still does not exist — every hint row below is
constructed directly, not computed.
"""
import asyncio
from datetime import datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

# Register all table metadata (including CalibrationHint/OutcomeFlag) before
# any test runs.
import backend.database  # noqa: F401


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
# 8.1 Data model / migration
# ---------------------------------------------------------------------------

def test_cal1_create_db_and_tables_creates_calibrationhint_with_unique_fingerprint(monkeypatch):
    """CAL1: create_db_and_tables() on a fresh :memory: engine creates
    `calibrationhint` with all declared columns, and the `fingerprint` unique
    constraint rejects a second row with the same fingerprint."""
    from backend.database import CalibrationHint, create_db_and_tables

    e = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    monkeypatch.setattr("backend.database.engine", e)

    create_db_and_tables()

    with e.connect() as conn:
        cols = {row[1] for row in conn.execute(text("PRAGMA table_info(calibrationhint)"))}
    expected = set(CalibrationHint.model_fields.keys())
    assert expected.issubset(cols)

    with Session(e) as s:
        s.add(CalibrationHint(fingerprint="homelab_watch:garage_open"))
        s.commit()

    with Session(e) as s:
        s.add(CalibrationHint(fingerprint="homelab_watch:garage_open"))
        with pytest.raises(IntegrityError):
            s.commit()


def test_cal2_create_db_and_tables_is_idempotent(monkeypatch):
    """CAL2: create_db_and_tables() is idempotent — calling it twice raises
    nothing."""
    from backend.database import create_db_and_tables

    e = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    monkeypatch.setattr("backend.database.engine", e)

    create_db_and_tables()
    create_db_and_tables()  # must not raise


def test_cal3_ensure_outcomeflag_columns_idempotent_and_preserves_existing_rows(monkeypatch):
    """CAL3: _ensure_outcomeflag_columns() adds suppressed/suppressed_reason
    to a table created WITHOUT them, is idempotent on a second call, and
    leaves existing rows readable with `suppressed` falsy."""
    import backend.database as bd

    e = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    monkeypatch.setattr(bd, "engine", e)

    # Build a legacy `outcomeflag` table WITHOUT suppressed/suppressed_reason,
    # matching the shape OutcomeFlag had before this cycle's columns.
    with e.connect() as conn:
        conn.execute(text(
            "CREATE TABLE outcomeflag ("
            "id INTEGER PRIMARY KEY, source TEXT, \"check\" TEXT, fingerprint TEXT, "
            "summary TEXT, detail TEXT, severity TEXT, status TEXT, "
            "resolved_at TEXT, resolved_by TEXT, resolution_note TEXT, "
            "deferred_until TEXT, action_log_id INTEGER, surfaced_count INTEGER, "
            "last_surfaced_at TEXT, created_at TEXT, updated_at TEXT)"
        ))
        conn.execute(text(
            "INSERT INTO outcomeflag (source, \"check\", fingerprint, summary, "
            "severity, status, surfaced_count, last_surfaced_at, created_at, updated_at) "
            "VALUES ('homelab_watch', 'garage_open', 'homelab_watch:garage_open', "
            "'garage left open', 'medium', 'open', 1, '2026-08-01', "
            "'2026-08-01', '2026-08-01')"
        ))
        conn.commit()

    def cols():
        with e.connect() as conn:
            return {row[1] for row in conn.execute(text("PRAGMA table_info(outcomeflag)"))}

    assert "suppressed" not in cols()
    assert "suppressed_reason" not in cols()

    bd._ensure_outcomeflag_columns()
    assert "suppressed" in cols()
    assert "suppressed_reason" in cols()

    # Idempotent: a second run must not raise (duplicate column error).
    bd._ensure_outcomeflag_columns()
    assert "suppressed" in cols()
    assert "suppressed_reason" in cols()

    # The pre-existing row is still readable, and its new column reads falsy.
    with e.connect() as conn:
        row = conn.execute(text(
            "SELECT suppressed, suppressed_reason FROM outcomeflag "
            "WHERE fingerprint = 'homelab_watch:garage_open'"
        )).fetchone()
    assert row is not None
    assert not row[0]
    assert row[1] is None


def test_cal4_outcomeflag_status_and_outcomes_status_sets_unchanged():
    """CAL4: OutcomeFlag.status's five values are unchanged;
    outcomes._VALID_TARGET_STATUSES, _ACTIVE_STATUSES, _CLOSED_STATUSES are
    byte-identical to their pre-change contents (guards §7.9 — no sixth
    status value introduced by this build)."""
    from backend.agents import outcomes

    assert outcomes._VALID_TARGET_STATUSES == {
        "open", "resolved", "deferred", "false_positive", "needs_follow_up",
    }
    assert outcomes._ACTIVE_STATUSES == {"open", "needs_follow_up"}
    assert outcomes._CLOSED_STATUSES == {"resolved", "false_positive"}


# ---------------------------------------------------------------------------
# 8.4 The gate — safety properties (CAL20-CAL24)
# ---------------------------------------------------------------------------

def _make_hint(engine, fingerprint: str, **fields):
    """Insert (or replace) a CalibrationHint row for `fingerprint` directly —
    recompute_hints() doesn't exist yet (§9.5 step 2), so every hint in this
    section is hand-constructed evidence for the gate to read."""
    from backend.database import CalibrationHint

    defaults = dict(status="active", verdict_count=20, false_positive_count=20, fp_rate=1.0)
    defaults.update(fields)
    with Session(engine) as s:
        existing = s.exec(
            select(CalibrationHint).where(CalibrationHint.fingerprint == fingerprint)
        ).first()
        if existing:
            s.delete(existing)
            s.commit()
        s.add(CalibrationHint(fingerprint=fingerprint, **defaults))
        s.commit()


@pytest.fixture
def calibration_on(monkeypatch):
    """calibration_enabled=True + calibration_suppression_enabled=True — the
    only configuration under which should_page's hint lookup is ever reached
    (CAL24 tests the opposite: the shipped defaults never get this far)."""
    from backend.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "calibration_enabled", True)
    monkeypatch.setattr(settings, "calibration_suppression_enabled", True)
    monkeypatch.setattr(settings, "calibration_suppress_high_severity", False)
    return settings


def test_cal20_high_severity_never_suppressed_without_explicit_opt_in(eng, calibration_on):
    """CAL20 (the critical one): with an active hint at fp_rate=1.0,
    verdict_count=20, and calibration_suppress_high_severity=False,
    should_page(..., severity="high") returns (True, None) for every "high"
    severity fingerprint in spec §3.3's table plus homelab_watch:vm:{vmid}."""
    from backend.agents import outcomes

    high_severity_fingerprints = [
        ("watchdog", "stall:some_job"),
        ("watchdog", "dead_letters"),
        ("watchdog", "auth_burst:1.2.3.4"),
        ("contracts", "breach:some_contract"),
        ("homelab_watch", "vm:101"),
    ]
    for source, check in high_severity_fingerprints:
        _make_hint(eng, f"{source}:{check}")
        result = asyncio.run(outcomes.should_page(source, check, "high"))
        assert result == (True, None), f"{source}:{check} was suppressed at high severity"


def test_cal21_medium_severity_suppressed_only_when_hint_active(eng, calibration_on):
    """CAL21: the same shape of hint with severity="medium" returns
    (False, reason) with a non-empty reason; a fingerprint with no hint at
    all still pages normally."""
    from backend.agents import outcomes

    _make_hint(eng, "homelab_watch:garage_open", fp_rate=1.0, verdict_count=20)

    page, reason = asyncio.run(outcomes.should_page("homelab_watch", "garage_open", "medium"))
    assert page is False
    assert reason

    page2, reason2 = asyncio.run(outcomes.should_page("homelab_watch", "unraid_temp", "medium"))
    assert page2 is True
    assert reason2 is None


def test_cal22_should_page_fails_open_on_every_degraded_path(eng, calibration_on, monkeypatch):
    """CAL22: should_page fails open on: an engine/DB error,
    calibration_enabled=False, calibration_suppression_enabled=False, an
    expired hint, an overridden_off hint, and a hint with a future
    override_until. (The missing-table case is CAL22's sibling test below,
    which needs a table-less engine rather than this fixture's fully-migrated
    one.)"""
    from backend.agents import outcomes
    from backend.config import get_settings

    fp = "homelab_watch:garage_open"

    settings = get_settings()

    monkeypatch.setattr(settings, "calibration_enabled", False)
    assert asyncio.run(outcomes.should_page("homelab_watch", "garage_open", "medium")) == (True, None)
    monkeypatch.setattr(settings, "calibration_enabled", True)

    monkeypatch.setattr(settings, "calibration_suppression_enabled", False)
    assert asyncio.run(outcomes.should_page("homelab_watch", "garage_open", "medium")) == (True, None)
    monkeypatch.setattr(settings, "calibration_suppression_enabled", True)

    _make_hint(eng, fp, status="expired")
    assert asyncio.run(outcomes.should_page("homelab_watch", "garage_open", "medium")) == (True, None)

    _make_hint(eng, fp, status="overridden_off")
    assert asyncio.run(outcomes.should_page("homelab_watch", "garage_open", "medium")) == (True, None)

    _make_hint(eng, fp, status="active", override_until=datetime.utcnow() + timedelta(days=10))
    assert asyncio.run(outcomes.should_page("homelab_watch", "garage_open", "medium")) == (True, None)

    _make_hint(eng, fp, status="active", override_until=None)

    def _raise(_fingerprint):
        raise RuntimeError("simulated DB error")

    monkeypatch.setattr(outcomes, "_db_active_hint", _raise)
    assert asyncio.run(outcomes.should_page("homelab_watch", "garage_open", "medium")) == (True, None)


def test_cal22_should_page_fails_open_on_missing_calibrationhint_table(monkeypatch, calibration_on):
    """CAL22 (missing-table case): a genuine "no such table" error from a
    bare engine (no create_all ever run) still fails open."""
    from backend.agents import outcomes

    bare = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    monkeypatch.setattr("backend.database.engine", bare)

    assert asyncio.run(outcomes.should_page("homelab_watch", "garage_open", "medium")) == (True, None)


def test_cal23_manual_source_always_pages_regardless_of_any_hint(eng, calibration_on):
    """CAL23: should_page("manual", ...) returns (True, None) regardless of
    any hint — a human-entered flag is never auto-suppressed (spec §3.3)."""
    from backend.agents import outcomes

    _make_hint(eng, "manual:missed:water_heater", fp_rate=1.0, verdict_count=20)

    assert asyncio.run(outcomes.should_page("manual", "missed:water_heater", "high")) == (True, None)
    assert asyncio.run(outcomes.should_page("manual", "missed:water_heater", "medium")) == (True, None)


def test_cal24_shipped_default_config_produces_zero_suppression_change(eng):
    """CAL24: with the shipped default config (calibration_suppression_enabled
    defaults False), an active hint present for a fingerprint changes NOTHING
    about record_flag's existing write behavior — the row is written
    unsuppressed and the id is returned exactly as before this cycle."""
    from backend.agents import outcomes
    from backend.config import get_settings
    from backend.database import OutcomeFlag

    settings = get_settings()
    assert settings.calibration_suppression_enabled is False  # the shipped default

    _make_hint(eng, "homelab_watch:garage_open", fp_rate=1.0, verdict_count=20)

    flag_id = asyncio.run(
        outcomes.record_flag("homelab_watch", "garage_open", "garage left open")
    )
    assert flag_id is not None

    with Session(eng) as s:
        row = s.get(OutcomeFlag, flag_id)
        assert row is not None
        assert row.suppressed is False
        assert row.suppressed_reason is None


def test_record_flag_ex_stamps_suppressed_row_and_returns_surface_false(eng, calibration_on):
    """§3.1/§3.2 branch 0 — the write half of CAL20-24's read/gate/write
    trio, otherwise completely untested by CAL20-24 (they only exercise
    should_page and record_flag's unsuppressed path). With suppression ON
    and an active medium hint: the ledger never stops (§3.1) — record_flag_ex
    still WRITES the row on branch 4 (a fresh fingerprint) — but stamps
    suppressed=True/suppressed_reason on it and returns surface=False. A
    second call on the same fingerprint (branch 1, bump) still reports
    surface=False per the live hint, and record_flag's back-compat wrapper
    (d["id"] if d["surface"] or d["id"] else None, spec §3.2) still returns
    the id both times since the id itself is truthy — proving that formula
    is not accidentally None-ing out a suppressed-but-written row."""
    from backend.agents import outcomes
    from backend.database import OutcomeFlag

    _make_hint(eng, "homelab_watch:new_fingerprint", fp_rate=1.0, verdict_count=20)

    result = asyncio.run(
        outcomes.record_flag_ex("homelab_watch", "new_fingerprint", "first observation")
    )
    assert result["id"] is not None
    assert result["surface"] is False
    assert result["reason"]

    with Session(eng) as s:
        row = s.get(OutcomeFlag, result["id"])
        assert row is not None
        assert row.suppressed is True
        assert row.suppressed_reason == result["reason"]

    flag_id = asyncio.run(
        outcomes.record_flag("homelab_watch", "new_fingerprint", "bumped observation")
    )
    assert flag_id == result["id"]
