from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.agents import telegram_commands


def _msg(text, chat_id=12345):
    return {"chat": {"id": chat_id}, "text": text, "date": 1234567890}


# ---------------------------------------------------------------------------
# dispatch() — parsing
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dispatch_bare_text_routes_to_chat_handler():
    with patch.dict(telegram_commands.COMMANDS, {}, clear=False), \
         patch("backend.agents.telegram_commands._cmd_chat", new_callable=AsyncMock, return_value="hi back") as mock_chat, \
         patch("backend.integrations.telegram.send_reply", new_callable=AsyncMock, return_value=True) as mock_reply:
        await telegram_commands.dispatch("hello there", _msg("hello there"))

    mock_chat.assert_awaited_once_with("hello there", _msg("hello there"))
    mock_reply.assert_awaited_once_with("hi back", chat_id=12345)


@pytest.mark.asyncio
async def test_dispatch_known_command_strips_slash_and_args():
    fake_handler = AsyncMock(return_value="ok")
    with patch.dict(telegram_commands.COMMANDS, {"status": (fake_handler, "desc")}), \
         patch("backend.integrations.telegram.send_reply", new_callable=AsyncMock, return_value=True):
        await telegram_commands.dispatch("/status", _msg("/status"))

    fake_handler.assert_awaited_once_with("", _msg("/status"))


@pytest.mark.asyncio
async def test_dispatch_command_with_args():
    fake_handler = AsyncMock(return_value="ok")
    with patch.dict(telegram_commands.COMMANDS, {"nx": (fake_handler, "desc")}), \
         patch("backend.integrations.telegram.send_reply", new_callable=AsyncMock, return_value=True):
        await telegram_commands.dispatch("/nx is the garage open", _msg("/nx is the garage open"))

    fake_handler.assert_awaited_once_with("is the garage open", _msg("/nx is the garage open"))


@pytest.mark.asyncio
async def test_dispatch_command_with_botname_suffix():
    fake_handler = AsyncMock(return_value="ok")
    with patch.dict(telegram_commands.COMMANDS, {"status": (fake_handler, "desc")}), \
         patch("backend.integrations.telegram.send_reply", new_callable=AsyncMock, return_value=True):
        await telegram_commands.dispatch("/status@cwiaibot", _msg("/status@cwiaibot"))

    fake_handler.assert_awaited_once()


@pytest.mark.asyncio
async def test_dispatch_unknown_command_replies_hint_no_exception():
    with patch("backend.integrations.telegram.send_reply", new_callable=AsyncMock, return_value=True) as mock_reply:
        await telegram_commands.dispatch("/frobnicate", _msg("/frobnicate"))

    mock_reply.assert_awaited_once()
    assert "Unknown command" in mock_reply.await_args.args[0]
    assert "/help" in mock_reply.await_args.args[0]


@pytest.mark.asyncio
async def test_dispatch_handler_exception_sends_error_reply_never_raises():
    fake_handler = AsyncMock(side_effect=RuntimeError("boom"))
    with patch.dict(telegram_commands.COMMANDS, {"status": (fake_handler, "desc")}), \
         patch("backend.integrations.telegram.send_reply", new_callable=AsyncMock, return_value=True) as mock_reply:
        await telegram_commands.dispatch("/status", _msg("/status"))  # must not raise

    assert "Something went wrong" in mock_reply.await_args.args[0]


def test_command_menu_matches_commands_dict():
    menu = telegram_commands.command_menu()
    names = {m["command"] for m in menu}
    assert names == set(telegram_commands.COMMANDS.keys())


# ---------------------------------------------------------------------------
# /image
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_image_command_sends_photo_and_returns_none():
    with patch("backend.integrations.image_gen.generate_image", new_callable=AsyncMock, return_value=b"img") as mock_gen, \
         patch("backend.integrations.telegram.send_photo", new_callable=AsyncMock, return_value=True) as mock_photo, \
         patch("backend.integrations.telegram.send_chat_action", new_callable=AsyncMock, return_value=True), \
         patch("backend.integrations.telegram.send_reply", new_callable=AsyncMock, return_value=True) as mock_reply:
        await telegram_commands.dispatch("/image a cat", _msg("/image a cat"))

    mock_gen.assert_awaited_once_with("a cat")
    mock_photo.assert_awaited_once()
    assert mock_photo.await_args.kwargs["caption"] == "a cat"
    mock_reply.assert_not_awaited()


