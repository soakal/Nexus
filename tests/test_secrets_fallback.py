"""Tests for the durable infisical -> legacy vault fallback signal
(backend/secrets/fallback_log.py + its wiring into manager.py, scheduler.py,
and api/safety.py).

Follows the repo's established conventions:
  - In-memory StaticPool engine monkeypatched onto backend.database.engine
    (same shape as tests/test_action_judge.py's make_engine/eng fixture) so
    nothing here ever touches the real, live nexus.db.
  - The manager-routing tests restore the real manager.get_secret past
    conftest.py's autouse mock_secrets fixture, exactly as
    tests/test_secrets_manager.py does (innermost patch wins).
"""

from datetime import timedelta

import pytest
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

# Ensure all tables (incl. SecretFallback) are registered on SQLModel.metadata.
import backend.database  # noqa: F401,E402
from backend.database import SecretFallback
from backend.secrets import fallback_log, manager

# Captured at collection time, before conftest's autouse mock_secrets fixture
# monkeypatches manager.get_secret for every test -- the recording-path tests
# exercise the real routing/fallback logic, so they restore it explicitly.
_real_get_secret = manager.get_secret


class _Settings:
    def __init__(self, *, backend="auto", url="", client_id="", client_secret="", project_id=""):
        self.secrets_backend = backend
        self.infisical_url = url
        self.infisical_client_id = client_id
        self.infisical_client_secret = client_secret
        self.infisical_project_id = project_id


def _patch_settings(monkeypatch, settings):
    monkeypatch.setattr("backend.config.get_settings", lambda: settings)


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


class _BoomSession:
    """Stand-in for sqlmodel.Session that fails on construction -- used to
    simulate the DB being genuinely unavailable at drain()/summary() time."""

    def __init__(self, *a, **k):
        raise RuntimeError("db unavailable")


@pytest.fixture(autouse=True)
def _reset_fallback_log():
    fallback_log.reset()
    yield
    fallback_log.reset()


# ---------------------------------------------------------------------------
# Recording path (via the real manager.get_secret)
# ---------------------------------------------------------------------------

def test_fallback_event_recorded_when_infisical_misses_and_vault_hits(monkeypatch):
    monkeypatch.setattr(manager, "get_secret", _real_get_secret)
    _patch_settings(monkeypatch, _Settings(backend="infisical", url="https://x", client_id="c", client_secret="s", project_id="p"))

    def infisical_miss(key):
        raise KeyError(key)

    monkeypatch.setattr("backend.secrets.infisical_client.get_secret", infisical_miss)
    monkeypatch.setattr("backend.secrets.vault.get_secret", lambda k: "from-legacy-vault")

    value = manager.get_secret("SOME_KEY")
    assert value == "from-legacy-vault"

    pend = fallback_log.pending()
    assert "SOME_KEY" in pend
    assert pend["SOME_KEY"]["count"] == 1
    assert pend["SOME_KEY"]["backend_from"] == "infisical"
    assert pend["SOME_KEY"]["backend_to"] == "vault"


def test_no_event_recorded_on_normal_infisical_hit(monkeypatch):
    monkeypatch.setattr(manager, "get_secret", _real_get_secret)
    _patch_settings(monkeypatch, _Settings(backend="infisical", url="https://x", client_id="c", client_secret="s", project_id="p"))
    monkeypatch.setattr("backend.secrets.infisical_client.get_secret", lambda k: "from-infisical")

    assert manager.get_secret("SOME_KEY") == "from-infisical"
    assert fallback_log.pending() == {}


def test_no_event_recorded_when_backend_is_vault(monkeypatch):
    monkeypatch.setattr(manager, "get_secret", _real_get_secret)
    _patch_settings(monkeypatch, _Settings(backend="vault"))
    monkeypatch.setattr("backend.secrets.vault.get_secret", lambda k: "from-vault")

    assert manager.get_secret("SOME_KEY") == "from-vault"
    assert fallback_log.pending() == {}


def test_no_event_recorded_on_env_fallback(monkeypatch):
    monkeypatch.setattr(manager, "get_secret", _real_get_secret)
    _patch_settings(monkeypatch, _Settings(backend="infisical", url="https://x", client_id="c", client_secret="s", project_id="p"))

    def miss(key):
        raise KeyError(key)

    monkeypatch.setattr("backend.secrets.infisical_client.get_secret", miss)
    monkeypatch.setattr("backend.secrets.vault.get_secret", miss)
    monkeypatch.setenv("SOME_ENV_ONLY_KEY_FB", "env-value")

    assert manager.get_secret("SOME_ENV_ONLY_KEY_FB") == "env-value"
    assert fallback_log.pending() == {}


