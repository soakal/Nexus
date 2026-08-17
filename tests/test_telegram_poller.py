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
# handle_callback — flag namespace (Outcome Tracker rollout step 3)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_flag_resolved_dispatches_and_edits_message():
    with patch("backend.agents.outcomes.resolve_flag", new_callable=AsyncMock, return_value="resolved") as mock_resolve, \
         patch("backend.integrations.telegram.answer_callback_query", new_callable=AsyncMock, return_value=True) as mock_answer, \
         patch("backend.integrations.telegram.edit_message_text", new_callable=AsyncMock, return_value=True) as mock_edit:
        await telegram_poller.handle_callback(_cq("flag:resolved:1"))

    mock_resolve.assert_awaited_once_with(1, "resolved", by="telegram")
    mock_answer.assert_awaited_once()
    mock_edit.assert_awaited_once()
    edited_text = mock_edit.await_args.args[2]
    assert "✓" in edited_text


@pytest.mark.asyncio
async def test_flag_wrong_chat_id_rejected():
    settings = MagicMock()
    settings.telegram_chat_id = "99999"
    with patch("backend.config.get_settings", return_value=settings), \
         patch("backend.agents.outcomes.resolve_flag", new_callable=AsyncMock) as mock_resolve, \
         patch("backend.integrations.telegram.answer_callback_query", new_callable=AsyncMock, return_value=True) as mock_answer:
        await telegram_poller.handle_callback(_cq("flag:resolved:1", chat_id=12345))

    mock_resolve.assert_not_called()
    mock_answer.assert_awaited_once_with("cq1", "Not authorized", show_alert=True)


@pytest.mark.asyncio
async def test_flag_non_integer_id_invalid_and_no_dispatch():
    with patch("backend.agents.outcomes.resolve_flag", new_callable=AsyncMock) as mock_resolve, \
         patch("backend.integrations.telegram.answer_callback_query", new_callable=AsyncMock, return_value=True) as mock_answer, \
         patch("backend.integrations.telegram.edit_message_text", new_callable=AsyncMock, return_value=True) as mock_edit:
        await telegram_poller.handle_callback(_cq("flag:resolved:abc"))

    mock_resolve.assert_not_called()
    mock_answer.assert_awaited_once_with("cq1", "Invalid id.", show_alert=True)
    mock_edit.assert_not_called()


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
async def test_poll_once_requests_both_callback_and_message_updates():
    """Pins the actual allowed_updates list — dropping 'message' would
    silently kill every text/voice command with zero other test failures,
    since every other poll_once test mocks get_updates' return value rather
    than asserting what was requested."""
    settings = MagicMock()
    settings.telegram_poll_timeout_s = 25
    settings.telegram_command_max_age_s = 300
    with patch("backend.config.get_settings", return_value=settings), \
         patch("backend.integrations.telegram.get_updates", new_callable=AsyncMock, return_value=[]) as mock_get_updates:
        await telegram_poller.poll_once(None)

    allowed = mock_get_updates.await_args.kwargs["allowed_updates"]
    assert "callback_query" in allowed
    assert "message" in allowed


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


# ---------------------------------------------------------------------------
# Phase 2a — text message handling
# ---------------------------------------------------------------------------

def _msg(text, chat_id=12345, date=None):
    import time
    return {"chat": {"id": chat_id}, "text": text, "date": date if date is not None else time.time()}


async def _run_and_await_created_tasks(coro):
    """Runs `coro`, then awaits any asyncio.create_task()'d work it spawned
    (telegram_poller._handle_message fires-and-forgets), so fire-and-forget
    dispatch is deterministic in tests instead of a bare asyncio.sleep(0)."""
    created = []
    orig_create_task = asyncio.create_task

    def _capture(c, *a, **kw):
        t = orig_create_task(c, *a, **kw)
        created.append(t)
        return t

    with patch("backend.agents.telegram_poller.asyncio.create_task", side_effect=_capture):
        await coro
    for t in created:
        await t


@pytest.mark.asyncio
async def test_authorized_message_dispatches_to_commands():
    settings = MagicMock()
    settings.telegram_chat_id = "12345"
    with patch("backend.config.get_settings", return_value=settings), \
         patch("backend.agents.telegram_commands.dispatch", new_callable=AsyncMock) as mock_dispatch:
        await _run_and_await_created_tasks(telegram_poller._handle_message(_msg("hello")))

    mock_dispatch.assert_awaited_once()
    assert mock_dispatch.await_args.args[0] == "hello"


