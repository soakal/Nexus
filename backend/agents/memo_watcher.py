import asyncio
import json
import logging
import os
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

_observer = None
_loop = None


async def _process_memo(file_path: str) -> None:
    try:
        from sqlmodel import Session

        from backend.agents.router import sonnet
        from backend.agents.voice import transcribe
        from backend.database import MemoLog, engine
        from backend.integrations.obsidian import create_note

        logger.info(f"Processing memo: {file_path}")
        transcript = await transcribe(file_path)

        cleanup_prompt = f"""You are processing a voice memo transcript. Clean it up and structure it.

RAW TRANSCRIPT:
{transcript}

Return JSON only:
{{
  "title": "short descriptive title (max 8 words)",
  "cleaned": "cleaned transcript with filler words removed, punctuation added",
  "action_items": ["action item 1"],
  "tags": ["tag1"]
}}"""

        raw = await sonnet(cleanup_prompt, label="memo_cleanup")
        start = raw.find("{")
        end = raw.rfind("}") + 1
        data = json.loads(raw[start:end])

        title = data.get("title", "Voice Memo")
        cleaned = data.get("cleaned", transcript)
        action_items = data.get("action_items", [])
        tags = data.get("tags", [])

        action_items_md = "\n".join([f"- [ ] {item}" for item in action_items]) if action_items else "- [ ] (none)"
        tags_str = " ".join([f"#{t}" for t in tags])
        ts = datetime.now().strftime("%Y-%m-%d %H:%M")

        note_content = f"""# {title}

*Recorded: {ts} | Transcribed by NEXUS*
Tags: {tags_str}

## Transcript
{cleaned}

## Action Items
{action_items_md}
"""

        obsidian_path = await create_note(title=title, content=note_content, folder="NEXUS/Voice Memos")
        logger.info(f"Memo note created: {obsidian_path}")

        # Move to processed
        src = Path(file_path)
        processed_dir = src.parent / "processed"
        processed_dir.mkdir(exist_ok=True)
        src.rename(processed_dir / src.name)

        # Log to DB
        with Session(engine) as session:
            session.add(MemoLog(filename=src.name, title=title, obsidian_path=obsidian_path))
            session.commit()

    except Exception as e:
        logger.error(f"Memo processing error for {file_path}: {e}")


class _MemoHandler:
    def __init__(self, loop):
        self.loop = loop

    def dispatch(self, event):
        if event.is_directory:
            return
        from watchdog.events import FileCreatedEvent
        if isinstance(event, FileCreatedEvent):
            path = event.src_path
            if any(path.lower().endswith(ext) for ext in (".m4a", ".wav", ".mp3")):
                try:
                    from backend import activity
                    activity.pulse("loop:memo_watcher", "memo_detected", os.path.basename(path))
                except Exception:
                    pass
                asyncio.run_coroutine_threadsafe(
                    _debounced_process(path), self.loop
                )


async def _wait_for_stable_size(
    path: str,
    *,
    poll_interval: float = 0.5,
    stable_checks: int = 2,
    timeout: float = 30.0,
) -> bool:
    """Wait until a file's size stops changing before it is processed.

    A recorder may still be writing a large .m4a/.wav/.mp3 when the watchdog
    FileCreatedEvent fires; transcribing it then yields a truncated memo. Poll the
    size using async sleeps only — never time.sleep, which would freeze the event
    loop — until it is unchanged for `stable_checks` consecutive reads. (Historical
    note: on Windows this also risked WinError 64, killing the IOCP accept socket;
    not applicable to the Linux inotify observer used here, but the async-sleep-only
    rule still applies.)

    Returns True once the size settles. Returns False on timeout (the caller still
    processes, with a warning, rather than dropping the memo) or if the file vanishes
    mid-wait (e.g. a cancelled recording).
    """
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    last_size = -1
    steady = 0
    while True:
        await asyncio.sleep(poll_interval)
        try:
            size = os.path.getsize(path)
        except FileNotFoundError:
            logger.warning(f"Memo file vanished while waiting for it to settle: {path}")
            return False
        # A zero-byte file (created but not yet written to) is never 'stable'.
        if size > 0 and size == last_size:
            steady += 1
            if steady >= stable_checks:
                return True
        else:
            steady = 0
        last_size = size
        if loop.time() >= deadline:
            logger.warning(
                f"Memo file still growing after {timeout}s; processing anyway: {path}"
            )
            return False


async def _debounced_process(path: str) -> None:
    settled = await _wait_for_stable_size(path)
    if not settled and not os.path.exists(path):
        # File was removed mid-wait (cancelled recording) — nothing to process.
        return
    await _process_memo(path)


def start_watcher_blocking(watch_folder: str, loop: asyncio.AbstractEventLoop) -> None:
    """Start the watchdog observer. This MUST be called from a plain OS thread
    (e.g. threading.Thread), NOT scheduled on the asyncio event loop.

    Historical note: on Windows, the heavy watchdog C-extension import
    (watchdog.observers.read_directory_changes) held the GIL for 10-25s, and if this
    ran on the loop thread — or the loop were awaiting it — the frozen event loop
    could let the Windows IOCP accept socket die with WinError 64. Running on a
    separate OS thread let the GIL be released back to the loop between bytecode
    ops, keeping the loop servicing connections while the import ground on. That
    mechanism doesn't apply here (this deployment uses the Linux inotify observer,
    not read_directory_changes), but the rule still holds: never run this on the
    loop thread.
    """
    global _observer, _loop

    _loop = loop
    watch_path = Path(watch_folder)
    watch_path.mkdir(parents=True, exist_ok=True)
    (watch_path / "processed").mkdir(exist_ok=True)

    from watchdog.observers import Observer  # heavy import, runs on this OS thread
    handler = _MemoHandler(_loop)
    obs = Observer()
    obs.schedule(handler, str(watch_path), recursive=False)
    obs.start()
    _observer = obs
    logger.info(f"Memo watcher started on {watch_folder}")


async def stop_watcher() -> None:
    global _observer
    if _observer and _observer.is_alive():
        _observer.stop()
        _observer.join()
        logger.info("Memo watcher stopped")