@pytest.mark.asyncio
async def test_image_command_empty_prompt_replies_usage():
    with patch("backend.integrations.telegram.send_photo", new_callable=AsyncMock) as mock_photo, \
         patch("backend.integrations.telegram.send_reply", new_callable=AsyncMock, return_value=True) as mock_reply:
        await telegram_commands.dispatch("/image", _msg("/image"))

    mock_photo.assert_not_awaited()
    assert mock_reply.await_args.args[0].startswith("Usage: /image")


@pytest.mark.asyncio
async def test_image_command_generation_failure_replies_text():
    with patch("backend.integrations.image_gen.generate_image", new_callable=AsyncMock, return_value=None), \
         patch("backend.integrations.telegram.send_chat_action", new_callable=AsyncMock, return_value=True), \
         patch("backend.integrations.telegram.send_photo", new_callable=AsyncMock) as mock_photo, \
         patch("backend.integrations.telegram.send_reply", new_callable=AsyncMock, return_value=True) as mock_reply:
        await telegram_commands.dispatch("/image a cat", _msg("/image a cat"))

    mock_photo.assert_not_awaited()
    assert "unavailable" in mock_reply.await_args.args[0]


def test_image_registered_in_command_menu():
    assert "image" in telegram_commands.COMMANDS
    menu_names = {m["command"] for m in telegram_commands.command_menu()}
    assert "image" in menu_names


# ---------------------------------------------------------------------------
# _cmd_chat — conversation continuity
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cmd_chat_persists_conversation_id():
    with patch("backend.safety.governor.get_telegram_conversation_id", return_value=42), \
         patch("backend.safety.governor.set_telegram_conversation_id") as mock_set, \
         patch("backend.agents.chat.chat", new_callable=AsyncMock, return_value={"conversation_id": 43, "reply": "hi"}) as mock_chat, \
         patch("backend.integrations.telegram.send_chat_action", new_callable=AsyncMock, return_value=True):
        reply = await telegram_commands._cmd_chat("hello", _msg("hello"))

    assert reply == "hi"
    mock_chat.assert_awaited_once_with(42, "hello")
    mock_set.assert_called_once_with(43)


@pytest.mark.asyncio
async def test_cmd_chat_empty_args_does_not_call_chat():
    with patch("backend.agents.chat.chat", new_callable=AsyncMock) as mock_chat:
        reply = await telegram_commands._cmd_chat("   ", _msg("   "))

    mock_chat.assert_not_called()
    assert "Ask me something" in reply


@pytest.mark.asyncio
async def test_cmd_clear_nulls_conversation_id():
    with patch("backend.safety.governor.set_telegram_conversation_id") as mock_set:
        reply = await telegram_commands._cmd_clear("", _msg("/clear"))

    mock_set.assert_called_once_with(None)
    assert "cleared" in reply.lower()


# ---------------------------------------------------------------------------
# _cmd_status — native, degrades independently per integration
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cmd_status_degrades_independently_on_partial_failure():
    px = MagicMock(node_status="online", cpu_pct=12.0, mem_used_gb=10.0, mem_total_gb=64.0, vms=[{"vmid": 101, "name": "vm1", "status": "running"}])
    uf = MagicMock(client_count=5)
    ag = MagicMock(filtering_enabled=True, blocked_pct=20.0)
    ha = MagicMock(alerts=[])
    cal = MagicMock(events=[1, 2])

    with patch("backend.integrations.proxmox.fetch", new_callable=AsyncMock, return_value=px), \
         patch("backend.integrations.unraid.fetch", new_callable=AsyncMock, side_effect=RuntimeError("unraid down")), \
         patch("backend.integrations.unifi.fetch", new_callable=AsyncMock, return_value=uf), \
         patch("backend.integrations.adguard.fetch", new_callable=AsyncMock, return_value=ag), \
         patch("backend.integrations.homeassistant.fetch", new_callable=AsyncMock, return_value=ha), \
         patch("backend.integrations.calendar.fetch", new_callable=AsyncMock, return_value=cal), \
         patch("backend.safety.governor.get_system_state", return_value={"daily_budget_usd": 25.0, "autonomy_enabled": True}), \
         patch("backend.safety.governor.today_spend_usd", return_value=1.5):
        reply = await telegram_commands._cmd_status("", _msg("/status"))

    assert "Proxmox: online" in reply
    assert "Unraid: unavailable" in reply
    assert "UniFi: 5 clients" in reply
    assert "Autonomy: enabled" in reply


