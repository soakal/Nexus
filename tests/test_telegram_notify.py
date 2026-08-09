import logging
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.integrations import telegram


# ---------------------------------------------------------------------------
# chunk_text
# ---------------------------------------------------------------------------

def test_chunk_text_passthrough_under_limit():
    assert telegram.chunk_text("hello") == ["hello"]


def test_chunk_text_empty_returns_empty_list():
    assert telegram.chunk_text("") == []


def test_chunk_text_splits_on_paragraphs():
    para = "x" * 3000
    text = f"{para}\n\n{para}\n\n{para}"
    chunks = telegram.chunk_text(text, limit=4000)
    assert all(len(c) <= 4000 for c in chunks)
    assert "".join(chunks).replace("\n\n", "") .count("x") == 9000


def test_chunk_text_hard_splits_oversized_line():
    line = "y" * 9000
    chunks = telegram.chunk_text(line, limit=4000)
    assert all(len(c) <= 4000 for c in chunks)
    assert "".join(chunks) == line


def test_chunk_text_drops_nothing():
    text = "\n\n".join(["para" + str(i) * 500 for i in range(5)])
    chunks = telegram.chunk_text(text, limit=800)
    # every original token still appears somewhere across the chunks
    for i in range(5):
        assert any(f"para{i}" in c for c in chunks)


# ---------------------------------------------------------------------------
# send_message — buttons, parse_mode, chunk targeting
# ---------------------------------------------------------------------------

def _mock_post_client(responses):
    """responses: list of httpx.Response-like mocks, one per call, in order."""
    mock_client = AsyncMock()
    mock_client.__aenter__.return_value.post = AsyncMock(side_effect=responses)
    return mock_client


@pytest.mark.asyncio
async def test_notify_buttons_on_last_chunk_only():
    para = "z" * 3000
    text = f"{para}\n\n{para}"  # forces 2 chunks at the default 4000 limit
    responses = [MagicMock(status_code=200) for _ in telegram.chunk_text(text)]
    assert len(responses) == 2

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client_cls.return_value = _mock_post_client(responses)
        result = await telegram.notify({
            "content": text,
            "buttons": [{"text": "Confirm", "callback_data": "safety:confirm:1"}],
        })
        assert result is True

        calls = mock_client_cls.return_value.__aenter__.return_value.post.call_args_list
        assert len(calls) == 2
        first_body = calls[0].kwargs["json"]
        second_body = calls[1].kwargs["json"]
        assert "reply_markup" not in first_body
        assert second_body["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "safety:confirm:1"


@pytest.mark.asyncio
async def test_notify_malformed_buttons_still_sends_text():
    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client_cls.return_value = _mock_post_client([MagicMock(status_code=200)])
        result = await telegram.notify({"content": "hi", "buttons": [{"no_text_key": True}]})
        assert result is True


@pytest.mark.asyncio
async def test_notify_parse_mode_passed_through():
    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client_cls.return_value = _mock_post_client([MagicMock(status_code=200)])
        await telegram.notify({"content": "hi", "parse_mode": "HTML"})
        call = mock_client_cls.return_value.__aenter__.return_value.post.call_args_list[0]
        assert call.kwargs["json"]["parse_mode"] == "HTML"


@pytest.mark.asyncio
async def test_notify_parse_mode_absent_when_not_set():
    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client_cls.return_value = _mock_post_client([MagicMock(status_code=200)])
        await telegram.notify({"content": "hi"})
        call = mock_client_cls.return_value.__aenter__.return_value.post.call_args_list[0]
        assert "parse_mode" not in call.kwargs["json"]


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["content", "message", "text"])
async def test_notify_accepts_content_message_or_text(key):
    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client_cls.return_value = _mock_post_client([MagicMock(status_code=200)])
        result = await telegram.notify({key: "hello"})
        assert result is True


@pytest.mark.asyncio
async def test_notify_empty_text_returns_false_without_calling_api():
    with patch("httpx.AsyncClient") as mock_client_cls:
        result = await telegram.notify({"content": ""})
        assert result is False
        mock_client_cls.assert_not_called()


