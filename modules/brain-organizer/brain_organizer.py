"""
Brain Organizer — main processing script.

Reads raw intake files from vault/raw/, uses Claude AI to detect topics and
synthesize wiki files, then cleans up and reports via NEXUS's own Telegram bot.

Usage:
    python brain_organizer.py
    python brain_organizer.py --config /path/to/config.json
"""
from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
import math
import os
import re
import shutil
import sys
import time
import threading
import uuid
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

import anthropic
import httpx
from anthropic.types import TextBlock

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

CONFIG_PATH = Path(__file__).parent / "config.json"

# Anthropic errors that warrant a retry before falling back to OpenRouter.
# APIConnectionError (DNS failure, connection refused/reset, TLS error) is the
# canonical transient network error -- it does NOT subclass APIStatusError (no
# .status_code), so without it here it fell through _call_api's except clauses
# entirely: no retry, no OpenRouter fallback, immediate propagation on the
# first attempt. That defeated the whole point of this function for exactly
# the failure mode (a brief 2am network blip) it exists to survive.
_RETRYABLE_ERRORS = (
    anthropic.APITimeoutError,
    anthropic.RateLimitError,
    anthropic.APIConnectionError,
)


# ---------------------------------------------------------------------------
# Config & logging
# ---------------------------------------------------------------------------

def load_config(config_path: Path | None = None) -> dict[str, Any]:
    path = config_path or CONFIG_PATH
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


# Keys with no default that must be present -- the pipeline cannot locate the
# vault or call an API without every one of these.
_CONFIG_REQUIRED: tuple[str, ...] = (
    "vault_path",
    "raw_folder",
    "wiki_folder",
    "meta_folder",
    "backup_folder",
    "logs_folder",
    "processed_file",
    "haiku_model",
    "sonnet_model",
)

# Every optional key brain_organizer.py reads, in one place, with the value
# that matches the real config.json -- not whatever inline literal a given
# config.get(...) call site historically hardcoded (see F4/§4.2: three of
# those literals had already drifted from production's actual config).
_CONFIG_DEFAULTS: dict[str, Any] = {
    "daily_folder": "wiki\\daily",
    "mcp_port": 8765,
    "mcp_host": "0.0.0.0",  # nosec B104 — intentional for Tailscale access, matches mcp_server.py
    "mcp_write_token": "",  # nosec B105 -- default matches mcp_server.py, empty means no auth configured
    "sonnet_max_tokens": 16384,
    "max_file_chars": 50000,
    "catalog_summary_chars": 300,
    "catalog_max_pages_in_prompt": 60,
    "router_catalog_ranking": True,
    "new_page_similarity_threshold": 0.82,
    "large_page_threshold_chars": 20000,
    "route_max_tokens": 1024,
    "max_parallel_files": 4,
    "api_provider": "anthropic",
    "max_file_attempts": 2,
    "backup_retention_days": 30,
}

# Numeric keys with a meaningful valid range, beyond "must be the right type".
_CONFIG_RANGES: dict[str, tuple[float, float]] = {
    "new_page_similarity_threshold": (0.0, 1.0),
    "mcp_port": (1, 65535),
}


def validate_config(config: dict[str, Any]) -> dict[str, Any]:
    """Validate a loaded config dict and merge in defaults for absent optional keys.

    - Missing required key -> raise ValueError naming it. A pipeline that
      cannot find the vault must not run.
    - Unknown key (not required, not in _CONFIG_DEFAULTS) -> log one WARNING
      naming it and keep going. Never raise -- Brian must be able to
      hand-annotate the file, and this is also how a typo'd key (e.g.
      catalog_max_pages_in_promt) gets surfaced instead of silently no-oping.
    - Wrong type or out-of-range numeric value for a known optional key ->
      raise ValueError.
    - Absent optional key -> filled from _CONFIG_DEFAULTS.

    Returns a new merged dict; does not mutate the input.
    """
    logger = logging.getLogger("brain_organizer")

    for key in _CONFIG_REQUIRED:
        if key not in config:
            raise ValueError(f"brain_organizer config is missing required key: {key!r}")

    known_keys = set(_CONFIG_REQUIRED) | set(_CONFIG_DEFAULTS)
    for key in config:
        if key not in known_keys:
            logger.warning(
                "brain_organizer config: unknown key %r (typo, or a key nothing reads)", key
            )

    merged: dict[str, Any] = dict(config)
    for key, default in _CONFIG_DEFAULTS.items():
        if key not in merged:
            merged[key] = default
            continue

        value = merged[key]
        expected_type = type(default)
        if expected_type is bool:
            if not isinstance(value, bool):
                raise ValueError(
                    f"brain_organizer config key {key!r} must be a bool, got {type(value).__name__}"
                )
        elif expected_type in (int, float):
            # bool is a subclass of int -- exclude it explicitly so True/False
            # can't silently pass for a numeric key.
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(
                    f"brain_organizer config key {key!r} must be numeric, got {type(value).__name__}"
                )
        elif not isinstance(value, expected_type):
            raise ValueError(
                f"brain_organizer config key {key!r} must be a {expected_type.__name__}, "
                f"got {type(value).__name__}"
            )

        if key in _CONFIG_RANGES:
            lo, hi = _CONFIG_RANGES[key]
            if not (lo <= value <= hi):
                raise ValueError(
                    f"brain_organizer config key {key!r}={value!r} is out of range [{lo}, {hi}]"
                )

    return merged


