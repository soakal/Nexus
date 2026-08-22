"""Covers backend/scheduler.py::_calibration_soak_reminder — the one-off nag
toward docs/calibration-loop-spec.md §9.5 step 7 (the ops flip of
calibration_suppression_enabled, which is Brian's decision, not this job's).

Mirrors tests/test_infisical_soak_reminder.py: hint_report is patched rather
than seeded, since the branch under test is the message-shaping one, not
calibration.py's own aggregation (already covered by test_calibration_loop.py).
"""
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from apscheduler.triggers.date import DateTrigger


def _settings(**overrides):
    s = MagicMock()
    s.calibration_suppression_enabled = False
    s.calibration_window_days = 30
    for k, v in overrides.items():
        setattr(s, k, v)
    return s


def _report(watching, **overrides):
    r = {
        "window_days": 30,
        "suppression_enabled": False,
        "fp_threshold": 0.60,
        "min_verdicts": 5,
        "suppressed": [],
        "watching": watching,
        "overridden": [],
    }
    r.update(overrides)
    return r


async def _run(report, settings=None):
    from backend.scheduler import _calibration_soak_reminder

    with patch("backend.config.get_settings", return_value=settings or _settings()), \
         patch("backend.agents.calibration.hint_report", new_callable=AsyncMock) as hr, \
         patch("backend.events.notify_phone", new_callable=AsyncMock) as notify:
        hr.return_value = report
        notify.return_value = True
        await _calibration_soak_reminder()
    return notify


@pytest.mark.asyncio
async def test_eligible_rules_are_named():
    """A fingerprint over BOTH bars is what suppression would actually silence,
    so it has to appear by name — a bare count would leave Brian no more able to
    decide than he was before the reminder fired."""
    notify = await _run(_report([
        {"fingerprint": "homelab_watch:adguard_down", "fp_rate": 0.8, "verdict_count": 10},
        {"fingerprint": "watchdog:stall", "fp_rate": 0.1, "verdict_count": 9},
    ]))

    notify.assert_awaited_once()
    args, kwargs = notify.call_args
    assert kwargs.get("kind") == "soak_reminder"
    message = args[0]
    assert "homelab_watch:adguard_down" in message
    assert "1 rule(s) now clear both bars" in message
    # Under the fp_threshold — must NOT be presented as suppressible.
    assert "watchdog:stall" not in message


@pytest.mark.asyncio
async def test_watching_but_nothing_eligible_says_so():
    """Spec §9.6 explicitly predicts zero eligible rules early on and warns
    against 'fixing' that by lowering min_verdicts — the message must read as
    expected-and-fine, not as a broken feature."""
    notify = await _run(_report([
        {"fingerprint": "watchdog:stall", "fp_rate": 0.9, "verdict_count": 2},
    ]))

    message = notify.call_args[0][0]
    assert "NONE yet clear both bars" in message
    assert "§9.6" in message


@pytest.mark.asyncio
async def test_empty_report_does_not_claim_clean():
    """hint_report degrades to a zeroed structure instead of raising, so an
    empty `watching` is ambiguous between 'quiet' and 'the read failed'. Same
    rule as _infisical_soak_reminder's error branch: never imply clean."""
    notify = await _run(_report([]))

    message = notify.call_args[0][0]
    assert "came back empty" in message
    assert "clear both bars" not in message


@pytest.mark.asyncio
async def test_no_reminder_once_suppression_is_already_on():
    """If Brian has already taken step 7 the decision is made; nagging about it
    would be pure noise."""
    notify = await _run(
        _report([{"fingerprint": "x", "fp_rate": 0.9, "verdict_count": 9}]),
        settings=_settings(calibration_suppression_enabled=True),
    )
    notify.assert_not_awaited()


@pytest.mark.asyncio
async def test_reminder_never_raises():
    """Same never-raises contract as every other scheduler job body."""
    from backend.scheduler import _calibration_soak_reminder

    with patch("backend.config.get_settings", side_effect=RuntimeError("boom")):
        await _calibration_soak_reminder()


def test_reminder_registered_with_date_trigger(monkeypatch):
    import backend.scheduler as sched_mod
    from backend.scheduler import scheduler, setup_scheduler

    monkeypatch.setattr(sched_mod, "CALIBRATION_SOAK_REMINDER_AT", datetime(2099, 1, 1, 9, 0))
    with patch.object(scheduler, "add_job") as mock_add:
        setup_scheduler("07:30", "America/New_York")

    ids = {c.kwargs.get("id"): c for c in mock_add.call_args_list}
    assert "calibration_soak_reminder" in ids
    assert isinstance(ids["calibration_soak_reminder"].args[1], DateTrigger)


def test_reminder_not_registered_after_fire_window(monkeypatch):
    """A gate date in the past must degrade to 'not registering', not to a job
    that silently never fires -- the exact failure the Infisical reminder was
    rescheduled for on 2026-08-22."""
    import backend.scheduler as sched_mod
    from backend.scheduler import scheduler, setup_scheduler

    monkeypatch.setattr(sched_mod, "CALIBRATION_SOAK_REMINDER_AT", datetime(2020, 1, 1, 9, 0))
    with patch.object(scheduler, "add_job") as mock_add:
        setup_scheduler("07:30", "America/New_York")  # must not raise

    ids = {c.kwargs.get("id") for c in mock_add.call_args_list}
    assert "calibration_soak_reminder" not in ids