def test_get_secret_still_returns_value_when_recorder_raises(monkeypatch):
    monkeypatch.setattr(manager, "get_secret", _real_get_secret)
    _patch_settings(monkeypatch, _Settings(backend="infisical", url="https://x", client_id="c", client_secret="s", project_id="p"))

    def infisical_miss(key):
        raise KeyError(key)

    monkeypatch.setattr("backend.secrets.infisical_client.get_secret", infisical_miss)
    monkeypatch.setattr("backend.secrets.vault.get_secret", lambda k: "from-legacy-vault")

    def boom(*a, **k):
        raise RuntimeError("recorder broken")

    monkeypatch.setattr(fallback_log, "record", boom)

    assert manager.get_secret("SOME_KEY") == "from-legacy-vault"


def test_repeat_fallbacks_coalesce_into_one_pending_entry(monkeypatch):
    monkeypatch.setattr(manager, "get_secret", _real_get_secret)
    _patch_settings(monkeypatch, _Settings(backend="infisical", url="https://x", client_id="c", client_secret="s", project_id="p"))

    def infisical_miss(key):
        raise KeyError(key)

    monkeypatch.setattr("backend.secrets.infisical_client.get_secret", infisical_miss)
    monkeypatch.setattr("backend.secrets.vault.get_secret", lambda k: "v")

    for _ in range(3):
        assert manager.get_secret("REPEAT_KEY") == "v"

    pend = fallback_log.pending()
    assert pend["REPEAT_KEY"]["count"] == 3
    assert pend["REPEAT_KEY"]["first_at"] <= pend["REPEAT_KEY"]["last_at"]


# ---------------------------------------------------------------------------
# Bounding
# ---------------------------------------------------------------------------

def test_record_bounded_at_max_keys():
    for i in range(200):
        fallback_log.record(f"KEY_{i}")

    assert len(fallback_log.pending()) == fallback_log.MAX_KEYS
    assert fallback_log.dropped() > 0


def test_record_never_raises_on_bad_input():
    fallback_log.record(None)
    fallback_log.record("")
    fallback_log.record(123)
    fallback_log.record("x" * 5000)

    for key in fallback_log.pending():
        assert len(key) <= 128


# ---------------------------------------------------------------------------
# Drain
# ---------------------------------------------------------------------------

def test_drain_writes_one_row_per_key(eng):
    fallback_log.record("KEY_A")
    fallback_log.record("KEY_A")
    fallback_log.record("KEY_A")
    fallback_log.record("KEY_B")

    n = fallback_log.drain()
    assert n == 2

    with Session(eng) as session:
        rows = session.exec(select(SecretFallback)).all()
    assert len(rows) == 2
    by_key = {r.secret_key: r for r in rows}
    assert by_key["KEY_A"].event_count == 3
    assert by_key["KEY_B"].event_count == 1

    assert fallback_log.pending() == {}


def test_drain_accumulates_across_calls(eng):
    for _ in range(3):
        fallback_log.record("KEY_A")
    fallback_log.record("KEY_B")
    assert fallback_log.drain() == 2

    with Session(eng) as session:
        row_a = session.exec(
            select(SecretFallback).where(SecretFallback.secret_key == "KEY_A")
        ).first()
    first_at_1 = row_a.first_at
    last_at_1 = row_a.last_at

    fallback_log.record("KEY_A")
    assert fallback_log.drain() == 1

    with Session(eng) as session:
        rows = session.exec(select(SecretFallback)).all()
    assert len(rows) == 2
    row_a2 = next(r for r in rows if r.secret_key == "KEY_A")
    assert row_a2.event_count == 4
    assert row_a2.first_at == first_at_1
    assert row_a2.last_at >= last_at_1


def test_drain_on_empty_buffer_returns_zero_and_writes_nothing(eng):
    assert fallback_log.drain() == 0

    with Session(eng) as session:
        rows = session.exec(select(SecretFallback)).all()
    assert rows == []


def test_drain_never_raises_and_requeues_when_db_unavailable(eng, monkeypatch):
    fallback_log.record("KEY_A")
    fallback_log.record("KEY_A")

    monkeypatch.setattr("sqlmodel.Session", _BoomSession)

    n = fallback_log.drain()
    assert n == 0

    pend = fallback_log.pending()
    assert pend["KEY_A"]["count"] == 2


def test_drain_requeue_merges_with_events_recorded_during_the_failure():
    fallback_log.record("KEY_A")
    t0 = fallback_log.pending()["KEY_A"]["first_at"]
    earlier = t0 - timedelta(seconds=10)
    later = t0 + timedelta(seconds=10)

    fallback_log._merge_back({
        "KEY_A": {
            "backend_from": "infisical",
            "backend_to": "vault",
            "first_at": earlier,
            "last_at": later,
            "count": 5,
        },
    })

    pend = fallback_log.pending()
    assert pend["KEY_A"]["count"] == 6
    assert pend["KEY_A"]["first_at"] == earlier
    assert pend["KEY_A"]["last_at"] == later


# ---------------------------------------------------------------------------
# Read path
# ---------------------------------------------------------------------------

