"""Tests for the Calibration Loop (docs/calibration-loop-spec.md).

§8.1 (CAL1-CAL4, rollout §9.5 step 1): the `CalibrationHint` table, the
`_ensure_outcomeflag_columns` shim adding `suppressed`/`suppressed_reason` to
the live `OutcomeFlag` table, and the nine new `calibration_*` config fields.

§8.4 (CAL20-CAL24, also step 1): the read/gate/write trio — outcomes.
active_hint, outcomes.should_page, and outcomes.record_flag_ex/record_flag's
back-compat wrapper. Every hint row in this section is constructed directly
via `_make_hint` (recompute_hints() did not exist yet when this section was
written) — the gate is tested independent of the aggregation that feeds it.

§8.2/§8.3 (CAL5-CAL19, rollout §9.5 step 2, this cycle):
`backend.agents.calibration.recompute_hints()` — the §2.3 honest-denominator
aggregation over real `OutcomeFlag` rows (built via `_make_flag`/
`_make_verdicts` below) plus the full §2.4/§2.5 persisted state machine
(activate, hysteresis-clear, mandatory re-probation expiry, override
stickiness, the CAL18 un-suppress side effect, the CAL19 notify-once-per-
activation contract). Still nothing pages differently as a result —
`calibration_suppression_enabled` stays False by default (outcomes.py, prior
cycle) — recompute_hints() only computes and persists hints.

CAL47-CAL49: calibration.py's own invariants (no broker import, no Session/
ORM crossing an await, zero LLM calls), mirroring outcomes.py's AC32/AC33 and
test_spend_report.py's blanket LLM-label guard.

Pattern: in-memory StaticPool engine monkeypatched onto backend.database.engine,
matching tests/test_outcome_flags.py / tests/test_db_pragma.py.
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


# ---------------------------------------------------------------------------
# Helpers for §8.2/§8.3 — build real OutcomeFlag rows for recompute_hints()
# to scan, rather than hand-constructing CalibrationHint rows directly.
# ---------------------------------------------------------------------------

def _make_flag(
    engine, *, fingerprint, source=None, check=None, status="open",
    resolved_by=None, resolved_at=None, suppressed=False, surfaced_count=1,
    severity="medium", created_at=None,
):
    """Insert one OutcomeFlag row directly. recompute_hints() reads real
    OutcomeFlag rows, so §8.2/§8.3's tests build the exact row shapes the
    spec's aggregation rules key off (status, resolved_by, suppressed,
    surfaced_count, created_at) rather than going through record_flag()'s
    dedup, which would collapse them into one row. Returns the new row's id."""
    from backend.database import OutcomeFlag

    if source is None or check is None:
        source, check = fingerprint.split(":", 1)
    with Session(engine) as s:
        row = OutcomeFlag(
            source=source, check=check, fingerprint=fingerprint,
            summary="test flag", status=status, resolved_by=resolved_by,
            resolved_at=resolved_at, suppressed=suppressed,
            surfaced_count=surfaced_count, severity=severity,
        )
        if created_at is not None:
            row.created_at = created_at
        s.add(row)
        s.commit()
        s.refresh(row)
        return row.id


def _make_verdicts(engine, fingerprint, *, false_positive, resolved, created_at=None):
    """Insert `false_positive` false_positive rows + `resolved` resolved rows
    (both resolved_by="telegram", i.e. genuine human verdicts) for
    `fingerprint` — the minimal shape recompute_hints() needs to produce a
    given verdict_count/fp_rate."""
    for _ in range(false_positive):
        _make_flag(engine, fingerprint=fingerprint, status="false_positive",
                    resolved_by="telegram", created_at=created_at)
    for _ in range(resolved):
        _make_flag(engine, fingerprint=fingerprint, status="resolved",
                    resolved_by="telegram", created_at=created_at)


# ---------------------------------------------------------------------------
# 8.2 The aggregation — the honest denominator (CAL5-CAL12)
# ---------------------------------------------------------------------------

