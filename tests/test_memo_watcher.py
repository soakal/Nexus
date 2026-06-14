import logging
from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
async def test_wait_for_stable_size_settles():
    # Size grows then holds steady -> returns True once unchanged for stable_checks reads.
    sizes = iter([10, 50, 100, 100, 100])
    with patch("backend.agents.memo_watcher.asyncio.sleep", new=AsyncMock()), \
         patch("backend.agents.memo_watcher.os.path.getsize", side_effect=lambda p: next(sizes)):
        from backend.agents.memo_watcher import _wait_for_stable_size
        assert await _wait_for_stable_size("memo.m4a", stable_checks=2, timeout=999) is True


@pytest.mark.asyncio
async def test_wait_for_stable_size_timeout_proceeds(caplog):
    # Always-growing file: never settles, returns False after timeout with a WARNING.
    counter = {"n": 0}

    def growing(_p):
        counter["n"] += 100
        return counter["n"]

    # loop.time() advances past the deadline quickly.
    times = iter([0.0] + [i * 0.5 for i in range(1, 200)])

    class FakeLoop:
        def time(self):
            return next(times)

    with patch("backend.agents.memo_watcher.asyncio.sleep", new=AsyncMock()), \
         patch("backend.agents.memo_watcher.asyncio.get_event_loop", return_value=FakeLoop()), \
         patch("backend.agents.memo_watcher.os.path.getsize", side_effect=growing), \
         caplog.at_level(logging.WARNING):
        from backend.agents.memo_watcher import _wait_for_stable_size
        result = await _wait_for_stable_size("memo.m4a", stable_checks=2, timeout=2.0)
    assert result is False
    assert any("still growing" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_wait_for_stable_size_zero_byte_not_stable():
    # A 0-byte file repeated is never 'stable'; it should keep polling until timeout.
    times = iter([0.0] + [i * 0.5 for i in range(1, 200)])

    class FakeLoop:
        def time(self):
            return next(times)

    with patch("backend.agents.memo_watcher.asyncio.sleep", new=AsyncMock()), \
         patch("backend.agents.memo_watcher.asyncio.get_event_loop", return_value=FakeLoop()), \
         patch("backend.agents.memo_watcher.os.path.getsize", return_value=0):
        from backend.agents.memo_watcher import _wait_for_stable_size
        assert await _wait_for_stable_size("memo.m4a", stable_checks=2, timeout=2.0) is False


@pytest.mark.asyncio
async def test_wait_for_stable_size_missing_file():
    with patch("backend.agents.memo_watcher.asyncio.sleep", new=AsyncMock()), \
         patch("backend.agents.memo_watcher.os.path.getsize", side_effect=FileNotFoundError):
        from backend.agents.memo_watcher import _wait_for_stable_size
        assert await _wait_for_stable_size("gone.m4a") is False


@pytest.mark.asyncio
async def test_debounced_process_skips_missing_file():
    # If the file settled-False AND no longer exists, _process_memo must not run.
    with patch("backend.agents.memo_watcher._wait_for_stable_size", new=AsyncMock(return_value=False)), \
         patch("backend.agents.memo_watcher.os.path.exists", return_value=False), \
         patch("backend.agents.memo_watcher._process_memo", new=AsyncMock()) as mock_proc:
        from backend.agents.memo_watcher import _debounced_process
        await _debounced_process("gone.m4a")
        mock_proc.assert_not_called()


@pytest.mark.asyncio
async def test_debounced_process_proceeds_on_timeout():
    # settled-False but file still exists (timeout case) -> process anyway.
    with patch("backend.agents.memo_watcher._wait_for_stable_size", new=AsyncMock(return_value=False)), \
         patch("backend.agents.memo_watcher.os.path.exists", return_value=True), \
         patch("backend.agents.memo_watcher._process_memo", new=AsyncMock()) as mock_proc:
        from backend.agents.memo_watcher import _debounced_process
        await _debounced_process("big.m4a")
        mock_proc.assert_awaited_once_with("big.m4a")
