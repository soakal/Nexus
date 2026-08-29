from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

import backend.database  # noqa: F401 — register all models (incl. OutcomeFlag) on metadata
from backend.agents import homelab_watch


@pytest.fixture(autouse=True)
def _reset():
    homelab_watch.reset()
    yield
    homelab_watch.reset()


@pytest.fixture
def eng(monkeypatch):
    """In-memory DB engine for outcome-flag write-path tests, matching
    tests/test_outcome_flags.py's `eng` fixture."""
    e = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(e)
    monkeypatch.setattr("backend.database.engine", e)
    return e


def _settings(**overrides):
    s = MagicMock()
    s.homelab_watch_enabled = True
    s.homelab_disk_temp_warn_c = 45
    s.homelab_garage_entity_id = "cover.garage_door_garage_door"
    s.homelab_garage_open_minutes = 30
    s.homelab_recovery_notify_enabled = False
    s.homelab_auto_restart_verify_minutes = 3
    # Default OFF: existing fired-alert tests exercise the six homelab checks,
    # not the proposer -- without this, _maybe_propose_on_alert would spawn a
    # REAL (unmocked) propose_goals_tick background task on every test whose
    # settings mock doesn't explicitly care, leaking a dangling asyncio task
    # into whichever test happens to fire an alert. Tests that actually want
    # to exercise event-driven proposing pass proposer_enabled=True explicitly.
    s.proposer_enabled = False
    for k, v in overrides.items():
        setattr(s, k, v)
    return s


def _proxmox_data(vms):
    return SimpleNamespace(vms=vms)


def _unraid_data(array_status="started", disk_health=None, docker_containers=None):
    return SimpleNamespace(
        array_status=array_status,
        disk_health=disk_health or [],
        docker_containers=docker_containers or [],
    )


def _ha_data(entities=None):
    return SimpleNamespace(entities=entities or [])


# ---------------------------------------------------------------------------
# VM/LXC transition-triggered alerts
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_vm_stopped_transition_fires_once():
    data_running = _proxmox_data([{"vmid": 101, "name": "plex-lxc", "status": "running", "type": "lxc"}])
    data_stopped = _proxmox_data([{"vmid": 101, "name": "plex-lxc", "status": "stopped", "type": "lxc"}])
    with patch("backend.integrations.proxmox.fetch", new_callable=AsyncMock, side_effect=[data_running, data_stopped, data_stopped]), \
         patch("backend.events.notify_phone", new_callable=AsyncMock, return_value=True) as mock_notify:
        await homelab_watch.check_proxmox_vms()  # seed: running
        fired1 = await homelab_watch.check_proxmox_vms()  # transition
        fired2 = await homelab_watch.check_proxmox_vms()  # steady state

    assert fired1 == ["vm:101"]
    assert fired2 == []
    mock_notify.assert_awaited_once()
    assert mock_notify.await_args.kwargs["kind"] == "homelab_vm_stopped"
    assert mock_notify.await_args.kwargs["buttons"] == [{"text": "▶ Start", "callback_data": "vm:start:101"}]


@pytest.mark.asyncio
async def test_vm_rearm_on_recovery_then_stop_again():
    running = _proxmox_data([{"vmid": 101, "name": "x", "status": "running", "type": "lxc"}])
    stopped = _proxmox_data([{"vmid": 101, "name": "x", "status": "stopped", "type": "lxc"}])
    with patch("backend.integrations.proxmox.fetch", new_callable=AsyncMock,
               side_effect=[running, stopped, running, stopped]), \
         patch("backend.events.notify_phone", new_callable=AsyncMock, return_value=True) as mock_notify:
        await homelab_watch.check_proxmox_vms()
        fired1 = await homelab_watch.check_proxmox_vms()
        await homelab_watch.check_proxmox_vms()
        fired2 = await homelab_watch.check_proxmox_vms()

    assert fired1 == ["vm:101"]
    assert fired2 == ["vm:101"]
    assert mock_notify.await_count == 2


@pytest.mark.asyncio
async def test_vm_stopped_on_first_observation_never_alerts():
    """No prior 'running' observation -> no alert. This is what makes an
    intentionally-stopped LXC safe with no allowlist needed."""
    stopped = _proxmox_data([{"vmid": 101, "name": "x", "status": "stopped", "type": "lxc"}])
    with patch("backend.integrations.proxmox.fetch", new_callable=AsyncMock, return_value=stopped), \
         patch("backend.events.notify_phone", new_callable=AsyncMock, return_value=True) as mock_notify:
        fired = await homelab_watch.check_proxmox_vms()

    assert fired == []
    mock_notify.assert_not_called()


@pytest.mark.asyncio
async def test_vm_recovery_notice_sent_when_enabled():
    running = _proxmox_data([{"vmid": 101, "name": "plex-lxc", "status": "running", "type": "lxc"}])
    stopped = _proxmox_data([{"vmid": 101, "name": "plex-lxc", "status": "stopped", "type": "lxc"}])
    with patch("backend.config.get_settings", return_value=_settings(homelab_recovery_notify_enabled=True)), \
         patch("backend.integrations.proxmox.fetch", new_callable=AsyncMock,
               side_effect=[running, stopped, running]), \
         patch("backend.events.notify_phone", new_callable=AsyncMock, return_value=True) as mock_notify:
        await homelab_watch.check_proxmox_vms()  # seed: running
        await homelab_watch.check_proxmox_vms()  # stop: pages
        await homelab_watch.check_proxmox_vms()  # recover: recovery notice

    assert mock_notify.await_count == 2
    assert mock_notify.await_args_list[0].kwargs["kind"] == "homelab_vm_stopped"
    assert mock_notify.await_args_list[1].kwargs["kind"] == "homelab_recovered"
    assert "plex-lxc" in mock_notify.await_args_list[1].args[0]


@pytest.mark.asyncio
async def test_vm_recovery_notice_not_sent_when_disabled_by_default():
    """Default settings (homelab_recovery_notify_enabled=False) -> the
    recovery notice never fires, only the original stop alert."""
    running = _proxmox_data([{"vmid": 101, "name": "x", "status": "running", "type": "lxc"}])
    stopped = _proxmox_data([{"vmid": 101, "name": "x", "status": "stopped", "type": "lxc"}])
    with patch("backend.config.get_settings", return_value=_settings()), \
         patch("backend.integrations.proxmox.fetch", new_callable=AsyncMock,
               side_effect=[running, stopped, running]), \
         patch("backend.events.notify_phone", new_callable=AsyncMock, return_value=True) as mock_notify:
        await homelab_watch.check_proxmox_vms()
        await homelab_watch.check_proxmox_vms()
        await homelab_watch.check_proxmox_vms()

    assert mock_notify.await_count == 1
    assert mock_notify.await_args.kwargs["kind"] == "homelab_vm_stopped"


@pytest.mark.asyncio
async def test_vm_recovery_notice_not_sent_when_original_alert_was_suppressed():
    """check_proxmox_vms adds to _paged_alerts in its own branch (separate
    code from _edge_alert's) -- a bug there could let a suppressed stop-alert
    still trigger an unprompted recovery page. Confirms it doesn't."""
    running = _proxmox_data([{"vmid": 101, "name": "x", "status": "running", "type": "lxc"}])
    stopped = _proxmox_data([{"vmid": 101, "name": "x", "status": "stopped", "type": "lxc"}])
    with patch("backend.config.get_settings", return_value=_settings(homelab_recovery_notify_enabled=True)), \
         patch("backend.integrations.proxmox.fetch", new_callable=AsyncMock,
               side_effect=[running, stopped, running]), \
         patch("backend.agents.outcomes.record_flag_ex", new_callable=AsyncMock,
               return_value={"id": None, "surface": False}), \
         patch("backend.events.notify_phone", new_callable=AsyncMock, return_value=True) as mock_notify:
        await homelab_watch.check_proxmox_vms()  # seed: running
        await homelab_watch.check_proxmox_vms()  # stop: suppressed, never paged
        await homelab_watch.check_proxmox_vms()  # recover: must stay silent

    mock_notify.assert_not_called()