def test_cal5_honest_denominator_counts_only_human_verdicts(eng):
    """CAL5: 6 false_positive + 3 resolved (both resolved_by="telegram") for
    one fingerprint -> recompute_hints() writes verdict_count=9,
    false_positive_count=6, fp_rate==pytest.approx(0.667)."""
    from backend.agents import calibration
    from backend.database import CalibrationHint

    fp = "homelab_watch:garage_open"
    _make_verdicts(eng, fp, false_positive=6, resolved=3)

    asyncio.run(calibration.recompute_hints())

    with Session(eng) as s:
        hint = s.exec(select(CalibrationHint).where(CalibrationHint.fingerprint == fp)).first()
    assert hint is not None
    assert hint.verdict_count == 9
    assert hint.false_positive_count == 6
    assert hint.fp_rate == pytest.approx(0.667, abs=0.001)


def test_cal6_auto_cleared_rows_excluded_from_both_numerator_and_denominator(eng):
    """CAL6: rows with resolved_by="auto:condition_cleared" are counted in
    auto_cleared_count and excluded from both numerator and denominator —
    6 FP + 3 auto-cleared yields verdict_count=6, fp_rate==1.0,
    auto_cleared_count=3 (Finding A's fix)."""
    from backend.agents import calibration
    from backend.database import CalibrationHint

    fp = "homelab_watch:garage_open"
    for _ in range(6):
        _make_flag(eng, fingerprint=fp, status="false_positive", resolved_by="telegram")
    for _ in range(3):
        _make_flag(eng, fingerprint=fp, status="resolved", resolved_by="auto:condition_cleared")

    asyncio.run(calibration.recompute_hints())

    with Session(eng) as s:
        hint = s.exec(select(CalibrationHint).where(CalibrationHint.fingerprint == fp)).first()
    assert hint.verdict_count == 6
    assert hint.fp_rate == 1.0
    assert hint.auto_cleared_count == 3


def test_cal7_open_deferred_needs_follow_up_contribute_to_neither(eng):
    """CAL7: rows still open, deferred, or needs_follow_up contribute to
    neither verdict_count nor false_positive_count."""
    from backend.agents import calibration
    from backend.database import CalibrationHint

    fp = "homelab_watch:unraid_temp"
    _make_flag(eng, fingerprint=fp, status="open")
    _make_flag(eng, fingerprint=fp, status="deferred")
    _make_flag(eng, fingerprint=fp, status="needs_follow_up")

    asyncio.run(calibration.recompute_hints())

    with Session(eng) as s:
        hint = s.exec(select(CalibrationHint).where(CalibrationHint.fingerprint == fp)).first()
    assert hint is not None
    assert hint.verdict_count == 0
    assert hint.false_positive_count == 0


def test_cal8_suppressed_rows_excluded_from_verdict_summed_into_suppressed_surfacings(eng):
    """CAL8: rows with suppressed=True are excluded from verdict_count and
    summed into suppressed_surfacings via their surfaced_count."""
    from backend.agents import calibration
    from backend.database import CalibrationHint

    fp = "homelab_watch:garage_open"
    _make_flag(eng, fingerprint=fp, status="false_positive", resolved_by="telegram",
               suppressed=True, surfaced_count=5)
    _make_flag(eng, fingerprint=fp, status="open", suppressed=True, surfaced_count=12)
    _make_flag(eng, fingerprint=fp, status="resolved", resolved_by="telegram")

    asyncio.run(calibration.recompute_hints())

    with Session(eng) as s:
        hint = s.exec(select(CalibrationHint).where(CalibrationHint.fingerprint == fp)).first()
    assert hint.verdict_count == 1  # only the one un-suppressed resolved row
    assert hint.suppressed_surfacings == 17  # 5 + 12


def test_cal9_rows_older_than_window_days_are_excluded(eng):
    """CAL9: rows older than calibration_window_days are excluded — no hint
    row is created when every row for a fingerprint falls outside the
    window."""
    from backend.agents import calibration
    from backend.database import CalibrationHint

    fp = "homelab_watch:garage_open"
    old = datetime.utcnow() - timedelta(days=45)
    _make_flag(eng, fingerprint=fp, status="false_positive", resolved_by="telegram", created_at=old)

    asyncio.run(calibration.recompute_hints())

    with Session(eng) as s:
        hint = s.exec(select(CalibrationHint).where(CalibrationHint.fingerprint == fp)).first()
    assert hint is None