@pytest.mark.asyncio
async def test_unauthorized_message_gets_no_reply():
    """The security-critical asymmetry: an unauthorized MESSAGE gets NOTHING
    sent back, only a log line — replying would confirm the bot is live to a
    stranger and (since bare text reaches chat()'s always-allowed HOME_CONTROL
    path) a reply-then-ignore gap here would be a real hole, not cosmetic."""
    settings = MagicMock()
    settings.telegram_chat_id = "99999"
    with patch("backend.config.get_settings", return_value=settings), \
         patch("backend.agents.telegram_commands.dispatch", new_callable=AsyncMock) as mock_dispatch, \
         patch("backend.integrations.telegram.send_reply", new_callable=AsyncMock) as mock_reply, \
         patch("backend.integrations.telegram.send_message", new_callable=AsyncMock) as mock_send:
        await _run_and_await_created_tasks(telegram_poller._handle_message(_msg("hello", chat_id=12345)))

    mock_dispatch.assert_not_called()
    mock_reply.assert_not_called()
    mock_send.assert_not_called()


@pytest.mark.asyncio
async def test_unreadable_chat_id_fails_closed_for_messages_too():
    settings = MagicMock()
    type(settings).telegram_chat_id = property(lambda self: (_ for _ in ()).throw(KeyError("TELEGRAM_CHAT_ID")))
    with patch("backend.config.get_settings", return_value=settings), \
         patch("backend.agents.telegram_commands.dispatch", new_callable=AsyncMock) as mock_dispatch, \
         patch("backend.integrations.telegram.send_reply", new_callable=AsyncMock) as mock_reply:
        await _run_and_await_created_tasks(telegram_poller._handle_message(_msg("hello")))

    mock_dispatch.assert_not_called()
    mock_reply.assert_not_called()


@pytest.mark.asyncio
async def test_non_text_message_from_authorized_chat_is_ignored():
    settings = MagicMock()
    settings.telegram_chat_id = "12345"
    msg = {"chat": {"id": 12345}, "date": 1234567890, "sticker": {"file_id": "abc"}}
    with patch("backend.config.get_settings", return_value=settings), \
         patch("backend.agents.telegram_commands.dispatch", new_callable=AsyncMock) as mock_dispatch:
        await _run_and_await_created_tasks(telegram_poller._handle_message(msg))

    mock_dispatch.assert_not_called()


# ---------------------------------------------------------------------------
# poll_once — replay-age guard
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_poll_once_drops_stale_message():
    import time
    settings = MagicMock()
    settings.telegram_poll_timeout_s = 25
    settings.telegram_command_max_age_s = 300
    stale_update = {"update_id": 10, "message": _msg("old", date=time.time() - 999)}
    with patch("backend.config.get_settings", return_value=settings), \
         patch("backend.integrations.telegram.get_updates", new_callable=AsyncMock, return_value=[stale_update]), \
         patch("backend.agents.telegram_poller._handle_message", new_callable=AsyncMock) as mock_handle:
        new_offset = await telegram_poller.poll_once(None)

    mock_handle.assert_not_called()
    assert new_offset == 11  # offset still advances so it isn't redelivered forever


@pytest.mark.asyncio
async def test_poll_once_processes_fresh_message():
    import time
    settings = MagicMock()
    settings.telegram_poll_timeout_s = 25
    settings.telegram_command_max_age_s = 300
    fresh_update = {"update_id": 11, "message": _msg("new", date=time.time())}
    with patch("backend.config.get_settings", return_value=settings), \
         patch("backend.integrations.telegram.get_updates", new_callable=AsyncMock, return_value=[fresh_update]), \
         patch("backend.agents.telegram_poller._handle_message", new_callable=AsyncMock) as mock_handle:
        new_offset = await telegram_poller.poll_once(None)

    mock_handle.assert_awaited_once()
    assert new_offset == 12


