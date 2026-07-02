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
import re
from datetime import datetime, date
from pathlib import Path

logger = logging.getLogger(__name__)

_observer = None
_loop = None

_LEDGER_NAME = ".wiki_ingest_state.json"
_WIKI_DIR = "Brain/wiki"
_INBOX = "Brain/wiki/Inbox.md"

# Session notes are never > 50 KB. Anything larger is a reference document that
# landed in Brain/raw by mistake — mark it seen so it won't retry.
MAX_RAW_FILE_BYTES = 50_000

# Any date token (2026-06-25, 2026_06_25, 20260625, optional trailing letter).
_DATE_TOKEN = re.compile(r"\b\d{4}[-_]?\d{2}[-_]?\d{2}[a-z]?\b")

# Noise words/separators stripped from a filename stem to leave a topic hint.
_NOISE_TOKENS = ("session", "save", "note", "notes", "log", "draft")


def _norm(s: str) -> str:
    """Lowercase and strip everything but [a-z0-9] — for comparing page names.

    So 'CWI-AI', 'CWI AI', and 'cwiai' all normalize to 'cwiai', and a hint
    built from a hyphenated filename can still match a hyphenated page stem.
    """
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _filename_hint(stem: str) -> str:
    """Strip dates + noise words from a filename stem, leaving topic words.

    'nexus-session-2026-06-25b-ha-cover-lock-fix' -> 'nexus ha cover lock fix'
    'NEXUS-save-2026-06-24'                        -> 'NEXUS'
    '2026-06-28'                                   -> ''  (daily file, no topic)
    """
    s = _DATE_TOKEN.sub(" ", stem)
    s = re.sub(r"[-_]", " ", s)
    words = [w for w in s.split() if w.lower() not in _NOISE_TOKENS]
    return " ".join(words)


def _match_existing_page(hint: str, known: list[str]) -> str | None:
    """Map a topic hint to an existing wiki page via shortest-prefix match.

    Joins hint words shortest-first ('cwi', 'cwiai', ...) and compares the
    normalized form against each normalized page stem, so 'CWI AI redesign'
    matches the existing 'CWI-AI' page instead of spawning a new one.
    Returns the page stem if matched, else None.
    """
    words = hint.lower().split()
    norm_known = {_norm(p): p for p in known}
    for n in range(1, len(words) + 1):
        prefix = _norm("".join(words[:n]))
        if prefix in norm_known:
            return norm_known[prefix]
    return None


def _clean_page_name(target) -> str:
    """Normalize a classify target into a safe bare page stem.

    Strips path separators (a target like 'wiki/Foo' must not escape the dir),
    a trailing '.md' (so 'NEXUS.md' doesn't become 'NEXUS.md.md'), and maps
    blank/none/null to the Inbox catch-all.
    """
    name = str(target).strip() if target else ""
    name = name.replace("\\", "/").rsplit("/", 1)[-1]  # last path segment only
    if name.lower().endswith(".md"):
        name = name[:-3]
    name = name.strip()
    if not name or name.lower() in ("none", "null"):
        return "Inbox"
    return name


def _looks_like_reference_doc(content: str) -> bool:
    """Heuristic: True if content looks like a structured reference document
    (build guide, manual) rather than a session note.

    Session notes are short and bullet/prose heavy. Reference docs have many
    markdown headers. Flag anything with >= 8 '#' headers as a reference doc
    so it's skipped instead of fragmenting the wiki.
    """
    headers = sum(1 for ln in content.splitlines() if ln.lstrip().startswith("#"))
    return headers >= 8


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


def _append_text(page: Path, section: str) -> None:
    """Append a section to a wiki page on disk (creates it if absent). UTF-8."""
    with page.open("a", encoding="utf-8") as f:
        f.write(section)


def _write_text(page: Path, content: str) -> None:
    """Write (overwrite/create) a wiki page on disk verbatim. UTF-8.

    Unlike _append_text, this replaces the file's contents. Used for verbatim
    reference-doc imports where we want the exact text, not an appended section.
    """
    page.write_text(content, encoding="utf-8")


def _write_bytes(path: Path, data: bytes) -> None:
    """Write raw bytes to disk (creates parent-less; caller ensures dir)."""
    path.write_bytes(data)


