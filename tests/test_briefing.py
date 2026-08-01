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


# ---------------------------------------------------------------------------
# Rollout step 6 (read path) -- outcome-tracker prompt injection + deterministic
# '## Open Items' section. Spec docs/outcome-tracker-spec.md §4.1/§4.2, AC24-26.
# ---------------------------------------------------------------------------

_STEP6_BRIEFING_TEXT = (
    "## Priority Actions\nNone\n## Weather\nOK\n## System Health\nOK\n"
    "## Network Security\nOK\n## GitHub Pulse\nOK\n## Media\nOK\n"
    "## From Your Vault\nOK\n## Today's Focus\nFocus."
)


@pytest.mark.asyncio
async def test_ac24_known_open_items_prompt_block_with_flag():
    """AC24 (present case): with one open flag, the prompt passed to the
    mocked sonnet contains its summary under the KNOWN OPEN ITEMS header."""
    open_flag = {
        "id": 7, "source": "homelab_watch", "check": "garage_open",
        "severity": "high", "summary": "MARKER-GARAGE-OPEN-9f2",
        "created_at": "2026-08-01T00:00:00",
    }
    with patch("backend.integrations.homeassistant.fetch", new_callable=AsyncMock, return_value=MagicMock(entities=[], alerts=[])), \
         patch("backend.integrations.unifi.fetch", new_callable=AsyncMock, return_value=MagicMock(client_count=0, uplink_status="ok", new_devices=[])), \
         patch("backend.integrations.unraid.fetch", new_callable=AsyncMock, return_value=MagicMock(array_status="started", parity_status="idle", mover_running=False, storage_used_gb=0, storage_total_gb=0, docker_containers=[])), \
         patch("backend.integrations.obsidian.fetch", new_callable=AsyncMock, return_value=MagicMock(open_tasks=[])), \
         patch("backend.integrations.github.fetch", new_callable=AsyncMock, return_value=MagicMock(open_prs=[], assigned_issues=[], stale_prs=[])), \
         patch("backend.integrations.weather.fetch", new_callable=AsyncMock, return_value=MagicMock(summary="Clear", high_f=75.0, low_f=60.0)), \
         patch("backend.integrations.channels_dvr.fetch", new_callable=AsyncMock, return_value=MagicMock(recording_now=[], upcoming=[], storage_used_gb=0, storage_total_gb=0)), \
         patch("backend.integrations.adguard.fetch", new_callable=AsyncMock, return_value=MagicMock(queries_today=0, blocked_today=0, blocked_pct=0, filtering_enabled=True)), \
         patch("backend.integrations.calendar.get_today_events", new_callable=AsyncMock, return_value="No events in the next 7 days."), \
         patch("backend.agents.router.sonnet", new_callable=AsyncMock, return_value=_STEP6_BRIEFING_TEXT) as mock_sonnet, \
         patch("backend.integrations.obsidian.create_note", new_callable=AsyncMock), \
         patch("backend.integrations.telegram.notify", new_callable=AsyncMock, return_value=True), \
         patch("backend.integrations.protonmail.list_recent", new_callable=AsyncMock, return_value='{"emails": []}'), \
         patch("backend.agents.mail_drafts._db_drafted_email_ids", return_value=set()), \
         patch("backend.agents.outcomes.open_flags", new_callable=AsyncMock, return_value=[open_flag]), \
         patch("backend.agents.outcomes.recently_closed", new_callable=AsyncMock, return_value=[]), \
         patch("backend.database.engine"), \
         patch("sqlmodel.Session"):

        from backend.agents.briefing import run_briefing
        result = await run_briefing()

    prompt_arg = mock_sonnet.call_args[0][0]
    assert "KNOWN OPEN ITEMS" in prompt_arg
    assert "MARKER-GARAGE-OPEN-9f2" in prompt_arg
    assert "## Today's Focus" in result


