"""Wiki ingestion watcher — fires on every new .md file in Brain/raw/.

When a session note (or any .md) lands in the vault's Brain/raw/ folder,
this module extracts key facts/decisions via Haiku, classifies each item
to a wiki page, and appends a dated section to that page. Creates the
wiki page if it doesn't exist. Unmatched items go to Brain/wiki/Inbox.md.

Two Haiku calls per file: extract + classify. Never rewrites existing wiki
content — append-only. Tracks processed files in Brain/.wiki_ingest_state.json.
"""

import asyncio
import json
import logging
import os
from datetime import datetime, date
from pathlib import Path

logger = logging.getLogger(__name__)

_observer = None
_loop = None

_LEDGER_NAME = ".wiki_ingest_state.json"
_WIKI_DIR = "Brain/wiki"
_INBOX = "Brain/wiki/Inbox.md"


# ---------------------------------------------------------------------------
# Ledger helpers (sync — called via to_thread)
# ---------------------------------------------------------------------------

def _ledger_path(vault: Path) -> Path:
    return vault / "Brain" / _LEDGER_NAME


def _load_ledger(vault: Path) -> set:
    p = _ledger_path(vault)
    try:
        return set(json.loads(p.read_text(encoding="utf-8")))
    except Exception:
        return set()


def _save_ledger(vault: Path, seen: set) -> None:
    p = _ledger_path(vault)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(sorted(seen), indent=2), encoding="utf-8")


def _known_wikis(vault: Path) -> list[str]:
    """Return existing wiki page stems (e.g. ['AdGuard', 'Hermes'])."""
    wiki_dir = vault / _WIKI_DIR
    if not wiki_dir.exists():
        return []
    return [f.stem for f in wiki_dir.glob("*.md")]


# ---------------------------------------------------------------------------
# Core ingestion (async)
# ---------------------------------------------------------------------------

async def ingest_file(file_path: str) -> dict:
    """Extract and route one session file to the correct wiki pages.

    Returns {"file", "items", "wikis_touched"} or {"file", "skipped", "reason"}.
    Never raises.
    """
    try:
        from backend.agents.router import haiku
        from backend.config import get_settings
        from backend.integrations.obsidian import _post_raw

        settings = get_settings()
        vault = Path(settings.obsidian_vault_path)
        path = Path(file_path)

        # --- dedup ---
        key = path.name
        seen = await asyncio.to_thread(_load_ledger, vault)
        if key in seen:
            return {"file": key, "skipped": True, "reason": "already_processed"}

        content = path.read_text(encoding="utf-8", errors="ignore").strip()
        if not content:
            return {"file": key, "skipped": True, "reason": "empty"}

        today = date.today().isoformat()
        known = await asyncio.to_thread(_known_wikis, vault)

        # --- Haiku call 1: extract ---
        extract_prompt = (
            "Extract the key facts, decisions, and updates from this session note. "
            "Return a JSON array only. Each element: "
            '{{"topic_hint": "short topic word", "bullet": "one clear sentence"}}. '
            "Only stable, durable information — not transient status or ephemeral tasks. "
            f"Return [] if nothing durable.\n\nSession note:\n{content[:6000]}"
        )
        raw_extract = await haiku(extract_prompt)
        items = _parse_json_array(raw_extract)
        if not items:
            seen.add(key)
            await asyncio.to_thread(_save_ledger, vault, seen)
            return {"file": key, "items": 0, "wikis_touched": []}

        # --- Haiku call 2: classify ---
        # Derive a filename hint by stripping common noise tokens from the stem
        _stem = path.stem
        for _tok in ("session", "Session", str(date.today()), "-", "_"):
            _stem = _stem.replace(_tok, " ")
        filename_hint = _stem.strip()
        _hint_line = (
            f"The session file is named '{path.name}' — strongly prefer wiki '{filename_hint}' "
            f"unless an item clearly belongs elsewhere.\n"
            if filename_hint else ""
        )
        classify_prompt = (
            f"You have a list of items extracted from a session note and a list of existing wiki pages.\n"
            f"{_hint_line}"
            f"Existing wiki pages: {known}\n"
            f"For each item, return the best matching wiki page name (exact existing name or a new "
            f"PascalCase name if none fit). Return a JSON array of strings, one per item, same order.\n\n"
            f"Items:\n{json.dumps([i.get('bullet','') for i in items])}"
        )
        raw_classify = await haiku(classify_prompt)
        targets = _parse_json_array(raw_classify)

        # Pad/trim targets to match items length
        while len(targets) < len(items):
            targets.append(None)
        targets = targets[:len(items)]

        # --- Group and append ---
        groups: dict[str, list[str]] = {}
        for item, target in zip(items, targets):
            wiki = str(target).strip() if target else None
            if not wiki or wiki.lower() in ("none", "null", ""):
                wiki = "Inbox"
            groups.setdefault(wiki, []).append(item.get("bullet", ""))

        wikis_touched = []
        for wiki_name, bullets in groups.items():
            section = f"\n## {today} — from {path.name}\n"
            section += "\n".join(f"- {b}" for b in bullets) + "\n"
            filename = f"{_WIKI_DIR}/{wiki_name}.md"
            await _post_raw(section, filename=filename)
            wikis_touched.append(wiki_name)
            logger.info(f"wiki_ingest: appended {len(bullets)} items to {wiki_name}.md")

        seen.add(key)
        await asyncio.to_thread(_save_ledger, vault, seen)
        return {"file": key, "items": len(items), "wikis_touched": wikis_touched}

    except Exception as e:
        logger.warning(f"wiki_ingest: error processing {file_path}: {e}")
        return {"file": str(file_path), "error": str(e)}