@pytest.mark.asyncio
async def test_docker_recovery_notice_not_sent_when_original_alert_was_suppressed():
    """Same guard as the VM test above, for check_docker's own separate
    _paged_alerts.add() branch."""
    running = _unraid_data(docker_containers=[{"name": "plex", "state": "RUNNING"}])
    stopped = _unraid_data(docker_containers=[{"name": "plex", "state": "EXITED"}])
    with patch("backend.config.get_settings", return_value=_settings(homelab_recovery_notify_enabled=True)), \
         patch("backend.integrations.unraid.fetch", new_callable=AsyncMock,
               side_effect=[running, stopped, running]), \
         patch("backend.agents.outcomes.record_flag_ex", new_callable=AsyncMock,
               return_value={"id": None, "surface": False}), \
         patch("backend.events.notify_phone", new_callable=AsyncMock, return_value=True) as mock_notify:
        await homelab_watch.check_docker()  # seed: running
        await homelab_watch.check_docker()  # stop: suppressed, never paged
        await homelab_watch.check_docker()  # recover: must stay silent

    mock_notify.assert_not_called()


@pytest.mark.asyncio
async def test_proxmox_fetch_failure_preserves_state_and_recovers():
    """A transient outage must not blank the baseline -- blanking would re-seed
    on recovery and silently swallow the transition that happened during it."""
    running = _proxmox_data([{"vmid": 101, "name": "x", "status": "running", "type": "lxc"}])
    stopped = _proxmox_data([{"vmid": 101, "name": "x", "status": "stopped", "type": "lxc"}])
    with patch("backend.integrations.proxmox.fetch", new_callable=AsyncMock,
               side_effect=[running, RuntimeError("proxmox down"), stopped]), \
         patch("backend.events.notify_phone", new_callable=AsyncMock, return_value=True) as mock_notify:
        await homelab_watch.check_proxmox_vms()
        fired_during_outage = await homelab_watch.check_proxmox_vms()
        fired_after = await homelab_watch.check_proxmox_vms()

    assert fired_during_outage == []
    assert fired_after == ["vm:101"]
    mock_notify.assert_awaited_once()


# ---------------------------------------------------------------------------
# Docker transition-triggered alerts
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_docker_stopped_transition_fires_once_with_restart_button():
    running = _unraid_data(docker_containers=[{"name": "plex", "state": "RUNNING"}])
    stopped = _unraid_data(docker_containers=[{"name": "plex", "state": "EXITED"}])
    with patch("backend.integrations.unraid.fetch", new_callable=AsyncMock, side_effect=[running, stopped, stopped]), \
         patch("backend.events.notify_phone", new_callable=AsyncMock, return_value=True) as mock_notify:
        await homelab_watch.check_docker()
        fired1 = await homelab_watch.check_docker()
        fired2 = await homelab_watch.check_docker()

    assert fired1 == ["docker:plex"]
    assert fired2 == []
    assert mock_notify.await_args.kwargs["kind"] == "homelab_docker_stopped"
    assert mock_notify.await_args.kwargs["buttons"] == [{"text": "↺ Restart", "callback_data": "docker:restart:plex"}]


@pytest.mark.asyncio
async def test_docker_recovery_notice_sent_when_enabled():
    running = _unraid_data(docker_containers=[{"name": "plex", "state": "RUNNING"}])
    stopped = _unraid_data(docker_containers=[{"name": "plex", "state": "EXITED"}])
    with patch("backend.config.get_settings", return_value=_settings(homelab_recovery_notify_enabled=True)), \
         patch("backend.integrations.unraid.fetch", new_callable=AsyncMock,
               side_effect=[running, stopped, running]), \
         patch("backend.events.notify_phone", new_callable=AsyncMock, return_value=True) as mock_notify:
        await homelab_watch.check_docker()
        await homelab_watch.check_docker()
        await homelab_watch.check_docker()

    assert mock_notify.await_count == 2
    assert mock_notify.await_args_list[1].kwargs["kind"] == "homelab_recovered"
    assert "plex" in mock_notify.await_args_list[1].args[0]


@pytest.mark.asyncio
async def test_docker_unsafe_name_gets_no_button():
    running = _unraid_data(docker_containers=[{"name": "plex;evil", "state": "RUNNING"}])
    stopped = _unraid_data(docker_containers=[{"name": "plex;evil", "state": "EXITED"}])
    with patch("backend.integrations.unraid.fetch", new_callable=AsyncMock, side_effect=[running, stopped]), \
         patch("backend.events.notify_phone", new_callable=AsyncMock, return_value=True) as mock_notify:
        await homelab_watch.check_docker()
        await homelab_watch.check_docker()

    assert mock_notify.await_args.kwargs["buttons"] is None


@pytest.mark.asyncio
async def test_docker_long_name_never_truncates_callback_data():
    long_name = "a" * 80  # pushes "docker:restart:<name>" past Telegram's 64-byte limit
    running = _unraid_data(docker_containers=[{"name": long_name, "state": "RUNNING"}])
    stopped = _unraid_data(docker_containers=[{"name": long_name, "state": "EXITED"}])
    with patch("backend.integrations.unraid.fetch", new_callable=AsyncMock, side_effect=[running, stopped]), \
         patch("backend.events.notify_phone", new_callable=AsyncMock, return_value=True) as mock_notify:
        await homelab_watch.check_docker()
        await homelab_watch.check_docker()

    assert mock_notify.await_args.kwargs["buttons"] is None


@pytest.mark.asyncio
async def test_docker_name_html_escaped_in_alert_text():
    running = _unraid_data(docker_containers=[{"name": "plex<b>", "state": "RUNNING"}])
    stopped = _unraid_data(docker_containers=[{"name": "plex<b>", "state": "EXITED"}])
    with patch("backend.integrations.unraid.fetch", new_callable=AsyncMock, side_effect=[running, stopped]), \
         patch("backend.events.notify_phone", new_callable=AsyncMock, return_value=True) as mock_notify:
        await homelab_watch.check_docker()
        await homelab_watch.check_docker()

    content = mock_notify.await_args.args[0]
    assert "<b>" not in content or "plex&lt;b&gt;" in content
    assert "&lt;b&gt;" in content


# ---------------------------------------------------------------------------
# Docker auto-restart (2026-08-28) — promoted "unraid_docker" auto-allow kind
# ---------------------------------------------------------------------------

def _policy_overrides(auto_allow=()):
    return {"auto_allow": set(auto_allow), "forbid": set()}


@pytest.mark.asyncio
async def test_docker_stopped_unpromoted_kind_is_byte_identical_to_today():
    """No "unraid_docker" in auto_allow -> _maybe_auto_restart_docker must not
    even attempt a dispatch; behavior matches pre-2026-08-28 exactly."""
    running = _unraid_data(docker_containers=[{"name": "plex", "state": "RUNNING"}])
    stopped = _unraid_data(docker_containers=[{"name": "plex", "state": "EXITED"}])
    with patch("backend.safety.governor.get_policy_overrides", return_value=_policy_overrides()), \
         patch("backend.safety.broker.execute_action", new_callable=AsyncMock) as mock_execute, \
         patch("backend.integrations.unraid.fetch", new_callable=AsyncMock, side_effect=[running, stopped]), \
         patch("backend.events.notify_phone", new_callable=AsyncMock, return_value=True) as mock_notify:
        await homelab_watch.check_docker()
        await homelab_watch.check_docker()

    mock_execute.assert_not_awaited()
    assert mock_notify.await_args.kwargs["kind"] == "homelab_docker_stopped"
    assert mock_notify.await_args.kwargs["buttons"] == [{"text": "↺ Restart", "callback_data": "docker:restart:plex"}]