@pytest.mark.asyncio
async def test_poll_once_never_age_filters_callbacks():
    import time
    settings = MagicMock()
    settings.telegram_poll_timeout_s = 25
    settings.telegram_command_max_age_s = 1  # aggressively short
    cq_update = {"update_id": 20, "callback_query": _cq("goal:approve:1")}
    with patch("backend.config.get_settings", return_value=settings), \
         patch("backend.integrations.telegram.get_updates", new_callable=AsyncMock, return_value=[cq_update]), \
         patch("backend.agents.telegram_poller.handle_callback", new_callable=AsyncMock) as mock_handle:
        await telegram_poller.poll_once(None)

    mock_handle.assert_awaited_once()


@pytest.mark.asyncio
async def test_poll_once_missing_date_fails_closed():
    """A missing 'date' field must be treated as stale (dropped), not as
    'now' — defaulting to fresh would fail OPEN, exactly what this guard
    exists to prevent."""
    settings = MagicMock()
    settings.telegram_poll_timeout_s = 25
    settings.telegram_command_max_age_s = 300
    update = {"update_id": 30, "message": {"chat": {"id": 12345}, "text": "hi"}}  # no "date" key
    with patch("backend.config.get_settings", return_value=settings), \
         patch("backend.integrations.telegram.get_updates", new_callable=AsyncMock, return_value=[update]), \
         patch("backend.agents.telegram_poller._handle_message", new_callable=AsyncMock) as mock_handle:
        new_offset = await telegram_poller.poll_once(None)

    mock_handle.assert_not_called()
    assert new_offset == 31  # offset still advances — never refetched forever


@pytest.mark.asyncio
async def test_poll_once_null_date_fails_closed():
    settings = MagicMock()
    settings.telegram_poll_timeout_s = 25
    settings.telegram_command_max_age_s = 300
    update = {"update_id": 31, "message": {"chat": {"id": 12345}, "text": "hi", "date": None}}
    with patch("backend.config.get_settings", return_value=settings), \
         patch("backend.integrations.telegram.get_updates", new_callable=AsyncMock, return_value=[update]), \
         patch("backend.agents.telegram_poller._handle_message", new_callable=AsyncMock) as mock_handle:
        new_offset = await telegram_poller.poll_once(None)

    mock_handle.assert_not_called()
    assert new_offset == 32


@pytest.mark.asyncio
async def test_poll_once_malformed_update_does_not_wedge_batch():
    """One update raising mid-processing must not lose the offset advance or
    stop the rest of the batch from being processed."""
    import time
    settings = MagicMock()
    settings.telegram_poll_timeout_s = 25
    settings.telegram_command_max_age_s = 300
    bad_update = {"update_id": 40, "message": {"chat": {"id": 12345}, "text": "bad", "date": time.time()}}
    good_update = {"update_id": 41, "message": {"chat": {"id": 12345}, "text": "good", "date": time.time()}}

    async def _handle_side_effect(msg):
        if msg["text"] == "bad":
            raise RuntimeError("boom")

    with patch("backend.config.get_settings", return_value=settings), \
         patch("backend.integrations.telegram.get_updates", new_callable=AsyncMock, return_value=[bad_update, good_update]), \
         patch("backend.agents.telegram_poller._handle_message", side_effect=_handle_side_effect) as mock_handle:
        new_offset = await telegram_poller.poll_once(None)

    assert new_offset == 42  # both updates' offsets accounted for
    assert mock_handle.call_count == 2  # the bad one didn't stop the good one from running


# ---------------------------------------------------------------------------
# Task reference retention (asyncio.create_task GC bug regression)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_message_task_is_tracked_until_done():
    """asyncio.create_task() only holds a weak reference — a dropped task can
    be garbage-collected mid-run. _handle_message must keep a strong
    reference in _message_tasks until the task completes."""
    settings = MagicMock()
    settings.telegram_chat_id = "12345"
    release = asyncio.Event()

    async def _slow_dispatch(text, msg):
        await release.wait()

    with patch("backend.config.get_settings", return_value=settings), \
         patch("backend.agents.telegram_commands.dispatch", side_effect=_slow_dispatch):
        await telegram_poller._handle_message(_msg("hello"))
        await asyncio.sleep(0)  # let the task start and reach the await point
        assert len(telegram_poller._message_tasks) == 1

        release.set()
        # drain: wait for the tracked task to finish and self-remove
        for t in list(telegram_poller._message_tasks):
            await t

    assert len(telegram_poller._message_tasks) == 0


