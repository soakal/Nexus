import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.agents import telegram_poller


def _cq(data, *, chat_id=12345, message_id=42, text="Alert text", cq_id="cq1"):
    return {
        "id": cq_id,
        "data": data,
        "message": {"chat": {"id": chat_id}, "message_id": message_id, "text": text},
    }


# ---------------------------------------------------------------------------
# handle_callback — goal namespace
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_goal_approve_dispatches_and_edits_message():
    with patch("backend.agents.goals.approve", new_callable=AsyncMock, return_value={"status": "approved"}) as mock_approve, \
         patch("backend.integrations.telegram.answer_callback_query", new_callable=AsyncMock, return_value=True) as mock_answer, \
         patch("backend.integrations.telegram.edit_message_text", new_callable=AsyncMock, return_value=True) as mock_edit:
        await telegram_poller.handle_callback(_cq("goal:approve:7"))

    mock_approve.assert_awaited_once_with(7)
    mock_answer.assert_awaited_once()
    mock_edit.assert_awaited_once()
    edited_text = mock_edit.await_args.args[2]
    assert "✓" in edited_text
    assert "Approved." in edited_text


@pytest.mark.asyncio
async def test_goal_reject_passes_reason():
    with patch("backend.agents.goals.reject", new_callable=AsyncMock, return_value={"status": "abandoned"}) as mock_reject, \
         patch("backend.integrations.telegram.answer_callback_query", new_callable=AsyncMock, return_value=True), \
         patch("backend.integrations.telegram.edit_message_text", new_callable=AsyncMock, return_value=True):
        await telegram_poller.handle_callback(_cq("goal:reject:7"))

    mock_reject.assert_awaited_once_with(7, reason="rejected via Telegram")


# ---------------------------------------------------------------------------
# handle_callback — safety namespace
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_safety_confirm_dispatches_with_ttl():
    settings = MagicMock()
    settings.action_confirm_ttl_seconds = 86400
    settings.telegram_chat_id = "12345"
    with patch("backend.config.get_settings", return_value=settings), \
         patch("backend.safety.broker.confirm_action", new_callable=AsyncMock, return_value=("executed", MagicMock())) as mock_confirm, \
         patch("backend.integrations.telegram.answer_callback_query", new_callable=AsyncMock, return_value=True), \
         patch("backend.integrations.telegram.edit_message_text", new_callable=AsyncMock, return_value=True) as mock_edit:
        await telegram_poller.handle_callback(_cq("safety:confirm:7"))

    mock_confirm.assert_awaited_once_with(7, ttl_seconds=86400)
    assert "✓" in mock_edit.await_args.args[2]
    assert "Executed." in mock_edit.await_args.args[2]


@pytest.mark.asyncio
async def test_safety_reject_dispatches():
    with patch("backend.safety.broker.reject_action", new_callable=AsyncMock, return_value=("rejected", None)) as mock_reject, \
         patch("backend.integrations.telegram.answer_callback_query", new_callable=AsyncMock, return_value=True), \
         patch("backend.integrations.telegram.edit_message_text", new_callable=AsyncMock, return_value=True) as mock_edit:
        await telegram_poller.handle_callback(_cq("safety:reject:7"))

    mock_reject.assert_awaited_once_with(7)
    assert "✗" in mock_edit.await_args.args[2]


@pytest.mark.asyncio
@pytest.mark.parametrize("status,expected_text", [
    ("not_found", "Action not found."),
    ("not_confirmable", "not awaiting confirmation"),
    ("expired", "expired"),
    ("forbidden", "paused"),
    ("failed", "Failed."),
])
async def test_safety_confirm_definitive_statuses_edit_message(status, expected_text):
    with patch("backend.safety.broker.confirm_action", new_callable=AsyncMock, return_value=(status, None)), \
         patch("backend.integrations.telegram.answer_callback_query", new_callable=AsyncMock, return_value=True) as mock_answer, \
         patch("backend.integrations.telegram.edit_message_text", new_callable=AsyncMock, return_value=True) as mock_edit:
        await telegram_poller.handle_callback(_cq("safety:confirm:7"))

    mock_answer.assert_awaited_once_with("cq1")  # no alert — definitive result
    mock_edit.assert_awaited_once()
    assert expected_text in mock_edit.await_args.args[2]