@pytest.mark.asyncio
async def test_docker_stopped_promoted_kind_dispatches_autonomous_restart():
    running = _unraid_data(docker_containers=[{"name": "plex", "state": "RUNNING"}])
    stopped = _unraid_data(docker_containers=[{"name": "plex", "state": "EXITED"}])
    from backend.safety.broker import ActionResult, Decision, Reversibility, Risk
    exec_result = ActionResult(decision=Decision.EXECUTED, risk=Risk.HIGH,
                                reversibility=Reversibility.REVERSIBLE_BY_INVERSE,
                                log_id=1, result={"success": True})
    with patch("backend.safety.governor.get_policy_overrides",
               return_value=_policy_overrides(["unraid_docker"])), \
         patch("backend.safety.broker.execute_action", new_callable=AsyncMock,
               return_value=exec_result) as mock_execute, \
         patch("backend.integrations.unraid.fetch", new_callable=AsyncMock, side_effect=[running, stopped]), \
         patch("backend.events.notify_phone", new_callable=AsyncMock, return_value=True) as mock_notify:
        await homelab_watch.check_docker()
        await homelab_watch.check_docker()

    mock_execute.assert_awaited_once_with(
        actor="autonomous", kind="unraid_docker", target="plex",
        payload={"container_id": "plex"},
    )
    assert mock_notify.await_args.kwargs["kind"] == "homelab_docker_stopped"
    assert "auto-restart dispatched" in mock_notify.await_args.args[0]
    assert "plex" in homelab_watch._pending_restart_verifies


@pytest.mark.asyncio
async def test_docker_auto_restart_verify_recovers_no_failure_page():
    running = _unraid_data(docker_containers=[{"name": "plex", "state": "RUNNING"}])
    stopped = _unraid_data(docker_containers=[{"name": "plex", "state": "EXITED"}])
    from backend.safety.broker import ActionResult, Decision, Reversibility, Risk
    exec_result = ActionResult(decision=Decision.EXECUTED, risk=Risk.HIGH,
                                reversibility=Reversibility.REVERSIBLE_BY_INVERSE,
                                log_id=1, result={"success": True})
    with patch("backend.safety.governor.get_policy_overrides",
               return_value=_policy_overrides(["unraid_docker"])), \
         patch("backend.safety.broker.execute_action", new_callable=AsyncMock, return_value=exec_result), \
         patch("backend.integrations.unraid.fetch", new_callable=AsyncMock, side_effect=[running, stopped, running]), \
         patch("backend.events.notify_phone", new_callable=AsyncMock, return_value=True) as mock_notify:
        await homelab_watch.check_docker()
        await homelab_watch.check_docker()  # stopped -> dispatch
        await homelab_watch.check_docker()  # recovered before the verify deadline

    assert "plex" not in homelab_watch._pending_restart_verifies
    kinds = [c.kwargs["kind"] for c in mock_notify.await_args_list]
    assert "homelab_docker_restart_failed" not in kinds


@pytest.mark.asyncio
async def test_docker_auto_restart_verify_timeout_pages_once_flag_stays_open(monkeypatch):
    running = _unraid_data(docker_containers=[{"name": "plex", "state": "RUNNING"}])
    stopped = _unraid_data(docker_containers=[{"name": "plex", "state": "EXITED"}])
    from backend.safety.broker import ActionResult, Decision, Reversibility, Risk
    exec_result = ActionResult(decision=Decision.EXECUTED, risk=Risk.HIGH,
                                reversibility=Reversibility.REVERSIBLE_BY_INVERSE,
                                log_id=1, result={"success": True})
    with patch("backend.safety.governor.get_policy_overrides",
               return_value=_policy_overrides(["unraid_docker"])), \
         patch("backend.safety.broker.execute_action", new_callable=AsyncMock, return_value=exec_result), \
         patch("backend.integrations.unraid.fetch", new_callable=AsyncMock, side_effect=[running, stopped, stopped]), \
         patch("backend.events.notify_phone", new_callable=AsyncMock, return_value=True) as mock_notify:
        await homelab_watch.check_docker()
        await homelab_watch.check_docker()  # stopped -> dispatch, verify armed
        # Force the verify deadline to have already passed, matching how a
        # real multi-minute gap between 60s ticks would look.
        homelab_watch._pending_restart_verifies["plex"] = 0.0
        await homelab_watch.check_docker()  # still not RUNNING -> timeout page

    assert "plex" not in homelab_watch._pending_restart_verifies
    failure_calls = [c for c in mock_notify.await_args_list if c.kwargs["kind"] == "homelab_docker_restart_failed"]
    assert len(failure_calls) == 1
    assert failure_calls[0].kwargs["buttons"] == [{"text": "↺ Restart", "callback_data": "docker:restart:plex"}]


@pytest.mark.asyncio
async def test_docker_auto_restart_stopped_result_pages_failure_immediately_no_wait():
    """The 3-state contract's 'stop succeeded, start failed' case (result has
    stopped=True, success is falsy) must page failure right away, not arm a
    3-minute wait for a container already confirmed down."""
    running = _unraid_data(docker_containers=[{"name": "plex", "state": "RUNNING"}])
    stopped = _unraid_data(docker_containers=[{"name": "plex", "state": "EXITED"}])
    from backend.safety.broker import ActionResult, Decision, Reversibility, Risk
    exec_result = ActionResult(decision=Decision.EXECUTED, risk=Risk.HIGH,
                                reversibility=Reversibility.REVERSIBLE_BY_INVERSE,
                                log_id=1, result={"success": False, "stopped": True, "error": "start failed"})
    with patch("backend.safety.governor.get_policy_overrides",
               return_value=_policy_overrides(["unraid_docker"])), \
         patch("backend.safety.broker.execute_action", new_callable=AsyncMock, return_value=exec_result), \
         patch("backend.integrations.unraid.fetch", new_callable=AsyncMock, side_effect=[running, stopped]), \
         patch("backend.events.notify_phone", new_callable=AsyncMock, return_value=True) as mock_notify:
        await homelab_watch.check_docker()
        await homelab_watch.check_docker()

    assert "plex" not in homelab_watch._pending_restart_verifies
    assert mock_notify.await_args.kwargs["kind"] == "homelab_docker_restart_failed"
    assert "confirmed STOPPED" in mock_notify.await_args.args[0]
    assert mock_notify.await_args.kwargs["buttons"] == [{"text": "↺ Restart", "callback_data": "docker:restart:plex"}]


def test_reset_clears_pending_restart_verifies():
    homelab_watch._pending_restart_verifies["plex"] = 123.0
    homelab_watch.reset()
    assert homelab_watch._pending_restart_verifies == {}


# ---------------------------------------------------------------------------
# Unraid array — level-triggered
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_array_unhealthy_edge_trigger():
    bad = _unraid_data(array_status="stopped")
    with patch("backend.integrations.unraid.fetch", new_callable=AsyncMock, return_value=bad), \
         patch("backend.events.notify_phone", new_callable=AsyncMock, return_value=True) as mock_notify:
        fired1 = await homelab_watch.check_unraid_array()
        fired2 = await homelab_watch.check_unraid_array()

    assert fired1 == ["unraid_array"]
    assert fired2 == []
    mock_notify.assert_awaited_once()


