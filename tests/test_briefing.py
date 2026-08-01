import json

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from backend.agents.briefing import _strip_unverified_sections, _build_protonmail_section


def test_strip_unverified_sections_drops_priority_actions_and_inbox():
    text = (
        "## Priority Actions (max 3)\n"
        "1. Dropbox Storage Limit Hit — your Dropbox is full.\n\n"
        "## Weather\nClear, 72°F.\n\n"
        "## Inbox\n3,888 unread. Dropbox — Storage limit hit.\n\n"
        "## Today's Focus\nStay focused."
    )
    stripped = _strip_unverified_sections(text)
    assert "Dropbox" not in stripped
    assert "## Priority Actions" not in stripped
    assert "## Inbox" not in stripped
    assert "## Weather\nClear, 72°F." in stripped
    assert "## Today's Focus\nStay focused." in stripped


def test_strip_unverified_sections_drops_proton_mail():
    text = (
        "## Weather\nClear, 72°F.\n\n"
        '## Proton Mail\n1 unread email(s) previously judged (conservative classifier) to need a personal reply:\n- Jane Doe — "Can you help?"\n\n'
        "## Today's Focus\nStay focused."
    )
    stripped = _strip_unverified_sections(text)
    assert "Jane Doe" not in stripped
    assert "## Proton Mail" not in stripped
    assert "## Weather\nClear, 72°F." in stripped
    assert "## Today's Focus\nStay focused." in stripped


# ---------------------------------------------------------------------------
# _build_protonmail_section — pure, never raises
# ---------------------------------------------------------------------------

def _emails_json(emails):
    return json.dumps({"emails": emails})


def test_build_protonmail_section_intersection_and_drafts():
    unread = _emails_json([
        {"email_id": "1", "sender": "Jane Doe", "subject": "Can you help?"},
        {"email_id": "2", "sender": "Promo", "subject": "Sale!"},  # not in drafted_ids -> excluded
    ])
    drafts = _emails_json([{"subject": "Re: Can you help?", "recipients": ["jane@example.com"]}])
    section = _build_protonmail_section(unread, drafts, drafted_ids={"1"})
    assert "## Proton Mail" in section
    assert "Jane Doe" in section
    assert "Sale!" not in section
    assert "Re: Can you help?" in section
    assert "jane@example.com" in section


def test_build_protonmail_section_both_empty():
    section = _build_protonmail_section(_emails_json([]), _emails_json([]), drafted_ids=set())
    assert section == "## Proton Mail\nNothing needing attention."


def test_build_protonmail_section_both_unavailable():
    section = _build_protonmail_section(RuntimeError("down"), RuntimeError("down"), drafted_ids=set())
    assert section == "## Proton Mail\nProton Mail data unavailable."


def test_build_protonmail_section_malformed_json_never_raises():
    section = _build_protonmail_section("not json", "also not json", drafted_ids=set())
    assert section.startswith("## Proton Mail")


def test_build_protonmail_section_partial_availability():
    section = _build_protonmail_section(RuntimeError("down"), _emails_json([]), drafted_ids=set())
    assert "Unread-mail data unavailable." in section


def test_build_protonmail_section_non_dict_list_elements_never_raises():
    """Type-malformed MCP output (list elements that aren't objects) must not
    crash the whole briefing -- non-dict entries are simply dropped."""
    unread = json.dumps({"emails": ["not-a-dict", 42, None, {"email_id": "1", "sender": "Jane", "subject": "Hi"}]})
    section = _build_protonmail_section(unread, _emails_json([]), drafted_ids={"1"})
    assert "Jane" in section


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
         patch("backend.integrations.calendar.get_today_events", new_callable=AsyncMock, return_value="No events in the next 7 days."), \
         patch("backend.agents.router.sonnet", new_callable=AsyncMock) as mock_opus, \
         patch("backend.integrations.obsidian.create_note", new_callable=AsyncMock) as mock_create_note, \
         patch("backend.integrations.telegram.notify", new_callable=AsyncMock) as mock_hermes, \
         patch("backend.integrations.protonmail.list_recent", new_callable=AsyncMock, return_value='{"emails": []}'), \
         patch("backend.agents.mail_drafts._db_drafted_email_ids", return_value=set()), \
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
        assert "## Proton Mail" in result


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
         patch("backend.integrations.calendar.get_today_events", new_callable=AsyncMock, return_value="No events in the next 7 days."), \
         patch("backend.agents.router.sonnet", new_callable=AsyncMock, return_value="## Priority Actions\nNone\n## Weather\nOK\n## System Health\nOK\n## Network Security\nOK\n## GitHub Pulse\nOK\n## Media\nOK\n## From Your Vault\nOK\n## Today's Focus\nFocus."), \
         patch("backend.integrations.obsidian.create_note", new_callable=AsyncMock) as mock_create_note, \
         patch("backend.integrations.telegram.notify", new_callable=AsyncMock, return_value=True), \
         patch("backend.integrations.protonmail.list_recent", new_callable=AsyncMock, return_value='{"emails": []}'), \
         patch("backend.agents.mail_drafts._db_drafted_email_ids", return_value=set()), \
         patch("backend.database.engine"), \
         patch("sqlmodel.Session"):

        mock_create_note.return_value = "NEXUS/Briefings/test.md"
        from backend.agents.briefing import run_briefing
        await run_briefing()
        mock_create_note.assert_called_once()
        call_kwargs = mock_create_note.call_args
        assert "NEXUS/Briefings" in str(call_kwargs)