@pytest.mark.asyncio
async def test_ac24_known_open_items_prompt_block_degrades_to_none():
    """AC24 (empty case): with no open flags, the KNOWN OPEN ITEMS block
    reads '(none)' and the briefing still succeeds."""
    with patch("backend.integrations.homeassistant.fetch", new_callable=AsyncMock, return_value=MagicMock(entities=[], alerts=[])), \
         patch("backend.integrations.unifi.fetch", new_callable=AsyncMock, return_value=MagicMock(client_count=0, uplink_status="ok", new_devices=[])), \
         patch("backend.integrations.unraid.fetch", new_callable=AsyncMock, return_value=MagicMock(array_status="started", parity_status="idle", mover_running=False, storage_used_gb=0, storage_total_gb=0, docker_containers=[])), \
         patch("backend.integrations.obsidian.fetch", new_callable=AsyncMock, return_value=MagicMock(open_tasks=[])), \
         patch("backend.integrations.github.fetch", new_callable=AsyncMock, return_value=MagicMock(open_prs=[], assigned_issues=[], stale_prs=[])), \
         patch("backend.integrations.weather.fetch", new_callable=AsyncMock, return_value=MagicMock(summary="Clear", high_f=75.0, low_f=60.0)), \
         patch("backend.integrations.channels_dvr.fetch", new_callable=AsyncMock, return_value=MagicMock(recording_now=[], upcoming=[], storage_used_gb=0, storage_total_gb=0)), \
         patch("backend.integrations.adguard.fetch", new_callable=AsyncMock, return_value=MagicMock(queries_today=0, blocked_today=0, blocked_pct=0, filtering_enabled=True)), \
         patch("backend.integrations.calendar.get_today_events", new_callable=AsyncMock, return_value="No events in the next 7 days."), \
         patch("backend.agents.router.sonnet", new_callable=AsyncMock, return_value=_STEP6_BRIEFING_TEXT) as mock_sonnet, \
         patch("backend.integrations.obsidian.create_note", new_callable=AsyncMock), \
         patch("backend.integrations.telegram.notify", new_callable=AsyncMock, return_value=True), \
         patch("backend.integrations.protonmail.list_recent", new_callable=AsyncMock, return_value='{"emails": []}'), \
         patch("backend.agents.mail_drafts._db_drafted_email_ids", return_value=set()), \
         patch("backend.agents.outcomes.open_flags", new_callable=AsyncMock, return_value=[]), \
         patch("backend.agents.outcomes.recently_closed", new_callable=AsyncMock, return_value=[]), \
         patch("backend.database.engine"), \
         patch("sqlmodel.Session"):

        from backend.agents.briefing import run_briefing
        result = await run_briefing()

    prompt_arg = mock_sonnet.call_args[0][0]
    assert "KNOWN OPEN ITEMS" in prompt_arg
    assert "(none)" in prompt_arg
    assert "## Today's Focus" in result