@pytest.mark.asyncio
async def test_array_unknown_status_is_not_a_breach():
    unknown = _unraid_data(array_status="unknown")
    with patch("backend.integrations.unraid.fetch", new_callable=AsyncMock, return_value=unknown), \
         patch("backend.events.notify_phone", new_callable=AsyncMock, return_value=True) as mock_notify:
        fired = await homelab_watch.check_unraid_array()

    assert fired == []
    mock_notify.assert_not_called()


@pytest.mark.asyncio
async def test_array_rearms_after_recovery():
    bad = _unraid_data(array_status="stopped")
    good = _unraid_data(array_status="started")
    with patch("backend.integrations.unraid.fetch", new_callable=AsyncMock, side_effect=[bad, good, bad]), \
         patch("backend.events.notify_phone", new_callable=AsyncMock, return_value=True) as mock_notify:
        await homelab_watch.check_unraid_array()
        await homelab_watch.check_unraid_array()
        fired = await homelab_watch.check_unraid_array()

    assert fired == ["unraid_array"]
    assert mock_notify.await_count == 2


@pytest.mark.asyncio
async def test_array_recovery_notice_sent_when_enabled():
    bad = _unraid_data(array_status="stopped")
    good = _unraid_data(array_status="started")
    with patch("backend.config.get_settings", return_value=_settings(homelab_recovery_notify_enabled=True)), \
         patch("backend.integrations.unraid.fetch", new_callable=AsyncMock, side_effect=[bad, good]), \
         patch("backend.events.notify_phone", new_callable=AsyncMock, return_value=True) as mock_notify:
        await homelab_watch.check_unraid_array()  # pages
        await homelab_watch.check_unraid_array()  # recovers

    assert mock_notify.await_count == 2
    assert mock_notify.await_args_list[1].kwargs["kind"] == "homelab_recovered"


def test_homelab_recovery_notify_defaults_to_false():
    """B10's whole safety story rests on this being off by default -- the
    other recovery tests above all supply their own MagicMock settings, so
    none of them actually pin the real Settings default. Flipping
    config.py's default silently would fail no other test."""
    from backend.config import Settings
    s = Settings(_env_file=None)
    assert s.homelab_recovery_notify_enabled is False


@pytest.mark.asyncio
async def test_array_recovery_notice_not_sent_when_original_alert_was_suppressed():
    """A recovery notice must only follow an alert that actually paged -- if
    outcomes.record_flag_ex suppressed the original page (dedup/cooldown),
    the matching recovery must stay silent too, not surprise-page on its own."""
    bad = _unraid_data(array_status="stopped")
    good = _unraid_data(array_status="started")
    with patch("backend.config.get_settings", return_value=_settings(homelab_recovery_notify_enabled=True)), \
         patch("backend.integrations.unraid.fetch", new_callable=AsyncMock, side_effect=[bad, good]), \
         patch("backend.agents.outcomes.record_flag_ex", new_callable=AsyncMock,
               return_value={"id": None, "surface": False}), \
         patch("backend.events.notify_phone", new_callable=AsyncMock, return_value=True) as mock_notify:
        await homelab_watch.check_unraid_array()  # suppressed, never paged
        await homelab_watch.check_unraid_array()  # recovers, must stay silent

    mock_notify.assert_not_called()


# ---------------------------------------------------------------------------
# Disk temps
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_disk_temp_over_threshold_fires_once_and_rearms():
    hot = _unraid_data(disk_health=[{"name": "disk1", "temp": 50, "status": "healthy"}])
    cool = _unraid_data(disk_health=[{"name": "disk1", "temp": 30, "status": "healthy"}])
    with patch("backend.config.get_settings", return_value=_settings()), \
         patch("backend.integrations.unraid.fetch", new_callable=AsyncMock, side_effect=[hot, hot, cool, hot]), \
         patch("backend.events.notify_phone", new_callable=AsyncMock, return_value=True) as mock_notify:
        fired1 = await homelab_watch.check_disk_temps()
        fired2 = await homelab_watch.check_disk_temps()
        await homelab_watch.check_disk_temps()
        fired3 = await homelab_watch.check_disk_temps()

    assert fired1 == ["unraid_temp"]
    assert fired2 == []
    assert fired3 == ["unraid_temp"]


@pytest.mark.asyncio
async def test_disk_temp_none_and_non_numeric_ignored_not_crashed():
    data = _unraid_data(disk_health=[
        {"name": "disk1", "temp": None, "status": "standby"},
        {"name": "disk2", "temp": "n/a", "status": "unknown"},
    ])
    with patch("backend.config.get_settings", return_value=_settings()), \
         patch("backend.integrations.unraid.fetch", new_callable=AsyncMock, return_value=data), \
         patch("backend.events.notify_phone", new_callable=AsyncMock, return_value=True) as mock_notify:
        fired = await homelab_watch.check_disk_temps()

    assert fired == []
    mock_notify.assert_not_called()


# ---------------------------------------------------------------------------
# Garage door
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_garage_open_under_threshold_no_alert():
    entities = [{"entity_id": "cover.garage_door_garage_door", "state": "open"}]
    with patch("backend.config.get_settings", return_value=_settings()), \
         patch("backend.integrations.homeassistant.fetch", new_callable=AsyncMock, return_value=_ha_data(entities)), \
         patch("time.monotonic", return_value=1000.0), \
         patch("backend.events.notify_phone", new_callable=AsyncMock, return_value=True) as mock_notify:
        fired = await homelab_watch.check_garage()

    assert fired == []
    mock_notify.assert_not_called()


@pytest.mark.asyncio
async def test_garage_open_past_threshold_fires_once(eng):
    """Uses the isolated in-memory `eng` fixture (not the real dev nexus.db) --
    without it, a leftover false_positive row for this exact fingerprint in
    dev's real db can fall inside the 30-day cooldown and make record_flag_ex
    correctly suppress paging, which would make this test flaky depending on
    what the dev db happens to contain rather than what this test sets up."""
    entities = [{"entity_id": "cover.garage_door_garage_door", "state": "open"}]
    with patch("backend.config.get_settings", return_value=_settings()), \
         patch("backend.integrations.homeassistant.fetch", new_callable=AsyncMock, return_value=_ha_data(entities)), \
         patch("backend.events.notify_phone", new_callable=AsyncMock, return_value=True) as mock_notify:
        with patch("time.monotonic", return_value=1000.0):
            fired1 = await homelab_watch.check_garage()
        with patch("time.monotonic", return_value=1000.0 + 31 * 60):
            fired2 = await homelab_watch.check_garage()
        with patch("time.monotonic", return_value=1000.0 + 32 * 60):
            fired3 = await homelab_watch.check_garage()

    assert fired1 == []
    assert fired2 == ["garage_open"]
    assert fired3 == []
    mock_notify.assert_awaited_once()