# ---------------------------------------------------------------------------
# Simple pass-through handlers
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cmd_calendar_calls_get_today_events():
    with patch("backend.integrations.calendar.get_today_events", new_callable=AsyncMock, return_value="Today: standup"):
        reply = await telegram_commands._cmd_calendar("", _msg("/calendar"))
    assert reply == "Today: standup"


@pytest.mark.asyncio
async def test_cmd_mail_calls_inbox_summary():
    with patch("backend.integrations.protonmail.inbox_summary", new_callable=AsyncMock, return_value="3 unread"):
        reply = await telegram_commands._cmd_mail("", _msg("/mail"))
    assert reply == "3 unread"


@pytest.mark.asyncio
async def test_cmd_spend_formats_budget():
    with patch("backend.safety.governor.get_system_state", return_value={"daily_budget_usd": 25.0, "per_task_budget_usd": 5.0}), \
         patch("backend.safety.governor.today_spend_usd", return_value=12.5):
        reply = await telegram_commands._cmd_spend("", _msg("/spend"))
    assert "12.50" in reply
    assert "25.00" in reply


@pytest.mark.asyncio
async def test_cmd_vms_lists_vms():
    data = MagicMock(vms=[{"vmid": 101, "name": "jellyfin", "status": "running"}])
    with patch("backend.integrations.proxmox.fetch", new_callable=AsyncMock, return_value=data):
        reply = await telegram_commands._cmd_vms("", _msg("/vms"))
    assert "jellyfin: running" in reply


@pytest.mark.asyncio
async def test_cmd_vms_degrades_on_error():
    with patch("backend.integrations.proxmox.fetch", new_callable=AsyncMock, side_effect=RuntimeError("no token")):
        reply = await telegram_commands._cmd_vms("", _msg("/vms"))
    assert "unavailable" in reply.lower()


@pytest.mark.asyncio
async def test_cmd_briefing_no_briefing_yet():
    session = MagicMock()
    session.exec.return_value.first.return_value = None
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=session)
    cm.__exit__ = MagicMock(return_value=False)
    with patch("backend.database.engine"), patch("sqlmodel.Session", return_value=cm):
        reply = await telegram_commands._cmd_briefing("", _msg("/briefing"))
    assert "No briefing yet" in reply


@pytest.mark.asyncio
async def test_cmd_briefing_returns_latest_content():
    row = MagicMock(content="Priority Actions...")
    session = MagicMock()
    session.exec.return_value.first.return_value = row
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=session)
    cm.__exit__ = MagicMock(return_value=False)
    with patch("backend.database.engine"), patch("sqlmodel.Session", return_value=cm):
        reply = await telegram_commands._cmd_briefing("", _msg("/briefing"))
    assert reply == "Priority Actions..."


@pytest.mark.asyncio
async def test_cmd_help_lists_all_commands():
    reply = await telegram_commands._cmd_help("", _msg("/help"))
    for name in telegram_commands.COMMANDS:
        assert f"/{name}" in reply


# ---------------------------------------------------------------------------
# Phase 2b — /remember, /facts, /forget
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cmd_remember_calls_extract_and_store():
    with patch("backend.agents.facts.extract_and_store", new_callable=AsyncMock) as mock_extract:
        reply = await telegram_commands._cmd_remember("my wifi password is hunter2", _msg("/remember ..."))

    mock_extract.assert_awaited_once_with("my wifi password is hunter2", conversation_id=None, source="telegram")
    assert "/facts" in reply


@pytest.mark.asyncio
async def test_cmd_remember_empty_args_does_not_call_extract():
    with patch("backend.agents.facts.extract_and_store", new_callable=AsyncMock) as mock_extract:
        reply = await telegram_commands._cmd_remember("   ", _msg("/remember"))

    mock_extract.assert_not_called()
    assert "Usage" in reply