@pytest.mark.asyncio
async def test_stop_cancels_inflight_message_tasks():
    async def _never_finishes():
        await asyncio.Event().wait()

    task = asyncio.create_task(_never_finishes())
    telegram_poller._message_tasks.add(task)
    try:
        await telegram_poller.stop()
        assert task.cancelled() or task.cancelling() > 0
    finally:
        task.cancel()
        telegram_poller._message_tasks.discard(task)


# ---------------------------------------------------------------------------
# run_poller — registers the "/" command menu once at start
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_poller_registers_command_menu():
    stop = asyncio.Event()

    async def _fake_poll_once(offset):
        stop.set()
        return offset

    with patch("backend.agents.telegram_poller.poll_once", side_effect=_fake_poll_once), \
         patch("backend.integrations.telegram.set_my_commands", new_callable=AsyncMock, return_value=True) as mock_set_cmds:
        await telegram_poller.run_poller(stop)

    mock_set_cmds.assert_awaited_once()


# ---------------------------------------------------------------------------
# Phase 2b — voice messages
# ---------------------------------------------------------------------------

def _voice_msg(chat_id=12345, file_id="voice-file-1"):
    return {"chat": {"id": chat_id}, "voice": {"file_id": file_id, "duration": 3}, "date": 1234567890}


@pytest.mark.asyncio
async def test_transcribe_voice_downloads_and_transcribes():
    with patch("backend.integrations.telegram.get_file_bytes", new_callable=AsyncMock, return_value=b"audio-bytes") as mock_get_file, \
         patch("backend.agents.voice.transcribe", new_callable=AsyncMock, return_value="what's the garage status") as mock_transcribe:
        result = await telegram_poller._transcribe_voice(_voice_msg())

    assert result == "what's the garage status"
    mock_get_file.assert_awaited_once_with("voice-file-1")
    mock_transcribe.assert_awaited_once()
    # the temp file path passed to transcribe() must have been cleaned up
    import os
    temp_path = mock_transcribe.await_args.args[0]
    assert not os.path.exists(temp_path)


@pytest.mark.asyncio
async def test_transcribe_voice_missing_file_id_returns_none():
    msg = {"chat": {"id": 12345}, "voice": {}, "date": 1234567890}
    result = await telegram_poller._transcribe_voice(msg)
    assert result is None


@pytest.mark.asyncio
async def test_transcribe_voice_survives_unlink_failure():
    """A successful transcript must not be discarded because deleting the temp
    file failed afterward (historically, Windows could transiently hold a
    handle on a just-written file — an unguarded finally: os.unlink() would
    supersede a successful return with the unlink's own exception)."""
    with patch("backend.integrations.telegram.get_file_bytes", new_callable=AsyncMock, return_value=b"audio-bytes"), \
         patch("backend.agents.voice.transcribe", new_callable=AsyncMock, return_value="turn off the garage light"), \
         patch("os.unlink", side_effect=PermissionError("file in use")):
        result = await telegram_poller._transcribe_voice(_voice_msg())

    assert result == "turn off the garage light"


@pytest.mark.asyncio
async def test_transcribe_voice_download_failure_returns_none_never_raises():
    with patch("backend.integrations.telegram.get_file_bytes", new_callable=AsyncMock, side_effect=RuntimeError("network down")):
        result = await telegram_poller._transcribe_voice(_voice_msg())
    assert result is None


@pytest.mark.asyncio
async def test_authorized_voice_message_transcribes_and_dispatches():
    settings = MagicMock()
    settings.telegram_chat_id = "12345"
    with patch("backend.config.get_settings", return_value=settings), \
         patch("backend.agents.telegram_poller._transcribe_voice", new_callable=AsyncMock, return_value="is the garage open") as mock_transcribe, \
         patch("backend.agents.telegram_commands.dispatch", new_callable=AsyncMock) as mock_dispatch:
        await _run_and_await_created_tasks(telegram_poller._handle_message(_voice_msg()))

    mock_transcribe.assert_awaited_once()
    mock_dispatch.assert_awaited_once()
    assert mock_dispatch.await_args.args[0] == "is the garage open"