@pytest.mark.asyncio
async def test_garage_closes_clears_timer_and_rearms(eng):
    """Uses the isolated in-memory `eng` fixture -- see
    test_garage_open_past_threshold_fires_once's docstring for why (a leftover
    false_positive row in the real dev db for this fingerprint would suppress
    paging via record_flag_ex's cooldown check, making this flaky)."""
    open_entities = [{"entity_id": "cover.garage_door_garage_door", "state": "open"}]
    closed_entities = [{"entity_id": "cover.garage_door_garage_door", "state": "closed"}]
    with patch("backend.config.get_settings", return_value=_settings()), \
         patch("backend.events.notify_phone", new_callable=AsyncMock, return_value=True) as mock_notify:
        with patch("backend.integrations.homeassistant.fetch", new_callable=AsyncMock, return_value=_ha_data(open_entities)), \
             patch("time.monotonic", return_value=1000.0):
            await homelab_watch.check_garage()
        with patch("backend.integrations.homeassistant.fetch", new_callable=AsyncMock, return_value=_ha_data(open_entities)), \
             patch("time.monotonic", return_value=1000.0 + 31 * 60):
            await homelab_watch.check_garage()  # fires
        with patch("backend.integrations.homeassistant.fetch", new_callable=AsyncMock, return_value=_ha_data(closed_entities)), \
             patch("time.monotonic", return_value=1000.0 + 32 * 60):
            await homelab_watch.check_garage()  # clears
        with patch("backend.integrations.homeassistant.fetch", new_callable=AsyncMock, return_value=_ha_data(open_entities)), \
             patch("time.monotonic", return_value=1000.0 + 33 * 60):
            await homelab_watch.check_garage()  # re-open, timer restarts
        with patch("backend.integrations.homeassistant.fetch", new_callable=AsyncMock, return_value=_ha_data(open_entities)), \
             patch("time.monotonic", return_value=1000.0 + 65 * 60):
            fired = await homelab_watch.check_garage()  # past threshold again

    assert fired == ["garage_open"]
    assert mock_notify.await_count == 2


@pytest.mark.asyncio
async def test_garage_edge_alert_records_flag_clears_and_rearms_same_row(eng):
    """docs/outcome-tracker-spec.md AC18-AC20, driven end-to-end against a real
    OutcomeFlag table (not a mocked outcomes.record_flag) so status transitions
    are actually pinned down, not just call args.

    AC18: door open past threshold -> record_flag('homelab_watch','garage_open',...)
    fires before notify_phone, and notify_phone's buttons carry
    flag:resolved:{id} / flag:false_positive:{id} for the row it created.
    AC20: a homelab_watch.reset() (simulated restart) with the condition still
    active re-fires the in-memory latch (unchanged existing behavior), but
    record_flag's dedup means the still-open DB row is reused, not duplicated
    -- exactly one row for the fingerprint. Exercised via _edge_alert directly
    (the exact function check_garage delegates to) rather than check_garage()
    a second time, because check_garage's OWN garage-open-timer state
    (_garage_open_since) is separately wiped by reset() too -- going back
    through check_garage would re-seed that timer at 0 elapsed and hit an
    intervening under-threshold tick, which legitimately auto-clears the flag
    per _edge_alert's documented "every falling-edge tick clears" contract.
    That is a real, separate behavior of the timer, not what AC20 (scoped to
    "leaving _active_alerts alone") is pinning down.
    AC19: the door closing on the next tick auto-resolves that same row via
    clear_flag, with resolved_by="auto:condition_cleared".
    """
    from backend.database import OutcomeFlag

    open_entities = [{"entity_id": "cover.garage_door_garage_door", "state": "open"}]
    closed_entities = [{"entity_id": "cover.garage_door_garage_door", "state": "closed"}]

    with patch("backend.config.get_settings", return_value=_settings()), \
         patch("backend.events.notify_phone", new_callable=AsyncMock, return_value=True) as mock_notify:
        # AC18 — seed then cross the threshold: latch fires, flag recorded.
        with patch("backend.integrations.homeassistant.fetch", new_callable=AsyncMock, return_value=_ha_data(open_entities)), \
             patch("time.monotonic", return_value=1000.0):
            await homelab_watch.check_garage()
        with patch("backend.integrations.homeassistant.fetch", new_callable=AsyncMock, return_value=_ha_data(open_entities)), \
             patch("time.monotonic", return_value=1000.0 + 31 * 60):
            fired1 = await homelab_watch.check_garage()

        assert fired1 == ["garage_open"]
        with Session(eng) as s:
            rows = s.exec(select(OutcomeFlag).where(OutcomeFlag.fingerprint == "homelab_watch:garage_open")).all()
        assert len(rows) == 1
        flag_id = rows[0].id
        assert rows[0].status == "open"
        assert mock_notify.await_args.kwargs["buttons"] == [
            {"text": "✓ Resolved", "callback_data": f"flag:resolved:{flag_id}"},
            {"text": "✗ False alarm", "callback_data": f"flag:false_positive:{flag_id}"},
        ]

        # AC20 — restart while the condition persists: reset() clears
        # _active_alerts, so the very next active=True tick re-fires the
        # latch, but the still-open DB row is reused (no duplicate insert).
        homelab_watch.reset()
        fired2 = await homelab_watch._edge_alert(
            "garage_open", True,
            "NEXUS: garage door has been open for over 30 minutes.",
            kind="homelab_garage",
        )

        assert fired2 is True
        with Session(eng) as s:
            rows_after_restart = s.exec(select(OutcomeFlag).where(OutcomeFlag.fingerprint == "homelab_watch:garage_open")).all()
        assert len(rows_after_restart) == 1
        assert rows_after_restart[0].id == flag_id
        assert rows_after_restart[0].status == "open"
        assert mock_notify.await_args.kwargs["buttons"][0]["callback_data"] == f"flag:resolved:{flag_id}"

        # AC19 — door closes: clear_flag auto-resolves that same row.
        with patch("backend.integrations.homeassistant.fetch", new_callable=AsyncMock, return_value=_ha_data(closed_entities)), \
             patch("time.monotonic", return_value=2000.0 + 32 * 60):
            await homelab_watch.check_garage()

    with Session(eng) as s:
        resolved_row = s.get(OutcomeFlag, flag_id)
    assert resolved_row.status == "resolved"
    assert resolved_row.resolved_by == "auto:condition_cleared"


@pytest.mark.asyncio
async def test_garage_active_hint_suppresses_page_but_still_writes_and_latches(eng):
    """CAL25/CAL26 (docs/calibration-loop-spec.md 8.5): with an active medium
    calibration hint for homelab_watch:garage_open, check_garage past
    threshold still calls record_flag_ex and writes a row -- stamped
    suppressed=True with a non-empty suppressed_reason -- but calls
    events.notify_phone zero times (CAL25). The in-memory latch still fires
    (_active_alerts still contains "garage_open" afterward), so a second tick
    writes no second row and pages no second time -- suppression must not
    bypass the latch (CAL26)."""
    from backend.database import CalibrationHint, OutcomeFlag

    with Session(eng) as s:
        s.add(CalibrationHint(
            fingerprint="homelab_watch:garage_open",
            status="active", verdict_count=20, false_positive_count=20, fp_rate=1.0,
        ))
        s.commit()

    open_entities = [{"entity_id": "cover.garage_door_garage_door", "state": "open"}]
    settings = _settings(
        calibration_enabled=True, calibration_suppression_enabled=True,
        calibration_suppress_high_severity=False,
    )
    with patch("backend.config.get_settings", return_value=settings), \
         patch("backend.integrations.homeassistant.fetch", new_callable=AsyncMock, return_value=_ha_data(open_entities)), \
         patch("backend.events.notify_phone", new_callable=AsyncMock, return_value=True) as mock_notify:
        with patch("time.monotonic", return_value=1000.0):
            await homelab_watch.check_garage()
        with patch("time.monotonic", return_value=1000.0 + 31 * 60):
            fired1 = await homelab_watch.check_garage()  # crosses threshold, suppressed
        with patch("time.monotonic", return_value=1000.0 + 32 * 60):
            fired2 = await homelab_watch.check_garage()  # second tick, still latched

    assert fired1 == ["garage_open"]
    assert "garage_open" in homelab_watch._active_alerts  # latch still holds -- CAL26
    assert fired2 == []
    mock_notify.assert_not_called()  # CAL25

    with Session(eng) as s:
        rows = s.exec(select(OutcomeFlag).where(OutcomeFlag.fingerprint == "homelab_watch:garage_open")).all()
    assert len(rows) == 1  # no duplicate row from the second tick -- CAL26
    assert rows[0].status == "open"
    assert rows[0].suppressed is True
    assert rows[0].suppressed_reason


