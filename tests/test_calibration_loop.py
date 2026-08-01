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
"""
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
