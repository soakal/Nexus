from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytest
from apscheduler.triggers.date import DateTrigger
from sqlmodel import SQLModel, create_engine
from sqlmodel.pool import StaticPool

# Ensure all tables (incl. SecretFallback) are registered on SQLModel.metadata.
import backend.database  # noqa: F401,E402


def _make_isolated_engine():
    """In-memory StaticPool engine so _infisical_soak_reminder's new
    fallback_log.drain()/summary() calls (Task 7) never touch the real,
    live nexus.db this repo runs against."""
    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(eng)
    return eng


@pytest.mark.asyncio
async def test_infisical_soak_reminder_notifies_phone(monkeypatch):
    from backend.scheduler import _infisical_soak_reminder

    monkeypatch.setattr("backend.database.engine", _make_isolated_engine())

    with patch("backend.events.notify_phone", new_callable=AsyncMock) as mock_notify:
        mock_notify.return_value = True
        await _infisical_soak_reminder()

    mock_notify.assert_awaited_once()
    args, kwargs = mock_notify.call_args
    assert kwargs.get("kind") == "soak_reminder"
    assert "buttons" not in kwargs or kwargs["buttons"] is None
    message = args[0]
    assert "2026-08-07" in message
    # Zero-events branch (no fallback recorded against the isolated engine):
    # quotes the durable DB-backed signal, not a log file. "Zero legacy-vault
    # fallbacks" is the zero-branch's own distinguishing phrase -- unlike
    # "SecretFallback" (which both branches mention), asserting on it actually
    # pins which branch fired instead of passing either way.
    assert "SecretFallback" in message
    assert "Zero legacy-vault fallbacks" in message
    assert "Top keys" not in message
    assert "logs/backend.err.log" not in message
    assert "resets on every backend restart" not in message


@pytest.mark.asyncio
async def test_infisical_soak_reminder_reports_recorded_fallbacks(monkeypatch):
    """The nonzero-events branch -- distinct code path from the test above,
    needs its own coverage rather than relying on the zero-events assertions
    to also happen to hold."""
    from backend.scheduler import _infisical_soak_reminder
    from backend.secrets import fallback_log

    monkeypatch.setattr("backend.database.engine", _make_isolated_engine())
    fallback_log.reset()
    fallback_log.record("SOME_SECRET_KEY")
    fallback_log.record("SOME_SECRET_KEY")

    with patch("backend.events.notify_phone", new_callable=AsyncMock) as mock_notify:
        mock_notify.return_value = True
        await _infisical_soak_reminder()

    message = mock_notify.call_args.args[0]
    assert "Zero legacy-vault fallbacks" not in message
    assert "SOME_SECRET_KEY" in message
    assert "2x" in message
    assert "Top keys" in message
    fallback_log.reset()


@pytest.mark.asyncio
async def test_infisical_soak_reminder_reports_unknown_on_read_error(monkeypatch):
    """total_events==0 alone can't distinguish 'genuinely clean' from 'the
    summary read failed' -- a failed read must never render as a clean
    all-clear (see fallback_log.summary()'s error-path shape: total_events=0
    plus an "error" key)."""
    from backend.scheduler import _infisical_soak_reminder
    from backend.secrets import fallback_log

    monkeypatch.setattr("backend.database.engine", _make_isolated_engine())
    monkeypatch.setattr(
        fallback_log, "summary",
        lambda: {"total_events": 0, "key_count": 0, "last_at": None, "keys": [],
                  "pending": 0, "dropped": 0, "error": "db unavailable"},
    )

    with patch("backend.events.notify_phone", new_callable=AsyncMock) as mock_notify:
        mock_notify.return_value = True
        await _infisical_soak_reminder()

    message = mock_notify.call_args.args[0]
    assert "Zero legacy-vault fallbacks" not in message
    assert "Looks clear" not in message
    assert "UNKNOWN" in message
    assert "db unavailable" in message


