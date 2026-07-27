from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.agents import homelab_digest


def _settings(**overrides):
    s = MagicMock()
    s.homelab_digest_enabled = True
    s.homelab_garage_entity_id = "cover.garage_door_garage_door"
    s.briefing_timezone = "America/Detroit"
    for k, v in overrides.items():
        setattr(s, k, v)
    return s


def _proxmox_data(vms=None):
    return SimpleNamespace(
        node="pve", node_status="online", cpu_pct=10.0,
        mem_used_gb=1.0, mem_total_gb=2.0,
        storage_used_gb=1.0, storage_total_gb=2.0,
        vms=vms or [],
    )


def _unifi_data(client_count=5, uplink_status="ok"):
    return SimpleNamespace(client_count=client_count, uplink_status=uplink_status, new_devices=[])


def _unraid_data(array_status="started", parity_status="disk_ok", disk_health=None, docker_containers=None):
    return SimpleNamespace(
        array_status=array_status, parity_status=parity_status,
        storage_used_gb=1.0, storage_total_gb=2.0,
        disk_health=disk_health or [], docker_containers=docker_containers or [],
    )


def _adguard_data(blocked_today=1, queries_today=10, blocked_pct=10.0, filtering_enabled=True):
    return SimpleNamespace(
        blocked_today=blocked_today, queries_today=queries_today,
        blocked_pct=blocked_pct, filtering_enabled=filtering_enabled,
    )


def _channels_data(recording_now=None, upcoming=None, failed_recordings=None):
    return SimpleNamespace(
        recording_now=recording_now or [], upcoming=upcoming or [],
        failed_recordings=failed_recordings or [],
        storage_used_gb=1.0, storage_total_gb=2.0,
    )


def _ha_data(entities=None):
    return SimpleNamespace(entities=entities or [])


def _patch_all_healthy():
    """Patches every integration this digest reads to a working, empty-ish
    fixture -- used as the baseline for tests that only care about one
    section's behavior."""
    return [
        patch("backend.integrations.proxmox.fetch", new_callable=AsyncMock, return_value=_proxmox_data()),
        patch("backend.integrations.unifi.fetch", new_callable=AsyncMock, return_value=_unifi_data()),
        patch("backend.integrations.unraid.fetch", new_callable=AsyncMock, return_value=_unraid_data()),
        patch("backend.integrations.adguard.fetch", new_callable=AsyncMock, return_value=_adguard_data()),
        patch("backend.integrations.channels_dvr.fetch", new_callable=AsyncMock, return_value=_channels_data()),
        patch("backend.integrations.homeassistant.fetch", new_callable=AsyncMock, return_value=_ha_data()),
        patch("backend.integrations.sports.get_tigers_last_game", new_callable=AsyncMock, return_value=None),
        patch("backend.integrations.sports.get_lions_last_game", new_callable=AsyncMock, return_value=None),
        patch("backend.safety.governor.today_spend_usd", return_value=0.0),
        patch("backend.config.get_settings", return_value=_settings()),
    ]


def _enter_all(patches):
    return [p.start() for p in patches], patches


def _exit_all(patches):
    for p in patches:
        p.stop()


@pytest.mark.asyncio
async def test_one_broken_section_does_not_kill_the_digest():
    """The whole point of the per-section isolation: a bug in ONE integration
    (mirrors homelab_watch.py's _run_check guarantee) must not cost every
    other section, or the send, or the Brain note."""
    patches = _patch_all_healthy()
    started, _ = _enter_all(patches)
    try:
        with patch("backend.integrations.proxmox.fetch", new_callable=AsyncMock, side_effect=RuntimeError("down")):
            text = await homelab_digest.build_digest_text()
        assert "error: unavailable" in text
        assert "Unifi" in text
        assert "AdGuard" in text
        assert "API spend today: $0.0000" in text
    finally:
        _exit_all(patches)


@pytest.mark.asyncio
async def test_disabled_setting_skips_without_sending():
    with patch("backend.config.get_settings", return_value=_settings(homelab_digest_enabled=False)), \
         patch("backend.events.notify_phone", new_callable=AsyncMock) as mock_notify:
        result = await homelab_digest.run_homelab_digest()
    assert result == {"skipped": True}
    mock_notify.assert_not_called()


@pytest.mark.asyncio
async def test_run_sends_with_kind_and_emits_brain_event():
    patches = _patch_all_healthy()
    started, _ = _enter_all(patches)
    try:
        with patch("backend.events.notify_phone", new_callable=AsyncMock, return_value=True) as mock_notify, \
             patch("backend.integrations.obsidian.emit_event", new_callable=AsyncMock) as mock_emit:
            result = await homelab_digest.run_homelab_digest()
        assert result == {"delivered": True}
        assert mock_notify.call_args.kwargs.get("kind") == "homelab_digest"
        assert mock_emit.call_args.args[0] == "nexus.daily-digest"
    finally:
        _exit_all(patches)


def test_count_or_plus_marks_truncation_at_cap():
    assert homelab_digest._count_or_plus(3, 10) == "3"
    assert homelab_digest._count_or_plus(10, 10) == "10+"
    assert homelab_digest._count_or_plus(0, 10) == "0"


@pytest.mark.asyncio
async def test_dynamic_values_are_html_escaped():
    """notify_phone sends parse_mode=HTML whenever app_base_url is configured
    -- an unescaped '<' or '&' in ANY interpolated value 400s the whole send
    (terminal, never queued). VM name/status is the field most likely to
    carry operator-chosen characters."""
    patches = _patch_all_healthy()
    started, _ = _enter_all(patches)
    try:
        with patch(
            "backend.integrations.proxmox.fetch",
            new_callable=AsyncMock,
            return_value=_proxmox_data(vms=[
                {"vmid": 999, "name": "<script>&fake</script>", "status": "running", "type": "lxc"}
            ]),
        ):
            text = await homelab_digest.build_digest_text()
        assert "<script>" not in text
        assert "&lt;script&gt;" in text
    finally:
        _exit_all(patches)
