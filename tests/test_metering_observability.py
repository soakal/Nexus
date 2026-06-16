"""Tests for Tier 3: spend-metering observability + crash-safe migration shims.

Covers:
  - _METER_COUNTS increments per _record_spend outcome branch
  - metering_health() aggregation
  - GET /api/safety/metering endpoint
  - _safe_add_column idempotency + duplicate-column race tolerance
  - _ensure_system_state idempotency under a racing duplicate insert
"""
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

import backend.database  # noqa: F401 — registers all table metadata


# ---------------------------------------------------------------------------
# Shared helpers (mirror test_governor.py style)
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


def _seed_spend(eng, cost, created_at=None, model="claude-sonnet-4-6", task_id=None):
    from backend.database import SpendLog
    with Session(eng) as s:
        row = SpendLog(model=model, cost_usd=cost, task_id=task_id)
        if created_at is not None:
            row.created_at = created_at
        s.add(row)
        s.commit()


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


def _all_spend(eng):
    from backend.database import SpendLog
    with Session(eng) as s:
        return s.exec(select(SpendLog)).all()


def _usage_resp(
    text_val="hi",
    input_tokens=1000,
    output_tokens=500,
    cache_creation=0,
    cache_read=0,
):
    """A Messages-API-shaped response with real (int) usage fields."""
    resp = SimpleNamespace()
    resp.content = [SimpleNamespace(type="text", text=text_val)]
    resp.usage = SimpleNamespace(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_creation_input_tokens=cache_creation,
        cache_read_input_tokens=cache_read,
    )
    return resp


def _reset_meter_counts():
    """Zero out all _METER_COUNTS entries at the start of a counter test."""
    from backend.agents import router
    for k in router._METER_COUNTS:
        router._METER_COUNTS[k] = 0


# ---------------------------------------------------------------------------
# PART 1 — metering outcome counters
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_recorded_counter_increments_on_real_usage(eng):
    """A real-usage response through router.sonnet increments 'recorded' by 1
    and writes exactly 1 SpendLog row."""
    _reset_meter_counts()
    resp = _usage_resp()
    with patch("anthropic.Anthropic") as mock_anthropic:
        mock_client = MagicMock()
        mock_client.messages.create.return_value = resp
        mock_anthropic.return_value = mock_client

        from backend.agents import router
        out = await router.sonnet("hi", label="counter_test")

    assert out == "hi"
    from backend.agents.router import _METER_COUNTS
    assert _METER_COUNTS["recorded"] == 1
    assert _METER_COUNTS["skipped_no_usage"] == 0
    assert _METER_COUNTS["skipped_unparseable"] == 0
    assert _METER_COUNTS["failed"] == 0

    rows = _all_spend(eng)
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_skipped_unparseable_counter_increments_on_mock_usage(eng):
    """A MagicMock usage response (token fields are MagicMock objects, not numeric)
    increments 'skipped_unparseable' and writes 0 SpendLog rows."""
    _reset_meter_counts()
    mock_resp = MagicMock()
    mock_resp.content = [MagicMock(type="text", text="x")]
    # usage is a MagicMock, so getattr(resp, "usage") is a MagicMock (not None),
    # but its token fields are also MagicMocks — not int/float/str — so _coerce raises.
    with patch("anthropic.Anthropic") as mock_anthropic:
        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_resp
        mock_anthropic.return_value = mock_client

        from backend.agents import router
        await router.sonnet("hi")

    from backend.agents.router import _METER_COUNTS
    assert _METER_COUNTS["skipped_unparseable"] == 1
    assert _METER_COUNTS["recorded"] == 0
    assert len(_all_spend(eng)) == 0


@pytest.mark.asyncio
async def test_skipped_no_usage_counter_increments_when_usage_none(eng):
    """A response whose .usage is None increments 'skipped_no_usage' and writes 0 rows."""
    _reset_meter_counts()
    resp = SimpleNamespace()
    resp.content = [SimpleNamespace(type="text", text="no_usage")]
    resp.usage = None

    with patch("anthropic.Anthropic") as mock_anthropic:
        mock_client = MagicMock()
        mock_client.messages.create.return_value = resp
        mock_anthropic.return_value = mock_client

        from backend.agents import router
        out = await router.sonnet("hi")

    assert out == "no_usage"
    from backend.agents.router import _METER_COUNTS
    assert _METER_COUNTS["skipped_no_usage"] == 1
    assert _METER_COUNTS["recorded"] == 0
    assert len(_all_spend(eng)) == 0


