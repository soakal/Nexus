import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

# Eagerly trigger the heavy watchdog C-extension import at module import time.
# On Windows this import can hold the GIL for 10-25s. Doing it here (during app
# import, before uvicorn binds/serves its socket) guarantees the GIL hold cannot
# collide with a live IOCP accept socket and produce WinError 64. The thread-based
# start in start_watcher_blocking() is the primary fix; this is defense in depth.
try:  # pragma: no cover - import side effect only
    import watchdog.observers  # noqa: F401
except Exception as _e:  # noqa: BLE001
    logging.getLogger(__name__).warning(f"watchdog preimport skipped: {_e}")

_observer = None
_loop = None


async def _process_memo(file_path: str) -> None:
    try:
        from sqlmodel import Session

        from backend.agents.router import opus
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

        raw = await opus(cleanup_prompt)
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
                asyncio.run_coroutine_threadsafe(
                    _debounced_process(path), self.loop
                )


_pending: dict = {}


async def _debounced_process(path: str) -> None:
    await asyncio.sleep(2)
    if path in _pending:
        del _pending[path]
    await _process_memo(path)


def start_watcher_blocking(watch_folder: str, loop: asyncio.AbstractEventLoop) -> None:
    """Start the watchdog observer. This MUST be called from a plain OS thread
    (e.g. threading.Thread), NOT scheduled on the asyncio event loop.

    The heavy watchdog C-extension import (watchdog.observers.read_directory_changes
    on Windows) holds the GIL for 10-25s. If this ran on the loop thread, or if the
    loop were awaiting it, the whole event loop would freeze long enough for the
    Windows IOCP accept socket to die with WinError 64. Running on a separate OS
    thread lets the GIL be released back to the loop between bytecode ops, so the
    loop keeps servicing connections while the import grinds.
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