# ---------------------------------------------------------------------------
# _match_voice_command — spoken command names must not fall through to chat()
#
# Real incident this guards against: saying "mute" (meaning the /mute
# notification command) transcribed closely enough to a real device name
# that chat()'s HOME_CONTROL branch turned on a real switch instead, with
# zero confirmation (actor="user" is always-allowed). Voice can't produce a
# literal "/", so without this, NO spoken command name is ever safe.
# ---------------------------------------------------------------------------

def test_match_voice_command_bare_command_name():
    assert telegram_poller._match_voice_command("mute") == "/mute"


def test_match_voice_command_case_insensitive_and_strips_punctuation():
    assert telegram_poller._match_voice_command("Mute.") == "/mute"
    assert telegram_poller._match_voice_command("STATUS") == "/status"


def test_match_voice_command_with_args():
    assert telegram_poller._match_voice_command("mute budget warn") == "/mute budget warn"


def test_match_voice_command_non_command_text_returns_none():
    assert telegram_poller._match_voice_command("what's the weather today") is None
    assert telegram_poller._match_voice_command("turn off the garage light") is None


def test_match_voice_command_empty_returns_none():
    assert telegram_poller._match_voice_command("") is None
    assert telegram_poller._match_voice_command("   ") is None


@pytest.mark.asyncio
async def test_voice_saying_bare_command_name_dispatches_as_real_command():
    """The actual incident, reproduced: transcript 'mute' must dispatch as
    the /mute command, never as bare chat text."""
    settings = MagicMock()
    settings.telegram_chat_id = "12345"
    with patch("backend.config.get_settings", return_value=settings), \
         patch("backend.agents.telegram_poller._transcribe_voice", new_callable=AsyncMock, return_value="mute"), \
         patch("backend.agents.telegram_commands.dispatch", new_callable=AsyncMock) as mock_dispatch:
        await _run_and_await_created_tasks(telegram_poller._handle_message(_voice_msg()))

    mock_dispatch.assert_awaited_once()
    assert mock_dispatch.await_args.args[0] == "/mute"


@pytest.mark.asyncio
async def test_voice_saying_conversational_text_still_reaches_chat():
    """Non-command speech must be unaffected — only an exact leading command
    name gets rewritten."""
    settings = MagicMock()
    settings.telegram_chat_id = "12345"
    with patch("backend.config.get_settings", return_value=settings), \
         patch("backend.agents.telegram_poller._transcribe_voice", new_callable=AsyncMock, return_value="is the garage open"), \
         patch("backend.agents.telegram_commands.dispatch", new_callable=AsyncMock) as mock_dispatch:
        await _run_and_await_created_tasks(telegram_poller._handle_message(_voice_msg()))

    assert mock_dispatch.await_args.args[0] == "is the garage open"


@pytest.mark.asyncio
async def test_voice_transcription_failure_does_not_dispatch_but_still_replies():
    """Silence on a failed transcription would be indistinguishable from the
    bot being down — every other inbound path (even an unknown command)
    replies, so this must too."""
    settings = MagicMock()
    settings.telegram_chat_id = "12345"
    with patch("backend.config.get_settings", return_value=settings), \
         patch("backend.agents.telegram_poller._transcribe_voice", new_callable=AsyncMock, return_value=None), \
         patch("backend.agents.telegram_commands.dispatch", new_callable=AsyncMock) as mock_dispatch, \
         patch("backend.integrations.telegram.send_reply", new_callable=AsyncMock, return_value=True) as mock_reply:
        await _run_and_await_created_tasks(telegram_poller._handle_message(_voice_msg()))

    mock_dispatch.assert_not_called()
    mock_reply.assert_awaited_once()
    assert "couldn't transcribe" in mock_reply.await_args.args[0].lower()


@pytest.mark.asyncio
async def test_unauthorized_voice_message_never_transcribes():
    """Transcription costs real compute (Whisper) — must not run for a
    stranger's voice message any more than a stranger's text message."""
    settings = MagicMock()
    settings.telegram_chat_id = "99999"
    with patch("backend.config.get_settings", return_value=settings), \
         patch("backend.agents.telegram_poller._transcribe_voice", new_callable=AsyncMock) as mock_transcribe:
        await _run_and_await_created_tasks(telegram_poller._handle_message(_voice_msg(chat_id=12345)))

    mock_transcribe.assert_not_called()