@pytest.mark.asyncio
async def test_cmd_facts_lists_stored_facts():
    rows = [{"id": 1, "subject": "wifi", "predicate": "password", "value": "hunter2", "confidence": 0.9}]
    with patch("backend.agents.facts._db_list_facts_for_audit", return_value=rows):
        reply = await telegram_commands._cmd_facts("", _msg("/facts"))
    assert "#1" in reply
    assert "wifi" in reply


@pytest.mark.asyncio
async def test_cmd_facts_empty():
    with patch("backend.agents.facts._db_list_facts_for_audit", return_value=[]):
        reply = await telegram_commands._cmd_facts("", _msg("/facts"))
    assert "No facts" in reply


@pytest.mark.asyncio
async def test_cmd_forget_dismisses_fact():
    with patch("backend.agents.facts.dismiss_fact", new_callable=AsyncMock, return_value=True) as mock_dismiss:
        reply = await telegram_commands._cmd_forget("7", _msg("/forget 7"))
    mock_dismiss.assert_awaited_once_with(7)
    assert "Forgot" in reply


@pytest.mark.asyncio
async def test_cmd_forget_not_found():
    with patch("backend.agents.facts.dismiss_fact", new_callable=AsyncMock, return_value=False):
        reply = await telegram_commands._cmd_forget("999", _msg("/forget 999"))
    assert "No fact" in reply


@pytest.mark.asyncio
async def test_cmd_forget_invalid_id():
    reply = await telegram_commands._cmd_forget("abc", _msg("/forget abc"))
    assert "Usage" in reply


# ---------------------------------------------------------------------------
# Phase 2b — /goals, /task, /tasks, /digest
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cmd_goals_lists_goals():
    rows = [{"id": 3, "status": "proposed", "title": "Archive old recordings"}]
    with patch("backend.agents.goals._db_list_goals", return_value=rows):
        reply = await telegram_commands._cmd_goals("", _msg("/goals"))
    assert "#3" in reply
    assert "Archive old recordings" in reply


@pytest.mark.asyncio
async def test_cmd_task_creates_and_enqueues():
    row = MagicMock(id=99)
    session = MagicMock()
    session.refresh = MagicMock(side_effect=lambda t: None)
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=session)
    cm.__exit__ = MagicMock(return_value=False)

    pool = MagicMock()
    pool.enqueue = AsyncMock()

    def _fake_session_ctor(*a, **kw):
        return cm

    with patch("backend.database.engine"), \
         patch("sqlmodel.Session", side_effect=_fake_session_ctor), \
         patch("backend.database.Task", return_value=row), \
         patch("backend.agents.worker_pool.get_pool", return_value=pool):
        reply = await telegram_commands._cmd_task("clean up docker images", _msg("/task ..."))

    pool.enqueue.assert_awaited_once_with(99)
    assert "#99" in reply
    assert "queued" in reply.lower()
    assert "message you here when it finishes" in reply.lower()


@pytest.mark.asyncio
async def test_cmd_task_empty_args():
    reply = await telegram_commands._cmd_task("   ", _msg("/task"))
    assert "Usage" in reply


@pytest.mark.asyncio
async def test_cmd_tasks_lists_recent():
    session = MagicMock()
    session.exec.return_value.all.return_value = [MagicMock(id=1, status="running", prompt="do the thing")]
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=session)
    cm.__exit__ = MagicMock(return_value=False)
    with patch("backend.database.engine"), patch("sqlmodel.Session", return_value=cm):
        reply = await telegram_commands._cmd_tasks("", _msg("/tasks"))
    assert "#1" in reply
    assert "do the thing" in reply


@pytest.mark.asyncio
async def test_cmd_digest_calls_build_autonomy_digest():
    with patch("backend.agents.digest.build_autonomy_digest", new_callable=AsyncMock, return_value="Digest text"):
        reply = await telegram_commands._cmd_digest("", _msg("/digest"))
    assert reply == "Digest text"


