"""Quill Telegram surface: ct:* buttons, /today /open /done /snooze /reopen, send_digest."""
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

import backend.database
from backend.agents import captures, telegram_commands, telegram_poller


@pytest.fixture
def eng(monkeypatch):
    e = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(e)
    monkeypatch.setattr("backend.database.engine", e)
    monkeypatch.setattr(captures, "schedule_refresh", MagicMock())
    return e


_n = iter(range(1, 10_000))


def _item(eng, **kw):
    with Session(eng) as s:
        row = backend.database.CaptureItem(
            **{"capture_id": 1, "item_index": next(_n), "category": "work", "kind": "task", "title": "T", **kw})
        s.add(row)
        s.commit()
        s.refresh(row)
        return row.id


def _cq(data, chat_id=12345):
    return {"id": "cq1", "data": data, "message": {"chat": {"id": chat_id}, "message_id": 42, "text": "Task"}}


async def _tap(data, **kw):
    with patch("backend.integrations.telegram.answer_callback_query", new_callable=AsyncMock) as ans, \
         patch("backend.integrations.telegram.edit_message_text", new_callable=AsyncMock) as edit:
        await telegram_poller.handle_callback(_cq(data, **kw))
    return ans, edit


async def test_done_edits_then_second_tap_already_done(eng):
    i = _item(eng)
    ans, edit = await _tap(f"ct:done:{i}")
    assert "✓" in edit.await_args.args[2] and "Done." in edit.await_args.args[2]
    ans.assert_awaited_once()
    captures.schedule_refresh.assert_called_once()
    _, edit = await _tap(f"ct:done:{i}")
    assert "Already done." in edit.await_args.args[2]


async def test_snooze_then_already_snoozed_with_deadline(eng):
    i = _item(eng)
    _, edit = await _tap(f"ct:snooze:{i}")
    assert "✓ Snoozed 1 day." in edit.await_args.args[2]
    _, edit = await _tap(f"ct:snooze:{i}")
    assert "Already snoozed until" in edit.await_args.args[2]


async def test_not_found(eng):
    _, edit = await _tap("ct:done:999")
    assert "Task not found." in edit.await_args.args[2]


async def test_open_answers_alert_and_does_not_edit(eng):
    i = _item(eng, title="Email Bob", notes="re invoice")
    ans, edit = await _tap(f"ct:open:{i}")
    edit.assert_not_awaited()
    ans.assert_awaited_once()
    assert ans.await_args.kwargs == {"show_alert": True}
    assert "Email Bob" in ans.await_args.args[1]


async def test_unauthorized_chat_rejected(eng):
    i = _item(eng)
    settings = MagicMock(telegram_chat_id="99999")
    with patch("backend.config.get_settings", return_value=settings):
        ans, edit = await _tap(f"ct:done:{i}")
    ans.assert_awaited_once_with("cq1", "Not authorized", show_alert=True)
    edit.assert_not_awaited()
    assert (await captures.get_item(i))["status"] == "open"


async def test_today_filters_by_category(eng):
    _item(eng, title="W task")
    _item(eng, title="P task", category="personal")
    out = await telegram_commands._cmd_today("work", {})
    assert "W task" in out and "P task" not in out
    assert "Usage" in await telegram_commands._cmd_open("bogus", {})


async def test_done_snooze_reopen_commands(eng):
    i = _item(eng)
    assert await telegram_commands._cmd_done(str(i), {}) == "Done."
    assert await telegram_commands._cmd_done(str(i), {}) == "Already done."
    assert await telegram_commands._cmd_reopen(str(i), {}) == "Reopened."
    assert await telegram_commands._cmd_snooze(f"{i} 3", {}) == "Snoozed 3 days."
    assert (await telegram_commands._cmd_snooze(str(i), {})).startswith("Already snoozed until")
    assert await telegram_commands._cmd_done("999", {}) == "Task not found."
    assert "Usage" in await telegram_commands._cmd_done("abc", {})


async def test_digest_sends_header_and_capped_buttons(eng):
    for n in range(10):
        _item(eng, title=f"Do <b>{n}</b>")
    _item(eng, title="idea", kind="idea")
    with patch("backend.events.notify_phone", new_callable=AsyncMock) as np:
        out = await captures.send_digest("work")
    assert out == {"sent": 8, "more": 2}
    assert np.await_count == 9
    assert "10 open" in np.await_args_list[0].args[0] and not np.await_args_list[0].kwargs.get("buttons")
    first = np.await_args_list[1]
    assert "&lt;b&gt;" in first.args[0] and first.kwargs["kind"] == "capture_digest"
    assert [b["callback_data"].split(":")[1] for b in first.kwargs["buttons"]] == ["done", "snooze", "open"]


async def test_digest_skips_when_empty_and_resurfaces_expired_snooze(eng):
    from datetime import datetime, timedelta
    with patch("backend.events.notify_phone", new_callable=AsyncMock) as np:
        assert await captures.send_digest("work") == {"skipped": "nothing open"}
        np.assert_not_awaited()
        _item(eng, status="snoozed", snoozed_until=datetime.utcnow() - timedelta(hours=1))
        assert (await captures.send_digest("work"))["sent"] == 1


async def test_digest_disabled_flag(eng, monkeypatch):
    monkeypatch.setenv("CAPTURE_DIGEST_ENABLED", "false")
    import backend.config as config_mod
    monkeypatch.setattr(config_mod, "_settings_instance", None)
    assert await captures.run_capture_digest("work") == {"skipped": "disabled"}


def test_capture_digest_is_registered_notify_kind():
    from backend.events import NOTIFY_KINDS
    assert "capture_digest" in NOTIFY_KINDS


async def test_open_list_has_no_cap(eng):
    for n in range(35):
        _item(eng, title=f"task-{n}")
    out = await telegram_commands._cmd_open("work", {})
    assert all(f"task-{n}" in out for n in range(35))


async def test_schedule_refresh_runs_in_background_and_swallows_errors(monkeypatch):
    import asyncio
    real = captures.__dict__["schedule_refresh"]  # fixture-free: eng fixture not used here
    ran = asyncio.Event()

    async def boom():
        ran.set()
        raise RuntimeError("x")

    monkeypatch.setattr(captures, "refresh_views", boom)
    real()
    await asyncio.wait_for(ran.wait(), 2)
    await asyncio.gather(*list(captures._refresh_tasks))  # must not raise
    assert not captures._refresh_tasks