# ---------------------------------------------------------------------------
# Internal/dispatch error — alert popup, buttons kept (no edit)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_internal_exception_alerts_and_keeps_buttons():
    with patch("backend.agents.goals.approve", new_callable=AsyncMock, side_effect=RuntimeError("db down")), \
         patch("backend.integrations.telegram.answer_callback_query", new_callable=AsyncMock, return_value=True) as mock_answer, \
         patch("backend.integrations.telegram.edit_message_text", new_callable=AsyncMock, return_value=True) as mock_edit:
        await telegram_poller.handle_callback(_cq("goal:approve:7"))

    mock_answer.assert_awaited_once()
    assert mock_answer.await_args.kwargs.get("show_alert") is True
    mock_edit.assert_not_called()


# ---------------------------------------------------------------------------
# Malformed callback_data
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("data", ["goal:approve", "goal:approve:abc", "onlyonepart"])
async def test_malformed_callback_data_answered_once_no_dispatch(data):
    with patch("backend.agents.goals.approve", new_callable=AsyncMock) as mock_approve, \
         patch("backend.integrations.telegram.answer_callback_query", new_callable=AsyncMock, return_value=True) as mock_answer, \
         patch("backend.integrations.telegram.edit_message_text", new_callable=AsyncMock, return_value=True) as mock_edit:
        await telegram_poller.handle_callback(_cq(data))

    mock_answer.assert_awaited_once()
    mock_approve.assert_not_called()
    mock_edit.assert_not_called()


# ---------------------------------------------------------------------------
# Authorization — wrong chat_id
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_wrong_chat_id_rejected():
    settings = MagicMock()
    settings.telegram_chat_id = "99999"
    with patch("backend.config.get_settings", return_value=settings), \
         patch("backend.agents.goals.approve", new_callable=AsyncMock) as mock_approve, \
         patch("backend.integrations.telegram.answer_callback_query", new_callable=AsyncMock, return_value=True) as mock_answer:
        await telegram_poller.handle_callback(_cq("goal:approve:7", chat_id=12345))

    mock_approve.assert_not_called()
    mock_answer.assert_awaited_once_with("cq1", "Not authorized", show_alert=True)


@pytest.mark.asyncio
async def test_unreadable_chat_id_fails_closed_not_open():
    """A missing/unreadable TELEGRAM_CHAT_ID (KeyError, or a transient secret
    store error) must reject every callback, not disable the check."""
    settings = MagicMock()
    type(settings).telegram_chat_id = property(lambda self: (_ for _ in ()).throw(KeyError("TELEGRAM_CHAT_ID")))
    with patch("backend.config.get_settings", return_value=settings), \
         patch("backend.agents.goals.approve", new_callable=AsyncMock) as mock_approve, \
         patch("backend.safety.broker.confirm_action", new_callable=AsyncMock) as mock_confirm, \
         patch("backend.integrations.telegram.answer_callback_query", new_callable=AsyncMock, return_value=True) as mock_answer:
        await telegram_poller.handle_callback(_cq("goal:approve:7", chat_id=12345))
        await telegram_poller.handle_callback(_cq("safety:confirm:7", chat_id=999999999))

    mock_approve.assert_not_called()
    mock_confirm.assert_not_called()
    assert mock_answer.await_count == 2
    for call in mock_answer.await_args_list:
        assert call.args[1:] == ("Not authorized",) or call.kwargs.get("show_alert") is True