# ---------------------------------------------------------------------------
# Phase 2b — /mute, /unmute, /muted
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cmd_mute_adds_kind():
    with patch("backend.safety.governor.add_muted_notify_kind") as mock_add:
        reply = await telegram_commands._cmd_mute("budget_warn", _msg("/mute budget_warn"))
    mock_add.assert_called_once_with("budget_warn")
    assert "budget_warn" in reply


@pytest.mark.asyncio
async def test_cmd_mute_empty_args():
    reply = await telegram_commands._cmd_mute("", _msg("/mute"))
    assert "Usage" in reply


@pytest.mark.asyncio
async def test_cmd_mute_rejects_never_mutable_kind():
    """The real safety guard, not mocked away — governor.add_muted_notify_kind
    genuinely raises for a safety-critical kind (before ever touching the DB),
    and the reply must surface that rejection, not claim success."""
    reply = await telegram_commands._cmd_mute("auth_burst", _msg("/mute auth_burst"))
    assert "cannot be muted" in reply


@pytest.mark.asyncio
async def test_cmd_mute_rejects_comma():
    reply = await telegram_commands._cmd_mute("budget_warn,goal_proposed", _msg("/mute ..."))
    assert "comma" in reply.lower()


@pytest.mark.asyncio
async def test_cmd_unmute_removes_kind():
    with patch("backend.safety.governor.remove_muted_notify_kind") as mock_remove:
        reply = await telegram_commands._cmd_unmute("budget_warn", _msg("/unmute budget_warn"))
    mock_remove.assert_called_once_with("budget_warn")
    assert "budget_warn" in reply


@pytest.mark.asyncio
async def test_cmd_muted_lists_kinds():
    with patch("backend.safety.governor.get_muted_notify_kinds", return_value={"budget_warn", "goal_proposed"}):
        reply = await telegram_commands._cmd_muted("", _msg("/muted"))
    assert "budget_warn" in reply
    assert "goal_proposed" in reply


@pytest.mark.asyncio
async def test_cmd_muted_empty():
    with patch("backend.safety.governor.get_muted_notify_kinds", return_value=set()):
        reply = await telegram_commands._cmd_muted("", _msg("/muted"))
    assert "Nothing muted" in reply


@pytest.mark.asyncio
async def test_cmd_mute_rejects_unknown_kind():
    """Not mocked -- exercises the real validation path, same as
    test_cmd_mute_rejects_never_mutable_kind above."""
    reply = await telegram_commands._cmd_mute("homlab_garage", _msg("/mute homlab_garage"))
    assert "is not a notification kind" in reply
    assert "homelab_garage" in reply


@pytest.mark.asyncio
async def test_cmd_mute_no_args_lists_valid_kinds():
    reply = await telegram_commands._cmd_mute("", _msg("/mute"))
    assert "budget_warn" in reply
    assert "homelab_garage" in reply
    assert "never mutable" in reply


@pytest.mark.asyncio
async def test_cmd_unmute_reports_kind_was_not_muted():
    with patch("backend.safety.governor.remove_muted_notify_kind", return_value=False):
        reply = await telegram_commands._cmd_unmute("budget_warn", _msg("/unmute budget_warn"))
    assert "wasn't muted" in reply


# ---------------------------------------------------------------------------
# Outcome Tracker rollout step 3 — /flags, /resolve
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cmd_flags_no_open_flags():
    with patch("backend.agents.outcomes.open_flags", new_callable=AsyncMock, return_value=[]):
        reply = await telegram_commands._cmd_flags("", _msg("/flags"))
    assert reply == "No open flags."


@pytest.mark.asyncio
async def test_cmd_resolve_with_status_and_note():
    with patch("backend.agents.outcomes.resolve_flag", new_callable=AsyncMock, return_value="false_positive") as mock_resolve:
        reply = await telegram_commands._cmd_resolve("3 false_positive typo", _msg("/resolve 3 false_positive typo"))

    mock_resolve.assert_awaited_once_with(3, "false_positive", note="typo", by="telegram")
    assert "false_positive" in reply