def setup_logging(config: dict[str, Any]) -> logging.Logger:
    logs_folder = Path(config["logs_folder"])
    logs_folder.mkdir(parents=True, exist_ok=True)
    log_file = logs_folder / "organizer.log"

    # Windows consoles default to cp1252 which can't encode emoji in log output.
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    fmt = "%(asctime)s [%(levelname)s] %(message)s"
    handlers: list[logging.Handler] = [
        RotatingFileHandler(log_file, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ]
    logging.basicConfig(level=logging.INFO, format=fmt, handlers=handlers, force=True)
    return logging.getLogger("brain_organizer")


# ---------------------------------------------------------------------------
# File helpers
# ---------------------------------------------------------------------------

def _make_temp_path(directory: Path, prefix: str, suffix: str) -> Path:
    """Return a unique temp path without the deprecated tempfile.mktemp."""
    return directory / f"{prefix}{uuid.uuid4().hex}{suffix}"


def compute_sha256(file_path: Path) -> str:
    h = hashlib.sha256()
    with open(file_path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def load_processed(config: dict[str, Any]) -> dict[str, Any]:
    path = Path(config["processed_file"])
    if path.exists():
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    return {}


def save_processed(config: dict[str, Any], processed: dict[str, Any]) -> None:
    """Atomic write via temp-file + os.replace so a kill mid-write never corrupts the ledger."""
    path = Path(config["processed_file"])
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = _make_temp_path(path.parent, ".processed_", ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(processed, fh, indent=2)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Topics registry (_meta/topics-registry.json)
# ---------------------------------------------------------------------------

def update_topics_registry(
    config: dict[str, Any],
    topics: list[tuple[str, Path]],
) -> None:
    """Upsert topic -> RESOLVED wiki file path in _meta/topics-registry.json
    (atomic write). Takes the actual written Path per topic rather than
    reconstructing wiki_folder/{name}.md -- daily notes resolve into
    daily_folder (a subfolder), and reconstructing from wiki_folder alone
    silently wrote a dangling path that pointed at a file that was never
    created there (found live 2026-07-25, see _daily_note_route)."""
    meta_folder = Path(config["vault_path"]) / config["meta_folder"]
    meta_folder.mkdir(parents=True, exist_ok=True)
    registry_path = meta_folder / "topics-registry.json"

    registry: dict[str, str] = {}
    if registry_path.exists():
        try:
            with open(registry_path, encoding="utf-8") as fh:
                registry = json.load(fh)
        except (json.JSONDecodeError, OSError):
            pass

    for topic, path in topics:
        registry[topic] = str(path)

    tmp = _make_temp_path(meta_folder, ".topics-registry_", ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(registry, fh, indent=2, sort_keys=True)
        os.replace(tmp, registry_path)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Vault scanning — respects failure records and max-attempt cap
# ---------------------------------------------------------------------------

def scan_raw_folder(
    config: dict[str, Any],
    processed: dict[str, Any],
) -> list[tuple[Path, str]]:
    raw_folder = Path(config["vault_path"]) / config["raw_folder"]
    raw_folder.mkdir(parents=True, exist_ok=True)
    max_attempts: int = config["max_file_attempts"]
    logger = logging.getLogger("brain_organizer")

    backup_folder = Path(config["vault_path"]) / config["backup_folder"]
    results: list[tuple[Path, str]] = []
    for ext in (".md", ".txt"):
        for f in sorted(raw_folder.rglob(f"*{ext}")):
            if not f.is_file():
                continue
            # Skip anything already inside the backups subfolder
            try:
                f.relative_to(backup_folder)
                continue
            except ValueError:
                pass
            try:
                content = f.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                content = None
            if content is not None and not content.strip():
                logger.warning("Skipping %s — empty or whitespace-only file.", f.name)
                continue
            sha = compute_sha256(f)
            record = processed.get(sha)
            if record is None:
                results.append((f, sha))
            elif record.get("status") == "failed":
                attempts = record.get("attempts", 0)
                if attempts < max_attempts:
                    results.append((f, sha))
                else:
                    logger.warning(
                        "Skipping %s — exceeded max attempts (%d). Move it out of raw/ to re-enable.",
                        f.name, max_attempts,
                    )
            elif "filename" in record and record["filename"] != f.name:
                # Content matches a completed note, but under a different filename —
                # this is a genuinely distinct file that was never processed (the
                # successfully-processed raw file is unlinked, so it can't still be here).
                results.append((f, sha))
            # else: success record with matching (or legacy, unrecorded) filename — skip
    return results


# ---------------------------------------------------------------------------
# Backup
# ---------------------------------------------------------------------------

def backup_file(config: dict[str, Any], file_path: Path) -> Path:
    backup_folder = Path(config["vault_path"]) / config["backup_folder"]
    backup_folder.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y-%m-%d_%H-%M-%S_%f")
    backup_path = backup_folder / f"{timestamp}_{file_path.name}"
    shutil.copy2(file_path, backup_path)
    return backup_path


# ---------------------------------------------------------------------------
# Topic name → safe filename (shared with mcp_server.py)
# ---------------------------------------------------------------------------

def sanitize_topic_name(topic: str) -> str:
    safe = re.sub(r"[^\w\s\-]", "", topic)
    safe = re.sub(r"\s+", "-", safe.strip())
    return safe or "Uncategorized"


# ---------------------------------------------------------------------------
# Wikilink target/stem indexing — shared with mcp_server.py's inbound-link
# resolver (_normalize_wikilinks).
# ---------------------------------------------------------------------------

def canonical_link_key(name: str) -> str:
    """Normalize a stem or link target for case/spacing comparison."""
    collapsed = re.sub(r"\s+", " ", name.strip())
    return collapsed.lower().replace(" ", "-")


def build_link_index(folders: list[Path]) -> dict[str, str]:
    """Return {canonical_link_key: actual_stem} for the given folders' .md files.

    Each folder is scanned with a non-recursive glob("*.md") -- deliberately
    NOT a blanket rglob: the wiki folder also contains wiki/processed/ (263
    archived pre-distillation source notes on the real vault, which the rest
    of the codebase treats as out-of-scope -- backend/agents/wiki_ingest.py
    explicitly skips parent.name == "processed"). A blanket rglob swept those
    in too (found live 2026-07-25: search result count roughly doubled, and
    a stem could resolve to a processed-folder duplicate that GET
    /wiki/<topic> then 404s on, since read_wiki only special-cases the daily
    folder). So callers pass exactly the folders they want scanned
    (non-recursively) -- e.g. brain_root, wiki_folder, wiki/daily -- nothing
    more. Later folders in the list win on key collision.
    """
    index: dict[str, str] = {}
    for folder in folders:
        if not folder.exists():
            continue
        for md in sorted(folder.glob("*.md")):
            if md.is_file():
                index[canonical_link_key(md.stem)] = md.stem
    return index


# ---------------------------------------------------------------------------
# Title normalisation — used by build_wiki_catalog, find_similar_page,
# consolidate_wiki.py (imported from here).
# ---------------------------------------------------------------------------

# "ment" adopted from consolidate_wiki.py's local copy during the §8 dedup
# (spec #2 §10 step 10) -- placed right after "tion" (both 4 chars, neither
# shadows the other) so the existing longest-first break-on-match order is
# preserved; e.g. "Deployment" -> "deploy" now matches "Deploy" -> "deploy".
_STEM_SUFFIXES = ("tion", "ment", "ing", "ion", "ed", "es", "s")


def _normalize_title(s: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace, and stem common suffixes.

    Examples:
        "Financial Forecasting" → "financial forecast"
        "Financial Forecast"    → "financial forecast"
        "Startups"              → "startup"
    """
    s = s.lower().strip()
    s = re.sub(r"[^\w\s]", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    words = s.split()
    stemmed = []
    for word in words:
        for suffix in _STEM_SUFFIXES:
            if word.endswith(suffix) and len(word) - len(suffix) >= 3:
                word = word[: len(word) - len(suffix)]
                break
        stemmed.append(word)
    return " ".join(stemmed)


# ---------------------------------------------------------------------------
# Frontmatter parsing (stdlib-only -- spec #3 §8.8; no pyyaml). Anchored
# strictly at position 0 of the (BOM-stripped) text: real wiki pages
# (NEXUS.md, Obsidian.md, Sales.md) carry mid-file "---" blocks that would
# false-positive as frontmatter under a document-wide search.
# ---------------------------------------------------------------------------

_FM_MAX_LINES = 100


def _find_frontmatter(text: str) -> tuple[int, int] | None:
    """Return (first_body_line_index, closing_delimiter_line_index) or None.

    Detects on ``text.lstrip("\ufeff")``. A leading code fence (```` ``` ````)
    -- 3 live pages wrap their whole document in one, hiding an interior
    "---" block from Obsidian -- returns None immediately. Otherwise line 0
    of the BOM-stripped text must be exactly "---", with a later exact "---"
    within the first ``_FM_MAX_LINES`` lines; either miss returns None.
    """
    stripped = text.lstrip("\ufeff")
    lines = stripped.splitlines()
    if not lines:
        return None
    first_non_blank = next((ln for ln in lines if ln.strip()), None)
    if first_non_blank is not None and first_non_blank.strip().startswith("```"):
        return None
    if lines[0] != "---":
        return None
    limit = min(len(lines), _FM_MAX_LINES)
    for i in range(1, limit):
        if lines[i] == "---":
            return (1, i)
    return None


def _parse_frontmatter_tags(text: str) -> list[str]:
    """Parse the ``tags:`` field from a document's top-of-file frontmatter.

    Only looks inside the block ``_find_frontmatter`` locates -- never a
    document-wide search. Handles the three live forms: YAML block list,
    hash-string (invalid YAML, but present verbatim on 4 live pages), and
    inline array. Returns original spellings, de-duplicated
    case-insensitively (first-seen spelling wins), order preserved.
    """
    fm = _find_frontmatter(text)
    if fm is None:
        return []
    start, end = fm
    lines = text.lstrip("\ufeff").splitlines()

    tags: list[str] = []
    seen: set[str] = set()

    def _add(raw_value: str) -> None:
        value = raw_value.strip().strip('"').strip("'").strip()
        if value and value.lower() not in seen:
            seen.add(value.lower())
            tags.append(value)

    key_re = re.compile(r"^tags\s*:\s*(.*)$", re.IGNORECASE)
    item_re = re.compile(r"^-\s*(.*)$")

    i = start
    while i < end:
        m = key_re.match(lines[i].strip())
        if m is None:
            i += 1
            continue
        rest = m.group(1).strip()
        if rest.startswith("["):
            # Inline array, e.g. tags: [risk-management, audit, "some tag"]
            buf = rest
            j = i
            while "]" not in buf and j + 1 < end:
                j += 1
                buf += " " + lines[j].strip()
            inner = buf.split("]", 1)[0]
            inner = inner[1:] if inner.startswith("[") else inner
            for part in inner.split(","):
                if part.strip():
                    _add(part)
            i = j + 1
        elif rest.startswith("#"):
            # Hash-string form (invalid YAML): tags: #compliance #legal #hipaa
            for tok in rest.split():
                _add(tok.lstrip("#"))
            i += 1
        elif rest == "":
            # Block list form:
            #   tags:
            #     - ford
            #     - beyondtrust
            j = i + 1
            while j < end:
                item = item_re.match(lines[j].strip())
                if item is None:
                    break
                _add(item.group(1))
                j += 1
            i = j
        else:
            i += 1

    return tags


# ---------------------------------------------------------------------------
# Per-page catalog entry extractor
# ---------------------------------------------------------------------------

def _extract_page_entry(f: Path, summary_chars: int = 300) -> dict[str, Any]:
    """Parse a wiki .md file and return a catalog entry dict.

    Keys: title, filename, path_str, headers, summary, tags.
    """
    text = f.read_text(encoding="utf-8-sig")
    lines = text.splitlines()

    # --- frontmatter, computed once: title/H1 and prose scans must both
    # start past it so a leading YAML block never gets mistaken for the
    # title or the summary paragraph ---
    fm = _find_frontmatter(text)
    fm_end = fm[1] if fm is not None else -1

    # --- title: first line starting with exactly one "#" ---
    title = f.stem
    h1_index = 0
    title_scan_start = fm[1] + 1 if fm is not None else 0
    for i, line in enumerate(lines[title_scan_start:], start=title_scan_start):
        if line.startswith("# ") and not line.startswith("## "):
            title = line[2:].strip()
            h1_index = i
            break

    # --- headers: all "## " lines (first 10, joined) ---
    raw_headers = [
        ln[3:].strip() for ln in lines if ln.startswith("## ")
    ][:10]
    headers_joined = " | ".join(raw_headers)

    # --- summary: first prose paragraph after the H1 (and past any
    # frontmatter, so a no-H1 page doesn't leak the YAML block as its
    # summary) ---
    _meta_re = re.compile(r"^\*\*[\w\s]+:\*\*|^>\s*\*\*[\w\s]+:\*\*")
    _rule_re = re.compile(r"^[-*_]{3,}$")
    prose_lines: list[str] = []
    collecting = False
    prose_start = max(h1_index + 1, fm_end + 1)
    for line in lines[prose_start:]:
        stripped = line.strip()
        # Skip blanks, rules, any header, metadata patterns
        if not stripped:
            if collecting and prose_lines:
                break  # end of first prose paragraph
            continue
        if stripped.startswith("#"):
            break  # hit a new section — stop
        if _rule_re.match(stripped):
            continue
        if _meta_re.match(stripped):
            continue
        prose_lines.append(stripped)
        collecting = True

    summary = " ".join(prose_lines)
    if len(summary) > summary_chars:
        summary = summary[:summary_chars]

    return {
        "title": title,
        "filename": f.name,
        "path_str": str(f),
        "headers": headers_joined,
        "summary": summary,
        "tags": _parse_frontmatter_tags(text),
    }


# ---------------------------------------------------------------------------
# Wiki catalog builder (incremental, cached in _meta/wiki-catalog.json)
# ---------------------------------------------------------------------------

# Bump whenever _extract_page_entry's output shape or parsing changes — a
# missing/mismatched version in the cached wiki-catalog.json forces a full
# re-parse of every page instead of silently reusing stale entries.
_CATALOG_PARSER_VERSION = 3


def build_wiki_catalog(
    wiki_folder: Path, meta_folder: Path, summary_chars: int = 300
) -> list[dict[str, Any]]:
    """Build (or incrementally refresh) a catalog of all wiki pages.

    Returns a list of entry dicts (title, filename, path_str, headers, summary),
    sorted by title (case-insensitive). On catastrophic failure returns [].
    """
    logger = logging.getLogger("brain_organizer")
    try:
        cache_path = meta_folder / "wiki-catalog.json"
        cached_by_filename: dict[str, dict[str, Any]] = {}
        built_at_ts = 0.0

        if cache_path.exists():
            try:
                with open(cache_path, encoding="utf-8") as fh:
                    cache = json.load(fh)
                if cache.get("parser_version") != _CATALOG_PARSER_VERSION:
                    # Missing or mismatched parser version — treat as a full
                    # cache miss so every page is re-parsed with the current
                    # _extract_page_entry logic (see _CATALOG_PARSER_VERSION).
                    built_at_ts = 0.0
                    cached_by_filename = {}
                else:
                    built_at_dt = datetime.fromisoformat(cache.get("built_at", ""))
                    built_at_ts = built_at_dt.timestamp()
                    cached_by_filename = {p["filename"]: p for p in cache.get("pages", [])}
            except Exception:
                built_at_ts = 0.0
                cached_by_filename = {}

        pages: list[dict[str, Any]] = []
        for f in sorted(wiki_folder.glob("*.md")):
            try:
                if f.name in cached_by_filename and f.stat().st_mtime <= built_at_ts:
                    pages.append(cached_by_filename[f.name])
                else:
                    pages.append(_extract_page_entry(f, summary_chars))
            except Exception as exc:
                logger.warning("catalog: skipping %s: %s", f.name, exc)
                continue

        pages.sort(key=lambda p: p["title"].lower())

        # Atomic write
        meta_folder.mkdir(parents=True, exist_ok=True)
        tmp = _make_temp_path(meta_folder, ".wiki-catalog_", ".tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(
                    {
                        "built_at": datetime.now(UTC).isoformat(),
                        "parser_version": _CATALOG_PARSER_VERSION,
                        "pages": pages,
                    },
                    fh, indent=2, ensure_ascii=False,
                )
            os.replace(tmp, cache_path)
        finally:
            if tmp.exists():
                tmp.unlink(missing_ok=True)

        return pages
    except Exception as exc:
        # Elevated to error (vs. the per-file "skipping" warning above): an
        # empty return here can now abort the whole run() (see §5.3
        # criterion 34 / §6.4 criterion 48) if wiki_folder is non-empty, so
        # this must read as more severe than a routine per-page skip.
        logging.getLogger("brain_organizer").error(
            "build_wiki_catalog failed catastrophically: %s", exc
        )
        return []


# ---------------------------------------------------------------------------
# Near-duplicate page finder
# ---------------------------------------------------------------------------

def find_similar_page(
    title: str,
    catalog: list[dict[str, Any]],
    threshold: float = 0.82,
) -> dict[str, Any] | None:
    """Return the best catalog entry whose title is near-duplicate of *title*.

    Only call this for titles intended as NEW — an exact-match title will
    trivially self-match at ratio 1.0, which is a false positive if the title
    is already a confirmed existing page.

    Returns None if no entry meets the threshold.
    """
    norm_title = _normalize_title(title)
    if not norm_title:
        return None

    best_entry: dict[str, Any] | None = None
    best_ratio = 0.0

    for entry in catalog:
        norm_entry = _normalize_title(entry["title"])
        ratio = difflib.SequenceMatcher(None, norm_title, norm_entry).ratio()
        is_candidate = ratio >= threshold

        # Jaccard boost (only when both titles are multi-word)
        if not is_candidate:
            words_a = set(norm_title.split())
            words_b = set(norm_entry.split())
            if len(words_a) > 1 and len(words_b) > 1:
                union = words_a | words_b
                jaccard = len(words_a & words_b) / len(union) if union else 0.0
                if jaccard > 0.7:
                    is_candidate = True

        if is_candidate and ratio > best_ratio:
            best_ratio = ratio
            best_entry = entry

    return best_entry


# ---------------------------------------------------------------------------
# Core API call — Anthropic with retry + OpenRouter fallback
# ---------------------------------------------------------------------------

_USAGE_LOG = Path(__file__).parent / "logs" / "usage.jsonl"


def _record_usage(model: str, provider: str, input_tokens: int, output_tokens: int) -> None:
    """Append one JSON line of token usage for the NEXUS spend governor to ingest.

    Best-effort and stdlib-only: metering must NEVER break note processing, so the
    whole body is wrapped in try/except: pass. Open-append-close per write so a
    concurrent NEXUS os.replace() claim can't corrupt a held handle.
    """
    try:
        _USAGE_LOG.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps({
            "ts": datetime.now(UTC).isoformat(),
            "model": model,
            "input_tokens": int(input_tokens or 0),
            "output_tokens": int(output_tokens or 0),
            "provider": provider,
        })
        with open(_USAGE_LOG, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        pass


class _APIUsageCapped(RuntimeError):
    """Raised when ALL providers are hard-capped (not transient) — caller should abort the run."""


def _call_api(
    model: str,
    messages: list[dict[str, Any]],
    max_tokens: int,
    config: dict[str, Any],
    client: anthropic.Anthropic,
    *,
    max_retries: int = 6,
) -> tuple[str, str]:
    """
    Call Anthropic with exponential-backoff retry, then fall back to OpenRouter.

    Returns (text, stop_reason).
    Raises _APIUsageCapped when both providers are hard-capped (abort the run).
    Raises RuntimeError on transient dual failure.
    """
    logger = logging.getLogger("brain_organizer")
    last_exc: Exception | None = None
    anthropic_usage_capped = False

    for attempt in range(1, max_retries + 1):
        try:
            msg = client.messages.create(
                model=model,
                max_tokens=max_tokens,
                messages=messages,  # type: ignore[arg-type]
            )
            block = next((b for b in msg.content if isinstance(b, TextBlock)), None)
            if block is None:
                raise ValueError("Anthropic response contained no text block")
            usage = getattr(msg, "usage", None)
            _record_usage(
                model, "anthropic",
                getattr(usage, "input_tokens", 0) or 0,
                getattr(usage, "output_tokens", 0) or 0,
            )
            return block.text.strip(), (msg.stop_reason or "")
        except _RETRYABLE_ERRORS as exc:
            last_exc = exc
            wait = min(2 ** attempt, 60)
            logger.warning(
                "Anthropic %s on attempt %d/%d — retrying in %ds",
                type(exc).__name__, attempt, max_retries, wait,
            )
            if attempt < max_retries:
                time.sleep(wait)
        except anthropic.APIStatusError as exc:
            if exc.status_code >= 500:
                last_exc = exc
                wait = min(2 ** attempt, 60)
                logger.warning(
                    "Anthropic HTTP %d on attempt %d/%d — retrying in %ds",
                    exc.status_code, attempt, max_retries, wait,
                )
                if attempt < max_retries:
                    time.sleep(wait)
            elif exc.status_code == 400 and "usage limits" in str(exc).lower():
                # Hard usage cap — no point retrying Anthropic at all
                anthropic_usage_capped = True
                last_exc = exc
                logger.warning("Anthropic hard usage cap hit — skipping retries, falling back to OpenRouter")
                break
            else:
                raise

    # All Anthropic retries exhausted — fall back to OpenRouter
    logger.warning("Anthropic API exhausted after %d retries — falling back to OpenRouter", max_retries)
    or_key = os.environ.get("OPENROUTER_API_KEY", "")
    if not or_key:
        raise _APIUsageCapped(
            "Anthropic usage-capped and OPENROUTER_API_KEY is not set — no provider available"
        ) if anthropic_usage_capped else RuntimeError(
            f"Anthropic API failed ({last_exc}) and OPENROUTER_API_KEY is not set"
        )

    try:
        or_model = "anthropic/" + re.sub(r"-\d{8}$", "", model)
        resp = httpx.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {or_key}",
                "Content-Type": "application/json",
            },
            json={"model": or_model, "max_tokens": max_tokens, "messages": messages},
            timeout=60.0,
        )
        if resp.status_code == 403:
            msg_text = "OpenRouter 403 Forbidden — API key invalid or out of credits (check openrouter.ai)"
            logger.error(msg_text)
            raise _APIUsageCapped(
                f"Anthropic capped + {msg_text}"
            ) if anthropic_usage_capped else RuntimeError(msg_text)
        resp.raise_for_status()
        data = resp.json()
        text = (data["choices"][0]["message"]["content"] or "").strip()
        finish_reason = data["choices"][0].get("finish_reason", "")
        or_usage = data.get("usage") or {}
        _record_usage(
            or_model, "openrouter",
            or_usage.get("prompt_tokens", 0) or 0,
            or_usage.get("completion_tokens", 0) or 0,
        )
        logger.info("OpenRouter fallback succeeded (finish_reason=%s)", finish_reason)
        # Normalize to Anthropic's "max_tokens" sentinel: OpenRouter/OpenAI-style
        # responses signal truncation as finish_reason == "length". Callers
        # (synthesize_wiki) only ever check for the literal string "max_tokens"
        # to decide whether to raise instead of writing truncated content --
        # without this, a truncated OpenRouter fallback response sailed past
        # that guard and got written as if it were complete.
        if finish_reason == "length":
            finish_reason = "max_tokens"
        return text, finish_reason
    except _APIUsageCapped:
        raise
    except Exception as or_exc:
        raise RuntimeError(
            f"Both Anthropic ({last_exc}) and OpenRouter ({or_exc}) failed"
        ) from or_exc


# ---------------------------------------------------------------------------
# Daily-note guard — bypasses route_topics entirely for dated/briefing files
# ---------------------------------------------------------------------------

_DAILY_NOTE_STEM_PAT = re.compile(r"^\d{4}-\d{2}-\d{2}[a-z]?$", re.IGNORECASE)
_DAILY_NOTE_NAME_PAT = re.compile(r"briefing|daily", re.IGNORECASE)
_DATE_IN_STEM_PAT = re.compile(r"\d{4}-\d{2}-\d{2}")
_EVENT_NOTE_PREFIX_PAT = re.compile(r"^event-[a-z0-9]+(?:-[a-z0-9]+)*?-", re.IGNORECASE)

# A proposed page title that names a session LOG rather than a topic: contains
# a hyphenated UUID, a standalone session/save token, or starts with a full
# date (log-file naming). The near-duplicate guard can never catch these --
# a UUID or dated title is unique by construction -- so Haiku routing a
# session note's own frontmatter title back as a "new topic" created one
# filename-titled wiki page per session (seen live 2026-07-08 and 2026-07-11).
_UUID_PAT = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE
)
_SESSION_TITLE_PAT = re.compile(r"(?:^|[-_ ])(session|save)(?:[-_ ]|$)", re.IGNORECASE)
_DATE_PREFIX_PAT = re.compile(r"^\d{4}-\d{2}-\d{2}")


def _looks_like_session_title(title: str) -> bool:
    """True for a proposed NEW page title that is session-log-shaped.

    Deterministic backstop, same pattern as the daily-note guard: never rely
    on the routing prompt alone to keep log names from becoming topic pages.
    """
    return bool(
        _UUID_PAT.search(title)
        or _SESSION_TITLE_PAT.search(title)
        or _DATE_PREFIX_PAT.match(title)
    )


def _is_daily_note(stem: str) -> bool:
    """True for a morning-briefing/daily-log filename (mirrors
    backend/agents/wiki_ingest.py::_is_daily_note).

    A pure date stem (optionally suffixed, e.g. "2026-07-24a") always
    counts. A "daily"/"briefing"-named file only counts if it ALSO contains
    a date or a generalized event-source prefix ("event-<source>-", e.g.
    "event-hermes-hermes-daily-digest-20260724T120009Z" or
    "event-nexus-nexus-daily-digest-20260802T120508Z", whose timestamps have
    no hyphens so _DATE_IN_STEM_PAT alone would miss them) -- otherwise a
    genuinely topical file that happens to have "daily" in its name (e.g.
    "Daily-Driver-Setup.md") would get hijacked into the daily-note path.
    """
    if _DAILY_NOTE_STEM_PAT.match(stem):
        return True
    if _DAILY_NOTE_NAME_PAT.search(stem):
        return bool(_DATE_IN_STEM_PAT.search(stem)) or bool(_EVENT_NOTE_PREFIX_PAT.match(stem))
    return False


def _daily_note_route(
    stem: str,
    catalog: list[dict[str, Any]],
    wiki_folder: Path,
    daily_folder: Path,
) -> list[tuple[str, Path, bool]] | None:
    """Deterministic route for a daily note. Returns None for non-daily notes.

    route_topics' near-duplicate guard (find_similar_page) compares titles,
    but a dated title like "Daily-Operations-Log-2026-07-08" is unique every
    day by construction, so it never fires and the model is free to invent a
    new page daily. Skip the model call entirely for daily notes.

    Date-stem notes (the common case -- NEXUS's own daily briefing) route
    into daily_folder/{date}.md, a dedicated subfolder kept OUT of
    build_wiki_catalog's non-recursive wiki_folder.glob("*.md") scan ON
    PURPOSE: one permanent page per calendar day, forever, would otherwise
    dominate both the topic list and the router's limited prompt context
    (catalog_max_pages_in_prompt) -- confirmed live, 10 date pages occupied
    the first 10 of 60 catalog slots shown to the router. is_new is decided
    by path.exists() directly, NOT a catalog scan, since the catalog can't
    see subfolder pages at all.

    Non-date-stem daily/briefing files (see _is_daily_note) fall back to the
    shared Daily-Log.md page AT WIKI ROOT instead -- that's a real
    synthesized topic page (sensor ranges, device inventory, etc, proven live
    to consolidate cleanly with zero page growth), not clutter, and stays in
    the catalog/router on purpose. This path keeps the original catalog-scan
    behavior so an existing Daily-Log.md's exact registered title is reused.
    """
    if not _is_daily_note(stem):
        return None
    m = _DATE_IN_STEM_PAT.search(stem)
    if m:
        date = m.group(0)
        path = daily_folder / f"{date}.md"
        return [(date, path, not path.exists())]
    filename = "Daily-Log.md"
    for entry in catalog:
        if entry["filename"] == filename:
            return [(entry["title"], Path(entry["path_str"]), False)]
    return [("Daily-Log", wiki_folder / filename, True)]


# The daily "Claude features digest" automation (see
# backend/agents/wiki_ingest.py) drops a file named
# claude-features-digest-YYYY-MM-DD.md into Brain/raw/ (the vault's ingest
# endpoint may append a _<UTC-timestamp>Z suffix on a same-name collision,
# which we don't control). Its topic hint reduces to the bare word "claude",
# which prefix-matches the generic Claude.md page and swallows the digest.
# Detect the filename shape and force it onto its own running-log page --
# ported from wiki_ingest.py's _FEATURES_DIGEST_PAT / _digest_date /
# _is_features_digest so this module's deterministic-route dispatch (spec
# #2 §7.2) can claim the same file shape before route_topics ever sees it.
_FEATURES_DIGEST_PAT = re.compile(
    r"^claude-features-digest-(\d{4}-\d{2}-\d{2})", re.IGNORECASE
)
_FEATURES_DIGEST_PAGE = "Claude Features Digest"


def _features_digest_date(stem: str) -> str | None:
    """Extract the embedded YYYY-MM-DD from a features-digest filename stem
    (mirrors backend/agents/wiki_ingest.py::_digest_date)."""
    m = _FEATURES_DIGEST_PAT.match(stem)
    return m.group(1) if m else None


def _is_features_digest(stem: str) -> bool:
    """True for the daily Claude-features-digest filename (plain or
    collision-suffixed) -- mirrors
    backend/agents/wiki_ingest.py::_is_features_digest."""
    return bool(_FEATURES_DIGEST_PAT.match(stem))


def _features_digest_route(
    stem: str,
    catalog: list[dict[str, Any]],
    wiki_folder: Path,
    daily_folder: Path,
) -> list[tuple[str, Path, bool]] | None:
    """Deterministic route for a Claude-features-digest note. Returns None
    for non-digest stems.

    Same shape as _daily_note_route's Daily-Log branch: reuse the existing
    catalog entry for wiki/Claude-Features-Digest.md by exact filename if
    one is already registered, otherwise point at a fresh page there --
    no new catalog-creation logic, just the same lookup-or-create pattern.
    """
    if not _is_features_digest(stem):
        return None
    filename = "Claude-Features-Digest.md"
    for entry in catalog:
        if entry["filename"] == filename:
            return [(entry["title"], Path(entry["path_str"]), False)]
    return [(_FEATURES_DIGEST_PAGE, wiki_folder / filename, True)]


# Table-driven dispatch (spec #2 §7.2): each row is (predicate, router).
# A new deterministic source registers by appending a row here instead of
# growing another `X or Y or route_topics(...)` chain at each call site.
_SOURCE_ROUTES: tuple[
    tuple[
        Callable[[str], bool],
        Callable[[str, list[dict[str, Any]], Path, Path], list[tuple[str, Path, bool]] | None],
    ],
    ...,
] = (
    (_is_daily_note, _daily_note_route),
    (_is_features_digest, _features_digest_route),
)


def _deterministic_route(
    stem: str,
    catalog: list[dict[str, Any]],
    wiki_folder: Path,
    daily_folder: Path,
) -> list[tuple[str, Path, bool]] | None:
    """Dispatch stem to the first _SOURCE_ROUTES row whose predicate
    matches, else None -- callers OR the result with route_topics(...) for
    the non-deterministic fallback (crit 53: ordinary stems MUST resolve to
    None here so route_topics still runs for everything else).
    """
    for predicate, router in _SOURCE_ROUTES:
        if predicate(stem):
            return router(stem, catalog, wiki_folder, daily_folder)
    return None


# ---------------------------------------------------------------------------
# Catalog-aware routing (Haiku)
# ---------------------------------------------------------------------------

# §4.3: small stopword set for the lexical ranking below -- common English
# function words that carry no topical signal and would otherwise dilute the
# IDF score just by sheer frequency. Only used by the router_catalog_ranking
# path; never touches the flag-off prompt.
_ROUTER_RANKING_STOPWORDS = frozenset({
    "the", "and", "for", "are", "but", "not", "you", "all", "can",
    "her", "was", "one", "our", "out", "day", "get", "has", "him",
    "his", "how", "man", "new", "now", "old", "see", "two", "way",
    "who", "did", "its", "let", "put", "say", "she", "too", "use",
    "with", "this", "that", "from", "have", "will", "your", "they",
    "been", "were", "into", "than", "then", "them", "some", "when",
    "what", "more", "also", "about", "there", "these", "which",
    "would", "could", "should", "each", "other", "such", "over",
    "only", "note", "notes",
})


def _router_ranking_tokens(text: str) -> list[str]:
    """Lowercase alphanumeric words of length >2, minus a small stopword set.

    Used only by the §4.3 lexical ranking below.
    """
    words = re.findall(r"[a-z0-9]+", text.lower())
    return [w for w in words if len(w) > 2 and w not in _ROUTER_RANKING_STOPWORDS]


def _rank_catalog_by_relevance(
    content: str, catalog: list[dict[str, Any]], max_in_prompt: int
) -> list[dict[str, Any]]:
    """§4.3: select the rich prompt window by relevance, not alphabet.

    Tokenizes `content[:3000]` and each catalog entry's `title + headers +
    summary`, scores each entry by summed IDF (computed across the full
    catalog) over the token intersection, and returns the top
    `max_in_prompt` entries sorted by (-score, title.lower()) -- ties,
    including the all-zero-overlap case, degrade gracefully to alphabetical
    order, same as today's truncation.
    """
    query_tokens = set(_router_ranking_tokens(content[:3000]))

    entry_tokens: list[set[str]] = []
    doc_freq: defaultdict[str, int] = defaultdict(int)
    for entry in catalog:
        headers = entry.get("headers", "")
        summary = entry.get("summary", "")
        tokens = set(_router_ranking_tokens(f"{entry['title']} {headers} {summary}"))
        entry_tokens.append(tokens)
        for token in tokens:
            doc_freq[token] += 1

    n_docs = len(catalog)
    idf: dict[str, float] = {
        token: math.log(n_docs / df) for token, df in doc_freq.items()
    }

    scored: list[tuple[float, str, dict[str, Any]]] = []
    for entry, tokens in zip(catalog, entry_tokens):
        score = sum(idf[t] for t in query_tokens & tokens)
        scored.append((-score, entry["title"].lower(), entry))
    scored.sort(key=lambda t: (t[0], t[1]))
    return [entry for _, _, entry in scored[:max_in_prompt]]


def route_topics(
    content: str,
    catalog: list[dict[str, Any]],
    config: dict[str, Any],
    client: anthropic.Anthropic,
) -> list[tuple[str, Path, bool]]:
    """Route note content to existing wiki pages or propose new ones.

    Returns list of (title, wiki_path, is_new) tuples (1-3 routes, de-duped by path).
    Haiku is given the catalog and strongly biased toward existing pages.
    Near-duplicate guard prevents new pages that are synonyms of existing ones.

    The system prompt is folded into the user message because _call_api only
    accepts a messages list (no top-level system= parameter).
    """
    logger = logging.getLogger("brain_organizer")
    wiki_folder = Path(config["vault_path"]) / config["wiki_folder"]

    def _uncategorized_fallback() -> list[tuple[str, Path, bool]]:
        return [("Uncategorized", wiki_folder / "Uncategorized.md", True)]

    # Build numbered catalog block (capped at config limit).
    router_catalog_ranking: bool = config["router_catalog_ranking"]
    max_in_prompt: int = config["catalog_max_pages_in_prompt"]
    # §4.3: with the flag on, the rich window is chosen by lexical relevance
    # to `content`, not alphabet/insertion order. Flag off keeps today's
    # alphabetical-truncation slice byte-for-byte.
    if router_catalog_ranking:
        catalog_pages = _rank_catalog_by_relevance(content, catalog, max_in_prompt)
    else:
        catalog_pages = catalog[:max_in_prompt]
    catalog_lines: list[str] = []
    for i, page in enumerate(catalog_pages, 1):
        title = page["title"]
        headers = page.get("headers", "")
        summary = page.get("summary", "").replace("\n", " ")
        if headers:
            line = f"{i}. {title} — Covers: {headers}. {summary}"
        else:
            line = f"{i}. {title}. {summary}"
        catalog_lines.append(line)
    catalog_block = "\n".join(catalog_lines)

    # §4.4: append a full title-only index of every remaining page so no page
    # is structurally unroutable behind the rich window's truncation. A
    # proper set difference against whatever §4.3 selected above, so the
    # index and the rich block never overlap or double-truncate. Gated by
    # router_catalog_ranking so the flag off preserves today's
    # alphabetical-truncation prompt byte-for-byte.
    if router_catalog_ranking:
        selected_ids = {id(page) for page in catalog_pages}
        remaining_pages = [page for page in catalog if id(page) not in selected_ids]
        if remaining_pages:
            index_lines = [
                f"{page['title']} ({Path(page['filename']).stem})"
                for page in remaining_pages
            ]
            catalog_block += (
                "\n\nALL OTHER PAGES (title — file):\n" + "\n".join(index_lines)
            )

    # System prompt folded into user message (see docstring)
    system_text = (
        "You are a routing assistant for a personal wiki. Your job is to route note content "
        "into the best-matching EXISTING pages. Only propose a new page title when no existing "
        "page is a genuine match."
    )
    user_text = (
        f"EXISTING WIKI PAGES (route to these whenever possible):\n{catalog_block}\n\n"
        f"NOTE TO ROUTE:\n{content[:3000]}\n\n"
        'Return ONLY a JSON object, no other text:\n'
        '{"routes": [\n'
        '  {"match": "existing", "title": "Exact Title From Catalog Above"},\n'
        '  {"match": "new", "title": "Concise New Topic"}\n'
        ']}\n\n'
        "Rules:\n"
        "- Use 1 to 3 routes.\n"
        '- STRONGLY prefer "match":"existing". Use the EXACT title text from the catalog.\n'
        '- Only use "match":"new" when NO existing page is a genuine subject match.\n'
        '- Never create a near-synonym of an existing title (e.g. do not invent '
        '"Financial Forecasting" when "Financial Forecast" exists).'
    )
    full_prompt = system_text + "\n\n" + user_text

    text, _ = _call_api(
        config["haiku_model"],
        [{"role": "user", "content": full_prompt}],
        config["route_max_tokens"],
        config,
        client,
    )

    # Strip markdown code fences
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[-1]
        cleaned = cleaned.rsplit("```", 1)[0].strip()

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        logger.warning("route_topics: JSON decode failed — using Uncategorized fallback")
        return _uncategorized_fallback()

    routes_raw = data.get("routes", [])
    if not isinstance(routes_raw, list) or not routes_raw:
        logger.warning("route_topics: empty/missing 'routes' key — using Uncategorized fallback")
        return _uncategorized_fallback()

    # Build lookups: first entry wins on duplicate titles/stems.
    by_title: dict[str, dict[str, Any]] = {}
    by_title_ci: dict[str, dict[str, Any]] = {}
    by_stem: dict[str, dict[str, Any]] = {}
    by_stem_ci: dict[str, dict[str, Any]] = {}
    for p in catalog:
        if p["title"] not in by_title:
            by_title[p["title"]] = p
        if p["title"].lower() not in by_title_ci:
            by_title_ci[p["title"].lower()] = p
        filename = p.get("filename")
        if filename:
            stem = Path(filename).stem
            if stem not in by_stem:
                by_stem[stem] = p
            if stem.lower() not in by_stem_ci:
                by_stem_ci[stem.lower()] = p

    def _resolve_existing(title: str) -> dict[str, Any] | None:
        """§4.5 ordered tolerant lookup: exact title -> exact filename stem ->
        case-insensitive title -> case-insensitive stem. Gated by
        router_catalog_ranking since §4.4 is what first exposes filename
        stems to the router menu.
        """
        if title in by_title:
            return by_title[title]
        if not router_catalog_ranking:
            return None
        if title in by_stem:
            return by_stem[title]
        if title.lower() in by_title_ci:
            return by_title_ci[title.lower()]
        if title.lower() in by_stem_ci:
            return by_stem_ci[title.lower()]
        return None

    result: list[tuple[str, Path, bool]] = []
    seen_paths: set[str] = set()

    for route in routes_raw[:3]:
        if not isinstance(route, dict):
            continue
        match_type = str(route.get("match", "")).strip()
        title = str(route.get("title", "")).strip()
        if not title:
            continue

        if match_type == "existing":
            entry = _resolve_existing(title)
            if entry is not None:
                wiki_path = Path(entry["path_str"])
                path_key = str(wiki_path)
                if path_key not in seen_paths:
                    seen_paths.add(path_key)
                    result.append((entry["title"], wiki_path, False))
                continue
            else:
                # Hallucinated existing title — re-check via near-dup guard
                logger.warning(
                    "route_topics: hallucinated existing title %r — re-checking as new", title
                )
                match_type = "new"

        if match_type == "new":
            if _looks_like_session_title(title):
                logger.warning(
                    "route_topics: rejected session-shaped new title %r", title
                )
                continue
            similar = find_similar_page(
                title, catalog, config["new_page_similarity_threshold"]
            )
            if similar is not None:
                logger.info(
                    "Near-duplicate guard: %r -> %r", title, similar["title"]
                )
                wiki_path = Path(similar["path_str"])
                path_key = str(wiki_path)
                if path_key not in seen_paths:
                    seen_paths.add(path_key)
                    result.append((similar["title"], wiki_path, False))
            else:
                safe = sanitize_topic_name(title)
                wiki_path = wiki_folder / f"{safe}.md"
                path_key = str(wiki_path)
                if path_key not in seen_paths:
                    seen_paths.add(path_key)
                    result.append((title, wiki_path, True))

    if not result:
        logger.warning("route_topics: no valid routes resolved — using Uncategorized fallback")
        return _uncategorized_fallback()

    return result


# ---------------------------------------------------------------------------
# Topic detection (Haiku) — back-compat wrapper around route_topics
# ---------------------------------------------------------------------------

def detect_topics(
    content: str,
    config: dict[str, Any],
    client: anthropic.Anthropic,
) -> list[str]:
    """Back-compat wrapper: returns a list of topic title strings.

    Builds the wiki catalog, calls route_topics, then extracts just the titles
    so existing callers/tests see an unchanged return type.
    """
    wiki_folder = Path(config["vault_path"]) / config["wiki_folder"]
    meta_folder = Path(config["vault_path"]) / config["meta_folder"]
    catalog = build_wiki_catalog(wiki_folder, meta_folder, config["catalog_summary_chars"])

    routes = route_topics(content, catalog, config, client)
    return [title for (title, _path, _is_new) in routes]


# ---------------------------------------------------------------------------
# Wiki synthesis (Sonnet)
# ---------------------------------------------------------------------------

WIKILINK_PAT = re.compile(r"(?<!\!)\[\[([^\]|#]+)(?:#([^\]|]*))?(?:\|([^\]]*))?\]\]")


def _defuse_unknown_wikilinks(
    text: str,
    topic: str,
    catalog: list[dict[str, Any]] | None,
    *,
    threshold: float = 0.82,
    stats: dict[str, int] | None = None,
) -> str:
    """Rewrite any [[wikilink]] the model generated into filename-space (the
    space Obsidian actually resolves), or backtick-defuse it if it names
    nothing the vault has.

    Obsidian resolves links by **filename**, but this module (prompts and,
    until this fix, this guard) speaks **titles** -- so a perfectly valid
    `[[Bug Fixes]]` (the real page's *title*) rendered as a permanently
    broken link because the file on disk is `Bug-Fixes.md`. This resolver
    closes that gap with an ordered filename-space lookup, first match wins:

        1. target is an exact filename stem            -> already correct
        2. target == topic (self-reference)             -> unchanged
        3. target is an exact catalog title              -> rewrite to stem
        4. case-insensitive stem match                    -> rewrite to stem
        5. case-insensitive title match                   -> rewrite to stem
        6. find_similar_page fuzzy match                  -> rewrite to stem
        7. otherwise                                      -> backtick-defuse

    Source material also often MENTIONS names that aren't vault pages at all
    (e.g. Claude Code memory-file names like "project_version_scheme"
    mentioned in a session note), and the model wikilinks them anyway --
    producing a permanently broken link every time, since nothing by that
    name will ever exist as a page. Never rely on the prompt instruction
    alone to prevent this -- same deterministic-backstop pattern as the
    daily-note guard and the night-exemption filter elsewhere in this
    codebase. Applied to every synthesis result (merge and create alike),
    not just the branch that introduced the instruction, since a merge can
    just as easily echo a hallucinated link from its input.

    Catalog entries missing a "filename" field degrade gracefully to
    today's title-only matching behavior (left unchanged if their title
    matches) rather than raising -- they simply cannot be rewritten to a
    stem since there is no filename to rewrite to.

    stats: optional mutable accumulator (e.g. {"rewritten": 0, "backticked": 0})
        incremented in place -- steps 3-6 above bump "rewritten", step 7 bumps
        "backticked". Steps 1/2 and the title-only degrade leave the link
        unchanged, so neither counter moves. Return type stays `-> str`;
        callers that never pass `stats` see identical behavior.
    """
    catalog = catalog or []

    # Config-sourced threshold isn't validated upstream -- a malformed or
    # out-of-range `new_page_similarity_threshold` (wrong type, negative,
    # >1) must degrade to the safe default rather than raise mid-synthesis
    # (a TypeError here would surface as a lost note write, not a config
    # error) or silently make every/no link match.
    try:
        threshold = float(threshold)
    except (TypeError, ValueError):
        threshold = 0.82
    if not (0.0 <= threshold <= 1.0):
        threshold = 0.82

    by_stem: dict[str, str] = {}
    by_title: dict[str, str] = {}
    title_only: set[str] = set()  # catalog entries with no filename to resolve to

    for p in catalog:
        filename = p.get("filename")
        title = p.get("title")
        if not filename:
            if title:
                title_only.add(title)
            continue
        stem = Path(filename).stem
        by_stem.setdefault(stem, stem)
        if title:
            by_title.setdefault(title, stem)

    by_stem_ci: dict[str, str] = {}
    for stem in by_stem:
        by_stem_ci.setdefault(stem.lower(), by_stem[stem])

    by_title_ci: dict[str, str] = {}
    for title, stem in by_title.items():
        by_title_ci.setdefault(title.lower(), stem)

    title_only_ci = {t.lower() for t in title_only}

    def _replace(m: "re.Match[str]") -> str:
        target = m.group(1).strip()
        heading = m.group(2)
        alias = m.group(3)

        # 1. exact filename stem match -- already correct, no rewrite needed
        if target in by_stem:
            return m.group(0)

        # 2. self-reference -- the page being written links to itself
        if target == topic:
            return m.group(0)

        # 3. exact catalog title match
        resolved_stem = by_title.get(target)
        # 4. case-insensitive stem match
        if resolved_stem is None:
            resolved_stem = by_stem_ci.get(target.lower())
        # 5. case-insensitive title match
        if resolved_stem is None:
            resolved_stem = by_title_ci.get(target.lower())

        if resolved_stem is None:
            if target in title_only or target.lower() in title_only_ci:
                # Known by title only (no filename on record) -- degrade to
                # today's title-only matching behavior instead of guessing.
                return m.group(0)
            # 6. fuzzy near-duplicate match
            similar = find_similar_page(target, catalog, threshold)
            if similar is not None:
                similar_filename = similar.get("filename")
                if similar_filename:
                    resolved_stem = Path(similar_filename).stem
                else:
                    # Matched entry has no filename either -- same graceful
                    # degradation as above.
                    return m.group(0)

        if resolved_stem is not None:
            display = alias.strip() if alias else target
            heading_part = f"#{heading}" if heading else ""
            alias_part = "" if display == resolved_stem else f"|{display}"
            if stats is not None:
                stats["rewritten"] = stats.get("rewritten", 0) + 1
            return f"[[{resolved_stem}{heading_part}{alias_part}]]"

        # 7. unknown -- backtick-defuse
        display = alias.strip() if alias else target + (f"#{heading}" if heading else "")
        if stats is not None:
            stats["backticked"] = stats.get("backticked", 0) + 1
        return f"`{display}`"

    return WIKILINK_PAT.sub(_replace, text)


def synthesize_wiki(
    topic: str,
    new_content: str,
    existing_content: str,
    config: dict[str, Any],
    client: anthropic.Anthropic,
    *,
    catalog_entry: dict[str, Any] | None = None,
    catalog: list[dict[str, Any]] | None = None,
    stats: dict[str, int] | None = None,
) -> str:
    """Synthesize or merge a wiki page for *topic*.

    Branch selector:
        is_merge + is_large → 5b: section-splice (returns only changed ## blocks)
        is_merge + normal   → 5a: full merge with optional scope contract
        create              → 5c: new page with related-pages wikilink hint

    Args:
        catalog_entry: Populated for EXISTING pages (is_merge). Provides scope
            contract (headers, title) so the model stays on-topic.
        catalog:       Full page list. Used in the CREATE branch (5c) to compute
            related-page wikilink suggestions.
        stats:         Optional mutable accumulator threaded straight through to
            every _defuse_unknown_wikilinks call in this function (both the
            5b splice loop and the 5a/5c final normalizer), so a caller can
            tally rewritten/backticked links across one synthesis call.
    """
    max_chars: int = config["max_file_chars"]
    large_threshold: int = config["large_page_threshold_chars"]

    is_merge = bool(existing_content)
    is_large = is_merge and len(existing_content) > large_threshold

    # -----------------------------------------------------------------
    # Scope-contract block (injected into merge prompts when we have a
    # catalog entry for the target page).
    # -----------------------------------------------------------------
    if catalog_entry is not None:
        scope_block = (
            f"PAGE SCOPE: This page covers: {catalog_entry['title']}.\n"
            f"Existing sections: {catalog_entry['headers']}.\n"
            "RULE: Merge ONLY content that belongs to this page's subject. "
            "If the new material contains content about a DIFFERENT subject, "
            "OMIT it entirely — it is being routed to other pages separately. "
            "Preserve the existing ## section structure. "
            "Add new ## sections only for genuinely new aspects of this same subject.\n"
        )
    else:
        scope_block = ""

    # -----------------------------------------------------------------
    # 5b — LARGE-page merge: request only changed/new ## sections then
    # splice them into the existing document locally.
    # This prevents max_tokens failures on pages > large_threshold chars.
    # -----------------------------------------------------------------
    if is_large:
        existing_headers = (
            catalog_entry["headers"] if catalog_entry else "(unknown)"
        )
        prompt = (
            "You are a personal knowledge base curator updating a LARGE existing wiki page. "
            "To save space, do NOT return the whole page. "
            "Return ONLY the markdown ## sections that are NEW or CHANGED, "
            "each as a complete \"## Header\" block. "
            "Do not return unchanged sections. Do not return the H1 title. "
            "If nothing should change, return the single line: NO_CHANGES.\n\n"
            + (scope_block + "\n" if scope_block else "")
            + f"PAGE TITLE: {topic}\n"
            f"EXISTING SECTION HEADERS: {existing_headers}\n\n"
            "New information to integrate:\n"
            f"{new_content[:max_chars]}\n\n"
            "Return only the changed/new ## blocks (or NO_CHANGES)."
        )

        logger = logging.getLogger("brain_organizer")
        max_tokens: int = config["sonnet_max_tokens"]
        text, stop_reason = _call_api(
            config["sonnet_model"],
            [{"role": "user", "content": prompt}],
            max_tokens,
            config,
            client,
        )

        # The large-merge path outputs small diffs; max_tokens here is anomalous.
        if stop_reason == "max_tokens":
            raise ValueError(
                f"Large-page synthesis for topic '{topic}' hit max_tokens ({max_tokens}) — "
                "this is unexpected for a diff-only response. "
                "Increase sonnet_max_tokens in config.json."
            )

        if text.strip() == "NO_CHANGES":
            return existing_content

        # --- Splice returned sections into existing_content ---
        try:
            # Split returned text on lines that START a new ## section.
            # re.split with a lookahead keeps the delimiter (## …) in each chunk.
            raw_chunks = re.split(r"(?m)(?=^## )", text)
            # Discard any leading preamble chunk that doesn't start with "##"
            section_chunks = [c for c in raw_chunks if c.lstrip().startswith("## ")]

            result = existing_content
            for chunk in section_chunks:
                chunk = chunk.rstrip()
                if not chunk:
                    continue
                # Extract the header line (first line of the chunk) BEFORE
                # normalization -- the section-match regex below is built from
                # this header and must match the identical, un-normalized
                # header already present in existing_content. Extracting it
                # after normalization could rewrite a wikilink inside the
                # header itself, causing the regex to miss the existing
                # section and append a duplicate instead of replacing it.
                header_line = chunk.splitlines()[0].rstrip()
                # Normalize wikilinks in this spliced chunk the same way the
                # 5a/5c path does at the bottom of this function -- branch-5b
                # previously returned before that normalizer ever ran, so
                # large-page diffs kept whatever raw [[links]] the model
                # emitted, broken or not.
                chunk = _defuse_unknown_wikilinks(
                    chunk, topic, catalog,
                    threshold=config["new_page_similarity_threshold"],
                    stats=stats,
                )
                # Find and replace the matching section in the existing content,
                # or append if not present.
                # A section spans from its ## header to the next ## header (or EOF).
                pattern = (
                    r"(?m)^"
                    + re.escape(header_line)
                    + r"\s*\n"          # header line
                    r"(?:(?!^## ).*\n)*"  # body lines (up to next ## or EOF)
                )
                match = re.search(pattern, result)
                if match:
                    # Replace existing section
                    result = result[: match.start()] + chunk + "\n" + result[match.end() :]
                else:
                    # Append new section at end
                    if not result.endswith("\n"):
                        result += "\n"
                    result += "\n" + chunk + "\n"

            return result
        except Exception as exc:
            # MUST raise, not return existing_content: process_file's raw-file
            # deletion happens unconditionally after synthesis "succeeds," so a
            # silent unchanged-content return here made the note's new
            # information vanish -- no error, no retry, ledger recorded a
            # success. Raising sends this through the normal failure path
            # instead: raw file kept, attempt counted, Telegram notified, clean
            # retry next run.
            raise ValueError(
                f"Large-page splice failed for '{topic}': {exc} — refusing to write, "
                "would have silently dropped the new content."
            ) from exc

    # -----------------------------------------------------------------
    # 5a — Normal MERGE (existing page, within size threshold)
    # -----------------------------------------------------------------
    if is_merge:
        prompt = (
            "You are a personal knowledge base curator. "
            "Intelligently merge new information into an existing Wiki document.\n\n"
            "Rules:\n"
            "- Never lose any existing information\n"
            "- Add new info where it logically fits within existing sections\n"
            "- Create new sections if needed\n"
            "- Remove duplicates\n"
            "- Clean Markdown with ## headers\n"
            "- No commentary about what you changed\n"
            + (scope_block + "\n" if scope_block else "\n")
            + f'Existing Wiki for topic "{topic}":\n'
            f"{existing_content[:max_chars]}\n\n"
            "New information to merge:\n"
            f"{new_content[:max_chars]}\n\n"
            "Return the complete updated Wiki document only."
        )
    else:
        # -----------------------------------------------------------------
        # 5c — CREATE branch: new page with related-pages wikilink hint
        # -----------------------------------------------------------------
        related_block = ""
        if catalog:
            # Rank catalog by normalized-title similarity to topic, exclude
            # an exact title match (would be the page being created itself).
            norm_topic = _normalize_title(topic)
            scored = []
            for entry in catalog:
                if entry["title"] == topic:
                    continue
                ratio = difflib.SequenceMatcher(
                    None, norm_topic, _normalize_title(entry["title"])
                ).ratio()
                # Filename-less catalog entries (title-only, seen in the wild)
                # degrade to the title as its own stem -- same as today's
                # [[title]] form -- rather than KeyError.
                filename = entry.get("filename")
                stem = Path(filename).stem if filename else entry["title"]
                scored.append((ratio, entry["title"], stem))
            scored.sort(reverse=True)
            top5 = [(t, s) for r, t, s in scored[:5] if r > 0.0]
            if top5:
                def _related_link(title: str, stem: str) -> str:
                    # Obsidian resolves by filename, not title -- link to the
                    # hyphenated stem and alias back to the display title so
                    # the model doesn't need to guess/rewrite it later.
                    return f"[[{stem}]]" if stem == title else f"[[{stem}|{title}]]"

                related_block = (
                    "Related pages in this wiki: "
                    + ", ".join(_related_link(t, s) for t, s in top5)
                    + ".\n"
                    "Use [[wikilinks]] ONLY for titles from this exact list -- do not "
                    "wikilink anything else, even if it looks like it should be a page "
                    "(e.g. a tool name, a file name, or something else the source "
                    "material mentions). If in doubt, use plain text instead. Use the "
                    "exact link text shown, including the hyphenated target before "
                    "the |. Do not duplicate content from those pages.\n\n"
                )

        prompt = (
            f'You are a personal knowledge base curator. Create a new Wiki document for the topic "{topic}".\n\n'
            "Rules:\n"
            "- Organize into logical ## sections\n"
            "- Clean Markdown formatting\n"
            "- Thorough but concise\n"
            "- No commentary\n\n"
            + related_block
            + "Source material:\n"
            f"{new_content[:max_chars]}\n\n"
            "Return the complete Wiki document only."
        )

    # 8192 tokens gives room for large wiki merges; still check for truncation.
    max_tokens = config["sonnet_max_tokens"]
    text, stop_reason = _call_api(
        config["sonnet_model"],
        [{"role": "user", "content": prompt}],
        max_tokens,
        config,
        client,
    )

    if stop_reason == "max_tokens":
        raise ValueError(
            f"Synthesis for topic '{topic}' hit max_tokens ({max_tokens}) — "
            "skipping write to prevent data loss. Increase sonnet_max_tokens in config.json."
        )

    # Sanity guard: neither branch previously validated the result before it
    # got os.replace'd over an existing page (wiki pages are never backed up --
    # only raw files are, so a bad write here was unrecoverable except via
    # iCloud versioning). An empty/refusal-style response with a normal
    # finish_reason would otherwise silently destroy a mature page, or (on
    # OpenRouter, where content can legitimately be "") replace it with nothing.
    if not text.strip():
        raise ValueError(f"Synthesis for topic '{topic}' returned empty content — refusing to write.")
    if is_merge and len(text) < len(existing_content) * 0.5:
        raise ValueError(
            f"Merge result for topic '{topic}' suspiciously short "
            f"({len(text)} vs {len(existing_content)} chars) — refusing to write to avoid data loss."
        )

    text = _defuse_unknown_wikilinks(
        text, topic, catalog,
        threshold=config["new_page_similarity_threshold"],
        stats=stats,
    )
    return text


# ---------------------------------------------------------------------------
# Telegram notifications (NEXUS's own bot — moved off the old Hermes relay
# 2026-07-27; this module never routed through backend/events.py's
# notify_phone, so Phase 1's Telegram migration didn't touch it until now)
# ---------------------------------------------------------------------------

def send_telegram_notification(
    config: dict[str, Any],
    message: str,
    priority: str = "normal",
    *,
    http_client: httpx.Client | None = None,
) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        logging.getLogger("brain_organizer").debug(
            "TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID not configured — skipping notification"
        )
        return

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": message}
    try:
        if http_client is not None:
            http_client.post(url, json=payload)
        else:
            with httpx.Client(timeout=10.0) as c:
                c.post(url, json=payload)
    except Exception as exc:
        logging.getLogger("brain_organizer").warning("Telegram notification failed: %s", exc)


# ---------------------------------------------------------------------------
# Per-file processing
# ---------------------------------------------------------------------------

def process_file(
    file_path: Path,
    config: dict[str, Any],
    client: anthropic.Anthropic,
    logger: logging.Logger,
    catalog: list[dict[str, Any]],
    *,
    _routes: list[tuple[str, Path, bool]] | None = None,
    _catalog_lock: threading.Lock | None = None,
    _registry_lock: threading.Lock | None = None,
    stats: dict[str, int] | None = None,
) -> list[str]:
    """
    Backup → route to existing/new pages → synthesize ALL wikis first → write all atomically
    → update registry → delete raw.

    Phase 1 (synthesis) must fully succeed before Phase 2 (writes) begins.
    This prevents partial application: if route 2 of 3 fails synthesis, route 1's
    wiki is untouched and the raw file stays for a clean retry.

    catalog is mutated in-place during Phase 2 so later files in the same run
    route against fresh content (prevents same-run duplicate page creation).

    stats: optional mutable accumulator passed straight through to every
        synthesize_wiki call for this file's routes -- callers that want a
        per-file rewritten/backticked tally pass one dict here and read it
        back after this function returns; return type stays `-> list[str]`.
    """
    raw_folder = Path(config["vault_path"]) / config["raw_folder"]
    display_name = file_path.relative_to(raw_folder) if file_path.is_relative_to(raw_folder) else file_path.name
    logger.info("Processing: %s", display_name)

    backup_path = backup_file(config, file_path)
    logger.info("Backed up to: %s", backup_path)

    content = file_path.read_text(encoding="utf-8")

    wiki_folder = Path(config["vault_path"]) / config["wiki_folder"]
    wiki_folder.mkdir(parents=True, exist_ok=True)
    daily_folder = Path(config["vault_path"]) / config["daily_folder"]
    daily_folder.mkdir(parents=True, exist_ok=True)

    if _routes is not None:
        routes = _routes
        logger.info("Routes (pre-computed): %s", [(t, is_new) for (t, _p, is_new) in routes])
    else:
        routes = (
            _deterministic_route(file_path.stem, catalog, wiki_folder, daily_folder)
            or route_topics(content, catalog, config, client)
        )
        logger.info("Routes: %s", [(t, is_new) for (t, _p, is_new) in routes])
    logger.info(
        "Routed '%s' -> [%s]",
        display_name,
        ", ".join(t for (t, _p, _is_new) in routes),
    )

    # Build a quick title -> catalog entry lookup for scope contracts
    catalog_by_title: dict[str, dict[str, Any]] = {p["title"]: p for p in catalog}

    # Phase 1: synthesize ALL routes into memory before touching the filesystem.
    # (topic, wiki_file_path, wiki_content)
    topic_results: list[tuple[str, Path, str]] = []
    for topic, wiki_path, is_new in routes:
        existing = wiki_path.read_text(encoding="utf-8") if wiki_path.exists() else ""
        catalog_entry = None if is_new else catalog_by_title.get(topic)
        wiki_content = synthesize_wiki(
            topic, content, existing, config, client,
            catalog_entry=catalog_entry,
            catalog=catalog,
            stats=stats,
        )
        topic_results.append((topic, wiki_path, wiki_content))
        logger.info("Synthesis complete for route: %s (new=%s)", topic, is_new)

    # Phase 2: all synthesized — write each wiki atomically to its RESOLVED path
    updated_topics: list[tuple[str, Path]] = []
    for topic, wiki_file, wiki_content in topic_results:
        wiki_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = _make_temp_path(wiki_file.parent, f".{wiki_file.stem}_", ".tmp")
        try:
            tmp.write_text(wiki_content, encoding="utf-8")
            os.replace(tmp, wiki_file)
        finally:
            if tmp.exists():
                tmp.unlink(missing_ok=True)
        logger.info("Wiki written: %s", wiki_file)
        updated_topics.append((topic, wiki_file))

        # Refresh the in-memory catalog entry so later files in THIS run route
        # against fresh content (prevents same-run duplicate creation).
        try:
            refreshed = _extract_page_entry(
                wiki_file, config["catalog_summary_chars"]
            )
            def _do_catalog_update():
                replaced = False
                for idx, p in enumerate(catalog):
                    if p["filename"] == refreshed["filename"]:
                        catalog[idx] = refreshed
                        replaced = True
                        break
                if not replaced:
                    catalog.append(refreshed)
            if _catalog_lock is not None:
                with _catalog_lock:
                    _do_catalog_update()
            else:
                _do_catalog_update()
        except Exception as exc:
            logger.warning("catalog refresh failed for %s: %s", wiki_file.name, exc)

    # Phase 3: update _meta/topics-registry.json
    if _registry_lock is not None:
        with _registry_lock:
            update_topics_registry(config, updated_topics)
    else:
        update_topics_registry(config, updated_topics)

    # Phase 4: raw file deleted only after all writes confirmed
    # missing_ok=True: iCloud may have evicted the file between read and delete — still a success
    file_path.unlink(missing_ok=True)
    logger.info("Deleted raw file: %s", file_path.name)

    return [topic for topic, _path in updated_topics]


# ---------------------------------------------------------------------------
# Main runner (injectable for tests)
# ---------------------------------------------------------------------------

def _group_files_by_shared_pages(
    routing_results: list[tuple[Path, str, "list[tuple[str, Path, bool]] | None", "Exception | None"]],
) -> dict[int, list]:
    """Group routed files so any two files that touch the SAME wiki page end up
    in the same group -- prevents a concurrent read-modify-write race on ANY of
    a file's routes, not just its first ("primary") one. The previous grouping
    keyed only on routes[0][1], so a file's 2nd/3rd route landing on a page
    another file's 1st route also targeted was never serialized: both could
    read the page before either wrote, and one write silently clobbered the
    other's contribution with no error.

    A file whose routing failed (routes is None/falsy) gets its own singleton
    group, so a routing failure can never block a real synthesis group.
    """
    n = len(routing_results)
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    path_to_first_idx: dict[str, int] = {}
    for i, (_fp, _sha, routes, _exc) in enumerate(routing_results):
        if not routes:
            continue
        for _title, path, _is_new in routes:
            key = str(path)
            if key in path_to_first_idx:
                union(i, path_to_first_idx[key])
            else:
                path_to_first_idx[key] = i

    groups: dict[int, list] = defaultdict(list)
    for i, item in enumerate(routing_results):
        groups[find(i)].append(item)
    return groups


def _prune_old_backups(config: dict[str, Any], logger: logging.Logger) -> None:
    """Delete raw/backups entries older than backup_retention_days (default 30).

    Backups exist for short-term manual recovery after a bad synthesis, not
    to accumulate forever. Left unpruned, every note ever processed stays
    copied there permanently (unbounded disk growth + iCloud sync churn), and
    scan_raw_folder's rglob() walks the whole pile every single run just to
    skip each entry via the relative_to() check.
    """
    backup_folder = Path(config["vault_path"]) / config["backup_folder"]
    if not backup_folder.exists():
        return
    retention_days: int = config["backup_retention_days"]
    cutoff = time.time() - retention_days * 86400
    pruned = 0
    for f in backup_folder.iterdir():
        try:
            if f.is_file() and f.stat().st_mtime < cutoff:
                f.unlink()
                pruned += 1
        except OSError as exc:
            logger.warning("backup prune: could not remove %s: %s", f.name, exc)
    if pruned:
        logger.info("Pruned %d backup(s) older than %d day(s)", pruned, retention_days)


def _count_raw_remaining(config: dict[str, Any]) -> int:
    """Count files still sitting in raw/ (excluding the backups subfolder),
    via a direct disk listing -- deliberately NOT scan_raw_folder(), which
    skips zero-byte/whitespace-only files (F2). A stuck empty file must
    still show up in this count even though scan_raw_folder will never
    route it.
    """
    raw_folder = Path(config["vault_path"]) / config["raw_folder"]
    backup_folder = Path(config["vault_path"]) / config["backup_folder"]
    count = 0
    for ext in (".md", ".txt"):
        for f in raw_folder.rglob(f"*{ext}"):
            if not f.is_file():
                continue
            try:
                f.relative_to(backup_folder)
                continue
            except ValueError:
                pass
            count += 1
    return count


def _send_run_summary(
    config: dict[str, Any],
    logger: logging.Logger,
    *,
    success_count: int,
    failed_count: int,
    created_count: int,
    merged_count: int,
    uncategorized_count: int,
    catalog_count: int,
    raw_remaining: int,
    links_rewritten: int = 0,
    links_backticked: int = 0,
    all_topics: set[str] | None = None,
    duration: float | None = None,
    _http_client: httpx.Client | None = None,
) -> None:
    """Send the end-of-run Telegram/log summary unconditionally (§6.3/§6.6
    criteria 45-47) -- a silent night, a stuck raw file, a flat page-
    creation rate, or a sustained wikilink-backtick rate must be visible in
    the signal, not indistinguishable from a healthy run.
    """
    lines = [
        "🧠 Brain Organizer — Run complete",
        f"✅ Files processed: {success_count}",
    ]
    if all_topics:
        lines.append(f"📝 Topics updated: {', '.join(sorted(all_topics))}")
    if duration is not None:
        lines.append(f"⏱ Duration: {duration:.1f}s")
    if failed_count:
        lines.append(f"⚠️ Failed: {failed_count}")
    lines.append(
        f"created={created_count} merged={merged_count} "
        f"uncategorized={uncategorized_count} catalog={catalog_count} "
        f"raw_remaining={raw_remaining} links_rewritten={links_rewritten} "
        f"links_backticked={links_backticked}"
    )
    summary = "\n".join(lines)
    logger.info(summary)
    send_telegram_notification(config, summary, http_client=_http_client)


def run(
    config_path: Path | None = None,
    *,
    _client: anthropic.Anthropic | None = None,
    _http_client: httpx.Client | None = None,
    _config: dict[str, Any] | None = None,
) -> int:
    """Run the organizer. Returns 0 on full success, 1 if any file failed."""
    config = validate_config(_config if _config is not None else load_config(config_path))
    logger = setup_logging(config)

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key and _client is None:
        logger.error("ANTHROPIC_API_KEY environment variable not set")
        return 1

    client = _client or anthropic.Anthropic(api_key=api_key)
    processed = load_processed(config)
    files = scan_raw_folder(config, processed)

    if not files:
        logger.info("Nothing to process")
        _send_run_summary(
            config, logger,
            success_count=0, failed_count=0,
            created_count=0, merged_count=0, uncategorized_count=0,
            # catalog not built on this path (avoids the disk-scan cost on a
            # run with nothing to do -- see the comment below); raw_remaining
            # is still a direct disk count, independent of that.
            catalog_count=0, raw_remaining=_count_raw_remaining(config),
            links_rewritten=0, links_backticked=0,
            _http_client=_http_client,
        )
        return 0

    _prune_old_backups(config, logger)

    # Build catalog ONCE after the empty-check so we don't pay the disk scan
    # cost on runs that have nothing to do.
    wiki_folder = Path(config["vault_path"]) / config["wiki_folder"]
    daily_folder = Path(config["vault_path"]) / config["daily_folder"]
    daily_folder.mkdir(parents=True, exist_ok=True)
    meta_folder = Path(config["vault_path"]) / config["meta_folder"]
    catalog = build_wiki_catalog(wiki_folder, meta_folder, config["catalog_summary_chars"])
    if not catalog and any(wiki_folder.glob("*.md")):
        # build_wiki_catalog only returns [] on a legitimately empty
        # wiki_folder or on its own catastrophic except-path (logged as
        # error there). Since wiki_folder demonstrably has *.md files,
        # this is the catastrophic case -- routing/synthesis against an
        # empty catalog would treat every page as new and duplicate the
        # whole wiki, so abort before any writes happen (§5.3 criterion
        # 34 / §6.4 criterion 48).
        logger.error(
            "Wiki catalog build returned no pages but wiki/ contains "
            "markdown files -- aborting run before routing/synthesis"
        )
        send_telegram_notification(
            config,
            "🧠 Brain Organizer — 🚨 Wiki catalog build failed\n"
            "wiki/ has markdown files but the catalog came back empty. "
            "Aborting run to avoid duplicating pages.",
            priority="high",
            http_client=_http_client,
        )
        return 1
    logger.info("Wiki catalog: %d page(s)", len(catalog))

    logger.info("Found %d new file(s) to process", len(files))
    start = time.monotonic()
    success_count = 0
    failed_count = 0
    all_topics: set[str] = set()
    created_count = 0
    merged_count = 0
    uncategorized_count = 0
    links_rewritten_count = 0
    links_backticked_count = 0

    # One unified flow for both sequential (max_parallel_files<=1) and
    # parallel runs. These used to be two independently-maintained ~60-line
    # implementations that had already drifted: the parallel path was missing
    # the sequential path's _APIUsageCapped abort handling (a hard provider
    # cap was treated as an ordinary per-file failure, burning attempts
    # against every remaining file), and a routing failure in the parallel
    # path returned an empty route list that process_file treated as
    # "nothing to route" -- deleting the raw file and recording success while
    # silently losing the note's content. max_workers=1 below still goes
    # through ThreadPoolExecutor, but with one worker processing one group at
    # a time it's observably sequential (same ordering, same behavior) --
    # one implementation to keep correct instead of two.
    max_workers = max(1, config["max_parallel_files"])

    catalog_lock = threading.Lock()
    registry_lock = threading.Lock()
    state_lock = threading.Lock()  # protects success_count, failed_count, all_topics, processed, saves_since_flush
    aborted = threading.Event()
    saves_since_flush = 0
    SAVE_BATCH = 5  # batch success-path ledger saves; failures still save immediately (see _record_failure)

    def _flush_processed(force: bool = False) -> None:
        nonlocal saves_since_flush
        # Caller must hold state_lock.
        if force or saves_since_flush >= SAVE_BATCH:
            save_processed(config, processed)
            saves_since_flush = 0

    # Phase A: route every file up front (Haiku, read-only -- safe to
    # parallelize regardless of max_workers since routing never writes).
    def _route_one(
        fp_sha: tuple[Path, str],
    ) -> tuple[Path, str, list | None, Exception | None]:
        fp, sha = fp_sha
        try:
            content = fp.read_text(encoding="utf-8")
            routes = _deterministic_route(fp.stem, list(catalog), wiki_folder, daily_folder) or \
                route_topics(content, list(catalog), config, client)
            logger.info("Routed %s -> %s", fp.name, [t for t, _p, _n in routes])
            return fp, sha, routes, None
        except Exception as exc:
            logger.error("Routing failed for %s: %s", fp.name, exc)
            return fp, sha, None, exc

    route_workers = min(max_workers * 2, 8)
    with ThreadPoolExecutor(max_workers=route_workers) as ex:
        routing_results = list(ex.map(_route_one, files))

    # Group files so ANY shared target page (not just each file's first/
    # "primary" route) is serialized within one group -- a file's 2nd or 3rd
    # route landing on the same page as another file's 1st route used to race
    # unprotected: both could read the page before either wrote, and one
    # write silently clobbered the other's contribution.
    groups = _group_files_by_shared_pages(routing_results)
    logger.info(
        "Routed %d file(s) into %d page group(s), %d worker(s)",
        len(files), len(groups), max_workers,
    )

    def _record_success(
        sha: str, fp: Path, updated: list[str], routes: list[tuple[str, Path, bool]],
        file_stats: dict[str, int] | None = None,
    ) -> None:
        nonlocal success_count, created_count, merged_count, uncategorized_count
        nonlocal links_rewritten_count, links_backticked_count
        with state_lock:
            processed[sha] = {
                "filename": fp.name,
                "timestamp": datetime.now(UTC).isoformat(),
                "topics": updated,
            }
            success_count += 1
            all_topics.update(updated)
            # §6.3 route buckets, counted per-route (not per-file) on this
            # succeeded file's already-resolved routes: Uncategorized fallback
            # routes always carry is_new=True, so title is checked first to
            # keep it a distinct bucket from a genuine new-page creation.
            for title, _path, is_new in routes:
                if title == "Uncategorized":
                    uncategorized_count += 1
                elif is_new:
                    created_count += 1
                else:
                    merged_count += 1
            # §6.3/47: this file's _defuse_unknown_wikilinks tally, accumulated
            # into the run-wide totals under the same lock as every other
            # counter above -- file_stats itself was only ever touched by this
            # file's own worker thread (never shared across files), so this
            # is the sole point it needs to be thread-safe.
            if file_stats:
                links_rewritten_count += file_stats.get("rewritten", 0)
                links_backticked_count += file_stats.get("backticked", 0)
            # Batched, not per-file: the raw file is already deleted by this
            # point, which is the real idempotency marker (scan_raw_folder
            # simply never sees it again) -- losing a few success records to
            # a crash only undercounts /status stats, it can never cause a
            # reprocess.
            _flush_processed()

    def _record_failure(sha: str, fp: Path, exc: Exception) -> None:
        nonlocal failed_count
        with state_lock:
            existing = processed.get(sha, {})
            attempts = existing.get("attempts", 0) + 1
            processed[sha] = {
                "filename": fp.name,
                "status": "failed",
                "attempts": attempts,
                "timestamp": datetime.now(UTC).isoformat(),
                "error": str(exc)[:500],
            }
            failed_count += 1
            # Failures save immediately, unbatched: "attempts" must be
            # accurate for the max_file_attempts permanent-skip cap to work.
            _flush_processed(force=True)
        send_telegram_notification(
            config,
            f"🧠 Brain Organizer — ⚠️ Error\nFile: {fp.name}\nError: {exc}",
            priority="high",
            http_client=_http_client,
        )

    def _process_group(group_items: list) -> None:
        for fp, sha, routes, route_exc in group_items:
            if aborted.is_set():
                return
            try:
                if route_exc is not None:
                    raise route_exc
                if not routes:
                    # route_topics always returns at least the Uncategorized
                    # fallback on success, so an empty/None route list here
                    # can only mean routing raised and was swallowed
                    # upstream. Treat it as a failure (raw file kept, retried
                    # next run) instead of silently deleting the raw file
                    # with nowhere for its content to have gone.
                    raise RuntimeError("routing produced no routes")
                # Per-file, not shared: this dict is only ever touched by
                # this file's own worker thread before _record_success
                # aggregates it under state_lock -- no lock needed here.
                file_stats: dict[str, int] = {"rewritten": 0, "backticked": 0}
                updated = process_file(
                    fp, config, client, logger, catalog,
                    _routes=routes,
                    _catalog_lock=catalog_lock,
                    _registry_lock=registry_lock,
                    stats=file_stats,
                )
                _record_success(sha, fp, updated, routes, file_stats)
            except _APIUsageCapped as exc:
                logger.error("API hard-capped — aborting run: %s", exc)
                if not aborted.is_set():
                    aborted.set()
                    send_telegram_notification(
                        config,
                        f"🧠 Brain Organizer — API capped, run aborted.\n{exc}",
                        priority="high",
                        http_client=_http_client,
                    )
                return
            except Exception as exc:
                logger.error("Failed to process %s: %s", fp.name, exc, exc_info=True)
                _record_failure(sha, fp, exc)

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs = [ex.submit(_process_group, items) for items in groups.values()]
        for fut in as_completed(futs):
            try:
                fut.result()
            except Exception as exc:
                logger.error("Page group error: %s", exc)

    with state_lock:
        _flush_processed(force=True)

    duration = time.monotonic() - start

    _send_run_summary(
        config, logger,
        success_count=success_count, failed_count=failed_count,
        created_count=created_count, merged_count=merged_count,
        uncategorized_count=uncategorized_count,
        catalog_count=len(catalog), raw_remaining=_count_raw_remaining(config),
        links_rewritten=links_rewritten_count, links_backticked=links_backticked_count,
        all_topics=all_topics, duration=duration,
        _http_client=_http_client,
    )

    return 1 if failed_count else 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Brain Organizer — process raw vault files")
    parser.add_argument("--config", type=Path, default=None, help="Path to config.json")
    args = parser.parse_args()

    lock_path = Path(__file__).parent / ".organizer.lock"
    if lock_path.exists():
        try:
            pid = int(lock_path.read_text().strip())
            import psutil  # type: ignore[import-untyped]
            lock_age_s = time.time() - lock_path.stat().st_mtime
            if psutil.pid_exists(pid) and lock_age_s < 3600:
                print(f"Brain Organizer already running (PID {pid}) — skipping.", flush=True)
                sys.exit(0)
            elif psutil.pid_exists(pid):
                print(f"Brain Organizer lock held by PID {pid} for {lock_age_s/60:.0f}m — treating as stale and reclaiming.", flush=True)
        except Exception:
            pass  # stale lock — proceed

    lock_path.write_text(str(os.getpid()))
    try:
        sys.exit(run(args.config))
    finally:
        lock_path.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