@pytest.mark.asyncio
async def test_ac25_open_items_section_present_and_stripped_before_extraction():
    """AC25: the returned briefing text contains a '## Open Items' section,
    and _strip_unverified_sections removes it -- so the text passed to
    extract_and_store contains none of the flag summaries."""
    open_flag = {
        "id": 9, "source": "briefing", "check": "github_stale_prs",
        "severity": "medium", "summary": "MARKER-STALE-PR-Zx81",
        "created_at": "2026-08-01T00:00:00",
    }
    with patch("backend.integrations.homeassistant.fetch", new_callable=AsyncMock, return_value=MagicMock(entities=[], alerts=[])), \
         patch("backend.integrations.unifi.fetch", new_callable=AsyncMock, return_value=MagicMock(client_count=0, uplink_status="ok", new_devices=[])), \
         patch("backend.integrations.unraid.fetch", new_callable=AsyncMock, return_value=MagicMock(array_status="started", parity_status="idle", mover_running=False, storage_used_gb=0, storage_total_gb=0, docker_containers=[])), \
         patch("backend.integrations.obsidian.fetch", new_callable=AsyncMock, return_value=MagicMock(open_tasks=[])), \
         patch("backend.integrations.github.fetch", new_callable=AsyncMock, return_value=MagicMock(open_prs=[], assigned_issues=[], stale_prs=[])), \
         patch("backend.integrations.weather.fetch", new_callable=AsyncMock, return_value=MagicMock(summary="Clear", high_f=75.0, low_f=60.0)), \
         patch("backend.integrations.channels_dvr.fetch", new_callable=AsyncMock, return_value=MagicMock(recording_now=[], upcoming=[], storage_used_gb=0, storage_total_gb=0)), \
         patch("backend.integrations.adguard.fetch", new_callable=AsyncMock, return_value=MagicMock(queries_today=0, blocked_today=0, blocked_pct=0, filtering_enabled=True)), \
         patch("backend.integrations.calendar.get_today_events", new_callable=AsyncMock, return_value="No events in the next 7 days."), \
         patch("backend.agents.router.sonnet", new_callable=AsyncMock, return_value=_STEP6_BRIEFING_TEXT), \
         patch("backend.agents.facts.extract_and_store", new_callable=AsyncMock) as mock_extract, \
         patch("backend.integrations.obsidian.create_note", new_callable=AsyncMock), \
         patch("backend.integrations.telegram.notify", new_callable=AsyncMock, return_value=True), \
         patch("backend.integrations.protonmail.list_recent", new_callable=AsyncMock, return_value='{"emails": []}'), \
         patch("backend.agents.mail_drafts._db_drafted_email_ids", return_value=set()), \
         patch("backend.agents.outcomes.open_flags", new_callable=AsyncMock, return_value=[open_flag]), \
         patch("backend.agents.outcomes.recently_closed", new_callable=AsyncMock, return_value=[]), \
         patch("backend.database.engine"), \
         patch("sqlmodel.Session"):

        from backend.agents.briefing import run_briefing
        result = await run_briefing()

    assert "## Open Items" in result
    assert "MARKER-STALE-PR-Zx81" in result

    mock_extract.assert_called_once()
    extracted_text = mock_extract.call_args[0][0]
    assert "## Open Items" not in extracted_text
    assert "MARKER-STALE-PR-Zx81" not in extracted_text


@pytest.mark.asyncio
async def test_ac26_open_flags_exception_still_completes_briefing():
    """AC26: an exception from open_flags (both the gather read and the
    post-LLM fresh read) degrades to '(none)'/'No open items.' rather than
    failing or blocking the briefing."""
    with patch("backend.integrations.homeassistant.fetch", new_callable=AsyncMock, return_value=MagicMock(entities=[], alerts=[])), \
         patch("backend.integrations.unifi.fetch", new_callable=AsyncMock, return_value=MagicMock(client_count=0, uplink_status="ok", new_devices=[])), \
         patch("backend.integrations.unraid.fetch", new_callable=AsyncMock, return_value=MagicMock(array_status="started", parity_status="idle", mover_running=False, storage_used_gb=0, storage_total_gb=0, docker_containers=[])), \
         patch("backend.integrations.obsidian.fetch", new_callable=AsyncMock, return_value=MagicMock(open_tasks=[])), \
         patch("backend.integrations.github.fetch", new_callable=AsyncMock, return_value=MagicMock(open_prs=[], assigned_issues=[], stale_prs=[])), \
         patch("backend.integrations.weather.fetch", new_callable=AsyncMock, return_value=MagicMock(summary="Clear", high_f=75.0, low_f=60.0)), \
         patch("backend.integrations.channels_dvr.fetch", new_callable=AsyncMock, return_value=MagicMock(recording_now=[], upcoming=[], storage_used_gb=0, storage_total_gb=0)), \
         patch("backend.integrations.adguard.fetch", new_callable=AsyncMock, return_value=MagicMock(queries_today=0, blocked_today=0, blocked_pct=0, filtering_enabled=True)), \
         patch("backend.integrations.calendar.get_today_events", new_callable=AsyncMock, return_value="No events in the next 7 days."), \
         patch("backend.agents.router.sonnet", new_callable=AsyncMock, return_value=_STEP6_BRIEFING_TEXT) as mock_sonnet, \
         patch("backend.integrations.obsidian.create_note", new_callable=AsyncMock), \
         patch("backend.integrations.telegram.notify", new_callable=AsyncMock, return_value=True), \
         patch("backend.integrations.protonmail.list_recent", new_callable=AsyncMock, return_value='{"emails": []}'), \
         patch("backend.agents.mail_drafts._db_drafted_email_ids", return_value=set()), \
         patch("backend.agents.outcomes.open_flags", new_callable=AsyncMock, side_effect=RuntimeError("db down")), \
         patch("backend.agents.outcomes.recently_closed", new_callable=AsyncMock, return_value=[]), \
         patch("backend.database.engine"), \
         patch("sqlmodel.Session"):

        from backend.agents.briefing import run_briefing
        result = await run_briefing()

    prompt_arg = mock_sonnet.call_args[0][0]
    assert "KNOWN OPEN ITEMS" in prompt_arg
    assert "(none)" in prompt_arg
    assert "## Today's Focus" in result
    assert "## Open Items" in result