@pytest.mark.asyncio
async def test_cmd_flags_lists_each_flag_with_pinned_format():
    """AC16 (docs/outcome-tracker-spec.md §6, line ~279): '/flags with flags,
    the reply contains each id.' Also pins the exact render shape from spec
    §3.2 (line 170) — '#{id} [{severity}] {source}:{check} — {summary}
    ({age})' — and the _format_age(None) fallback to '?', which a real DB
    row with a null created_at can hit and must not raise."""
    from datetime import datetime

    now_iso = datetime.utcnow().isoformat()
    rows = [
        {
            "id": 7,
            "severity": "high",
            "source": "homelab_watch",
            "check": "garage_open",
            "summary": "Garage door left open 2h",
            "created_at": now_iso,
        },
        {
            "id": 3,
            "severity": "medium",
            "source": "budget_watch",
            "check": "daily_spend",
            "summary": "Spend approaching limit",
            "created_at": None,
        },
    ]
    with patch("backend.agents.outcomes.open_flags", new_callable=AsyncMock, return_value=rows):
        reply = await telegram_commands._cmd_flags("", _msg("/flags"))

    lines = reply.split("\n")
    assert "#7" in reply
    assert "#3" in reply
    assert "Garage door left open 2h" in reply
    assert "Spend approaching limit" in reply
    # Full-line shape pinned for a normal (non-null created_at) flag.
    assert "#7 [high] homelab_watch:garage_open — Garage door left open 2h (just now)" in lines
    # Full-line shape pinned for the created_at=None fallback branch.
    assert "#3 [medium] budget_watch:daily_spend — Spend approaching limit (?)" in lines


# ---------------------------------------------------------------------------
# /flag (rollout step 6, spec §6 — missed-detection capture)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cmd_flag_plain_text_unaffected_by_missed_prefix_handling():
    """A plain /flag <text> (no 'missed ' prefix) must remain byte-identical
    to pre-existing behavior: severity stays 'medium' and check is the same
    slugified-first-six-words value as before."""
    with patch("backend.agents.outcomes.record_flag", new_callable=AsyncMock, return_value=5) as mock_record:
        reply = await telegram_commands._cmd_flag("foo", _msg("/flag foo"))

    mock_record.assert_awaited_once_with("manual", "foo", "foo", severity="medium")
    assert reply == "Flag #5 recorded."


@pytest.mark.asyncio
async def test_cmd_flag_missed_prefix_case_insensitive_records_high_severity():
    """A case-insensitive 'missed ' prefix records check=f"missed:{slug}"
    with severity="high" instead of the default manual-note handling."""
    with patch("backend.agents.outcomes.record_flag", new_callable=AsyncMock, return_value=9) as mock_record:
        reply = await telegram_commands._cmd_flag(
            "missed the water heater leaked", _msg("/flag missed the water heater leaked"),
        )

    mock_record.assert_awaited_once_with(
        "manual", "missed:the_water_heater_leaked", "missed the water heater leaked", severity="high",
    )
    assert reply == "Flag #9 recorded."


@pytest.mark.asyncio
async def test_cmd_flag_missed_prefix_uppercase_still_matches():
    with patch("backend.agents.outcomes.record_flag", new_callable=AsyncMock, return_value=1) as mock_record:
        await telegram_commands._cmd_flag("Missed the leak", _msg("/flag Missed the leak"))

    mock_record.assert_awaited_once_with("manual", "missed:the_leak", "Missed the leak", severity="high")


# ---------------------------------------------------------------------------
# /calibration (docs/calibration-loop-spec.md §4/§8 — CAL36-CAL44)
# ---------------------------------------------------------------------------

def _empty_hint_report():
    return {
        "window_days": 30,
        "suppression_enabled": False,
        "fp_threshold": 0.60,
        "min_verdicts": 5,
        "suppressed": [],
        "watching": [],
        "overridden": [],
    }


@pytest.mark.asyncio
async def test_cmd_calibration_no_data_replies_no_data_yet():
    """CAL36: bare /calibration with a fully-empty hint_report() replies with
    a 'no calibration data yet' message, not an error or an empty string."""
    with patch("backend.agents.calibration.hint_report", new_callable=AsyncMock, return_value=_empty_hint_report()):
        reply = await telegram_commands._cmd_calibration("", _msg("/calibration"))

    assert "no calibration data yet" in reply.lower()
    assert reply.strip() != ""


