"""Regression guards for errors that used to be swallowed silently.

Each test here pins a failure that previously produced an indistinguishable
"success" (or an indistinguishable zero) for the caller: a kill switch that
reports OK while the scheduler kept running, a secret backend outage that looks
exactly like a missing key, and a corrupt organiser state file that renders as
"nothing has ever run".
"""
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from backend.secrets.manager import get_secret as real_get_secret  # pre-mock reference


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """App client on an in-memory DB, mirroring tests/test_governor.py's fixture."""
    (tmp_path / ".vault.key").write_bytes(b"A" * 32)
    (tmp_path / "nexus.vault").write_text("{}")
    monkeypatch.chdir(tmp_path)

    from backend.database import get_session  # registers the models on SQLModel.metadata

    test_engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(test_engine)
    monkeypatch.setattr("backend.database.engine", test_engine)

    def override_session():
        with Session(test_engine) as session:
            yield session

    with patch("backend.database.create_db_and_tables"), \
         patch("backend.scheduler.setup_scheduler"), \
         patch("backend.scheduler.scheduler") as sched, \
         patch("backend.agents.memo_watcher.start_watcher_blocking"), \
         patch("backend.agents.memo_watcher.stop_watcher", new_callable=AsyncMock):
        sched.running = False
        from backend.auth import require_api_key
        from backend.main import app
        app.dependency_overrides[get_session] = override_session
        app.dependency_overrides[require_api_key] = lambda: None
        with TestClient(app) as c:
            yield c
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Kill switch — a scheduler that refuses to pause must be visible
# ---------------------------------------------------------------------------

class _ExplodingScheduler:
    running = True

    def pause(self):
        raise RuntimeError("scheduler wedged")

    def resume(self):
        raise RuntimeError("scheduler wedged")


def test_pause_surfaces_scheduler_failure(client, caplog):
    with patch("backend.scheduler.scheduler", _ExplodingScheduler()):
        with caplog.at_level(logging.ERROR):
            resp = client.post("/api/safety/pause")
    assert resp.status_code == 200
    body = resp.json()
    # The autonomy flag still flipped — that half succeeded.
    assert body["autonomy_enabled"] is False
    # ...but the caller can now tell the scheduler did NOT actually pause.
    assert body["scheduler_error"] == "scheduler wedged"
    assert any("scheduler.pause() failed" in r.message for r in caplog.records)


def test_resume_surfaces_scheduler_failure(client):
    with patch("backend.scheduler.scheduler", _ExplodingScheduler()):
        resp = client.post("/api/safety/resume")
    assert resp.status_code == 200
    assert resp.json()["scheduler_error"] == "scheduler wedged"


def test_pause_reports_no_error_on_healthy_scheduler(client):
    healthy = MagicMock()
    healthy.running = True
    with patch("backend.scheduler.scheduler", healthy):
        resp = client.post("/api/safety/pause")
    assert resp.json()["scheduler_error"] is None
    healthy.pause.assert_called_once()


# ---------------------------------------------------------------------------
# Secret manager — a backend outage is not a missing key
# ---------------------------------------------------------------------------

def test_backend_outage_is_chained_onto_the_keyerror(monkeypatch, caplog):
    from backend.secrets import manager

    broken = MagicMock()
    broken.__name__ = "backend.secrets.vault"
    broken.get_secret.side_effect = RuntimeError("vault key unreadable")
    monkeypatch.setattr(manager, "_backend", lambda: broken)
    monkeypatch.delenv("SOME_KEY", raising=False)

    with caplog.at_level(logging.WARNING):
        with pytest.raises(KeyError) as excinfo:
            real_get_secret("SOME_KEY")

    cause = excinfo.value.__cause__
    assert isinstance(cause, RuntimeError)
    assert "vault key unreadable" in str(cause)
    assert any("vault key unreadable" in r.message for r in caplog.records)


def test_missing_key_still_raises_plain_keyerror(monkeypatch):
    from backend.secrets import manager

    empty = MagicMock()
    empty.__name__ = "backend.secrets.vault"
    empty.get_secret.side_effect = KeyError("NOPE")
    monkeypatch.setattr(manager, "_backend", lambda: empty)
    monkeypatch.delenv("NOPE", raising=False)

    with pytest.raises(KeyError):
        real_get_secret("NOPE")


def test_env_fallback_still_wins_over_a_failing_backend(monkeypatch):
    from backend.secrets import manager

    broken = MagicMock()
    broken.__name__ = "backend.secrets.vault"
    broken.get_secret.side_effect = RuntimeError("down")
    monkeypatch.setattr(manager, "_backend", lambda: broken)
    monkeypatch.setenv("FROM_ENV", "value-from-env")

    assert real_get_secret("FROM_ENV") == "value-from-env"


# ---------------------------------------------------------------------------
# Brain Organizer status — a corrupt state file must not read as "idle"
# ---------------------------------------------------------------------------

def test_status_reports_unreadable_state_file(client, tmp_path, monkeypatch, caplog):
    from backend.api import brain_organizer

    corrupt = tmp_path / "processed.json"
    corrupt.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(brain_organizer, "_PROCESSED", corrupt)
    monkeypatch.setattr(brain_organizer, "_CONFIG", tmp_path / "missing-config.json")
    monkeypatch.setattr(brain_organizer, "_LOG", tmp_path / "missing.log")

    with caplog.at_level(logging.WARNING):
        resp = client.get("/api/brain-organizer/status")

    assert resp.status_code == 200
    errors = resp.json()["errors"]
    assert any("processed.json unreadable" in e for e in errors)
    assert any("unreadable" in r.message for r in caplog.records)


def test_status_has_no_errors_when_state_is_readable(client, tmp_path, monkeypatch):
    from backend.api import brain_organizer

    good = tmp_path / "processed.json"
    good.write_text('{"a.md": {"status": "ok", "timestamp": "2026-01-01T00:00:00"}}', encoding="utf-8")
    monkeypatch.setattr(brain_organizer, "_PROCESSED", good)
    monkeypatch.setattr(brain_organizer, "_CONFIG", tmp_path / "missing-config.json")
    monkeypatch.setattr(brain_organizer, "_LOG", tmp_path / "missing.log")

    body = client.get("/api/brain-organizer/status").json()
    assert body["errors"] == []
    assert body["succeeded"] == 1


# ---------------------------------------------------------------------------
# 401-burst counter — still never raises, but no longer disappears
# ---------------------------------------------------------------------------

def test_record_failure_logs_instead_of_vanishing(monkeypatch, caplog):
    from backend.safety import authfail

    class _BrokenState(dict):
        def __contains__(self, item):
            raise RuntimeError("counter bug")

    monkeypatch.setattr(authfail, "_STATE", _BrokenState())
    with caplog.at_level(logging.WARNING):
        authfail.record_failure("1.2.3.4", "/api/tasks")  # must not raise
    assert any("counter bug" in r.message for r in caplog.records)