@pytest.mark.asyncio
async def test_ac24_known_open_items_prompt_block_caps_at_ten():
    """Gap found by the Verifier: docs/outcome-tracker-spec.md #167;214 says
    the KNOWN OPEN ITEMS/RECENTLY CLOSED prompt blocks are 'capped at
    outcome_flag_briefing_max (default 10) items to bound prompt growth' --
    _build_flag_prompt_block does this with flags[:cap], but none of the
    Engineer's AC24-26 tests supply more than one flag, so the cap itself
    was never exercised. With 15 open flags, only the first 10 (by the
    open_flags() 'newest first' ordering the list already arrives in) may
    reach the prompt."""
    many_flags = [
        {
            "id": i, "source": "homelab_watch", "check": "garage_open",
            "severity": "high", "summary": f"MARKER-FLAG-{i:02d}",
            "created_at": "2026-08-01T00:00:00",
        }
        for i in range(15)
    ]
    with patch("backend.integrations.homeassistant.fetch", new_callable=AsyncMock, return_value=MagicMock(entities=[], alerts=[])), \
         patch("backend.integrations.unifi.fetch", new_callable=AsyncMock, return_value=MagicMock(client_count=0, uplink_status="ok", new_devices=[])), \
         patch("backend.integrations.unraid.fetch", new_callable=AsyncMock, return_value=MagicMock(array_status="started", parity_status="idle", mover_running=False, storage_used_gb=0, storage_total_gb=0, docker_containers=[])), \
         patch("backend.integrations.obsidian.fetch", new_callable=AsyncMock, return_value=MagicMock(open_tasks=[])), \
         patch("backend.integrations.github.fetch", new_callable=AsyncMock, return_value=MagicMock(open_prs=[], assigned_issues=[], stale_prs=[])), \
         patch("backend.integrations.weather.fetch", new_callable=AsyncMock, return_value=MagicMock(summary="Clear", high_f=75.0, low_f=60.0)), \
         patch("backend.integrations.channels_dvr.fetch", new_callable=AsyncMock, return_value=MagicMock(recording_now=[], upcoming=[], storage_used_gb=0, storage_total_gb=0)), \
         patch("backend.integrations.adguard.fetch", new_callable=AsyncMock, return_value=MagicMock(queries_today=0, blocked_today=0, blocked_pct=0, filtering_enabled=True)), \
         patch("backend.integrations.calendar.get_today_events", new_callable=AsyncMock, return_value="No events in the next 7 days."), \
         patch("backend.agents.router.sonnet", new_callable=AsyncMock, return_value=_STEP6_BRIEFING_TEXT) as mock_sonnet, \
         patch("backend.integrations.obsidian.create_note", new_callable=AsyncMock), \
         patch("backend.integrations.telegram.notify", new_callable=AsyncMock, return_value=True), \
         patch("backend.integrations.protonmail.list_recent", new_callable=AsyncMock, return_value='{"emails": []}'), \
         patch("backend.agents.mail_drafts._db_drafted_email_ids", return_value=set()), \
         patch("backend.agents.outcomes.open_flags", new_callable=AsyncMock, return_value=many_flags), \
         patch("backend.agents.outcomes.recently_closed", new_callable=AsyncMock, return_value=[]), \
         patch("backend.database.engine"), \
         patch("sqlmodel.Session"):

        from backend.agents.briefing import run_briefing
        await run_briefing()

    prompt_arg = mock_sonnet.call_args[0][0]
    # Only the KNOWN OPEN ITEMS block is capped by _build_flag_prompt_block;
    # isolate it from the rest of the prompt (which also contains the raw
    # JSON snapshot) before counting markers.
    known_block = prompt_arg.split("KNOWN OPEN ITEMS")[1].split("RECENTLY CLOSED")[0]
    present = [f"MARKER-FLAG-{i:02d}" for i in range(15) if f"MARKER-FLAG-{i:02d}" in known_block]
    assert present == [f"MARKER-FLAG-{i:02d}" for i in range(10)]