def test_cal10_manual_missed_rows_excluded_entirely_no_hint_row_created(eng):
    """CAL10: source="manual", check="missed:foo" rows are excluded entirely
    — no hint row is created for them (guards §6)."""
    from backend.agents import calibration
    from backend.database import CalibrationHint

    fp = "manual:missed:water_heater"
    for _ in range(10):
        _make_flag(eng, fingerprint=fp, source="manual", check="missed:water_heater",
                    status="false_positive", resolved_by="telegram")

    result = asyncio.run(calibration.recompute_hints())

    with Session(eng) as s:
        hint = s.exec(select(CalibrationHint).where(CalibrationHint.fingerprint == fp)).first()
    assert hint is None
    assert fp not in result["activated"]
    assert result["scanned"] == 0


def test_cal11_empty_table_returns_zeroed_scanned_and_creates_no_rows(eng):
    """CAL11: recompute_hints() on an empty table returns
    {"scanned": 0, ...} and creates no rows."""
    from backend.agents import calibration
    from backend.database import CalibrationHint

    result = asyncio.run(calibration.recompute_hints())

    assert result["scanned"] == 0
    assert result["activated"] == []
    assert result["expired"] == []
    assert result["unchanged"] == 0
    assert result["skipped_override"] == []

    with Session(eng) as s:
        assert s.exec(select(CalibrationHint)).all() == []


def test_cal12_recompute_hints_never_raises_when_db_unavailable(monkeypatch):
    """CAL12: recompute_hints() never raises: with backend.database.engine
    patched to raise, it returns a zeroed dict and logs (mirrors AC9's
    never-raises assertion). A bare engine with no create_all() run against
    it means the first query genuinely raises "no such table"."""
    from backend.agents import calibration

    bare = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    monkeypatch.setattr("backend.database.engine", bare)

    result = asyncio.run(calibration.recompute_hints())

    assert result == {
        "scanned": 0, "activated": [], "expired": [], "unchanged": 0, "skipped_override": [],
    }


# ---------------------------------------------------------------------------
# 8.3 Threshold state machine (CAL13-CAL19)
# ---------------------------------------------------------------------------

def test_cal13_threshold_crossing_activates_with_state_fields_set(eng):
    """CAL13: fp_rate=0.70, verdict_count=9 -> hint status="active",
    first_active_at set, expires_at == first_active_at +
    calibration_hint_max_days, reason non-empty."""
    from backend.agents import calibration
    from backend.config import get_settings
    from backend.database import CalibrationHint

    fp = "homelab_watch:garage_open"
    _make_verdicts(eng, fp, false_positive=7, resolved=3)  # 7/10 == 0.70

    before = datetime.utcnow()
    asyncio.run(calibration.recompute_hints())
    after = datetime.utcnow()

    with Session(eng) as s:
        hint = s.exec(select(CalibrationHint).where(CalibrationHint.fingerprint == fp)).first()
    assert hint.status == "active"
    assert hint.verdict_count == 10
    assert hint.fp_rate == pytest.approx(0.70)
    assert hint.first_active_at is not None
    assert before <= hint.first_active_at <= after
    max_days = get_settings().calibration_hint_max_days
    assert hint.expires_at == hint.first_active_at + timedelta(days=max_days)
    assert hint.reason


def test_cal14_below_min_verdicts_floor_never_activates(eng):
    """CAL14: fp_rate=1.0 but verdict_count=3 (< calibration_min_verdicts=5)
    -> no active hint (Finding A's sample-size guard, tested directly). The
    row is still written — RISK: CalibrationHint.status defaults to "active"
    on the model, so a below-threshold/watching row must be stamped
    "expired" explicitly or step 7's gate would suppress everything."""
    from backend.agents import calibration
    from backend.database import CalibrationHint

    fp = "homelab_watch:garage_open"
    _make_verdicts(eng, fp, false_positive=3, resolved=0)  # n=3 < min_verdicts=5

    asyncio.run(calibration.recompute_hints())

    with Session(eng) as s:
        hint = s.exec(select(CalibrationHint).where(CalibrationHint.fingerprint == fp)).first()
    assert hint is not None
    assert hint.status != "active"
    assert hint.verdict_count == 3