# ---------------------------------------------------------------------------
# Phase 2c — docker/vm callback namespaces (homelab alert action buttons)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_docker_restart_dispatches_via_broker_with_user_actor():
    from backend.safety.broker import ActionResult, Decision, Risk, Reversibility
    result = ActionResult(decision=Decision.EXECUTED, risk=Risk.HIGH,
                           reversibility=Reversibility.REVERSIBLE_BY_INVERSE,
                           log_id=1, result={"success": True})
    with patch("backend.safety.broker.execute_action", new_callable=AsyncMock, return_value=result) as mock_exec, \
         patch("backend.integrations.telegram.answer_callback_query", new_callable=AsyncMock, return_value=True), \
         patch("backend.integrations.telegram.edit_message_text", new_callable=AsyncMock, return_value=True) as mock_edit:
        await telegram_poller.handle_callback(_cq("docker:restart:plex"))

    mock_exec.assert_awaited_once_with(
        actor="user", kind="unraid_docker", target="plex",
        payload={"container_id": "plex"},
    )
    assert "✓" in mock_edit.await_args.args[2]
    assert "Restart sent." in mock_edit.await_args.args[2]


@pytest.mark.asyncio
async def test_docker_restart_failure_alerts_and_keeps_buttons():
    """decision==EXECUTED but result.success==False is a FAILED restart, not a
    success — the exact gap the ✓/✗ icon logic must not paper over."""
    from backend.safety.broker import ActionResult, Decision, Risk, Reversibility
    result = ActionResult(decision=Decision.EXECUTED, risk=Risk.HIGH,
                           reversibility=Reversibility.REVERSIBLE_BY_INVERSE,
                           log_id=1, result={"success": False})
    with patch("backend.safety.broker.execute_action", new_callable=AsyncMock, return_value=result), \
         patch("backend.integrations.telegram.answer_callback_query", new_callable=AsyncMock, return_value=True) as mock_answer, \
         patch("backend.integrations.telegram.edit_message_text", new_callable=AsyncMock, return_value=True) as mock_edit:
        await telegram_poller.handle_callback(_cq("docker:restart:plex"))

    assert mock_answer.await_args.kwargs.get("show_alert") is True
    mock_edit.assert_not_called()


@pytest.mark.asyncio
async def test_vm_start_dispatches_via_native_vm_power():
    """vm:start:<vmid> dispatches the native vm_power kind
    (proxmox.set_vm_power directly) -- vmid is
    coerced to int (the 'vm' namespace isn't in _INT_ID_NAMESPACES, so
    obj_id arrives as a string from callback_data)."""
    from backend.safety.broker import ActionResult, Decision, Risk, Reversibility
    result = ActionResult(decision=Decision.EXECUTED, risk=Risk.HIGH,
                           reversibility=Reversibility.REVERSIBLE_BY_INVERSE,
                           log_id=1, result={"ok": True})
    with patch("backend.safety.broker.execute_action", new_callable=AsyncMock, return_value=result) as mock_exec, \
         patch("backend.integrations.telegram.answer_callback_query", new_callable=AsyncMock, return_value=True), \
         patch("backend.integrations.telegram.edit_message_text", new_callable=AsyncMock, return_value=True) as mock_edit:
        await telegram_poller.handle_callback(_cq("vm:start:101"))

    mock_exec.assert_awaited_once_with(
        actor="user", kind="vm_power", target="101",
        payload={"vmid": 101, "action": "start"},
    )
    assert "✓" in mock_edit.await_args.args[2]