@pytest.mark.asyncio
async def test_open_items_section_uses_fresh_post_record_read_not_stale_gather_read():
    """Gap found by the Verifier: the '## Open Items' section is built from
    a SECOND, fresh outcomes.open_flags() call made after
    _record_briefing_flags() -- deliberately not the pre-LLM gather result
    -- 'so today's newly-recorded flags appear' (briefing.py comment). None
    of the Engineer's tests give the two open_flags() calls different
    return values, so this freshness guarantee was never actually pinned:
    a regression that silently reused the stale pre-LLM list would still
    pass every existing test. Here the first (gather) call returns no
    flags and the second (post-record) call returns one, so the KNOWN OPEN
    ITEMS prompt block must read '(none)' while the deterministic Open
    Items section must show the newly-recorded flag."""
    fresh_flag = {
        "id": 42, "source": "briefing", "check": "garage_open",
        "severity": "high", "summary": "MARKER-FRESH-ONLY",
        "created_at": "2026-08-01T00:00:00",
    }
    with patch("backend.integrations.homeassistant.fetch", new_callable=AsyncMock, return_value=MagicMock(entities=[], alerts=[])), \
         patch("backend.integrations.unifi.fetch", new_callable=AsyncMock, return_value=MagicMock(client_count=0, uplink_status="ok", new_devices=[])), \
         patch("backend.integrations.unraid.fetch", new_callable=AsyncMock, return_value=MagicMock(array_status="started", parity_status="idle", mover_running=False, storage_used_gb=0, storage_total_gb=0, docker_containers=[])), \
         patch("backend.integrations.obsidian.fetch", new_callable=AsyncMock, return_value=MagicMock(open_tasks=[])), \
         patch("backend.integrations.github.fetch", new_callable=AsyncMock, return_value=MagicMock(open_prs=[], assigned_issues=[], stale_prs=[])), \
         patch("backend.integrations.weather.fetch", new_callable=AsyncMock, return_value=MagicMock(summary="Clear", high_f=75.0, low_f=60.0)), \
         patch("backend.integrations.channels_dvr.fetch", new_callable=AsyncMock, return_value=MagicMock(recording_now=[], upcoming=[], storage_used_gb=0, storage_total_gb=0)), \
         patch("backend.integrations.adguard.fetch", new_callable=AsyncMock, return_value=MagicMock(queries_today=0, blocked_today=0, blocked_pct=0, filtering_enabled=True)), \
         patch("backend.integrations.calendar.get_today_events", new_callable=AsyncMock, return_value="No events in the next 7 days."), \
         patch("backend.agents.router.sonnet", new_callable=AsyncMock, return_value=_STEP6_BRIEFING_TEXT) as mock_sonnet, \
         patch("backend.integrations.obsidian.create_note", new_callable=AsyncMock), \
         patch("backend.integrations.telegram.notify", new_callable=AsyncMock, return_value=True), \
         patch("backend.integrations.protonmail.list_recent", new_callable=AsyncMock, return_value='{"emails": []}'), \
         patch("backend.agents.mail_drafts._db_drafted_email_ids", return_value=set()), \
         patch("backend.agents.outcomes.open_flags", new_callable=AsyncMock, side_effect=[[], [fresh_flag]]), \
         patch("backend.agents.outcomes.recently_closed", new_callable=AsyncMock, return_value=[]), \
         patch("backend.database.engine"), \
         patch("sqlmodel.Session"):

        from backend.agents.briefing import run_briefing
        result = await run_briefing()

    prompt_arg = mock_sonnet.call_args[0][0]
    known_block = prompt_arg.split("KNOWN OPEN ITEMS")[1].split("RECENTLY CLOSED")[0]
    assert "(none)" in known_block
    assert "MARKER-FRESH-ONLY" not in prompt_arg

    assert "## Open Items" in result
    assert "MARKER-FRESH-ONLY" in result