def test_cal15_hysteresis_gap_prevents_flapping(eng):
    """CAL15: an active hint whose rate falls to 0.50 (between clear=0.40
    and suppress=0.60) stays active; once it drops below 0.40 it flips to
    expired."""
    from backend.agents import calibration
    from backend.database import CalibrationHint

    fp = "homelab_watch:garage_open"
    _make_verdicts(eng, fp, false_positive=7, resolved=3)  # 0.70 -> activates
    asyncio.run(calibration.recompute_hints())

    with Session(eng) as s:
        hint = s.exec(select(CalibrationHint).where(CalibrationHint.fingerprint == fp)).first()
    assert hint.status == "active"
    first_active_at = hint.first_active_at

    # cumulative: FP=10, resolved=10, total=20 -> rate 0.50 (between the two
    # thresholds).
    _make_verdicts(eng, fp, false_positive=3, resolved=7)
    asyncio.run(calibration.recompute_hints())

    with Session(eng) as s:
        hint = s.exec(select(CalibrationHint).where(CalibrationHint.fingerprint == fp)).first()
    assert hint.status == "active"
    assert hint.fp_rate == pytest.approx(0.50)
    assert hint.first_active_at == first_active_at  # never reset while still active

    # cumulative: FP stays 10, resolved=16, total=26 -> rate ~0.3846 < 0.40.
    _make_verdicts(eng, fp, false_positive=0, resolved=6)
    asyncio.run(calibration.recompute_hints())

    with Session(eng) as s:
        hint = s.exec(select(CalibrationHint).where(CalibrationHint.fingerprint == fp)).first()
    assert hint.status == "expired"
    assert hint.fp_rate < 0.40


def test_verifier_boundary_values_are_inclusive_to_activate_exclusive_to_clear(eng):
    """Verifier gap (CAL13/14/15 leave the exact boundary values untested):
    activation uses `fp_rate >= calibration_fp_threshold` and
    `verdict_count >= calibration_min_verdicts` (§2.4), so a fingerprint
    sitting exactly AT n=5/rate=0.60 must activate, not just above it (CAL14
    only proves n=3 stays inactive, CAL13 only exercises n=10/rate=0.70).
    Hysteresis clearing uses strict `fp_rate < calibration_clear_threshold`
    (§2.5a "drops... when fp_rate < 0.40"), so a hint sitting exactly AT
    fp_rate=0.40 must stay active — CAL15 only tests 0.50 (stays active) and
    ~0.3846 (expires), skipping the boundary itself, which is exactly where
    a `<=`-vs-`<` off-by-one would hide."""
    from backend.agents import calibration
    from backend.database import CalibrationHint

    fp = "homelab_watch:garage_open"

    # n=5 (== calibration_min_verdicts), fp_rate=3/5=0.60 (== calibration_fp_threshold).
    # Both floor conditions met at the exact boundary -> must activate.
    _make_verdicts(eng, fp, false_positive=3, resolved=2)
    asyncio.run(calibration.recompute_hints())
    with Session(eng) as s:
        hint = s.exec(select(CalibrationHint).where(CalibrationHint.fingerprint == fp)).first()
    assert hint.verdict_count == 5
    assert hint.fp_rate == pytest.approx(0.60)
    assert hint.status == "active"

    # Cumulative FP=4, resolved=6, total=10 -> fp_rate=0.40 exactly
    # (== calibration_clear_threshold). Strictly-less-than semantics mean
    # this must NOT clear.
    _make_verdicts(eng, fp, false_positive=1, resolved=4)
    asyncio.run(calibration.recompute_hints())
    with Session(eng) as s:
        hint = s.exec(select(CalibrationHint).where(CalibrationHint.fingerprint == fp)).first()
    assert hint.verdict_count == 10
    assert hint.fp_rate == pytest.approx(0.40)
    assert hint.status == "active"

    # One more resolved verdict, no new FP: FP=4, resolved=7, total=11 ->
    # fp_rate=4/11≈0.3636, just below the clear threshold -> must expire.
    _make_verdicts(eng, fp, false_positive=0, resolved=1)
    asyncio.run(calibration.recompute_hints())
    with Session(eng) as s:
        hint = s.exec(select(CalibrationHint).where(CalibrationHint.fingerprint == fp)).first()
    assert hint.verdict_count == 11
    assert hint.fp_rate < 0.40
    assert hint.status == "expired"