@pytest.mark.asyncio
async def test_cmd_calibration_suppressed_never_truncated_and_flags_high_severity_watching():
    """CAL37 (no-black-box): with 2 active hints and 40 watching rules, BOTH
    suppressed fingerprints render in full — rate, since-date, re-test date,
    and silenced count — before any truncation, even though WATCHING is
    capped at 15 (only the first 15 watching fingerprints may appear).
    CAL38: a watching rule flagged never_auto_suppressed renders the
    explicit 'HIGH, never auto-suppressed' annotation."""
    report = _empty_hint_report()
    report["suppressed"] = [
        {
            "fingerprint": "homelab_watch:garage_open",
            "fp_rate": 0.78,
            "false_positive_count": 7,
            "verdict_count": 9,
            "since": "2026-08-14T00:00:00",
            "retest_at": "2026-09-13T00:00:00",
            "suppressed_surfacings": 41,
            "never_auto_suppressed": False,
        },
        {
            "fingerprint": "briefing:unifi_new_devices",
            "fp_rate": 1.0,
            "false_positive_count": 6,
            "verdict_count": 6,
            "since": "2026-08-20T00:00:00",
            "retest_at": "2026-09-19T00:00:00",
            "suppressed_surfacings": 3,
            "never_auto_suppressed": False,
        },
    ]
    watching = []
    for i in range(40):
        watching.append({
            "fingerprint": f"watchdog:rule_{i}",
            "fp_rate": 0.5,
            "false_positive_count": 2,
            "verdict_count": 4,
            "auto_cleared_count": 0,
            "never_auto_suppressed": i == 1,  # second entry is the HIGH one
        })
    report["watching"] = watching

    with patch("backend.agents.calibration.hint_report", new_callable=AsyncMock, return_value=report):
        reply = await telegram_commands._cmd_calibration("", _msg("/calibration"))

    # SUPPRESSED renders first and both fingerprints appear in full, with
    # rate/since/re-test/silenced-count, regardless of the 40-item watching
    # list below it.
    suppressed_idx = reply.index("SUPPRESSED")
    watching_idx = reply.index("WATCHING")
    assert suppressed_idx < watching_idx

    assert "homelab_watch:garage_open — 78% false alarm (7/9 judged)" in reply
    assert "since 2026-08-14, re-tests 2026-09-13 · 41 occurrences silenced" in reply
    assert "briefing:unifi_new_devices — 100% false alarm (6/6 judged)" in reply
    assert "since 2026-08-20, re-tests 2026-09-19 · 3 occurrences silenced" in reply

    # WATCHING is capped at 15 — the first 15 fingerprints appear, the 16th
    # does not, even though hint_report() supplied 40. Assert on the full
    # rendered line (not the bare "watchdog:rule_{i}" fingerprint fragment),
    # since e.g. "watchdog:rule_1" is also a substring of a rendered
    # "watchdog:rule_15" line — the full "... — 50% false alarm (2/4 judged)"
    # clause can't be satisfied by a longer fingerprint's line.
    for i in range(15):
        assert f"watchdog:rule_{i} — 50% false alarm (2/4 judged)" in reply
    assert "watchdog:rule_15" not in reply
    assert "watchdog:rule_39" not in reply

    # CAL38 — the never_auto_suppressed watching entry is annotated.
    assert "watchdog:rule_1 — 50% false alarm (2/4 judged) · HIGH, never auto-suppressed" in reply


