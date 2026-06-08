import pytest
from unittest.mock import AsyncMock, MagicMock, patch


@pytest.mark.asyncio
async def test_briefing_generates_content():
    mock_briefing_text = """## Priority Actions
Nothing urgent today.

## Weather
Clear, 72°F. High 78°F / Low 65°F.

## System Health
All systems nominal.

## Network Security
1000 queries, 23% blocked.

## GitHub Pulse
No open PRs.

## Media
Nothing recording. DVR 500/2000 GB.

## From Your Vault
No open tasks.

## Today's Focus
Focus on your priorities."""

    with patch("backend.integrations.homeassistant.fetch", new_callable=AsyncMock) as ha, \
         patch("backend.integrations.unifi.fetch", new_callable=AsyncMock) as unifi, \
         patch("backend.integrations.unraid.fetch", new_callable=AsyncMock) as unraid, \
         patch("backend.integrations.obsidian.fetch", new_callable=AsyncMock) as obs, \
         patch("backend.integrations.github.fetch", new_callable=AsyncMock) as gh, \
         patch("backend.integrations.weather.fetch", new_callable=AsyncMock) as wx, \
         patch("backend.integrations.channels_dvr.fetch", new_callable=AsyncMock) as channels, \
         patch("backend.integrations.adguard.fetch", new_callable=AsyncMock) as ag, \
         patch("backend.agents.router.opus", new_callable=AsyncMock) as mock_opus, \
         patch("backend.integrations.obsidian.create_note", new_callable=AsyncMock) as mock_create_note, \
         patch("backend.integrations.hermes.notify", new_callable=AsyncMock) as mock_hermes, \
         patch("backend.database.engine"), \
         patch("sqlmodel.Session"):

        from backend.integrations.homeassistant import HAData
        from backend.integrations.unifi import UniFiData
        from backend.integrations.unraid import UnraidData
        from backend.integrations.obsidian import ObsidianData
        from backend.integrations.github import GitHubData
        from backend.integrations.weather import WeatherData
        from backend.integrations.channels_dvr import ChannelsData
        from backend.integrations.adguard import AdGuardData

        ha.return_value = HAData()
        unifi.return_value = UniFiData()
        unraid.return_value = UnraidData()
        obs.return_value = ObsidianData()
        gh.return_value = GitHubData()
        wx.return_value = WeatherData(summary="Clear, 72°F", high_f=78.0, low_f=65.0)
        channels.return_value = ChannelsData()
        ag.return_value = AdGuardData()
        mock_opus.return_value = mock_briefing_text
        mock_create_note.return_value = "NEXUS/Briefings/2024-01-01.md"
        mock_hermes.return_value = True

        from backend.agents.briefing import run_briefing
        result = await run_briefing()
        assert "## Priority Actions" in result
        assert "## Weather" in result
        assert "## System Health" in result
        assert "## Network Security" in result
        assert "## GitHub Pulse" in result
        assert "## Media" in result
        assert "## From Your Vault" in result
        assert "## Today's Focus" in result


@pytest.mark.asyncio
async def test_briefing_obsidian_write_called():
    with patch("backend.integrations.homeassistant.fetch", new_callable=AsyncMock, return_value=MagicMock(entities=[], alerts=[])), \
         patch("backend.integrations.unifi.fetch", new_callable=AsyncMock, return_value=MagicMock(client_count=0, uplink_status="ok", new_devices=[])), \
         patch("backend.integrations.unraid.fetch", new_callable=AsyncMock, return_value=MagicMock(array_status="started", parity_status="idle", mover_running=False, storage_used_gb=0, storage_total_gb=0, docker_containers=[])), \
         patch("backend.integrations.obsidian.fetch", new_callable=AsyncMock, return_value=MagicMock(open_tasks=[])), \
         patch("backend.integrations.github.fetch", new_callable=AsyncMock, return_value=MagicMock(open_prs=[], assigned_issues=[], stale_prs=[])), \
         patch("backend.integrations.weather.fetch", new_callable=AsyncMock, return_value=MagicMock(summary="Clear", high_f=75.0, low_f=60.0)), \
         patch("backend.integrations.channels_dvr.fetch", new_callable=AsyncMock, return_value=MagicMock(recording_now=[], upcoming=[], storage_used_gb=0, storage_total_gb=0)), \
         patch("backend.integrations.adguard.fetch", new_callable=AsyncMock, return_value=MagicMock(queries_today=0, blocked_today=0, blocked_pct=0, filtering_enabled=True)), \
         patch("backend.agents.router.opus", new_callable=AsyncMock, return_value="## Priority Actions\nNone\n## Weather\nOK\n## System Health\nOK\n## Network Security\nOK\n## GitHub Pulse\nOK\n## Media\nOK\n## From Your Vault\nOK\n## Today's Focus\nFocus."), \
         patch("backend.integrations.obsidian.create_note", new_callable=AsyncMock) as mock_create_note, \
         patch("backend.integrations.hermes.notify", new_callable=AsyncMock, return_value=True), \
         patch("backend.database.engine"), \
         patch("sqlmodel.Session"):

        mock_create_note.return_value = "NEXUS/Briefings/test.md"
        from backend.agents.briefing import run_briefing
        await run_briefing()
        mock_create_note.assert_called_once()
        call_kwargs = mock_create_note.call_args
        assert "NEXUS/Briefings" in str(call_kwargs)