@pytest.mark.asyncio
async def test_briefing_fact_extraction_excludes_priority_actions_and_inbox():
    briefing_text = (
        "## Priority Actions (max 3)\n"
        "1. Dropbox Storage Limit Hit — your Dropbox is full.\n\n"
        "## Weather\nClear, 72°F.\n\n"
        "## Inbox\n3,888 unread. Dropbox — Storage limit hit.\n\n"
        "## Today's Focus\nStay focused."
    )
    with patch("backend.integrations.homeassistant.fetch", new_callable=AsyncMock, return_value=MagicMock(entities=[], alerts=[])), \
         patch("backend.integrations.unifi.fetch", new_callable=AsyncMock, return_value=MagicMock(client_count=0, uplink_status="ok", new_devices=[])), \
         patch("backend.integrations.unraid.fetch", new_callable=AsyncMock, return_value=MagicMock(array_status="started", parity_status="idle", mover_running=False, storage_used_gb=0, storage_total_gb=0, docker_containers=[])), \
         patch("backend.integrations.obsidian.fetch", new_callable=AsyncMock, return_value=MagicMock(open_tasks=[])), \
         patch("backend.integrations.github.fetch", new_callable=AsyncMock, return_value=MagicMock(open_prs=[], assigned_issues=[], stale_prs=[])), \
         patch("backend.integrations.weather.fetch", new_callable=AsyncMock, return_value=MagicMock(summary="Clear", high_f=75.0, low_f=60.0)), \
         patch("backend.integrations.channels_dvr.fetch", new_callable=AsyncMock, return_value=MagicMock(recording_now=[], upcoming=[], storage_used_gb=0, storage_total_gb=0)), \
         patch("backend.integrations.adguard.fetch", new_callable=AsyncMock, return_value=MagicMock(queries_today=0, blocked_today=0, blocked_pct=0, filtering_enabled=True)), \
         patch("backend.integrations.calendar.get_today_events", new_callable=AsyncMock, return_value="No events in the next 7 days."), \
         patch("backend.agents.router.sonnet", new_callable=AsyncMock, return_value=briefing_text), \
         patch("backend.agents.facts.extract_and_store", new_callable=AsyncMock) as mock_extract, \
         patch("backend.integrations.obsidian.create_note", new_callable=AsyncMock), \
         patch("backend.integrations.telegram.notify", new_callable=AsyncMock, return_value=True), \
         patch("backend.integrations.protonmail.list_recent", new_callable=AsyncMock, return_value='{"emails": []}'), \
         patch("backend.agents.mail_drafts._db_drafted_email_ids", return_value=set()), \
         patch("backend.database.engine"), \
         patch("sqlmodel.Session"):

        from backend.agents.briefing import run_briefing
        await run_briefing()
        mock_extract.assert_called_once()
        extracted_text = mock_extract.call_args[0][0]
        assert "Dropbox" not in extracted_text
        assert "## Weather\nClear, 72°F." in extracted_text
        assert "## Today's Focus\nStay focused." in extracted_text
        assert "## Proton Mail" not in extracted_text