def test_cal16_mandatory_reprobation_expires_even_at_perfect_fp_rate(eng):
    """CAL16: an active hint whose expires_at has passed flips to expired on
    the next recompute even at fp_rate=1.0 — the Finding D guard. Without
    this, a suppressed rule that never gets re-observed by a human would
    stay active forever on stale evidence."""
    from backend.agents import calibration
    from backend.database import CalibrationHint

    fp = "homelab_watch:garage_open"
    _make_verdicts(eng, fp, false_positive=10, resolved=0)  # fp_rate=1.0
    asyncio.run(calibration.recompute_hints())

    with Session(eng) as s:
        hint = s.exec(select(CalibrationHint).where(CalibrationHint.fingerprint == fp)).first()
    assert hint.status == "active"

    # Force the mandatory re-probation window to have already passed.
    with Session(eng) as s:
        hint = s.exec(select(CalibrationHint).where(CalibrationHint.fingerprint == fp)).first()
        hint.expires_at = datetime.utcnow() - timedelta(days=1)
        s.add(hint)
        s.commit()

    asyncio.run(calibration.recompute_hints())

    with Session(eng) as s:
        hint = s.exec(select(CalibrationHint).where(CalibrationHint.fingerprint == fp)).first()
    assert hint.status == "expired"
    assert hint.fp_rate == pytest.approx(1.0)


def test_cal17_override_until_future_blocks_reactivation(eng):
    """CAL17: a hint with override_until in the future is not re-activated
    by a recompute even when every threshold condition is met; it appears
    in the return dict's skipped_override."""
    from backend.agents import calibration
    from backend.database import CalibrationHint

    fp = "homelab_watch:garage_open"
    _make_verdicts(eng, fp, false_positive=8, resolved=2)  # fp_rate=0.80, n=10

    with Session(eng) as s:
        s.add(CalibrationHint(
            fingerprint=fp, status="overridden_off",
            override_until=datetime.utcnow() + timedelta(days=30),
        ))
        s.commit()

    result = asyncio.run(calibration.recompute_hints())

    with Session(eng) as s:
        hint = s.exec(select(CalibrationHint).where(CalibrationHint.fingerprint == fp)).first()
    assert hint.status == "overridden_off"  # untouched by this recompute
    assert fp in result["skipped_override"]
    assert fp not in result["activated"]


def test_cal18_transition_out_of_active_clears_suppressed_on_open_flag(eng):
    """CAL18: on any transition out of active, an existing status="open"
    OutcomeFlag row for that fingerprint has suppressed set back to False
    and suppressed_reason to None (§2.5's side effect) — otherwise a rule
    that un-suppresses while its condition is still live would stay
    invisible in /flags until it happened to re-fire."""
    from backend.agents import calibration
    from backend.database import CalibrationHint, OutcomeFlag

    fp = "homelab_watch:garage_open"
    _make_verdicts(eng, fp, false_positive=10, resolved=0)  # fp_rate=1.0 -> activates
    asyncio.run(calibration.recompute_hints())

    with Session(eng) as s:
        hint = s.exec(select(CalibrationHint).where(CalibrationHint.fingerprint == fp)).first()
    assert hint.status == "active"

    open_id = _make_flag(eng, fingerprint=fp, status="open", suppressed=True)
    with Session(eng) as s:
        row = s.get(OutcomeFlag, open_id)
        row.suppressed_reason = "calibration:homelab_watch:garage_open fp_rate=1.00 n=10"
        s.add(row)
        s.commit()

    # Force re-probation expiry so this recompute transitions the hint OUT
    # of active (independent of the rate) -- the transition CAL18 tests.
    with Session(eng) as s:
        hint = s.exec(select(CalibrationHint).where(CalibrationHint.fingerprint == fp)).first()
        hint.expires_at = datetime.utcnow() - timedelta(days=1)
        s.add(hint)
        s.commit()

    asyncio.run(calibration.recompute_hints())

    with Session(eng) as s:
        hint = s.exec(select(CalibrationHint).where(CalibrationHint.fingerprint == fp)).first()
        flag = s.get(OutcomeFlag, open_id)
    assert hint.status == "expired"
    assert flag.suppressed is False
    assert flag.suppressed_reason is None