async def run_all_unprocessed() -> dict:
    """Batch-ingest every .md in Brain/raw/ not yet in the ledger. Called by scheduler at 01:55."""
    try:
        from backend.config import get_settings
        settings = get_settings()
        vault = Path(settings.obsidian_vault_path)
        raw_dir = vault / "Brain" / "raw"
        if not raw_dir.exists():
            return {"processed": 0, "skipped": 0, "results": []}

        seen = await asyncio.to_thread(_load_ledger, vault)
        files = sorted(raw_dir.glob("*.md"))
        results, processed, skipped = [], 0, 0
        for f in files:
            if f.name in seen:
                skipped += 1
                continue
            res = await ingest_file(str(f))
            results.append(res)
            processed += 1
        logger.info(f"wiki_ingest batch done: {processed} processed, {skipped} skipped")
        return {"processed": processed, "skipped": skipped, "results": results}
    except Exception as e:
        logger.warning(f"wiki_ingest batch error: {e}")
        return {"error": str(e)}


def _parse_json_array(raw: str) -> list:
    try:
        start = raw.find("[")
        end = raw.rfind("]") + 1
        if start >= 0 and end > start:
            parsed = json.loads(raw[start:end])
            if isinstance(parsed, list):
                return parsed
    except Exception:
        pass
    return []


# ---------------------------------------------------------------------------
# Watchdog handler
# ---------------------------------------------------------------------------

async def _wait_stable(path: str, poll: float = 0.5, checks: int = 2, timeout: float = 15.0) -> bool:
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    last_size = -1
    steady = 0
    while True:
        await asyncio.sleep(poll)
        try:
            size = os.path.getsize(path)
        except FileNotFoundError:
            return False
        if size > 0 and size == last_size:
            steady += 1
            if steady >= checks:
                return True
        else:
            steady = 0
        last_size = size
        if loop.time() >= deadline:
            return True  # process anyway


async def _handle_new_file(path: str) -> None:
    await _wait_stable(path)
    if not os.path.exists(path):
        return
    result = await ingest_file(path)
    logger.info(f"wiki_ingest result: {result}")


class _WikiHandler:
    def __init__(self, loop: asyncio.AbstractEventLoop):
        self.loop = loop

    def dispatch(self, event):
        if event.is_directory:
            return
        from watchdog.events import FileCreatedEvent
        if isinstance(event, FileCreatedEvent) and event.src_path.lower().endswith(".md"):
            asyncio.run_coroutine_threadsafe(
                _handle_new_file(event.src_path), self.loop
            )


def start_wiki_watcher(watch_folder: str, loop: asyncio.AbstractEventLoop) -> None:
    """Start watchdog on Brain/raw/. Call from a daemon OS thread (same pattern as memo_watcher)."""
    global _observer, _loop
    _loop = loop
    watch_path = Path(watch_folder)
    watch_path.mkdir(parents=True, exist_ok=True)

    from watchdog.observers import Observer
    handler = _WikiHandler(loop)
    obs = Observer()
    obs.schedule(handler, str(watch_path), recursive=False)
    obs.start()
    _observer = obs
    logger.info(f"Wiki ingest watcher started on {watch_folder}")


async def stop_wiki_watcher() -> None:
    global _observer
    if _observer and _observer.is_alive():
        _observer.stop()
        _observer.join()
        logger.info("Wiki ingest watcher stopped")