# ---------------------------------------------------------------------------
# Failure classification — terminal (no queue) vs retryable (queue)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [400, 401, 403, 404])
async def test_notify_terminal_status_not_queued(status_code, caplog):
    resp = MagicMock(status_code=status_code, text="bad request")
    with patch("httpx.AsyncClient") as mock_client_cls, \
         patch("backend.integrations.telegram._queue_delivery") as mock_queue, \
         caplog.at_level(logging.ERROR):
        mock_client_cls.return_value = _mock_post_client([resp])
        result = await telegram.notify({"content": "hi"})
        assert result is False
        mock_queue.assert_not_called()
        assert any(r.levelno == logging.ERROR for r in caplog.records)


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [429, 500, 502, 503])
async def test_notify_retryable_status_is_queued(status_code):
    resp = MagicMock(status_code=status_code, text="", json=MagicMock(return_value={}))
    with patch("httpx.AsyncClient") as mock_client_cls, \
         patch("backend.integrations.telegram._queue_delivery") as mock_queue:
        mock_client_cls.return_value = _mock_post_client([resp])
        result = await telegram.notify({"content": "hi"})
        assert result is False
        mock_queue.assert_called_once()


@pytest.mark.asyncio
async def test_notify_missing_bot_token_not_queued(caplog, monkeypatch):
    def _raise(key, *a, **kw):
        raise KeyError(key)

    monkeypatch.setattr("backend.secrets.manager.get_secret", _raise)
    with patch("backend.integrations.telegram._queue_delivery") as mock_queue, \
         caplog.at_level(logging.ERROR):
        result = await telegram.notify({"content": "hi"})
        assert result is False
        mock_queue.assert_not_called()
        assert any(r.levelno == logging.ERROR for r in caplog.records)


@pytest.mark.asyncio
async def test_notify_transport_exception_is_queued():
    mock_client = AsyncMock()
    mock_client.__aenter__.return_value.post = AsyncMock(side_effect=Exception("connection refused"))
    with patch("httpx.AsyncClient") as mock_client_cls, \
         patch("backend.integrations.telegram._queue_delivery") as mock_queue:
        mock_client_cls.return_value = mock_client
        result = await telegram.notify({"content": "hi"})
        assert result is False
        mock_queue.assert_called_once()


# ---------------------------------------------------------------------------
# health_check
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_health_check_true_on_ok():
    resp = MagicMock(status_code=200, json=MagicMock(return_value={"ok": True}))
    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client_cls.return_value = _mock_post_client([resp])
        assert await telegram.health_check() is True


@pytest.mark.asyncio
async def test_health_check_false_on_exception():
    mock_client = AsyncMock()
    mock_client.__aenter__.return_value.post = AsyncMock(side_effect=Exception("timeout"))
    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client_cls.return_value = mock_client
        assert await telegram.health_check() is False


# ---------------------------------------------------------------------------
# get_updates — 409 conflict, other status, success
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_updates_409_raises_conflict():
    resp = MagicMock(status_code=409)
    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client_cls.return_value = _mock_post_client([resp])
        with pytest.raises(telegram.TelegramConflict):
            await telegram.get_updates(None, timeout=25, allowed_updates=["callback_query"])


@pytest.mark.asyncio
async def test_get_updates_success_returns_result_list():
    resp = MagicMock(status_code=200)
    resp.json.return_value = {"result": [{"update_id": 1}]}
    resp.raise_for_status = MagicMock()
    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client_cls.return_value = _mock_post_client([resp])
        result = await telegram.get_updates(5, timeout=25, allowed_updates=["callback_query"])
        assert result == [{"update_id": 1}]


# ---------------------------------------------------------------------------
# deliver_pending — queue mechanics
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_deliver_pending_success():
    pending = [{"id": 1, "payload_json": '{"content": "test"}', "delivery_type": "notify"}]
    with patch("httpx.AsyncClient") as mock_client_cls, \
         patch("backend.integrations.telegram._load_pending", return_value=pending), \
         patch("backend.integrations.telegram._apply_pending_results") as mock_apply:
        mock_client_cls.return_value = _mock_post_client([MagicMock(status_code=200)])
        await telegram.deliver_pending()
        mock_apply.assert_called_once_with([1], [])