# ---------------------------------------------------------------------------
# calibration_line -- spec §4.4 advisory prompt-INPUT line
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_calibration_line_present_in_prompt_with_data():
    """Spec §4.4: outcomes.calibration_summary(30) is injected into the
    briefing prompt as one advisory line, formatted like
    build_autonomy_digest's own calibration line (digest.py:187-208)."""
    calibration = {"homelab_watch:garage_open": {"open": 6, "false_positive": 2}}
    with patch("backend.integrations.homeassistant.fetch", new_callable=AsyncMock, return_value=MagicMock(entities=[], alerts=[])), \
         patch("backend.integrations.unifi.fetch", new_callable=AsyncMock, return_value=MagicMock(client_count=0, uplink_status="ok", new_devices=[])), \
         patch("backend.integrations.unraid.fetch", new_callable=AsyncMock, return_value=MagicMock(array_status="started", parity_status="idle", mover_running=False, storage_used_gb=0, storage_total_gb=0, docker_containers=[])), \
         patch("backend.integrations.obsidian.fetch", new_callable=AsyncMock, return_value=MagicMock(open_tasks=[])), \
         patch("backend.integrations.github.fetch", new_callable=AsyncMock, return_value=MagicMock(open_prs=[], assigned_issues=[], stale_prs=[])), \
         patch("backend.integrations.weather.fetch", new_callable=AsyncMock, return_value=MagicMock(summary="Clear", high_f=75.0, low_f=60.0)), \
         patch("backend.integrations.channels_dvr.fetch", new_callable=AsyncMock, return_value=MagicMock(recording_now=[], upcoming=[], storage_used_gb=0, storage_total_gb=0)), \
         patch("backend.integrations.adguard.fetch", new_callable=AsyncMock, return_value=MagicMock(queries_today=0, blocked_today=0, blocked_pct=0, filtering_enabled=True)), \
         patch("backend.integrations.calendar.get_today_events", new_callable=AsyncMock, return_value="No events in the next 7 days."), \
         patch("backend.agents.router.sonnet", new_callable=AsyncMock, return_value=_STEP6_BRIEFING_TEXT) as mock_sonnet, \
         patch("backend.integrations.obsidian.create_note", new_callable=AsyncMock), \
         patch("backend.integrations.telegram.notify", new_callable=AsyncMock, return_value=True), \
         patch("backend.integrations.protonmail.list_recent", new_callable=AsyncMock, return_value='{"emails": []}'), \
         patch("backend.agents.mail_drafts._db_drafted_email_ids", return_value=set()), \
         patch("backend.agents.outcomes.open_flags", new_callable=AsyncMock, return_value=[]), \
         patch("backend.agents.outcomes.recently_closed", new_callable=AsyncMock, return_value=[]), \
         patch("backend.agents.outcomes.calibration_summary", new_callable=AsyncMock, return_value=calibration), \
         patch("backend.database.engine"), \
         patch("sqlmodel.Session"):

        from backend.agents.briefing import run_briefing
        result = await run_briefing()

    prompt_arg = mock_sonnet.call_args[0][0]
    assert "Flag calibration (30d): homelab_watch:garage_open — 8 raised, 2 false_positive." in prompt_arg
    # Prompt-input only -- must NOT become a new rendered output section.
    assert "## Flag calibration" not in result