@pytest.mark.asyncio
async def test_garage_fp_cooldown_suppresses_page_then_pages_again_after(eng):
    """CAL30 (docs/calibration-loop-spec.md 8.5): the existing FP-cooldown
    branch of record_flag_ex now suppresses the PAGE, not just the row (the
    Finding B fix) -- reached via check_garage's own call site, not just
    outcomes.py directly (test_outcome_flags.py's test_ac7_record_flag_false_
    positive_cooldown pins the row-level dedup only, predates this cycle, and
    never touches notify_phone). After a human resolves the flag as
    false_positive, the next check_garage rising edge inside the cooldown
    window calls notify_phone zero times; past the cooldown it pages again."""
    from datetime import datetime, timedelta

    from backend.agents import outcomes
    from backend.database import OutcomeFlag

    settings = _settings(outcome_flag_false_positive_cooldown_days=7)
    open_entities = [{"entity_id": "cover.garage_door_garage_door", "state": "open"}]

    with patch("backend.config.get_settings", return_value=settings), \
         patch("backend.integrations.homeassistant.fetch", new_callable=AsyncMock, return_value=_ha_data(open_entities)), \
         patch("backend.events.notify_phone", new_callable=AsyncMock, return_value=True) as mock_notify:
        with patch("time.monotonic", return_value=1000.0):
            await homelab_watch.check_garage()
        with patch("time.monotonic", return_value=1000.0 + 31 * 60):
            fired1 = await homelab_watch.check_garage()  # rising edge, pages once

    assert fired1 == ["garage_open"]
    mock_notify.assert_awaited_once()

    with Session(eng) as s:
        row = s.exec(select(OutcomeFlag).where(OutcomeFlag.fingerprint == "homelab_watch:garage_open")).one()
        flag_id = row.id

    assert await outcomes.resolve_flag(flag_id, "false_positive") == "false_positive"
    homelab_watch.reset()  # simulate a fresh rising edge, same technique as AC20's test

    # Still inside the 7-day cooldown -> notify_phone is NOT called this time.
    with patch("backend.config.get_settings", return_value=settings), \
         patch("backend.integrations.homeassistant.fetch", new_callable=AsyncMock, return_value=_ha_data(open_entities)), \
         patch("backend.events.notify_phone", new_callable=AsyncMock, return_value=True) as mock_notify2:
        with patch("time.monotonic", return_value=1000.0):
            await homelab_watch.check_garage()
        with patch("time.monotonic", return_value=1000.0 + 31 * 60):
            fired2 = await homelab_watch.check_garage()

    assert fired2 == ["garage_open"]  # the latch still fires...
    mock_notify2.assert_not_called()  # ...but the page is suppressed by the cooldown -- CAL30

    # Backdate the resolution past the cooldown window.
    with Session(eng) as s:
        row = s.get(OutcomeFlag, flag_id)
        row.resolved_at = datetime.utcnow() - timedelta(days=8)
        s.add(row)
        s.commit()
    homelab_watch.reset()

    with patch("backend.config.get_settings", return_value=settings), \
         patch("backend.integrations.homeassistant.fetch", new_callable=AsyncMock, return_value=_ha_data(open_entities)), \
         patch("backend.events.notify_phone", new_callable=AsyncMock, return_value=True) as mock_notify3:
        with patch("time.monotonic", return_value=1000.0):
            await homelab_watch.check_garage()
        with patch("time.monotonic", return_value=1000.0 + 31 * 60):
            fired3 = await homelab_watch.check_garage()

    assert fired3 == ["garage_open"]
    mock_notify3.assert_awaited_once()  # cooldown lapsed -> pages again


@pytest.mark.asyncio
async def test_outcome_flags_disabled_fails_open_still_pages(eng):
    """Cycle 7 regression (Realist finding): record_flag_ex's outcome_flags_
    enabled=False branch must return surface=True (fail open), matching the
    pre-outcome-tracker rollback guarantee (docs/outcome-tracker-spec.md §7.7,
    docs/calibration-loop-spec.md §9.7) that alerts and briefings behave
    exactly as before when the tracker is disabled. Covers both call-site
    shapes: _edge_alert (garage, id-less) and the direct record_flag_ex call
    in check_proxmox_vms (severity=high, also id-less here since id stays
    None in this branch) -- both must still call notify_phone with
    buttons=None, not be silently swallowed by `if d["surface"]:`."""
    settings = _settings(outcome_flags_enabled=False)

    open_entities = [{"entity_id": "cover.garage_door_garage_door", "state": "open"}]
    with patch("backend.config.get_settings", return_value=settings), \
         patch("backend.integrations.homeassistant.fetch", new_callable=AsyncMock, return_value=_ha_data(open_entities)), \
         patch("backend.events.notify_phone", new_callable=AsyncMock, return_value=True) as mock_notify:
        with patch("time.monotonic", return_value=1000.0):
            await homelab_watch.check_garage()
        with patch("time.monotonic", return_value=1000.0 + 31 * 60):
            fired = await homelab_watch.check_garage()

    assert fired == ["garage_open"]
    mock_notify.assert_awaited_once()
    assert mock_notify.await_args.kwargs["buttons"] is None

    data_running = _proxmox_data([{"vmid": 101, "name": "plex-lxc", "status": "running", "type": "lxc"}])
    data_stopped = _proxmox_data([{"vmid": 101, "name": "plex-lxc", "status": "stopped", "type": "lxc"}])
    with patch("backend.config.get_settings", return_value=settings), \
         patch("backend.integrations.proxmox.fetch", new_callable=AsyncMock, side_effect=[data_running, data_stopped]), \
         patch("backend.events.notify_phone", new_callable=AsyncMock, return_value=True) as mock_notify_vm:
        await homelab_watch.check_proxmox_vms()  # seed: running
        fired_vm = await homelab_watch.check_proxmox_vms()  # transition, stopped

    assert fired_vm == ["vm:101"]
    mock_notify_vm.assert_awaited_once()
    assert mock_notify_vm.await_args.kwargs["buttons"] == [{"text": "▶ Start", "callback_data": "vm:start:101"}]


@pytest.mark.asyncio
async def test_garage_entity_missing_no_alert_no_exception():
    with patch("backend.config.get_settings", return_value=_settings()), \
         patch("backend.integrations.homeassistant.fetch", new_callable=AsyncMock, return_value=_ha_data([])), \
         patch("backend.events.notify_phone", new_callable=AsyncMock, return_value=True) as mock_notify:
        fired = await homelab_watch.check_garage()

    assert fired == []
    mock_notify.assert_not_called()


# ---------------------------------------------------------------------------
# Backups
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_backup_failed_fires_once_and_rearms():
    failed = {"node": "pve", "status": "failed", "detail": "job errors"}
    ok = {"node": "pve", "status": "ok", "detail": "OK"}
    with patch("backend.integrations.proxmox.fetch_backups", new_callable=AsyncMock, side_effect=[failed, failed, ok, failed]), \
         patch("backend.events.notify_phone", new_callable=AsyncMock, return_value=True) as mock_notify:
        fired1 = await homelab_watch.check_backups()
        fired2 = await homelab_watch.check_backups()
        await homelab_watch.check_backups()
        fired3 = await homelab_watch.check_backups()

    assert fired1 == ["vzdump_failed"]
    assert fired2 == []
    assert fired3 == ["vzdump_failed"]


