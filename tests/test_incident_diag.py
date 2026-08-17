from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.agents import incident_diag


@pytest.fixture(autouse=True)
def _reset():
    incident_diag.reset()
    yield
    incident_diag.reset()


def _settings(**overrides):
    s = MagicMock()
    s.incident_diag_enabled = True
    s.orchestrator_executor_model = "claude-sonnet-4-6"
    for k, v in overrides.items():
        setattr(s, k, v)
    return s


@pytest.mark.asyncio
async def test_maybe_diagnose_fires_on_vm_key():
    run_mock = AsyncMock(return_value="diagnosis text")
    notify_mock = AsyncMock(return_value=True)
    with patch("backend.config.get_settings", return_value=_settings()), \
         patch("backend.agents.router.run_with_tools", run_mock), \
         patch("backend.events.notify_phone", notify_mock):
        incident_diag.maybe_diagnose({"vms": ["vm:100"], "docker": [], "array": [], "backups": []})
        await incident_diag._diag_task

    run_mock.assert_awaited_once()
    notify_mock.assert_awaited_once()
    assert notify_mock.await_args.kwargs["kind"] == "incident_diagnosis"
    assert notify_mock.await_args.kwargs["buttons"] == [{"text": "▶ Start", "callback_data": "vm:start:100"}]


@pytest.mark.asyncio
async def test_maybe_diagnose_ignores_garage_and_temp_keys():
    run_mock = AsyncMock(return_value="diagnosis text")
    with patch("backend.config.get_settings", return_value=_settings()), \
         patch("backend.agents.router.run_with_tools", run_mock):
        incident_diag.maybe_diagnose({
            "vms": [], "docker": [], "array": [], "backups": [],
            "disk_temps": ["disk:sda"], "garage": ["garage_open"],
        })

    assert incident_diag._diag_task is None
    run_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_maybe_diagnose_respects_per_key_cooldown():
    run_mock = AsyncMock(return_value="diagnosis text")
    notify_mock = AsyncMock(return_value=True)
    with patch("backend.config.get_settings", return_value=_settings()), \
         patch("backend.agents.router.run_with_tools", run_mock), \
         patch("backend.events.notify_phone", notify_mock):
        incident_diag.maybe_diagnose({"vms": ["vm:100"], "docker": [], "array": [], "backups": []})
        await incident_diag._diag_task
        incident_diag.maybe_diagnose({"vms": ["vm:100"], "docker": [], "array": [], "backups": []})

    run_mock.assert_awaited_once()  # second call within cooldown must not fire


@pytest.mark.asyncio
async def test_maybe_diagnose_noop_when_disabled():
    run_mock = AsyncMock(return_value="diagnosis text")
    with patch("backend.config.get_settings", return_value=_settings(incident_diag_enabled=False)), \
         patch("backend.agents.router.run_with_tools", run_mock):
        incident_diag.maybe_diagnose({"vms": ["vm:100"], "docker": [], "array": [], "backups": []})

    assert incident_diag._diag_task is None
    run_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_diagnose_docker_button_omitted_for_unsafe_name():
    run_mock = AsyncMock(return_value="diagnosis text")
    notify_mock = AsyncMock(return_value=True)
    with patch("backend.config.get_settings", return_value=_settings()), \
         patch("backend.agents.router.run_with_tools", run_mock), \
         patch("backend.events.notify_phone", notify_mock):
        await incident_diag.diagnose("docker:sneaky;rm -rf")

    notify_mock.assert_awaited_once()
    assert notify_mock.await_args.kwargs["buttons"] is None


@pytest.mark.asyncio
async def test_diagnose_docker_button_omitted_when_callback_too_long():
    run_mock = AsyncMock(return_value="diagnosis text")
    notify_mock = AsyncMock(return_value=True)
    long_name = "a" * 80  # docker:restart:<name> exceeds Telegram's 64-byte callback_data limit
    with patch("backend.config.get_settings", return_value=_settings()), \
         patch("backend.agents.router.run_with_tools", run_mock), \
         patch("backend.events.notify_phone", notify_mock):
        await incident_diag.diagnose(f"docker:{long_name}")

    assert notify_mock.await_args.kwargs["buttons"] is None


@pytest.mark.asyncio
async def test_diagnose_run_with_tools_exception_never_propagates():
    run_mock = AsyncMock(side_effect=RuntimeError("boom"))
    notify_mock = AsyncMock(return_value=True)
    with patch("backend.config.get_settings", return_value=_settings()), \
         patch("backend.agents.router.run_with_tools", run_mock), \
         patch("backend.events.notify_phone", notify_mock):
        await incident_diag.diagnose("vm:100")  # must not raise

    notify_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_diagnose_budget_exceeded_skips_silently():
    from backend.safety.governor import BudgetExceeded
    run_mock = AsyncMock(side_effect=BudgetExceeded("daily", 4.0, 4.0, None))
    notify_mock = AsyncMock(return_value=True)
    with patch("backend.config.get_settings", return_value=_settings()), \
         patch("backend.agents.router.run_with_tools", run_mock), \
         patch("backend.events.notify_phone", notify_mock):
        await incident_diag.diagnose("vm:100")  # must not raise, must not notify

    notify_mock.assert_not_awaited()