@pytest.mark.asyncio
async def test_briefing_degrades_gracefully_when_protonmail_unavailable():
    """A Proton Mail/MCP outage must never block the other 9 data sources or
    the LLM call — the briefing still completes with an 'unavailable' Proton
    section."""
    with patch("backend.integrations.homeassistant.fetch", new_callable=AsyncMock, return_value=MagicMock(entities=[], alerts=[])), \
         patch("backend.integrations.unifi.fetch", new_callable=AsyncMock, return_value=MagicMock(client_count=0, uplink_status="ok", new_devices=[])), \
         patch("backend.integrations.unraid.fetch", new_callable=AsyncMock, return_value=MagicMock(array_status="started", parity_status="idle", mover_running=False, storage_used_gb=0, storage_total_gb=0, docker_containers=[])), \
         patch("backend.integrations.obsidian.fetch", new_callable=AsyncMock, return_value=MagicMock(open_tasks=[])), \
         patch("backend.integrations.github.fetch", new_callable=AsyncMock, return_value=MagicMock(open_prs=[], assigned_issues=[], stale_prs=[])), \
         patch("backend.integrations.weather.fetch", new_callable=AsyncMock, return_value=MagicMock(summary="Clear", high_f=75.0, low_f=60.0)), \
         patch("backend.integrations.channels_dvr.fetch", new_callable=AsyncMock, return_value=MagicMock(recording_now=[], upcoming=[], storage_used_gb=0, storage_total_gb=0)), \
         patch("backend.integrations.adguard.fetch", new_callable=AsyncMock, return_value=MagicMock(queries_today=0, blocked_today=0, blocked_pct=0, filtering_enabled=True)), \
         patch("backend.integrations.calendar.get_today_events", new_callable=AsyncMock, return_value="No events in the next 7 days."), \
         patch("backend.agents.router.sonnet", new_callable=AsyncMock,
               return_value="## Priority Actions\nNone\n## Weather\nOK\n## System Health\nOK\n## Network Security\nOK\n## GitHub Pulse\nOK\n## Media\nOK\n## From Your Vault\nOK\n## Today's Focus\nFocus."), \
         patch("backend.integrations.obsidian.create_note", new_callable=AsyncMock), \
         patch("backend.integrations.telegram.notify", new_callable=AsyncMock, return_value=True), \
         patch("backend.integrations.protonmail.list_recent", new_callable=AsyncMock, side_effect=RuntimeError("mcp down")), \
         patch("backend.agents.mail_drafts._db_drafted_email_ids", return_value=set()), \
         patch("backend.database.engine"), \
         patch("sqlmodel.Session"):

        from backend.agents.briefing import run_briefing
        result = await run_briefing()
        assert "## Today's Focus" in result
        assert "## Proton Mail" in result
        assert "unavailable" in result


@pytest.mark.asyncio
async def test_briefing_mail_data_never_reaches_llm_or_fact_extraction():
    """Load-bearing: mail data is a finished judgment appended AFTER the LLM
    call, never fed into the prompt (the known Priority-Actions-echoes-raw-
    inbox-content gotcha)."""
    marker_subject = "MARKER-SUBJECT-Xk92"
    unread = json.dumps({"emails": [{"email_id": "1", "sender": "Jane Doe", "subject": marker_subject}]})

    with patch("backend.integrations.homeassistant.fetch", new_callable=AsyncMock, return_value=MagicMock(entities=[], alerts=[])), \
         patch("backend.integrations.unifi.fetch", new_callable=AsyncMock, return_value=MagicMock(client_count=0, uplink_status="ok", new_devices=[])), \
         patch("backend.integrations.unraid.fetch", new_callable=AsyncMock, return_value=MagicMock(array_status="started", parity_status="idle", mover_running=False, storage_used_gb=0, storage_total_gb=0, docker_containers=[])), \
         patch("backend.integrations.obsidian.fetch", new_callable=AsyncMock, return_value=MagicMock(open_tasks=[])), \
         patch("backend.integrations.github.fetch", new_callable=AsyncMock, return_value=MagicMock(open_prs=[], assigned_issues=[], stale_prs=[])), \
         patch("backend.integrations.weather.fetch", new_callable=AsyncMock, return_value=MagicMock(summary="Clear", high_f=75.0, low_f=60.0)), \
         patch("backend.integrations.channels_dvr.fetch", new_callable=AsyncMock, return_value=MagicMock(recording_now=[], upcoming=[], storage_used_gb=0, storage_total_gb=0)), \
         patch("backend.integrations.adguard.fetch", new_callable=AsyncMock, return_value=MagicMock(queries_today=0, blocked_today=0, blocked_pct=0, filtering_enabled=True)), \
         patch("backend.integrations.calendar.get_today_events", new_callable=AsyncMock, return_value="No events in the next 7 days."), \
         patch("backend.agents.router.sonnet", new_callable=AsyncMock,
               return_value="## Priority Actions\nNone\n## Weather\nOK\n## System Health\nOK\n## Network Security\nOK\n## GitHub Pulse\nOK\n## Media\nOK\n## From Your Vault\nOK\n## Today's Focus\nFocus.") as mock_sonnet, \
         patch("backend.agents.facts.extract_and_store", new_callable=AsyncMock) as mock_extract, \
         patch("backend.integrations.obsidian.create_note", new_callable=AsyncMock), \
         patch("backend.integrations.telegram.notify", new_callable=AsyncMock, return_value=True), \
         patch("backend.integrations.protonmail.list_recent", new_callable=AsyncMock, return_value=unread), \
         patch("backend.agents.mail_drafts._db_drafted_email_ids", return_value={"1"}), \
         patch("backend.database.engine"), \
         patch("sqlmodel.Session"):

        from backend.agents.briefing import run_briefing
        result = await run_briefing()

        # The marker DOES appear in the final returned/stored text...
        assert marker_subject in result
        # ...but NEVER reached the LLM prompt...
        prompt_arg = mock_sonnet.call_args[0][0]
        assert marker_subject not in prompt_arg
        # ...and never reaches fact-extraction either (Proton Mail is stripped).
        extracted_text = mock_extract.call_args[0][0]
        assert marker_subject not in extracted_text


