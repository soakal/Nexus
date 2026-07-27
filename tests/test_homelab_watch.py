from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.agents import homelab_watch


@pytest.fixture(autouse=True)
def _reset():
    homelab_watch.reset()
    yield
    homelab_watch.reset()


def _settings(**overrides):
    s = MagicMock()
    s.homelab_watch_enabled = True
    s.homelab_disk_temp_warn_c = 45
    s.homelab_garage_entity_id = "cover.garage_door_garage_door"
    s.homelab_garage_open_minutes = 30
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
async def test_garage_open_past_threshold_fires_once():
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
async def test_garage_closes_clears_timer_and_rearms():
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

    assert set(result.keys()) == {"vms", "docker", "array", "disk_temps", "garage", "backups"}


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