@pytest.mark.asyncio
async def test_deliver_pending_failure_increments():
    pending = [{"id": 7, "payload_json": '{"content": "test"}', "delivery_type": "notify"}]
    with patch("httpx.AsyncClient") as mock_client_cls, \
         patch("backend.integrations.telegram._load_pending", return_value=pending), \
         patch("backend.integrations.telegram._apply_pending_results") as mock_apply:
        mock_client_cls.return_value = _mock_post_client([MagicMock(status_code=500, text="", json=MagicMock(return_value={}))])
        await telegram.deliver_pending()
        mock_apply.assert_called_once_with([], [7])


@pytest.mark.asyncio
async def test_deliver_pending_legacy_action_type_dropped():
    """A row of a legacy, no-longer-produced delivery type can't be replayed
    to Telegram — it should drop (delivered_ids) rather than dead-letter
    forever."""
    pending = [{"id": 3, "payload_json": '{"message": "restart vm"}', "delivery_type": "action"}]
    with patch("httpx.AsyncClient") as mock_client_cls, \
         patch("backend.integrations.telegram._load_pending", return_value=pending), \
         patch("backend.integrations.telegram._apply_pending_results") as mock_apply:
        await telegram.deliver_pending()
        mock_apply.assert_called_once_with([3], [])
        mock_client_cls.assert_not_called()


@pytest.mark.asyncio
async def test_deliver_pending_empty_is_noop():
    with patch("httpx.AsyncClient") as mock_client_cls, \
         patch("backend.integrations.telegram._load_pending", return_value=[]), \
         patch("backend.integrations.telegram._apply_pending_results") as mock_apply:
        await telegram.deliver_pending()
        mock_client_cls.assert_not_called()
        mock_apply.assert_not_called()


def test_next_eligible_backoff_schedule():
    base = datetime(2026, 1, 1, 0, 0, 0)
    assert telegram._next_eligible(0, None) <= datetime.utcnow()
    assert telegram._next_eligible(1, base) == base + timedelta(seconds=60)
    assert telegram._next_eligible(2, base) == base + timedelta(seconds=120)
    assert telegram._next_eligible(3, base) == base + timedelta(seconds=240)
    assert telegram._next_eligible(99, base) == base + timedelta(seconds=telegram._BACKOFF_CAP_SECONDS)


def _fake_pending(**kw):
    defaults = dict(id=1, payload_json='{"content": "test"}', delivery_type="notify",
                    attempts=0, last_attempt=None, created_at=datetime(2026, 1, 1))
    defaults.update(kw)
    return SimpleNamespace(**defaults)


def _patch_load_session(rows):
    session = MagicMock()
    session.exec.return_value.all.return_value = rows
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=session)
    cm.__exit__ = MagicMock(return_value=False)
    return patch("sqlmodel.Session", return_value=cm)


def test_load_pending_skips_not_yet_eligible():
    now = datetime.utcnow()
    fresh_fail = _fake_pending(id=1, attempts=3, last_attempt=now)
    eligible_old = _fake_pending(id=2, attempts=1, last_attempt=now - timedelta(hours=1))
    with patch("backend.database.engine"), _patch_load_session([fresh_fail, eligible_old]):
        result = telegram._load_pending()
    assert [r["id"] for r in result] == [2]


def test_load_pending_skips_dead_lettered():
    dead = _fake_pending(id=1, attempts=telegram._MAX_ATTEMPTS, last_attempt=None)
    with patch("backend.database.engine"), _patch_load_session([dead]):
        result = telegram._load_pending()
    assert result == []


def test_apply_results_dead_letters_once(caplog):
    row = _fake_pending(id=5, attempts=telegram._MAX_ATTEMPTS - 1, delivery_type="notify")
    session = MagicMock()
    session.exec.return_value.all.return_value = [row]
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=session)
    cm.__exit__ = MagicMock(return_value=False)
    with patch("backend.database.engine"), patch("sqlmodel.Session", return_value=cm), \
         caplog.at_level(logging.WARNING):
        telegram._apply_pending_results([], [5])
    assert row.attempts == telegram._MAX_ATTEMPTS
    assert sum("Dead-lettering" in r.message for r in caplog.records) == 1