# ---------------------------------------------------------------------------
# _record_briefing_flags -- outcome-tracker write path (spec §2.2-C, AC22/AC23)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_record_briefing_flags_dispatches_checks_and_respects_unknown_boundary():
    """Pins _record_briefing_flags' six-check dispatch: a non-empty signal
    records the flag (AC22's record half), an 'unknown' Unraid array/parity
    status is treated as healthy and clears rather than flags (the boundary
    called out in the diff's own comments), and AdGuard's coerced 'unknown'
    (raw filtering_enabled=None) neither flags nor clears -- AC23, the
    2026-07-26 unknown-vs-off fix."""
    record_mock = AsyncMock(return_value=1)
    clear_mock = AsyncMock(return_value=0)

    with patch("backend.integrations.homeassistant.fetch", new_callable=AsyncMock, return_value=MagicMock(entities=[], alerts=[{"entity": "sensor.x", "state": "unavailable"}])), \
         patch("backend.integrations.unifi.fetch", new_callable=AsyncMock, return_value=MagicMock(client_count=0, uplink_status="ok", new_devices=[])), \
         patch("backend.integrations.unraid.fetch", new_callable=AsyncMock, return_value=MagicMock(array_status="unknown", parity_status="unknown", mover_running=False, storage_used_gb=0, storage_total_gb=0, docker_containers=[])), \
         patch("backend.integrations.obsidian.fetch", new_callable=AsyncMock, return_value=MagicMock(open_tasks=[])), \
         patch("backend.integrations.github.fetch", new_callable=AsyncMock, return_value=MagicMock(open_prs=[], assigned_issues=[], stale_prs=[{"number": 1, "title": "stale"}])), \
         patch("backend.integrations.weather.fetch", new_callable=AsyncMock, return_value=MagicMock(summary="Clear", high_f=75.0, low_f=60.0)), \
         patch("backend.integrations.channels_dvr.fetch", new_callable=AsyncMock, return_value=MagicMock(recording_now=[], upcoming=[], storage_used_gb=0, storage_total_gb=0)), \
         patch("backend.integrations.adguard.fetch", new_callable=AsyncMock, return_value=MagicMock(queries_today=0, blocked_today=0, blocked_pct=0, filtering_enabled=None)), \
         patch("backend.integrations.calendar.get_today_events", new_callable=AsyncMock, return_value="No events in the next 7 days."), \
         patch("backend.agents.router.sonnet", new_callable=AsyncMock,
               return_value="## Priority Actions\nNone\n## Weather\nOK\n## System Health\nOK\n## Network Security\nOK\n## GitHub Pulse\nOK\n## Media\nOK\n## From Your Vault\nOK\n## Today's Focus\nFocus."), \
         patch("backend.integrations.obsidian.create_note", new_callable=AsyncMock), \
         patch("backend.integrations.telegram.notify", new_callable=AsyncMock, return_value=True), \
         patch("backend.integrations.protonmail.list_recent", new_callable=AsyncMock, return_value='{"emails": []}'), \
         patch("backend.agents.mail_drafts._db_drafted_email_ids", return_value=set()), \
         patch("backend.agents.outcomes.record_flag", record_mock), \
         patch("backend.agents.outcomes.clear_flag", clear_mock), \
         patch("backend.database.engine"), \
         patch("sqlmodel.Session"):

        from backend.agents.briefing import run_briefing
        await run_briefing()

    recorded = {(c.args[0], c.args[1]) for c in record_mock.await_args_list}
    cleared = {(c.args[0], c.args[1]) for c in clear_mock.await_args_list}

    assert ("briefing", "ha_unavailable_entities") in recorded
    assert ("briefing", "github_stale_prs") in recorded
    # "unknown" Unraid status is not a breach -- clears, never flags.
    assert ("briefing", "unraid_array") in cleared
    assert ("briefing", "unraid_parity") in cleared
    assert ("briefing", "unraid_array") not in recorded
    assert ("briefing", "unraid_parity") not in recorded
    # no new UniFi devices -> clear.
    assert ("briefing", "unifi_new_devices") in cleared
    # AdGuard filtering_enabled=None coerces to "unknown" -- neither flags nor clears.
    assert ("briefing", "adguard_filtering_off") not in recorded
    assert ("briefing", "adguard_filtering_off") not in cleared