def test_cal19_activation_notifies_once_unchanged_and_expiring_notify_zero(eng, monkeypatch):
    """CAL19: newly-activated hints fire exactly one
    events.notify_phone(kind="calibration_suppress") each; an unchanged or
    expiring hint fires zero."""
    from backend.agents import calibration

    calls = []

    async def _fake_notify(content, *, kind="autonomy_alert", buttons=None):
        calls.append((content, kind))
        return True

    monkeypatch.setattr("backend.events.notify_phone", _fake_notify)

    fp_a = "homelab_watch:garage_open"
    fp_b = "homelab_watch:unraid_temp"
    _make_verdicts(eng, fp_a, false_positive=10, resolved=0)  # activates
    _make_verdicts(eng, fp_b, false_positive=1, resolved=9)   # 0.10 -- never activates

    result = asyncio.run(calibration.recompute_hints())

    assert fp_a in result["activated"]
    assert fp_b not in result["activated"]
    assert len(calls) == 1
    assert calls[0][1] == "calibration_suppress"
    assert fp_a in calls[0][0]

    # A second recompute with no new data: fp_a stays active (unchanged),
    # fires zero further notifications.
    calls.clear()
    result2 = asyncio.run(calibration.recompute_hints())
    assert result2["activated"] == []
    assert calls == []


# ---------------------------------------------------------------------------
# 8.8 Invariants — calibration.py-specific (CAL47-CAL49)
# ---------------------------------------------------------------------------

def test_cal47_calibration_module_does_not_import_broker():
    """CAL47: backend/agents/calibration.py does not import
    backend.safety.broker (grep-style assertion, matching test_tools.py's
    existing no-broker-import guard and outcomes.py's AC32)."""
    import inspect

    from backend.agents import calibration

    module_src = inspect.getsource(calibration)
    import_lines = [
        ln for ln in module_src.splitlines()
        if ln.strip().startswith(("import ", "from "))
    ]
    for ln in import_lines:
        assert "broker" not in ln, f"calibration.py imports the broker: {ln!r}"
        assert "websocket" not in ln, f"calibration.py imports the websocket layer: {ln!r}"


def test_cal48_no_session_or_orm_crosses_await_in_calibration():
    """CAL48: no Session or ORM object crosses an await in calibration.py —
    every DB helper is sync (def, not async def) and every async wrapper
    (async def) calls it via asyncio.to_thread (mirrors outcomes.py's AC33
    source-level check)."""
    import ast
    import pathlib

    src = pathlib.Path(__file__).parent.parent / "backend" / "agents" / "calibration.py"
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
        direct_db_calls = [
            n for n in ast.walk(fn)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name)
            and n.func.id.startswith("_db_")
        ]
        assert not direct_db_calls, (
            f"{fn.name} calls a _db_* helper directly instead of via "
            f"asyncio.to_thread: {[n.func.id for n in direct_db_calls]}"
        )
        assert "asyncio.to_thread" in fn_src or not any(
            isinstance(n, ast.Name) and n.id.startswith("_db_") for n in ast.walk(fn)
        ), f"{fn.name} references a _db_* helper but never calls asyncio.to_thread"


def test_cal49_calibration_module_has_zero_llm_calls():
    """CAL49: tests/test_spend_report.py::test_no_unlabeled_llm_calls_in_agents
    still passes (it globs backend/agents/*.py, which now includes this
    file); this test pins the same zero-LLM-calls invariant directly on
    calibration.py, matching CAL47/CAL48's directness."""
    import pathlib
    import re

    src = pathlib.Path(__file__).parent.parent / "backend" / "agents" / "calibration.py"
    src_text = src.read_text(encoding="utf-8")
    assert not re.search(r"\b(haiku|sonnet|opus)\s*\(", src_text), (
        "calibration.py contains an LLM call"
    )