def test_failed_counter_increments_on_db_error(eng):
    """A DB failure inside _record_spend increments 'failed' and does not raise.

    We call _record_spend directly with a real-usage response but patch the
    engine's connect() to raise after coercion succeeds, triggering the outer
    except branch without needing a real DB error.
    """
    _reset_meter_counts()
    from backend.agents import router as _router

    resp = _usage_resp(text_val="answer")

    # Patch the SpendLog Session write to raise a RuntimeError so the outer
    # except branch in _record_spend fires. We patch backend.database.engine
    # with a broken engine (its connect raises), which _record_spend imports
    # lazily inside the function.
    class _BrokenEngine:
        def connect(self):
            raise RuntimeError("simulated db failure at connect")

    with patch("backend.database.engine", _BrokenEngine()):
        _router._record_spend(_router.SONNET_MODEL, resp, "test_failed")

    from backend.agents.router import _METER_COUNTS
    assert _METER_COUNTS["failed"] == 1
    assert _METER_COUNTS["recorded"] == 0


def test_metering_counters_returns_snapshot():
    """metering_counters() returns a plain dict copy, not the live dict."""
    from backend.agents.router import metering_counters, _METER_COUNTS
    snap = metering_counters()
    assert isinstance(snap, dict)
    assert set(snap.keys()) == {"recorded", "skipped_no_usage", "skipped_unparseable", "failed"}
    # It's a copy — mutating it doesn't affect the module dict.
    snap["recorded"] = 99999
    assert _METER_COUNTS["recorded"] != 99999


# ---------------------------------------------------------------------------
# PART 2 — metering_health()
# ---------------------------------------------------------------------------

def test_metering_health_returns_all_keys(eng):
    """metering_health() returns counters + today_spend_usd + today_row_count
    + prices_verified with correct types."""
    _seed_state(eng)
    from backend.safety.governor import metering_health
    result = metering_health()
    assert set(result.keys()) == {"counters", "today_spend_usd", "today_row_count", "prices_verified"}
    assert isinstance(result["counters"], dict)
    assert isinstance(result["today_spend_usd"], float)
    assert isinstance(result["today_row_count"], int)
    assert isinstance(result["prices_verified"], bool)


def test_metering_health_today_row_count(eng):
    """Seeding 2 SpendLog rows today gives today_row_count == 2."""
    now = datetime.utcnow()
    _seed_spend(eng, 1.0, created_at=now)
    _seed_spend(eng, 2.0, created_at=now)
    # Yesterday row must NOT count.
    _seed_spend(eng, 99.0, created_at=now - timedelta(days=1, hours=1))

    from backend.safety.governor import metering_health
    result = metering_health()
    assert result["today_row_count"] == 2
    assert result["today_spend_usd"] == pytest.approx(3.0)


def test_metering_health_reports_prices_verified(eng):
    """metering_health() surfaces the prices_verified config flag (now True after
    the 2026-06-16 verification of _PRICE_PER_MTOK against Anthropic's pricing)."""
    from backend.safety.governor import metering_health
    from backend.config import get_settings
    result = metering_health()
    assert result["prices_verified"] is bool(get_settings().prices_verified)
    assert result["prices_verified"] is True


# ---------------------------------------------------------------------------
# PART 3 — GET /api/safety/metering endpoint
# ---------------------------------------------------------------------------

@pytest.fixture
def safety_client(tmp_path, monkeypatch):
    vault_key = tmp_path / ".vault.key"
    vault_file = tmp_path / "nexus.vault"
    vault_key.write_bytes(b"A" * 32)
    vault_file.write_text("{}")
    monkeypatch.chdir(tmp_path)

    test_engine = make_engine()
    monkeypatch.setattr("backend.database.engine", test_engine)

    from backend.database import get_session

    def override_session():
        with Session(test_engine) as session:
            yield session

    with patch("backend.database.create_db_and_tables"), \
         patch("backend.scheduler.setup_scheduler"), \
         patch("backend.scheduler.scheduler") as sched, \
         patch("backend.agents.memo_watcher.start_watcher_blocking"), \
         patch("backend.agents.memo_watcher.stop_watcher", new_callable=AsyncMock):
        sched.running = False
        from backend.main import app
        app.dependency_overrides[get_session] = override_session
        with TestClient(app) as c:
            c._engine = test_engine
            yield c
        app.dependency_overrides.clear()


def test_metering_endpoint_requires_auth(safety_client):
    resp = safety_client.get("/api/safety/metering")
    assert resp.status_code == 401