@pytest.mark.asyncio
async def test_cmd_calibration_suppressed_flags_high_severity_never_auto_suppressed(monkeypatch):
    """CAL38 (SUPPRESSED group): calibration.py:191-208 deliberately ignores
    severity when deciding activation (spec §2.4) — a HIGH-severity rule that
    crosses the FP threshold really does become status="active" exactly like
    a medium/low one, so hint_report() (calibration.py:444-457) classifies it
    into "suppressed" with never_auto_suppressed=True and — before this
    step — no explanation on the rendered line. Drives the REAL
    recompute_hints() -> hint_report() -> _cmd_calibration() path (nothing
    inside calibration itself is mocked) with 4/5 false-positive verdicts on
    a severity="high" watchdog:dead_letters fingerprint — enough to cross
    the default 60% fp_threshold / 5-verdict min (backend/config.py) — and
    asserts the SUPPRESSED line carries the same "HIGH, never
    auto-suppressed" annotation the WATCHING loop already renders (CAL38's
    existing mocked coverage above)."""
    from sqlmodel import Session, SQLModel, create_engine
    from sqlmodel.pool import StaticPool

    import backend.database as db
    from backend.database import OutcomeFlag
    from backend.agents import calibration

    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(eng)
    monkeypatch.setattr(db, "engine", eng)

    fp = "watchdog:dead_letters"
    for status in ("false_positive", "false_positive", "false_positive", "false_positive", "resolved"):
        with Session(eng) as s:
            s.add(OutcomeFlag(
                source="watchdog", check="dead_letters", fingerprint=fp,
                summary="test flag", status=status, resolved_by="telegram",
                severity="high",
            ))
            s.commit()

    await calibration.recompute_hints()

    reply = await telegram_commands._cmd_calibration("", _msg("/calibration"))

    assert "SUPPRESSED (1)" in reply
    assert (
        "watchdog:dead_letters — 80% false alarm (4/5 judged) · HIGH, never auto-suppressed"
        in reply
    )


@pytest.mark.asyncio
async def test_cmd_calibration_suppress_applied_calls_set_override():
    """/calibration suppress <fp> calls set_override(fingerprint, active=True,
    by="telegram") and reports the applied result."""
    with patch("backend.agents.calibration.set_override", new_callable=AsyncMock, return_value="applied") as mock_override:
        reply = await telegram_commands._cmd_calibration(
            "suppress homelab_watch:garage_open", _msg("/calibration suppress homelab_watch:garage_open")
        )

    mock_override.assert_awaited_once_with("homelab_watch:garage_open", active=True, by="telegram")
    assert "homelab_watch:garage_open" in reply
    assert "suppressed" in reply.lower()


@pytest.mark.asyncio
async def test_cmd_calibration_unsuppress_applied_calls_set_override():
    """CAL39: /calibration unsuppress <fp> calls set_override(fingerprint,
    active=False, by="telegram") and reports the applied result."""
    with patch("backend.agents.calibration.set_override", new_callable=AsyncMock, return_value="applied") as mock_override:
        reply = await telegram_commands._cmd_calibration(
            "unsuppress homelab_watch:garage_open", _msg("/calibration unsuppress homelab_watch:garage_open")
        )

    mock_override.assert_awaited_once_with("homelab_watch:garage_open", active=False, by="telegram")
    assert "homelab_watch:garage_open" in reply
    assert "un-suppressed" in reply.lower()


@pytest.mark.asyncio
async def test_cmd_calibration_unsuppress_not_found():
    """CAL42: /calibration unsuppress <unknown> replies 'not found' and
    changes nothing beyond the set_override call itself."""
    with patch("backend.agents.calibration.set_override", new_callable=AsyncMock, return_value="not_found"):
        reply = await telegram_commands._cmd_calibration(
            "unsuppress homelab_watch:no_such_rule", _msg("/calibration unsuppress homelab_watch:no_such_rule")
        )

    assert reply == "No calibration hint found for homelab_watch:no_such_rule."


@pytest.mark.asyncio
async def test_cmd_calibration_suppress_invalid_fingerprint():
    """set_override's 'invalid' result (malformed fingerprint) is surfaced
    distinctly from 'not_found'."""
    with patch("backend.agents.calibration.set_override", new_callable=AsyncMock, return_value="invalid"):
        reply = await telegram_commands._cmd_calibration("suppress no_colon_here", _msg("/calibration suppress no_colon_here"))

    assert reply == "Invalid fingerprint: no_colon_here"


def test_calibration_registered_in_command_menu():
    """CAL44: 'calibration' is registered in COMMANDS and reachable via
    command_menu()/help. Unlike test_command_menu_matches_commands_dict
    (which only checks command_menu() names == COMMANDS.keys() — a
    tautology that would hold even if 'calibration' were removed from
    both), this pins the specific entry."""
    assert "calibration" in telegram_commands.COMMANDS
    menu_names = {m["command"] for m in telegram_commands.command_menu()}
    assert "calibration" in menu_names