# ---------------------------------------------------------------------------
# 8.8 Scheduler and invariants (CAL45-CAL46)
# ---------------------------------------------------------------------------

def test_cal45_calibration_recompute_registered_at_0350_when_enabled(monkeypatch):
    """CAL45: setup_scheduler registers job id "calibration_recompute" with a
    CronTrigger at 03:50 in the configured timezone when calibration_enabled
    is True (the default) -- mirrors test_infisical_soak_reminder.py's
    add_job-mock pattern for asserting job registration without running the
    scheduler."""
    from unittest.mock import patch

    from apscheduler.triggers.cron import CronTrigger

    from backend.config import get_settings
    from backend.scheduler import scheduler, setup_scheduler

    monkeypatch.setattr(get_settings(), "calibration_enabled", True)

    with patch.object(scheduler, "add_job") as mock_add:
        setup_scheduler("07:30", "America/New_York")

    ids = {c.kwargs.get("id"): c for c in mock_add.call_args_list}
    assert "calibration_recompute" in ids
    trigger = ids["calibration_recompute"].args[1]
    assert isinstance(trigger, CronTrigger)
    fields = {f.name: str(f) for f in trigger.fields}
    assert fields["hour"] == "3"
    assert fields["minute"] == "50"
    assert str(trigger.timezone) == "America/New_York"


def test_cal45_calibration_recompute_absent_when_disabled(monkeypatch):
    """CAL45: no "calibration_recompute" job is registered when
    calibration_enabled is False."""
    from unittest.mock import patch

    from backend.config import get_settings
    from backend.scheduler import scheduler, setup_scheduler

    monkeypatch.setattr(get_settings(), "calibration_enabled", False)

    with patch.object(scheduler, "add_job") as mock_add:
        setup_scheduler("07:30", "America/New_York")

    ids = {c.kwargs.get("id") for c in mock_add.call_args_list}
    assert "calibration_recompute" not in ids


# ---------------------------------------------------------------------------
# §5.2 hint_report / set_override (rollout §9.5 step 3, first slice —
# REST/backend layer; the Telegram command and digest lines are a later
# cycle). Covers set_override's three-way applied/not_found/invalid return
# contract (including the malformed-fingerprint case), the
# override_until/expires_at date-math invariant (CAL41 "never permanent" on
# active=True), and the suppressed/suppressed_reason clearing side effect on
# active=False.
# ---------------------------------------------------------------------------

def test_set_override_invalid_fingerprint_returns_invalid_without_touching_db(eng):
    """A fingerprint missing ':' (or falsy) returns "invalid" without ever
    reaching the DB — no CalibrationHint row is created for it."""
    from backend.agents import calibration
    from backend.database import CalibrationHint

    assert asyncio.run(calibration.set_override("no_colon_here", True, by="brian")) == "invalid"
    assert asyncio.run(calibration.set_override("", True, by="brian")) == "invalid"

    with Session(eng) as s:
        assert s.exec(select(CalibrationHint)).all() == []


def test_set_override_active_false_not_found_when_no_hint_row(eng):
    """active=False against a fingerprint with no existing CalibrationHint row
    returns "not_found" (the only branch on which "not_found" is reachable —
    active=True always creates the row, per the module's own docstring)."""
    from backend.agents import calibration

    result = asyncio.run(
        calibration.set_override("homelab_watch:never_seen", False, by="brian")
    )
    assert result == "not_found"