# ---------------------------------------------------------------------------
# poll_once / run_poller — 409 conflict, arbitrary exception, offset advance
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_poll_once_advances_offset_and_dispatches():
    updates = [{"update_id": 100, "callback_query": _cq("goal:approve:1")}]
    with patch("backend.integrations.telegram.get_updates", new_callable=AsyncMock, return_value=updates), \
         patch("backend.agents.telegram_poller.handle_callback", new_callable=AsyncMock) as mock_handle:
        new_offset = await telegram_poller.poll_once(None)
    assert new_offset == 101
    mock_handle.assert_awaited_once()


@pytest.mark.asyncio
async def test_poll_once_logs_unknown_message():
    updates = [{"update_id": 5, "message": {"chat": {"id": 999}}}]
    with patch("backend.integrations.telegram.get_updates", new_callable=AsyncMock, return_value=updates):
        new_offset = await telegram_poller.poll_once(None)
    assert new_offset == 6


@pytest.mark.asyncio
async def test_run_poller_survives_conflict_and_stops_cleanly(caplog):
    from backend.integrations.telegram import TelegramConflict

    call_count = [0]

    async def _fake_poll_once(offset):
        call_count[0] += 1
        if call_count[0] == 1:
            raise TelegramConflict("another poller")
        stop.set()
        return offset

    stop = asyncio.Event()
    with patch("backend.agents.telegram_poller.poll_once", side_effect=_fake_poll_once), \
         patch("backend.agents.telegram_poller._sleep_or_stop", new_callable=AsyncMock) as mock_sleep, \
         caplog.at_level(logging.ERROR):
        await telegram_poller.run_poller(stop)

    assert any("conflict" in r.message.lower() for r in caplog.records if r.levelno == logging.ERROR)
    mock_sleep.assert_awaited_once()
    assert mock_sleep.await_args.args[1] == 30


@pytest.mark.asyncio
async def test_run_poller_survives_arbitrary_exception_with_backoff():
    call_count = [0]

    async def _fake_poll_once(offset):
        call_count[0] += 1
        if call_count[0] == 1:
            raise RuntimeError("transient")
        stop.set()
        return offset

    stop = asyncio.Event()
    with patch("backend.agents.telegram_poller.poll_once", side_effect=_fake_poll_once), \
         patch("backend.agents.telegram_poller._sleep_or_stop", new_callable=AsyncMock) as mock_sleep:
        await telegram_poller.run_poller(stop)

    mock_sleep.assert_awaited_once()
    assert mock_sleep.await_args.args[1] == 1.0  # first backoff


# ---------------------------------------------------------------------------
# start() — disabled / missing token -> None, no task
# ---------------------------------------------------------------------------

def test_start_returns_none_when_disabled():
    settings = MagicMock()
    settings.telegram_poll_enabled = False
    with patch("backend.config.get_settings", return_value=settings), \
         patch("asyncio.create_task") as mock_create_task:
        result = telegram_poller.start()
    assert result is None
    mock_create_task.assert_not_called()


def test_start_returns_none_when_token_missing():
    settings = MagicMock()
    settings.telegram_poll_enabled = True
    type(settings).telegram_bot_token = property(lambda self: (_ for _ in ()).throw(KeyError("TELEGRAM_BOT_TOKEN")))
    with patch("backend.config.get_settings", return_value=settings), \
         patch("asyncio.create_task") as mock_create_task:
        result = telegram_poller.start()
    assert result is None
    mock_create_task.assert_not_called()


@pytest.mark.asyncio
async def test_start_creates_task_when_configured():
    settings = MagicMock()
    settings.telegram_poll_enabled = True
    settings.telegram_bot_token = "real-token"
    with patch("backend.config.get_settings", return_value=settings), \
         patch("backend.agents.telegram_poller.run_poller", new_callable=AsyncMock):
        task = telegram_poller.start()
        assert task is not None
        await telegram_poller.stop()