@pytest.mark.asyncio
async def test_backup_running_or_none_status_not_a_breach():
    for status in ("running", "none"):
        homelab_watch.reset()
        with patch("backend.integrations.proxmox.fetch_backups", new_callable=AsyncMock,
                   return_value={"node": "pve", "status": status}), \
             patch("backend.events.notify_phone", new_callable=AsyncMock, return_value=True) as mock_notify:
            fired = await homelab_watch.check_backups()
        assert fired == []
        mock_notify.assert_not_called()


@pytest.mark.asyncio
async def test_backup_fetch_raise_is_not_a_failed_backup():
    """A transport error is an outage, not a failed backup -- must not latch."""
    with patch("backend.integrations.proxmox.fetch_backups", new_callable=AsyncMock, side_effect=RuntimeError("timeout")), \
         patch("backend.events.notify_phone", new_callable=AsyncMock, return_value=True) as mock_notify:
        fired = await homelab_watch.check_backups()

    assert fired == []
    mock_notify.assert_not_called()


@pytest.mark.asyncio
async def test_edge_alert_severity_high_for_array_and_backup_medium_for_others():
    """Rollout step 6 (spec §3.4): _edge_alert bumps severity to "high" for
    unraid_array and vzdump_failed only -- unraid_temp and garage_open must
    stay at record_flag_ex's "medium" default, matching CAL25/CAL27/CAL30's
    assumption."""
    record_flag_ex_mock = AsyncMock(return_value={"id": 1, "surface": True, "reason": None})
    with patch("backend.agents.outcomes.record_flag_ex", record_flag_ex_mock), \
         patch("backend.events.notify_phone", new_callable=AsyncMock, return_value=True):
        await homelab_watch._edge_alert("unraid_array", True, "array bad", kind="homelab_array")
        homelab_watch.reset()
        await homelab_watch._edge_alert("vzdump_failed", True, "backup bad", kind="homelab_backup")
        homelab_watch.reset()
        await homelab_watch._edge_alert("unraid_temp", True, "temp bad", kind="homelab_disk_temp")
        homelab_watch.reset()
        await homelab_watch._edge_alert("garage_open", True, "garage bad", kind="homelab_garage")

    severities = {c.args[1]: c.kwargs["severity"] for c in record_flag_ex_mock.await_args_list}
    assert severities["unraid_array"] == "high"
    assert severities["vzdump_failed"] == "high"
    assert severities["unraid_temp"] == "medium"
    assert severities["garage_open"] == "medium"


# ---------------------------------------------------------------------------
# check_expected_resources — declared-baseline comparison (2026-08-21)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_expected_resources_no_baseline_declared_is_noop(eng):
    """No ExpectedResource rows declared -> nothing evaluated, no fetch calls
    at all -- declaring a baseline is opt-in."""
    with patch("backend.config.get_settings", return_value=_settings()), \
         patch("backend.integrations.unraid.fetch", new_callable=AsyncMock) as mock_unraid, \
         patch("backend.integrations.proxmox.fetch", new_callable=AsyncMock) as mock_proxmox, \
         patch("backend.events.notify_phone", new_callable=AsyncMock) as mock_notify:
        fired = await homelab_watch.check_expected_resources()

    assert fired == []
    mock_unraid.assert_not_called()
    mock_proxmox.assert_not_called()
    mock_notify.assert_not_called()


@pytest.mark.asyncio
async def test_expected_resources_mismatch_fires_with_resolve_buttons_and_detail(eng):
    """A docker container declared 'running' but observed stopped fires once,
    pages with the standard flag:resolved/flag:false_positive buttons, and
    records a detail blob outcomes.resolve_flag can read back to sync the
    baseline (kind/identifier/observed_state)."""
    import json as _json

    from backend.agents import expected_resources
    from backend.database import OutcomeFlag

    await expected_resources.upsert("docker", "sonarr", "running")

    with patch("backend.config.get_settings", return_value=_settings()), \
         patch("backend.integrations.unraid.fetch", new_callable=AsyncMock,
               return_value=_unraid_data(docker_containers=[{"name": "sonarr", "state": "EXITED"}])), \
         patch("backend.events.notify_phone", new_callable=AsyncMock, return_value=True) as mock_notify:
        fired1 = await homelab_watch.check_expected_resources()
        fired2 = await homelab_watch.check_expected_resources()  # steady state, no re-fire

    assert fired1 == ["expected:docker:sonarr"]
    assert fired2 == []
    mock_notify.assert_awaited_once()
    assert mock_notify.await_args.kwargs["kind"] == "homelab_expected_mismatch"

    with Session(eng) as s:
        row = s.exec(select(OutcomeFlag).where(OutcomeFlag.fingerprint == "homelab_watch:expected:docker:sonarr")).all()
    assert len(row) == 1
    detail = _json.loads(row[0].detail)
    assert detail == {"kind": "docker", "identifier": "sonarr", "observed_state": "stopped"}
    assert mock_notify.await_args.kwargs["buttons"] == [
        {"text": "✓ Resolved", "callback_data": f"flag:resolved:{row[0].id}"},
        {"text": "✗ False alarm", "callback_data": f"flag:false_positive:{row[0].id}"},
    ]


@pytest.mark.asyncio
async def test_expected_resources_match_does_not_fire(eng):
    from backend.agents import expected_resources

    await expected_resources.upsert("docker", "sonarr", "running")

    with patch("backend.config.get_settings", return_value=_settings()), \
         patch("backend.integrations.unraid.fetch", new_callable=AsyncMock,
               return_value=_unraid_data(docker_containers=[{"name": "sonarr", "state": "RUNNING"}])), \
         patch("backend.events.notify_phone", new_callable=AsyncMock, return_value=True) as mock_notify:
        fired = await homelab_watch.check_expected_resources()

    assert fired == []
    mock_notify.assert_not_called()


@pytest.mark.asyncio
async def test_expected_resources_vm_and_lxc_kinds_use_proxmox(eng):
    from backend.agents import expected_resources

    await expected_resources.upsert("lxc", "201", "running")

    with patch("backend.config.get_settings", return_value=_settings()), \
         patch("backend.integrations.proxmox.fetch", new_callable=AsyncMock,
               return_value=_proxmox_data([{"vmid": 201, "name": "plex-lxc", "status": "stopped", "type": "lxc"}])), \
         patch("backend.events.notify_phone", new_callable=AsyncMock, return_value=True) as mock_notify:
        fired = await homelab_watch.check_expected_resources()

    assert fired == ["expected:lxc:201"]
    mock_notify.assert_awaited_once()


@pytest.mark.asyncio
async def test_expected_resources_unreadable_live_state_skips_not_fires(eng):
    """A fetch failure means the live state couldn't be read this tick -- must
    be skipped (not treated as a mismatch or a clear), never raise."""
    from backend.agents import expected_resources

    await expected_resources.upsert("docker", "sonarr", "running")

    with patch("backend.config.get_settings", return_value=_settings()), \
         patch("backend.integrations.unraid.fetch", new_callable=AsyncMock, side_effect=RuntimeError("down")), \
         patch("backend.events.notify_phone", new_callable=AsyncMock) as mock_notify:
        fired = await homelab_watch.check_expected_resources()

    assert fired == []
    mock_notify.assert_not_called()


