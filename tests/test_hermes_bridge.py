import logging
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.mark.asyncio
async def test_notify_success():
    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_resp = MagicMock(status_code=200)
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value.post = AsyncMock(return_value=mock_resp)
        mock_client_cls.return_value = mock_client

        from backend.integrations.hermes import notify
        result = await notify({"type": "test", "content": "hello"})
        assert result is True


@pytest.mark.asyncio
async def test_notify_failure_queues():
    with patch("httpx.AsyncClient") as mock_client_cls, \
         patch("backend.database.engine"), \
         patch("sqlmodel.Session") as mock_session:

        mock_client = AsyncMock()
        mock_client.__aenter__.return_value.post = AsyncMock(side_effect=Exception("connection refused"))
        mock_client_cls.return_value = mock_client

        session_mock = MagicMock()
        mock_session.return_value.__enter__ = MagicMock(return_value=session_mock)
        mock_session.return_value.__exit__ = MagicMock(return_value=False)

        from backend.integrations.hermes import notify
        result = await notify({"type": "test"})
        assert result is False
        session_mock.add.assert_called_once()
        session_mock.commit.assert_called_once()


@pytest.mark.asyncio
async def test_deliver_pending_success():
    # deliver_pending now does DB I/O in threads via two helpers:
    #   _load_pending() -> list[dict]   (read, off the event loop)
    #   _apply_pending_results(delivered_ids, failed_ids)  (write, off the loop)
    # A 200 response must route the row's id into delivered_ids.
    pending = [{
        "id": 1,
        "payload_json": '{"type": "notify", "content": "test"}',
        "delivery_type": "notify",
    }]

    with patch("httpx.AsyncClient") as mock_client_cls, \
         patch("backend.integrations.hermes._load_pending", return_value=pending), \
         patch("backend.integrations.hermes._apply_pending_results") as mock_apply:

        mock_resp = MagicMock(status_code=200)
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value.post = AsyncMock(return_value=mock_resp)
        mock_client_cls.return_value = mock_client

        from backend.integrations.hermes import deliver_pending
        await deliver_pending()

        mock_apply.assert_called_once_with([1], [])


@pytest.mark.asyncio
async def test_deliver_pending_failure_increments():
    # A non-2xx response must route the row's id into failed_ids (attempts++).
    pending = [{
        "id": 7,
        "payload_json": '{"type": "notify"}',
        "delivery_type": "notify",
    }]

    with patch("httpx.AsyncClient") as mock_client_cls, \
         patch("backend.integrations.hermes._load_pending", return_value=pending), \
         patch("backend.integrations.hermes._apply_pending_results") as mock_apply:

        mock_resp = MagicMock(status_code=500)
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value.post = AsyncMock(return_value=mock_resp)
        mock_client_cls.return_value = mock_client

        from backend.integrations.hermes import deliver_pending
        await deliver_pending()

        mock_apply.assert_called_once_with([], [7])


@pytest.mark.asyncio
async def test_deliver_pending_empty_is_noop():
    # No pending rows => no HTTP client, no write phase.
    with patch("httpx.AsyncClient") as mock_client_cls, \
         patch("backend.integrations.hermes._load_pending", return_value=[]), \
         patch("backend.integrations.hermes._apply_pending_results") as mock_apply:

        from backend.integrations.hermes import deliver_pending
        await deliver_pending()

        mock_client_cls.assert_not_called()
        mock_apply.assert_not_called()


def test_next_eligible_backoff_schedule():
    from backend.integrations.hermes import (
        _BACKOFF_CAP_SECONDS,
        _next_eligible,
    )
    base = datetime(2026, 1, 1, 0, 0, 0)
    # Never attempted -> eligible immediately (far in the past).
    assert _next_eligible(0, None) <= datetime.utcnow()
    # Exponential: attempt 1 -> +60s, 2 -> +120s, 3 -> +240s.
    assert _next_eligible(1, base) == base + timedelta(seconds=60)
    assert _next_eligible(2, base) == base + timedelta(seconds=120)
    assert _next_eligible(3, base) == base + timedelta(seconds=240)
    # Large attempt count is capped.
    assert _next_eligible(99, base) == base + timedelta(seconds=_BACKOFF_CAP_SECONDS)


def _fake_pending(**kw):
    defaults = dict(id=1, payload_json='{"type": "notify"}', delivery_type="notify",
                    attempts=0, last_attempt=None, created_at=datetime(2026, 1, 1))
    defaults.update(kw)
    return SimpleNamespace(**defaults)


def _patch_load_session(rows):
    """Patch the Session used inside _load_pending so session.exec(...).all() -> rows."""
    session = MagicMock()
    session.exec.return_value.all.return_value = rows
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=session)
    cm.__exit__ = MagicMock(return_value=False)
    return patch("sqlmodel.Session", return_value=cm)


def test_load_pending_skips_not_yet_eligible():
    from backend.integrations.hermes import _load_pending
    now = datetime.utcnow()
    fresh_fail = _fake_pending(id=1, attempts=3, last_attempt=now)          # backoff window open
    eligible_old = _fake_pending(id=2, attempts=1, last_attempt=now - timedelta(hours=1))
    with patch("backend.database.engine"), _patch_load_session([fresh_fail, eligible_old]):
        result = _load_pending()
    ids = [r["id"] for r in result]
    assert ids == [2]


def test_load_pending_skips_dead_lettered():
    from backend.integrations.hermes import _MAX_ATTEMPTS, _load_pending
    dead = _fake_pending(id=1, attempts=_MAX_ATTEMPTS, last_attempt=None)
    with patch("backend.database.engine"), _patch_load_session([dead]):
        result = _load_pending()
    assert result == []


def test_apply_results_dead_letters_once(caplog):
    from backend.integrations.hermes import _MAX_ATTEMPTS, _apply_pending_results
    # A row that will cross the cap on this increment.
    row = _fake_pending(id=5, attempts=_MAX_ATTEMPTS - 1, delivery_type="notify")
    session = MagicMock()
    session.exec.return_value.all.return_value = [row]
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=session)
    cm.__exit__ = MagicMock(return_value=False)
    with patch("backend.database.engine"), patch("sqlmodel.Session", return_value=cm), \
         caplog.at_level(logging.WARNING):
        _apply_pending_results([], [5])
    assert row.attempts == _MAX_ATTEMPTS
    assert sum("Dead-lettering" in r.message for r in caplog.records) == 1