@pytest.mark.asyncio
async def test_calibration_line_degrades_to_none_on_exception():
    """AC-equivalent to open_flags/recently_closed's own exception handling:
    a calibration_summary failure must never block or fail the briefing --
    it degrades to a harmless '(none)' advisory line."""
    with patch("backend.integrations.homeassistant.fetch", new_callable=AsyncMock, return_value=MagicMock(entities=[], alerts=[])), \
         patch("backend.integrations.unifi.fetch", new_callable=AsyncMock, return_value=MagicMock(client_count=0, uplink_status="ok", new_devices=[])), \
         patch("backend.integrations.unraid.fetch", new_callable=AsyncMock, return_value=MagicMock(array_status="started", parity_status="idle", mover_running=False, storage_used_gb=0, storage_total_gb=0, docker_containers=[])), \
         patch("backend.integrations.obsidian.fetch", new_callable=AsyncMock, return_value=MagicMock(open_tasks=[])), \
         patch("backend.integrations.github.fetch", new_callable=AsyncMock, return_value=MagicMock(open_prs=[], assigned_issues=[], stale_prs=[])), \
         patch("backend.integrations.weather.fetch", new_callable=AsyncMock, return_value=MagicMock(summary="Clear", high_f=75.0, low_f=60.0)), \
         patch("backend.integrations.channels_dvr.fetch", new_callable=AsyncMock, return_value=MagicMock(recording_now=[], upcoming=[], storage_used_gb=0, storage_total_gb=0)), \
         patch("backend.integrations.adguard.fetch", new_callable=AsyncMock, return_value=MagicMock(queries_today=0, blocked_today=0, blocked_pct=0, filtering_enabled=True)), \
         patch("backend.integrations.calendar.get_today_events", new_callable=AsyncMock, return_value="No events in the next 7 days."), \
         patch("backend.agents.router.sonnet", new_callable=AsyncMock, return_value=_STEP6_BRIEFING_TEXT) as mock_sonnet, \
         patch("backend.integrations.obsidian.create_note", new_callable=AsyncMock), \
         patch("backend.integrations.telegram.notify", new_callable=AsyncMock, return_value=True), \
         patch("backend.integrations.protonmail.list_recent", new_callable=AsyncMock, return_value='{"emails": []}'), \
         patch("backend.agents.mail_drafts._db_drafted_email_ids", return_value=set()), \
         patch("backend.agents.outcomes.open_flags", new_callable=AsyncMock, return_value=[]), \
         patch("backend.agents.outcomes.recently_closed", new_callable=AsyncMock, return_value=[]), \
         patch("backend.agents.outcomes.calibration_summary", new_callable=AsyncMock, side_effect=RuntimeError("db down")), \
         patch("backend.database.engine"), \
         patch("sqlmodel.Session"):

        from backend.agents.briefing import run_briefing
        result = await run_briefing()

    prompt_arg = mock_sonnet.call_args[0][0]
    assert "Flag calibration (30d): (none)" in prompt_arg
    assert "## Today's Focus" in result