@pytest.mark.asyncio
async def test_record_briefing_flags_db_error_never_blocks_briefing():
    """The try/except wrapping the _record_briefing_flags() call in
    run_briefing() must swallow a DB error raised by record_flag rather than
    let it fail or delay the briefing (diff: the new call-site wrapping)."""
    record_mock = AsyncMock(side_effect=RuntimeError("db down"))

    with patch("backend.integrations.homeassistant.fetch", new_callable=AsyncMock, return_value=MagicMock(entities=[], alerts=[{"entity": "sensor.x"}])), \
         patch("backend.integrations.unifi.fetch", new_callable=AsyncMock, return_value=MagicMock(client_count=0, uplink_status="ok", new_devices=[])), \
         patch("backend.integrations.unraid.fetch", new_callable=AsyncMock, return_value=MagicMock(array_status="started", parity_status="idle", mover_running=False, storage_used_gb=0, storage_total_gb=0, docker_containers=[])), \
         patch("backend.integrations.obsidian.fetch", new_callable=AsyncMock, return_value=MagicMock(open_tasks=[])), \
         patch("backend.integrations.github.fetch", new_callable=AsyncMock, return_value=MagicMock(open_prs=[], assigned_issues=[], stale_prs=[])), \
         patch("backend.integrations.weather.fetch", new_callable=AsyncMock, return_value=MagicMock(summary="Clear", high_f=75.0, low_f=60.0)), \
         patch("backend.integrations.channels_dvr.fetch", new_callable=AsyncMock, return_value=MagicMock(recording_now=[], upcoming=[], storage_used_gb=0, storage_total_gb=0)), \
         patch("backend.integrations.adguard.fetch", new_callable=AsyncMock, return_value=MagicMock(queries_today=0, blocked_today=0, blocked_pct=0, filtering_enabled=True)), \
         patch("backend.integrations.calendar.get_today_events", new_callable=AsyncMock, return_value="No events in the next 7 days."), \
         patch("backend.agents.router.sonnet", new_callable=AsyncMock,
               return_value="## Priority Actions\nNone\n## Weather\nOK\n## System Health\nOK\n## Network Security\nOK\n## GitHub Pulse\nOK\n## Media\nOK\n## From Your Vault\nOK\n## Today's Focus\nFocus."), \
         patch("backend.integrations.obsidian.create_note", new_callable=AsyncMock), \
         patch("backend.integrations.telegram.notify", new_callable=AsyncMock, return_value=True), \
         patch("backend.integrations.protonmail.list_recent", new_callable=AsyncMock, return_value='{"emails": []}'), \
         patch("backend.agents.mail_drafts._db_drafted_email_ids", return_value=set()), \
         patch("backend.agents.outcomes.record_flag", record_mock), \
         patch("backend.database.engine"), \
         patch("sqlmodel.Session"):

        from backend.agents.briefing import run_briefing
        result = await run_briefing()

    # The ha_unavailable_entities check called record_flag and it raised --
    # proving the outer try/except in run_briefing() is what's swallowing it.
    assert record_mock.await_count >= 1
    assert "## Today's Focus" in result
