import json
import logging
from unittest.mock import AsyncMock, MagicMock, patch

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


# ------------------------------------------------------------- _process_memo

@pytest.mark.asyncio
async def test_process_memo_happy_path(tmp_path):
    memo = tmp_path / "memo.m4a"
    memo.write_bytes(b"audio")

    cleanup_json = json.dumps({
        "title": "Buy groceries",
        "cleaned": "Remember to buy milk and eggs.",
        "action_items": ["Buy milk", "Buy eggs"],
        "tags": ["errands", "home"],
    })
    session = MagicMock()
    session_cm = MagicMock()
    session_cm.__enter__ = MagicMock(return_value=session)
    session_cm.__exit__ = MagicMock(return_value=False)

    with patch("backend.agents.voice.transcribe", new=AsyncMock(return_value="raw text")), \
         patch("backend.agents.router.sonnet", new=AsyncMock(return_value=f"noise {cleanup_json} tail")), \
         patch("backend.integrations.obsidian.create_note",
               new=AsyncMock(return_value="NEXUS/Voice Memos/Buy groceries.md")) as mock_create, \
         patch("backend.database.engine"), \
         patch("sqlmodel.Session", return_value=session_cm):
        from backend.agents.memo_watcher import _process_memo
        await _process_memo(str(memo))

    # Note content assembled from the parsed JSON
    _, kwargs = mock_create.call_args
    assert kwargs["title"] == "Buy groceries"
    assert kwargs["folder"] == "NEXUS/Voice Memos"
    assert "Remember to buy milk and eggs." in kwargs["content"]
    assert "- [ ] Buy milk" in kwargs["content"]
    assert "#errands" in kwargs["content"]

    # File moved into processed/ and a MemoLog row written
    assert not memo.exists()
    assert (tmp_path / "processed" / "memo.m4a").exists()
    session.add.assert_called_once()
    session.commit.assert_called_once()


@pytest.mark.asyncio
async def test_process_memo_swallows_errors(caplog):
    # transcribe raising must not propagate; it is logged and the memo is left alone.
    with patch("backend.agents.voice.transcribe",
               new=AsyncMock(side_effect=Exception("whisper down"))), \
         caplog.at_level(logging.ERROR):
        from backend.agents.memo_watcher import _process_memo
        await _process_memo("/does/not/exist.m4a")
    assert any("Memo processing error" in r.message for r in caplog.records)


# ------------------------------------------------------------- _MemoHandler

def test_memo_handler_dispatches_audio_file():
    from watchdog.events import FileCreatedEvent

    from backend.agents.memo_watcher import _MemoHandler
    handler = _MemoHandler(loop="fake-loop")
    event = FileCreatedEvent("/watch/recording.m4a")

    with patch("backend.agents.memo_watcher.asyncio.run_coroutine_threadsafe") as sched, \
         patch("backend.agents.memo_watcher._debounced_process") as proc:
        handler.dispatch(event)
    sched.assert_called_once()
    # scheduled with the debounced coroutine and the handler's loop
    proc.assert_called_once_with("/watch/recording.m4a")
    assert sched.call_args[0][1] == "fake-loop"


def test_memo_handler_ignores_non_audio_file():
    from watchdog.events import FileCreatedEvent

    from backend.agents.memo_watcher import _MemoHandler
    handler = _MemoHandler(loop="fake-loop")
    event = FileCreatedEvent("/watch/notes.txt")

    with patch("backend.agents.memo_watcher.asyncio.run_coroutine_threadsafe") as sched:
        handler.dispatch(event)
    sched.assert_not_called()


def test_memo_handler_ignores_directory_event():
    from watchdog.events import DirCreatedEvent

    from backend.agents.memo_watcher import _MemoHandler
    handler = _MemoHandler(loop="fake-loop")
    event = DirCreatedEvent("/watch/subdir")

    with patch("backend.agents.memo_watcher.asyncio.run_coroutine_threadsafe") as sched:
        handler.dispatch(event)
    sched.assert_not_called()


# -------------------------------------------------------------- stop_watcher

@pytest.mark.asyncio
async def test_stop_watcher_stops_running_observer():
    import backend.agents.memo_watcher as mw
    obs = MagicMock()
    obs.is_alive.return_value = True
    with patch.object(mw, "_observer", obs):
        await mw.stop_watcher()
    obs.stop.assert_called_once()
    obs.join.assert_called_once()


@pytest.mark.asyncio
async def test_stop_watcher_noop_when_no_observer():
    import backend.agents.memo_watcher as mw
    with patch.object(mw, "_observer", None):
        # Must not raise when there is no live observer.
        await mw.stop_watcher()


# ------------------------------------------------------ start_watcher_blocking

def test_start_watcher_blocking_creates_dirs_and_starts_observer(tmp_path):
    import backend.agents.memo_watcher as mw
    watch = tmp_path / "memos"
    fake_observer = MagicMock()

    with patch("watchdog.observers.Observer", return_value=fake_observer):
        mw.start_watcher_blocking(str(watch), loop="fake-loop")

    # watch folder + processed/ subdir created
    assert watch.is_dir()
    assert (watch / "processed").is_dir()
    # observer scheduled + started, module state wired up
    fake_observer.schedule.assert_called_once()
    fake_observer.start.assert_called_once()
    assert mw._observer is fake_observer
    assert mw._loop == "fake-loop"