def test_set_override_active_true_never_permanent_and_clears_prior_override_until(eng):
    """CAL41: active=True sets status="active", first_active_at=now,
    expires_at == first_active_at + calibration_hint_max_days (never a
    permanent suppression), and override_until=None -- even when the hint
    previously carried a future override_until from an earlier
    active=False call."""
    from backend.agents import calibration
    from backend.config import get_settings
    from backend.database import CalibrationHint

    fp = "homelab_watch:garage_open"

    before = datetime.utcnow()
    result = asyncio.run(calibration.set_override(fp, True, by="brian", note="testing suppress"))
    after = datetime.utcnow()
    assert result == "applied"

    with Session(eng) as s:
        hint = s.exec(select(CalibrationHint).where(CalibrationHint.fingerprint == fp)).first()
    assert hint is not None
    assert hint.status == "active"
    assert hint.first_active_at is not None
    assert before <= hint.first_active_at <= after
    max_days = get_settings().calibration_hint_max_days
    assert hint.expires_at == hint.first_active_at + timedelta(days=max_days)
    assert hint.override_until is None  # CAL41: never permanent
    assert hint.override_by == "brian"
    assert hint.override_note == "testing suppress"


def test_set_override_active_false_sets_override_until_and_clears_suppressed_open_flag(eng):
    """active=False: status="overridden_off", override_until == override_at +
    calibration_override_days, and any open OutcomeFlag row for that
    fingerprint has suppressed/suppressed_reason cleared (mirrors CAL18's
    recompute-driven side effect, but triggered by an explicit override)."""
    from backend.agents import calibration
    from backend.config import get_settings
    from backend.database import CalibrationHint, OutcomeFlag

    fp = "homelab_watch:garage_open"
    asyncio.run(calibration.set_override(fp, True, by="brian"))  # seed an active hint

    open_id = _make_flag(eng, fingerprint=fp, status="open", suppressed=True)
    with Session(eng) as s:
        row = s.get(OutcomeFlag, open_id)
        row.suppressed_reason = "calibration:homelab_watch:garage_open fp_rate=1.00 n=10"
        s.add(row)
        s.commit()

    before = datetime.utcnow()
    result = asyncio.run(calibration.set_override(fp, False, by="brian", note="false alarm confirmed"))
    after = datetime.utcnow()
    assert result == "applied"

    with Session(eng) as s:
        hint = s.exec(select(CalibrationHint).where(CalibrationHint.fingerprint == fp)).first()
        flag = s.get(OutcomeFlag, open_id)

    assert hint.status == "overridden_off"
    override_days = get_settings().calibration_override_days
    assert hint.override_at is not None
    assert before <= hint.override_at <= after
    assert hint.override_until == hint.override_at + timedelta(days=override_days)
    assert hint.override_note == "false alarm confirmed"

    assert flag.suppressed is False
    assert flag.suppressed_reason is None


def test_hint_report_groups_persisted_status_into_suppressed_and_overridden(eng):
    """hint_report(days) sorts a status="active" hint into "suppressed" and a
    status="overridden_off" hint into "overridden", rendering the PERSISTED
    row's frozen fields (not a live recompute) for both groups."""
    from backend.agents import calibration

    fp_active = "homelab_watch:garage_open"
    fp_overridden = "homelab_watch:unraid_temp"
    asyncio.run(calibration.set_override(fp_active, True, by="brian"))
    asyncio.run(calibration.set_override(fp_overridden, True, by="brian"))
    asyncio.run(calibration.set_override(fp_overridden, False, by="brian"))

    report = asyncio.run(calibration.hint_report(30))

    assert [row["fingerprint"] for row in report["suppressed"]] == [fp_active]
    assert [row["fingerprint"] for row in report["overridden"]] == [fp_overridden]
    assert report["watching"] == []


@pytest.mark.asyncio
async def test_cal46_calibration_recompute_swallows_exception_and_logs(caplog):
    """CAL46: _calibration_recompute swallows a raising recompute_hints and
    logs rather than propagating -- mirrors test_watchdog.py's
    test_watchdog_scheduler_wrapper_swallows_exception (patch the lazily-
    imported target function, call the wrapper, assert it does not raise)."""
    import logging
    from unittest.mock import AsyncMock, patch

    from backend.scheduler import _calibration_recompute

    recompute_mock = AsyncMock(side_effect=RuntimeError("calibration exploded"))

    with caplog.at_level(logging.ERROR):
        with patch("backend.agents.calibration.recompute_hints", recompute_mock):
            await _calibration_recompute()  # must not raise

    recompute_mock.assert_awaited_once()
    assert any(
        "Calibration recompute job error" in rec.message for rec in caplog.records
    )