# ---------------------------------------------------------------------------
# Reference-doc verbatim import helpers
# ---------------------------------------------------------------------------

# ![alt](data:image/png;base64,BLOB) — the blob is one long base64 string that
# may contain whitespace/newlines. We capture alt and the raw blob separately.
_B64_IMG = re.compile(
    r"!\[(?P<alt>[^\]]*)\]\(\s*data:image/png;base64,\s*(?P<blob>[A-Za-z0-9+/=\s]+?)\s*\)",
    re.MULTILINE,
)


def _slugify(text: str) -> str:
    """Lowercase, replace every run of non-alnum chars with a single '_'.

    'Patio Layout!' -> 'patio_layout'. Blank -> '' (caller supplies a counter).
    """
    s = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return s


def _extract_reference_images(content: str, counter_start: int = 1):
    """Pull base64 PNGs out of content.

    Returns (new_content, images) where images is a list of (filename, bytes).
    Each ![alt](data:image/png;base64,BLOB) is replaced in new_content with an
    Obsidian embed ![[filename.png]]. Filenames are slugged from alt text (or
    'image_{n}' when blank) and deduplicated by appending _1, _2, ... on collision.
    """
    import base64

    images: list[tuple[str, bytes]] = []
    used: dict[str, int] = {}
    counter = {"n": counter_start}

    def _repl(m: "re.Match") -> str:
        alt = m.group("alt").strip()
        blob = re.sub(r"\s+", "", m.group("blob"))
        try:
            data = base64.b64decode(blob, validate=False)
        except Exception:
            # Not decodable — leave the original text untouched.
            return m.group(0)

        base = _slugify(alt) or f"image_{counter['n']}"
        counter["n"] += 1
        # Deduplicate: first use keeps the bare name; collisions get _1, _2, ...
        if base in used:
            used[base] += 1
            name = f"{base}_{used[base]}"
        else:
            used[base] = 0
            name = base
        filename = f"{name}.png"
        images.append((filename, data))
        return f"![[{filename}]]"

    new_content = _B64_IMG.sub(_repl, content)
    return new_content, images