@pytest.mark.asyncio
async def test_calibration_line_caps_at_ten_and_follows_recently_closed():
    """Gap found by the Verifier (same class as test_ac24_known_open_items_
    prompt_block_caps_at_ten above): _format_calibration_line slices
    calibration.items()[:cap], but neither of the Engineer's two tests
    supplies more than one source:check pair, so the cap was never actually
    exercised. Also pins the Arbiter's STEP requirement that {calibration_line}
    sits after the RECENTLY CLOSED block (not merely present somewhere in the
    prompt)."""
    many_calibration = {
        f"source{i}:check{i}": {"raised": 1, "false_positive": 0} for i in range(15)
    }
    with patch("backend.integrations.homeassistant.fetch", new_callable=AsyncMock, return_value=MagicMock(entities=[], alerts=[])), \
         patch("backend.integrations.unifi.fetch", new_callable=AsyncMock, return_value=MagicMock(client_count=0, uplink_status="ok", new_devices=[])), \
         patch("backend.integrations.unraid.fetch", new_callable=AsyncMock, return_value=MagicMock(array_status="started", parity_status="idle", mover_running=False, storage_used_gb=0, storage_total_gb=0, docker_containers=[])), \
         patch("backend.integrations.obsidian.fetch", new_callable=AsyncMock, return_value=MagicMock(open_tasks=[])), \
         patch("backend.integrations.github.fetch", new_callable=AsyncMock, return_value=MagicMock(open_prs=[], assigned_issues=[], stale_prs=[])), \
         patch("backend.integrations.weather.fetch", new_callable=AsyncMock, return_value=MagicMock(summary="Clear", high_f=75.0, low_f=60.0)), \
         patch("backend.integrations.channels_dvr.fetch", new_callable=AsyncMock, return_value=MagicMock(recording_now=[], upcoming=[], storage_used_gb=0, storage_total_gb=0)), \
         patch("backend.integrations.adguard.fetch", new_callable=AsyncMock, return_value=MagicMock(queries_today=0, blocked_today=0, blocked_pct=0, filtering_enabled=True)), \
         patch("backend.integrations.calendar.get_today_events", new_callable=AsyncMock, return_value="No events in the next 7 days."), \
         patch("backend.agents.router.sonnet", new_callable=AsyncMock, return_value=_STEP6_BRIEFING_TEXT) as mock_sonnet, \
         patch("backend.integrations.obsidian.create_note", new_callable=AsyncMock), \
         patch("backend.integrations.telegram.notify", new_callable=AsyncMock, return_value=True), \
         patch("backend.integrations.protonmail.list_recent", new_callable=AsyncMock, return_value='{"emails": []}'), \
         patch("backend.agents.mail_drafts._db_drafted_email_ids", return_value=set()), \
         patch("backend.agents.outcomes.open_flags", new_callable=AsyncMock, return_value=[]), \
         patch("backend.agents.outcomes.recently_closed", new_callable=AsyncMock, return_value=[]), \
         patch("backend.agents.outcomes.calibration_summary", new_callable=AsyncMock, return_value=many_calibration), \
         patch("backend.database.engine"), \
         patch("sqlmodel.Session"):

        from backend.agents.briefing import run_briefing
        await run_briefing()

    prompt_arg = mock_sonnet.call_args[0][0]

    # Cap: only the first 10 (insertion-order) source:check pairs may appear.
    for i in range(10):
        assert f"source{i}:check{i}" in prompt_arg
    for i in range(10, 15):
        assert f"source{i}:check{i}" not in prompt_arg

    # Position: calibration_line must follow the RECENTLY CLOSED block and
    # precede the "Produce a morning brief" section instruction, matching
    # the Arbiter's STEP placement ("after the RECENTLY CLOSED block").
    closed_idx = prompt_arg.index("RECENTLY CLOSED")
    calibration_idx = prompt_arg.index("Flag calibration (30d):")
    produce_idx = prompt_arg.index("Produce a morning brief")
    assert closed_idx < calibration_idx < produce_idx