@pytest.mark.asyncio
async def test_infisical_soak_reminder_flags_undrained_pending_events(monkeypatch):
    """Events sitting in the in-memory buffer but not yet drained to the DB
    must not be silently omitted from the reminder -- a "0 events" read
    right after a failed drain would otherwise look identical to genuinely
    zero fallbacks."""
    from backend.scheduler import _infisical_soak_reminder
    from backend.secrets import fallback_log

    monkeypatch.setattr("backend.database.engine", _make_isolated_engine())
    monkeypatch.setattr(
        fallback_log, "summary",
        lambda: {"total_events": 3, "key_count": 1, "last_at": None, "keys": [],
                  "pending": 2, "dropped": 0},
    )

    with patch("backend.events.notify_phone", new_callable=AsyncMock) as mock_notify:
        mock_notify.return_value = True
        await _infisical_soak_reminder()

    message = mock_notify.call_args.args[0]
    assert "Zero legacy-vault fallbacks" not in message
    assert "2 more event(s) buffered" in message


@pytest.mark.asyncio
async def test_infisical_soak_reminder_escapes_html_in_secret_key(monkeypatch):
    """secret_key is stored from an arbitrary string (fallback_log.record has
    no charset restriction) and this message can go out with parse_mode=HTML
    (see backend/events.py) -- an unescaped '<'/'&' would risk Telegram
    rejecting the whole message (same class of bug homelab_watch.py already
    guards against for its own interpolated names)."""
    from backend.scheduler import _infisical_soak_reminder
    from backend.secrets import fallback_log

    monkeypatch.setattr("backend.database.engine", _make_isolated_engine())
    monkeypatch.setattr(
        fallback_log, "summary",
        lambda: {"total_events": 1, "key_count": 1, "last_at": None,
                  "keys": [{"secret_key": "<script>KEY", "event_count": 1,
                            "backend_from": "infisical", "backend_to": "vault",
                            "first_at": "2026-07-29T00:00:00", "last_at": "2026-07-29T00:00:00"}],
                  "pending": 0, "dropped": 0},
    )

    with patch("backend.events.notify_phone", new_callable=AsyncMock) as mock_notify:
        mock_notify.return_value = True
        await _infisical_soak_reminder()

    message = mock_notify.call_args.args[0]
    assert "<script>" not in message
    assert "&lt;script&gt;" in message


@pytest.mark.asyncio
async def test_infisical_soak_reminder_never_raises(monkeypatch):
    from backend.scheduler import _infisical_soak_reminder

    monkeypatch.setattr("backend.database.engine", _make_isolated_engine())

    with patch("backend.events.notify_phone", new_callable=AsyncMock, side_effect=Exception("telegram down")):
        await _infisical_soak_reminder()  # must not raise


def test_infisical_soak_reminder_registered_with_date_trigger(monkeypatch):
    import backend.scheduler as sched_mod
    from backend.scheduler import setup_scheduler, scheduler

    monkeypatch.setattr(sched_mod, "INFISICAL_SOAK_REMINDER_AT", datetime(2099, 1, 1, 9, 0))
    with patch.object(scheduler, "add_job") as mock_add:
        setup_scheduler("07:30", "America/New_York")

    ids = {c.kwargs.get("id"): c for c in mock_add.call_args_list}
    assert "infisical_soak_reminder" in ids
    trigger = ids["infisical_soak_reminder"].args[1]
    assert isinstance(trigger, DateTrigger)


def test_infisical_soak_reminder_not_registered_after_fire_window(monkeypatch):
    import backend.scheduler as sched_mod
    from backend.scheduler import setup_scheduler, scheduler

    monkeypatch.setattr(sched_mod, "INFISICAL_SOAK_REMINDER_AT", datetime(2020, 1, 1, 9, 0))
    with patch.object(scheduler, "add_job") as mock_add:
        setup_scheduler("07:30", "America/New_York")  # must not raise

    ids = {c.kwargs.get("id") for c in mock_add.call_args_list}
    assert "infisical_soak_reminder" not in ids


def test_soak_reminder_kind_not_suppressed_by_default():
    from backend.config import Settings
    s = Settings(_env_file=None)
    assert "soak_reminder" not in s.phone_suppressed_kinds