async def _import_reference_doc(vault: Path, path: Path, content: str) -> dict:
    """Verbatim import of a large/reference doc: extract images, write full text.

    No Haiku. Decodes embedded base64 PNGs to Brain/wiki/{filename}.png, rewrites
    the refs to Obsidian embeds, writes the cleaned full text to Brain/wiki/{stem}.md,
    and moves the source into Brain/wiki/processed/. Returns a result dict.
    """
    wiki_dir = vault / "Brain" / "wiki"
    await asyncio.to_thread(wiki_dir.mkdir, parents=True, exist_ok=True)

    cleaned, images = _extract_reference_images(content)

    for filename, data in images:
        img_path = wiki_dir / filename
        await asyncio.to_thread(_write_bytes, img_path, data)

    page = wiki_dir / f"{path.stem}.md"
    await asyncio.to_thread(_write_text, page, cleaned)
    logger.info(
        "wiki_ingest: imported reference doc %s → %s.md verbatim (%d image(s))",
        path.name, path.stem, len(images),
    )

    # Move the source into processed/ (mirror the normal flow).
    processed_dir = wiki_dir / "processed"
    await asyncio.to_thread(processed_dir.mkdir, parents=True, exist_ok=True)
    dest = processed_dir / path.name
    try:
        await asyncio.to_thread(path.rename, dest)
        logger.info("wiki_ingest: moved %s → processed/", path.name)
    except Exception as e:
        logger.warning("wiki_ingest: could not move %s to processed/: %s", path.name, e)

    return {
        "file": path.name,
        "reference_doc_imported": True,
        "wiki_page": f"{path.stem}.md",
        "images": len(images),
    }


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

        settings = get_settings()
        vault = Path(settings.obsidian_vault_path)
        path = Path(file_path)

        # --- dedup ---
        key = path.name
        seen = await asyncio.to_thread(_load_ledger, vault)
        if key in seen:
            return {"file": key, "skipped": True, "reason": "already_processed"}

        # --- size guard: skip reference docs, build guides, etc. ---
        try:
            file_bytes = path.stat().st_size
        except Exception:
            file_bytes = 0
        if file_bytes > MAX_RAW_FILE_BYTES:
            # Too large to be a session note. Don't summarize/strip it — import
            # the full text (and any embedded base64 images) verbatim to the wiki.
            content = await asyncio.to_thread(
                lambda: path.read_text(encoding="utf-8", errors="ignore").strip()
            )
            if not content:
                seen.add(key)
                await asyncio.to_thread(_save_ledger, vault, seen)
                return {"file": key, "skipped": True, "reason": "empty"}
            result = await _import_reference_doc(vault, path, content)
            result["bytes"] = file_bytes
            seen.add(key)
            await asyncio.to_thread(_save_ledger, vault, seen)
            return result

        content = await asyncio.to_thread(
            lambda: path.read_text(encoding="utf-8", errors="ignore").strip()
        )
        if not content:
            return {"file": key, "skipped": True, "reason": "empty"}

        # --- content guard: structured reference docs under the size cap ---
        # A 40 KB manual passes the byte limit but is still not a session note.
        # Import it verbatim (full text + extracted images) instead of summarizing.
        if _looks_like_reference_doc(content):
            result = await _import_reference_doc(vault, path, content)
            seen.add(key)
            await asyncio.to_thread(_save_ledger, vault, seen)
            return result

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

        # --- classify ---
        # Strip dates + noise from the stem to get a topic hint, then match it
        # against existing wiki pages so session files append to the canonical
        # page instead of spawning new date-stamped ones.
        filename_hint = _filename_hint(path.stem)
        matched_page = _match_existing_page(filename_hint, known)

        if matched_page:
            # The filename maps to an existing page with confidence — route ALL
            # items there directly. No Haiku call, so Haiku can't fragment it.
            targets = [matched_page] * len(items)
        else:
            # No confident filename match — ask Haiku, biasing toward existing pages.
            _hint_line = (
                f"The session file is named '{path.name}'. "
                f"Prefer '{filename_hint}' as the target wiki page for all items from this "
                f"file unless an item clearly belongs to a different existing page.\n"
                if filename_hint else ""
            )
            classify_prompt = (
                f"You have a list of items extracted from a session note and a list of existing wiki pages.\n"
                f"{_hint_line}"
                f"Existing wiki pages: {known}\n"
                f"For each item, return the wiki page name it belongs to. Rules:\n"
                f"1. STRONGLY prefer an EXACT existing page name from the list above — reuse, don't fragment.\n"
                f"2. Only invent a NEW PascalCase name if NO existing page is even loosely related.\n"
                f"3. Never create a near-duplicate of an existing page (e.g. if 'AdGuard' exists, never return 'AdGuard-Status' or 'AdGuardDNS').\n"
                f"Return a JSON array of strings, one per item, same order.\n\n"
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
            wiki = _clean_page_name(target)
            groups.setdefault(wiki, []).append(item.get("bullet", ""))

        # Append directly to the wiki page on disk. Brain MCP /raw IGNORES the
        # subfolder in the filename and dumps everything into Brain/raw/, so we
        # write the file ourselves to land in Brain/wiki/.
        wiki_dir = vault / "Brain" / "wiki"
        wiki_dir.mkdir(parents=True, exist_ok=True)
        wikis_touched = []
        for wiki_name, bullets in groups.items():
            section = f"\n## {today} — from {path.name}\n"
            section += "\n".join(f"- {b}" for b in bullets) + "\n"
            page = wiki_dir / f"{wiki_name}.md"
            await asyncio.to_thread(_append_text, page, section)
            wikis_touched.append(wiki_name)
            logger.info(f"wiki_ingest: appended {len(bullets)} items to {wiki_name}.md")

        seen.add(key)
        await asyncio.to_thread(_save_ledger, vault, seen)

        # Move processed file to Brain/wiki/processed/ so it's archived, not deleted
        from backend.config import get_settings as _gs
        _vault = Path(_gs().obsidian_vault_path)
        processed_dir = _vault / "Brain" / "wiki" / "processed"
        processed_dir.mkdir(parents=True, exist_ok=True)
        dest = processed_dir / path.name
        await asyncio.to_thread(path.rename, dest)
        logger.info(f"wiki_ingest: moved {path.name} → processed/")

        return {"file": key, "items": len(items), "wikis_touched": wikis_touched}

    except Exception as e:
        logger.warning(f"wiki_ingest: error processing {file_path}: {e}")
        return {"file": str(file_path), "error": str(e)}


_DATE_PAT = re.compile(r"^\d{4}-\d{2}-\d{2}")
# A session/save log anywhere in the stem (not just date-prefixed) — e.g.
# 'nexus-session-2026-06-25b' — is the exact shape that fragments the wiki.
_SESSION_NAME_PAT = re.compile(r"(?:^|[-_ ])(session|save)(?:[-_ ]|$)", re.IGNORECASE)


def _is_session_file(f: Path) -> bool:
    """True if a wiki file looks like a session log rather than a knowledge page."""
    if _DATE_PAT.match(f.stem) or _SESSION_NAME_PAT.search(f.stem):
        return True
    try:
        head = f.read_text(encoding="utf-8", errors="ignore")[:400]
        return "type: daily-session" in head or "consolidated_sessions" in head
    except Exception:
        return False


async def run_all_unprocessed() -> dict:
    """Batch-ingest unprocessed .md files from Brain/raw/ AND Brain/wiki/ session logs.

    Brain/raw/  — every .md file is a candidate.
    Brain/wiki/ — only date-named files or files with daily-session frontmatter.
    Called by scheduler at 01:55.
    """
    try:
        from backend.config import get_settings
        settings = get_settings()
        vault = Path(settings.obsidian_vault_path)
        raw_dir = vault / "Brain" / "raw"
        wiki_dir = vault / "Brain" / "wiki"

        seen = await asyncio.to_thread(_load_ledger, vault)

        # Brain/raw/ — all .md files
        files = sorted(raw_dir.glob("*.md")) if raw_dir.exists() else []

        # Brain/wiki/ — only session-shaped files (not the processed subfolder)
        if wiki_dir.exists():
            for f in sorted(wiki_dir.glob("*.md")):
                if f.parent.name != "processed" and _is_session_file(f):
                    files.append(f)
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


_CLUSTER_TAIL = re.compile(r"[-_ ]?(?:pass)?\d+[a-z]?$", re.IGNORECASE)


def _fragmentation_report_sync(vault: Path) -> list[str]:
    """Read-only: group wiki pages by shared prefix; flag clusters of >=5 small files.

    Never merges or deletes — returns human-readable warning lines only.
    """
    wiki_dir = vault / "Brain" / "wiki"
    if not wiki_dir.exists():
        return []
    clusters: dict[str, list[str]] = {}
    for f in wiki_dir.glob("*.md"):
        if f.parent.name == "processed":
            continue
        # cluster key = stem with trailing number/pass token stripped
        prefix = _CLUSTER_TAIL.sub("", f.stem).rstrip("-_ ")
        if not prefix:
            continue
        # only count small files (stubs) as fragmentation candidates
        try:
            if f.stat().st_size <= 4096:
                clusters.setdefault(prefix, []).append(f.name)
        except Exception:
            continue
    return [
        f"Possible fragmentation: {prefix}-* ({len(names)} small files) — review for consolidation."
        for prefix, names in sorted(clusters.items())
        if len(names) >= 5
    ]


async def weekly_fragmentation_report() -> dict:
    """Append a fragmentation warning to Inbox.md if any cluster of >=5 stubs exists.

    Read-only audit — never merges/deletes. Called weekly by the scheduler.
    """
    try:
        from backend.config import get_settings
        vault = Path(get_settings().obsidian_vault_path)
        lines = await asyncio.to_thread(_fragmentation_report_sync, vault)
        if not lines:
            return {"clusters": 0}
        today = date.today().isoformat()
        section = f"\n## {today} — Fragmentation report\n" + "\n".join(f"- {ln}" for ln in lines) + "\n"
        inbox = vault / "Brain" / "wiki" / "Inbox.md"
        inbox.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(_append_text, inbox, section)
        logger.info(f"wiki fragmentation report: {len(lines)} clusters flagged to Inbox.md")
        return {"clusters": len(lines)}
    except Exception as e:
        logger.warning(f"fragmentation report error: {e}")
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