@pytest.mark.asyncio
async def test_expected_resources_recovers_and_clears_flag(eng):
    from backend.agents import expected_resources
    from backend.database import OutcomeFlag

    await expected_resources.upsert("docker", "sonarr", "running")

    with patch("backend.config.get_settings", return_value=_settings()), \
         patch("backend.events.notify_phone", new_callable=AsyncMock, return_value=True):
        with patch("backend.integrations.unraid.fetch", new_callable=AsyncMock,
                   return_value=_unraid_data(docker_containers=[{"name": "sonarr", "state": "EXITED"}])):
            await homelab_watch.check_expected_resources()
        with patch("backend.integrations.unraid.fetch", new_callable=AsyncMock,
                   return_value=_unraid_data(docker_containers=[{"name": "sonarr", "state": "RUNNING"}])):
            fired = await homelab_watch.check_expected_resources()

    assert fired == []
    with Session(eng) as s:
        row = s.exec(select(OutcomeFlag).where(OutcomeFlag.fingerprint == "homelab_watch:expected:docker:sonarr")).first()
    assert row.status == "resolved"
    assert row.resolved_by == "auto:condition_cleared"


@pytest.mark.asyncio
async def test_expected_resources_included_in_run_homelab_watch(eng):
    from backend.agents import expected_resources

    await expected_resources.upsert("docker", "sonarr", "running")

    with patch("backend.config.get_settings", return_value=_settings()), \
         patch("backend.integrations.proxmox.fetch", new_callable=AsyncMock, return_value=_proxmox_data([])), \
         patch("backend.integrations.unraid.fetch", new_callable=AsyncMock,
               return_value=_unraid_data(docker_containers=[{"name": "sonarr", "state": "EXITED"}])), \
         patch("backend.integrations.homeassistant.fetch", new_callable=AsyncMock, return_value=_ha_data([])), \
         patch("backend.integrations.proxmox.fetch_backups", new_callable=AsyncMock, return_value={"node": "pve", "status": "none"}), \
         patch("backend.events.notify_phone", new_callable=AsyncMock, return_value=True):
        result = await homelab_watch.run_homelab_watch()

    assert result["expected"] == ["expected:docker:sonarr"]


# ---------------------------------------------------------------------------
# run_homelab_watch — top-level entry point
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_homelab_watch_disabled_skips_everything():
    with patch("backend.config.get_settings", return_value=_settings(homelab_watch_enabled=False)), \
         patch("backend.integrations.proxmox.fetch", new_callable=AsyncMock) as mock_proxmox:
        result = await homelab_watch.run_homelab_watch()

    assert result == {"skipped": True}
    mock_proxmox.assert_not_called()


@pytest.mark.asyncio
async def test_run_homelab_watch_all_integrations_failing_never_raises():
    with patch("backend.config.get_settings", return_value=_settings()), \
         patch("backend.integrations.proxmox.fetch", new_callable=AsyncMock, side_effect=RuntimeError("down")), \
         patch("backend.integrations.unraid.fetch", new_callable=AsyncMock, side_effect=RuntimeError("down")), \
         patch("backend.integrations.homeassistant.fetch", new_callable=AsyncMock, side_effect=RuntimeError("down")), \
         patch("backend.integrations.proxmox.fetch_backups", new_callable=AsyncMock, side_effect=RuntimeError("down")):
        result = await homelab_watch.run_homelab_watch()

    assert isinstance(result, dict)
    assert result["vms"] == []
    assert result["docker"] == []


@pytest.mark.asyncio
async def test_run_homelab_watch_one_check_raising_does_not_cancel_the_others():
    """A bug INSIDE one check (not just its integration's fetch() failing) must
    not cancel the other five -- proven by patching a check directly to raise
    (bypassing its own internal try/except) and confirming a LATER check in
    the dict-literal evaluation order still ran."""
    with patch("backend.config.get_settings", return_value=_settings()), \
         patch("backend.agents.homelab_watch.check_garage", side_effect=RuntimeError("bug")), \
         patch("backend.agents.homelab_watch.check_backups", new_callable=AsyncMock, return_value=["vzdump_failed"]) as mock_backups, \
         patch("backend.agents.homelab_watch.check_proxmox_vms", new_callable=AsyncMock, return_value=[]), \
         patch("backend.agents.homelab_watch.check_docker", new_callable=AsyncMock, return_value=[]), \
         patch("backend.agents.homelab_watch.check_unraid_array", new_callable=AsyncMock, return_value=[]), \
         patch("backend.agents.homelab_watch.check_disk_temps", new_callable=AsyncMock, return_value=[]):
        result = await homelab_watch.run_homelab_watch()

    assert result["garage"] == []  # the raiser degraded to empty, not propagated
    assert result["backups"] == ["vzdump_failed"]  # and did NOT get cancelled by it
    mock_backups.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_homelab_watch_returns_summary_dict_shape():
    with patch("backend.config.get_settings", return_value=_settings()), \
         patch("backend.integrations.proxmox.fetch", new_callable=AsyncMock, return_value=_proxmox_data([])), \
         patch("backend.integrations.unraid.fetch", new_callable=AsyncMock, return_value=_unraid_data()), \
         patch("backend.integrations.homeassistant.fetch", new_callable=AsyncMock, return_value=_ha_data([])), \
         patch("backend.integrations.proxmox.fetch_backups", new_callable=AsyncMock, return_value={"node": "pve", "status": "none"}):
        result = await homelab_watch.run_homelab_watch()

    assert set(result.keys()) == {"vms", "docker", "array", "disk_temps", "garage", "backups", "expected"}


# ---------------------------------------------------------------------------
# Notify kind contract — locks in the six kind strings and their mute-ability
# ---------------------------------------------------------------------------

def test_kinds_are_not_on_the_never_mutable_floor():
    from backend.safety import governor
    kinds = {
        "homelab_vm_stopped", "homelab_docker_stopped", "homelab_array",
        "homelab_disk_temp", "homelab_garage", "homelab_backup_failed",
    }
    assert kinds.isdisjoint(governor._NEVER_MUTABLE_NOTIFY_KINDS)


# ---------------------------------------------------------------------------
# Event-driven proposing (2026-08-17) — a fired alert triggers one proposer
# tick instead of waiting up to 6h for the next scheduled one.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_maybe_propose_on_alert_schedules_when_fired():
    tick_mock = AsyncMock(return_value={"status": "ok"})
    with patch("backend.config.get_settings", return_value=_settings(proposer_enabled=True)), \
         patch("backend.agents.proposer.propose_goals_tick", tick_mock):
        homelab_watch._maybe_propose_on_alert(True)
        await homelab_watch._event_propose_task

    tick_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_maybe_propose_on_alert_respects_cooldown():
    tick_mock = AsyncMock(return_value={"status": "ok"})
    with patch("backend.config.get_settings", return_value=_settings(proposer_enabled=True)), \
         patch("backend.agents.proposer.propose_goals_tick", tick_mock):
        homelab_watch._maybe_propose_on_alert(True)
        await homelab_watch._event_propose_task
        homelab_watch._maybe_propose_on_alert(True)  # within cooldown -- must not schedule again

    tick_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_maybe_propose_on_alert_noop_when_nothing_fired():
    tick_mock = AsyncMock(return_value={"status": "ok"})
    with patch("backend.config.get_settings", return_value=_settings(proposer_enabled=True)), \
         patch("backend.agents.proposer.propose_goals_tick", tick_mock):
        homelab_watch._maybe_propose_on_alert(False)

    assert homelab_watch._event_propose_task is None
    tick_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_maybe_propose_on_alert_noop_when_proposer_disabled():
    tick_mock = AsyncMock(return_value={"status": "ok"})
    with patch("backend.config.get_settings", return_value=_settings(proposer_enabled=False)), \
         patch("backend.agents.proposer.propose_goals_tick", tick_mock):
        homelab_watch._maybe_propose_on_alert(True)

    assert homelab_watch._event_propose_task is None
    tick_mock.assert_not_awaited()