def test_delivery_queue_health_reports_secret_present():
    with patch("sqlmodel.Session") as mock_session:
        session = MagicMock()
        session.exec.return_value.all.return_value = []
        mock_session.return_value.__enter__ = MagicMock(return_value=session)
        mock_session.return_value.__exit__ = MagicMock(return_value=False)
        health = telegram.delivery_queue_health()
    assert health["secret_present"] is True  # MOCK_SECRETS provides TELEGRAM_BOT_TOKEN
    assert health["pending_count"] == 0


def test_delivery_queue_health_never_raises_on_db_error():
    with patch("sqlmodel.Session", side_effect=Exception("db down")):
        health = telegram.delivery_queue_health()
    assert health == {
        "pending_count": 0,
        "oldest_age_seconds": None,
        "dead_lettered_count": 0,
        "secret_present": False,
    }


# ---------------------------------------------------------------------------
# get_file_bytes — Phase 2b voice message download
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_file_bytes_downloads_content():
    getfile_resp = MagicMock(status_code=200)
    getfile_resp.raise_for_status = MagicMock()
    getfile_resp.json.return_value = {"result": {"file_path": "voice/file_1.oga"}}

    download_resp = MagicMock(status_code=200, content=b"fake-audio-bytes")
    download_resp.raise_for_status = MagicMock()

    mock_client = AsyncMock()
    mock_client.__aenter__.return_value.post = AsyncMock(return_value=getfile_resp)
    mock_client.__aenter__.return_value.get = AsyncMock(return_value=download_resp)

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client_cls.return_value = mock_client
        content = await telegram.get_file_bytes("file_id_123")

    assert content == b"fake-audio-bytes"
    # Telegram file downloads use a DIFFERENT base path than every other Bot
    # API call: /file/bot<token>/..., not /bot<token>/<method>.
    get_call_args = mock_client.__aenter__.return_value.get.call_args
    assert "/file/bot" in get_call_args.args[0]
    assert "voice/file_1.oga" in get_call_args.args[0]


@pytest.mark.asyncio
async def test_get_file_bytes_raises_when_too_large():
    getfile_resp = MagicMock(status_code=200)
    getfile_resp.raise_for_status = MagicMock()
    getfile_resp.json.return_value = {"result": {"file_path": "voice/file_1.oga"}}

    download_resp = MagicMock(status_code=200, content=b"x" * 100)
    download_resp.raise_for_status = MagicMock()

    mock_client = AsyncMock()
    mock_client.__aenter__.return_value.post = AsyncMock(return_value=getfile_resp)
    mock_client.__aenter__.return_value.get = AsyncMock(return_value=download_resp)

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client_cls.return_value = mock_client
        with pytest.raises(ValueError, match="too large"):
            await telegram.get_file_bytes("file_id_123", max_bytes=50)


# ---------------------------------------------------------------------------
# _queue_delivery — real body, not mocked away
#
# Every other test in this file patches _queue_delivery itself to observe
# WHETHER it was called; none exercise its actual body (session.add +
# commit). A regression there (e.g. a typo'd field name) would make
# deliver_pending()/notify() look like they're queuing correctly while
# silently writing nothing — the "looks queued but isn't" failure mode.
# ---------------------------------------------------------------------------

def test_queue_delivery_writes_a_real_row():
    import json
    from sqlmodel import Session, SQLModel, create_engine, select
    from sqlmodel.pool import StaticPool
    import backend.database  # noqa: F401 — register PendingDelivery on metadata

    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(eng)

    with patch("backend.database.engine", eng):
        telegram._queue_delivery({"content": "test alert"}, "notify")

    from backend.database import PendingDelivery
    with Session(eng) as session:
        rows = session.exec(select(PendingDelivery)).all()
        assert len(rows) == 1
        assert rows[0].delivery_type == "notify"
        assert json.loads(rows[0].payload_json) == {"content": "test alert"}