def test_summary_on_zero_rows(eng):
    s = fallback_log.summary()
    assert s["total_events"] == 0
    assert s["key_count"] == 0
    assert s["keys"] == []
    assert s["last_at"] is None


def test_summary_on_n_rows(eng):
    fallback_log.record("KEY_A")
    fallback_log.record("KEY_A")
    fallback_log.record("KEY_B")
    fallback_log.record("KEY_C")
    fallback_log.drain()

    s = fallback_log.summary()
    assert s["key_count"] == 3
    assert s["total_events"] == 4

    last_ats = [k["last_at"] for k in s["keys"]]
    assert last_ats == sorted(last_ats, reverse=True)

    with Session(eng) as session:
        newest = max(session.exec(select(SecretFallback)).all(), key=lambda r: r.last_at)
    assert s["last_at"] == newest.last_at.isoformat()


def test_summary_reports_pending_and_dropped(eng):
    fallback_log.record("KEY_DRAINED")
    fallback_log.drain()
    fallback_log.record("KEY_PENDING")

    for i in range(100):
        fallback_log.record(f"OVERFLOW_{i}")

    s = fallback_log.summary()
    assert s["pending"] == fallback_log.MAX_KEYS
    assert s["dropped"] > 0


def test_summary_never_raises_when_db_unavailable(eng, monkeypatch):
    monkeypatch.setattr("sqlmodel.Session", _BoomSession)

    s = fallback_log.summary()
    assert s["total_events"] == 0
    assert s["key_count"] == 0
    assert s["keys"] == []
    assert s["last_at"] is None
    assert "error" in s


# ---------------------------------------------------------------------------
# Privacy
# ---------------------------------------------------------------------------

def test_secret_value_is_never_persisted(monkeypatch, eng):
    monkeypatch.setattr(manager, "get_secret", _real_get_secret)
    _patch_settings(monkeypatch, _Settings(backend="infisical", url="https://x", client_id="c", client_secret="s", project_id="p"))

    def infisical_miss(key):
        raise KeyError(key)

    sentinel = "SUPER-SECRET-VALUE-123"
    monkeypatch.setattr("backend.secrets.infisical_client.get_secret", infisical_miss)
    monkeypatch.setattr("backend.secrets.vault.get_secret", lambda k: sentinel)

    assert manager.get_secret("SENTINEL_KEY") == sentinel

    fallback_log.drain()

    with Session(eng) as session:
        rows = session.exec(select(SecretFallback)).all()
    row_blob = " ".join(
        f"{r.secret_key}|{r.backend_from}|{r.backend_to}|{r.event_count}|{r.first_at}|{r.last_at}"
        for r in rows
    )
    assert sentinel not in row_blob

    s = fallback_log.summary()
    assert sentinel not in str(s)


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------

def test_scheduler_registers_secret_fallback_drain_job(monkeypatch):
    from backend import scheduler as scheduler_module
    from backend.config import get_settings

    monkeypatch.setattr(get_settings(), "watchdog_enabled", False)
    scheduler_module.setup_scheduler("07:00", "America/Detroit")

    job = scheduler_module.scheduler.get_job("secret_fallback_drain")
    assert job is not None
    assert job.trigger.interval.total_seconds() == 300


def test_safety_status_includes_secret_fallback(safety_client, auth_headers):
    resp = safety_client.get("/api/safety/status", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert "secret_fallback" in body
    assert "total_events" in body["secret_fallback"]
    assert "keys" in body["secret_fallback"]


# ---------------------------------------------------------------------------
# safety_client fixture -- same shape as tests/test_action_judge.py's, needed
# here for test_safety_status_includes_secret_fallback above.
# ---------------------------------------------------------------------------

@pytest.fixture
def safety_client(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock, patch as _patch
    from fastapi.testclient import TestClient

    vault_key = tmp_path / ".vault.key"
    vault_file = tmp_path / "nexus.vault"
    vault_key.write_bytes(b"A" * 32)
    vault_file.write_text("{}")
    monkeypatch.chdir(tmp_path)

    test_engine = make_engine()
    monkeypatch.setattr("backend.database.engine", test_engine)

    def override_session():
        with Session(test_engine) as session:
            yield session

    with _patch("backend.database.create_db_and_tables"), \
         _patch("backend.scheduler.setup_scheduler"), \
         _patch("backend.scheduler.scheduler") as sched, \
         _patch("backend.agents.memo_watcher.start_watcher_blocking"), \
         _patch("backend.agents.memo_watcher.stop_watcher", new_callable=AsyncMock), \
         _patch("backend.state_workers.prime_state_workers", new_callable=AsyncMock):
        sched.running = False
        from backend.database import get_session
        from backend.main import app
        app.dependency_overrides[get_session] = override_session
        with TestClient(app) as c:
            c._engine = test_engine
            yield c
        app.dependency_overrides.clear()