@pytest.mark.asyncio
async def test_system_restart_dispatches_via_broker_with_user_actor():
    """system:restart:nexus (the deploy-drift alert's button, and /restart's
    underlying dispatch) goes through the native system_restart kind,
    actor="user" -- same precedent as docker/vm above."""
    from backend.safety.broker import ActionResult, Decision, Risk, Reversibility
    result = ActionResult(decision=Decision.EXECUTED, risk=Risk.HIGH,
                           reversibility=Reversibility.REVERSIBLE_BY_INVERSE,
                           log_id=1, result={"ok": True, "scheduled": True})
    with patch("backend.safety.broker.execute_action", new_callable=AsyncMock, return_value=result) as mock_exec, \
         patch("backend.integrations.telegram.answer_callback_query", new_callable=AsyncMock, return_value=True), \
         patch("backend.integrations.telegram.edit_message_text", new_callable=AsyncMock, return_value=True) as mock_edit:
        await telegram_poller.handle_callback(_cq("system:restart:nexus"))

    mock_exec.assert_awaited_once_with(
        actor="user", kind="system_restart", target="nexus", payload={},
    )
    assert "✓" in mock_edit.await_args.args[2]
    assert "Restarting" in mock_edit.await_args.args[2]


@pytest.mark.asyncio
async def test_system_restart_failure_alerts_and_keeps_buttons():
    from backend.safety.broker import ActionResult, Decision, Risk, Reversibility
    result = ActionResult(decision=Decision.FAILED, risk=Risk.HIGH,
                           reversibility=Reversibility.REVERSIBLE_BY_INVERSE,
                           log_id=1, error="boom")
    with patch("backend.safety.broker.execute_action", new_callable=AsyncMock, return_value=result), \
         patch("backend.integrations.telegram.answer_callback_query", new_callable=AsyncMock, return_value=True) as mock_answer, \
         patch("backend.integrations.telegram.edit_message_text", new_callable=AsyncMock, return_value=True) as mock_edit:
        await telegram_poller.handle_callback(_cq("system:restart:nexus"))

    assert mock_answer.await_args.kwargs.get("show_alert") is True
    mock_edit.assert_not_called()


@pytest.mark.asyncio
async def test_vm_start_invalid_vmid_returns_error_not_crash():
    with patch("backend.safety.broker.execute_action", new_callable=AsyncMock) as mock_exec, \
         patch("backend.integrations.telegram.answer_callback_query", new_callable=AsyncMock, return_value=True), \
         patch("backend.integrations.telegram.edit_message_text", new_callable=AsyncMock, return_value=True):
        await telegram_poller.handle_callback(_cq("vm:start:not-a-number"))

    mock_exec.assert_not_awaited()


@pytest.mark.asyncio
async def test_docker_and_vm_callbacks_unauthorized_chat_never_dispatches():
    """The authorization gate must cover the new namespaces exactly like the
    existing goal/safety ones — checked BEFORE execute_action is ever awaited."""
    settings = MagicMock()
    settings.telegram_chat_id = "99999"
    with patch("backend.config.get_settings", return_value=settings), \
         patch("backend.safety.broker.execute_action", new_callable=AsyncMock) as mock_exec, \
         patch("backend.integrations.telegram.answer_callback_query", new_callable=AsyncMock, return_value=True) as mock_answer:
        await telegram_poller.handle_callback(_cq("docker:restart:plex", chat_id=12345))
        await telegram_poller.handle_callback(_cq("vm:start:101", chat_id=12345))

    mock_exec.assert_not_called()
    assert mock_answer.await_count == 2
    for call in mock_answer.await_args_list:
        assert call.args[1:] == ("Not authorized",) or call.kwargs.get("show_alert") is True


@pytest.mark.asyncio
async def test_goal_approve_int_coercion_still_enforced():
    """Regression: only goal/safety ids coerce to int — docker/vm ids must not
    accidentally lose that guard for their own namespace."""
    with patch("backend.agents.goals.approve", new_callable=AsyncMock) as mock_approve, \
         patch("backend.integrations.telegram.answer_callback_query", new_callable=AsyncMock, return_value=True) as mock_answer:
        await telegram_poller.handle_callback(_cq("goal:approve:abc"))

    mock_approve.assert_not_called()
    mock_answer.assert_awaited_once_with("cq1", "Invalid id.", show_alert=True)


@pytest.mark.asyncio
async def test_unknown_namespace_still_non_definitive():
    with patch("backend.integrations.telegram.answer_callback_query", new_callable=AsyncMock, return_value=True) as mock_answer, \
         patch("backend.integrations.telegram.edit_message_text", new_callable=AsyncMock, return_value=True) as mock_edit:
        await telegram_poller.handle_callback(_cq("bogus:x:1"))

    assert mock_answer.await_args.kwargs.get("show_alert") is True
    mock_edit.assert_not_called()