def test_metering_endpoint_returns_expected_keys(safety_client, auth_headers):
    """GET /api/safety/metering returns 200 with the four documented keys."""
    _seed_state(safety_client._engine)
    resp = safety_client.get("/api/safety/metering", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    for key in ("counters", "today_spend_usd", "today_row_count", "prices_verified"):
        assert key in body, f"missing key: {key}"
    assert isinstance(body["counters"], dict)
    assert set(body["counters"].keys()) == {
        "recorded", "skipped_no_usage", "skipped_unparseable", "failed"
    }
    assert isinstance(body["today_spend_usd"], float)
    assert isinstance(body["today_row_count"], int)
    assert isinstance(body["prices_verified"], bool)


# ---------------------------------------------------------------------------
# PART 4 — _safe_add_column idempotency + duplicate-column race
# ---------------------------------------------------------------------------

def test_safe_add_column_adds_new_column(monkeypatch):
    """_safe_add_column adds a column that doesn't exist yet."""
    eng = make_engine()
    monkeypatch.setattr("backend.database.engine", eng)

    # Create a throwaway table without a 'notes' column.
    with eng.connect() as conn:
        conn.execute(text("CREATE TABLE _test_shim (id INTEGER PRIMARY KEY, val TEXT)"))
        conn.commit()

    from backend.database import _safe_add_column
    _safe_add_column("_test_shim", "notes", "TEXT")

    with eng.connect() as conn:
        cols = {row[1] for row in conn.execute(text("PRAGMA table_info(_test_shim)"))}
    assert "notes" in cols


def test_safe_add_column_idempotent_second_call(monkeypatch):
    """_safe_add_column called twice for the same column does not raise."""
    eng = make_engine()
    monkeypatch.setattr("backend.database.engine", eng)

    with eng.connect() as conn:
        conn.execute(text("CREATE TABLE _test_shim2 (id INTEGER PRIMARY KEY)"))
        conn.commit()

    from backend.database import _safe_add_column
    _safe_add_column("_test_shim2", "extra", "INTEGER DEFAULT 0")
    # Second call: column already present — must return silently.
    _safe_add_column("_test_shim2", "extra", "INTEGER DEFAULT 0")

    with eng.connect() as conn:
        cols = {row[1] for row in conn.execute(text("PRAGMA table_info(_test_shim2)"))}
    assert "extra" in cols


def test_safe_add_column_tolerates_duplicate_column_race(monkeypatch):
    """Simulates the race: column is added between PRAGMA and ALTER.

    We verify that an exception whose message contains 'duplicate column' is
    swallowed (returns None, no re-raise).
    """
    from unittest.mock import MagicMock, call

    eng = make_engine()
    monkeypatch.setattr("backend.database.engine", eng)

    with eng.connect() as conn:
        conn.execute(text("CREATE TABLE _test_race (id INTEGER PRIMARY KEY)"))
        # Pre-add the column so PRAGMA reports it absent on first read but
        # ALTER would fail. We simulate this by reporting cols as empty but
        # having the ALTER raise 'duplicate column name'.
        conn.commit()

    orig_connect = eng.connect

    call_count = 0

    class _FakeConn:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, stmt):
            nonlocal call_count
            call_count += 1
            stmt_str = str(stmt)
            if "PRAGMA" in stmt_str:
                # Report the column as absent so the code attempts ALTER.
                return []
            if "ALTER" in stmt_str:
                raise Exception("table _test_race already has a column named racing_col (duplicate column name)")
            return MagicMock()

        def commit(self):
            pass

    monkeypatch.setattr(eng, "connect", lambda: _FakeConn())

    from backend.database import _safe_add_column
    # Must not raise.
    _safe_add_column("_test_race", "racing_col", "TEXT")


# ---------------------------------------------------------------------------
# PART 5 — _ensure_system_state idempotency (including racing duplicate)
# ---------------------------------------------------------------------------

def test_ensure_system_state_idempotent(eng):
    """Two calls to _ensure_system_state produce exactly one SystemState row."""
    from backend.database import SystemState, _ensure_system_state

    with Session(eng) as s:
        assert s.get(SystemState, 1) is None

    _ensure_system_state()
    _ensure_system_state()

    with Session(eng) as s:
        rows = s.exec(select(SystemState)).all()
    assert len(rows) == 1
    assert rows[0].id == 1


def test_ensure_system_state_tolerates_integrity_error(eng, monkeypatch):
    """If a racing boot inserts id=1 between our get() and add()+commit(), the
    resulting IntegrityError is caught, session is rolled back, and no exception
    propagates."""
    from sqlalchemy.exc import IntegrityError as SAIntegrityError
    from backend.database import SystemState, _ensure_system_state

    original_get = Session.get

    inserted = False

    def _racing_get(self, model, ident, **kwargs):
        nonlocal inserted
        result = original_get(self, model, ident, **kwargs)
        # On the first call when id=1 is absent, simulate a racing insert
        # BEFORE our add()+commit() so our commit raises IntegrityError.
        if result is None and model is SystemState and ident == 1 and not inserted:
            inserted = True
            # Insert the row directly on the same session's connection to set
            # up the uniqueness violation.
            with eng.connect() as conn:
                conn.execute(
                    text("INSERT OR IGNORE INTO systemstate (id, autonomy_enabled, daily_budget_usd, per_task_budget_usd, updated_at) VALUES (1, 1, 25.0, 5.0, datetime('now'))")
                )
                conn.commit()
        return result

    monkeypatch.setattr(Session, "get", _racing_get)

    # Must not raise.
    _ensure_system_state()

    # Row exists (the racing insert won).
    with Session(eng) as s:
        rows = s.exec(select(SystemState)).all()
    assert len(rows) == 1
