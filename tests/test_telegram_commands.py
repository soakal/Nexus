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
    assert all("description" in m for m in menu)


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
