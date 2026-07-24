from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytest
from apscheduler.triggers.date import DateTrigger


@pytest.mark.asyncio
async def test_infisical_soak_reminder_notifies_phone():
    from backend.scheduler import _infisical_soak_reminder

    with patch("backend.events.notify_phone", new_callable=AsyncMock) as mock_notify:
        mock_notify.return_value = True
        await _infisical_soak_reminder()

    mock_notify.assert_awaited_once()
    args, kwargs = mock_notify.call_args
    assert kwargs.get("kind") == "soak_reminder"
    assert "buttons" not in kwargs or kwargs["buttons"] is None
    message = args[0]
    assert "2026-08-07" in message
    assert "served from legacy vault fallback" in message


@pytest.mark.asyncio
async def test_infisical_soak_reminder_never_raises():
    from backend.scheduler import _infisical_soak_reminder

    with patch("backend.events.notify_phone", new_callable=AsyncMock, side_effect=Exception("hermes down")):
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
