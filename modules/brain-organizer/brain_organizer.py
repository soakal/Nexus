"""
Brain Organizer — main processing script.

Reads raw intake files from vault/raw/, uses Claude AI to detect topics and
synthesize wiki files, then cleans up and reports via Hermes.

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
from typing import Any

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
        logging.FileHandler(log_file, encoding="utf-8"),
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
    topics: list[str],
    wiki_folder: Path,
) -> None:
    """Upsert topic → wiki file path in _meta/topics-registry.json (atomic write)."""
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

    for topic in topics:
        safe_name = sanitize_topic_name(topic)
        registry[topic] = str(wiki_folder / f"{safe_name}.md")

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
    max_attempts: int = config.get("max_file_attempts", 5)
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
            # else: success record — skip
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
# Title normalisation — used by build_wiki_catalog, find_similar_page,
# consolidate_wiki.py (imported from here).
# ---------------------------------------------------------------------------

_STEM_SUFFIXES = ("tion", "ing", "ion", "ed", "es", "s")


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
# Per-page catalog entry extractor
# ---------------------------------------------------------------------------

def _extract_page_entry(f: Path, summary_chars: int = 300) -> dict[str, Any]:
    """Parse a wiki .md file and return a catalog entry dict.

    Keys: title, filename, path_str, headers, summary.
    """
    text = f.read_text(encoding="utf-8")
    lines = text.splitlines()

    # --- title: first line starting with exactly one "#" ---
    title = f.stem
    h1_index = 0
    for i, line in enumerate(lines):
        if line.startswith("# ") and not line.startswith("## "):
            title = line[2:].strip()
            h1_index = i
            break

    # --- headers: all "## " lines (first 10, joined) ---
    raw_headers = [
        ln[3:].strip() for ln in lines if ln.startswith("## ")
    ][:10]
    headers_joined = " | ".join(raw_headers)

    # --- summary: first prose paragraph after the H1 ---
    _meta_re = re.compile(r"^\*\*[\w\s]+:\*\*|^>\s*\*\*[\w\s]+:\*\*")
    _rule_re = re.compile(r"^[-*_]{3,}$")
    prose_lines: list[str] = []
    collecting = False
    for line in lines[h1_index + 1 :]:
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
    }


# ---------------------------------------------------------------------------
# Wiki catalog builder (incremental, cached in _meta/wiki-catalog.json)
# ---------------------------------------------------------------------------

def build_wiki_catalog(wiki_folder: Path, meta_folder: Path) -> list[dict[str, Any]]:
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
                    pages.append(_extract_page_entry(f))
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
                    {"built_at": datetime.now(UTC).isoformat(), "pages": pages},
                    fh, indent=2, ensure_ascii=False,
                )
            os.replace(tmp, cache_path)
        finally:
            if tmp.exists():
                tmp.unlink(missing_ok=True)

        return pages
    except Exception as exc:
        logging.getLogger("brain_organizer").warning(
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
    backend/agents/wiki_ingest.py::_is_daily_note)."""
    return bool(_DAILY_NOTE_STEM_PAT.match(stem) or _DAILY_NOTE_NAME_PAT.search(stem))


def _daily_note_route(
    stem: str,
    catalog: list[dict[str, Any]],
    wiki_folder: Path,
) -> list[tuple[str, Path, bool]] | None:
    """Deterministic route for a daily note. Returns None for non-daily notes.

    route_topics' near-duplicate guard (find_similar_page) compares titles,
    but a dated title like "Daily-Operations-Log-2026-07-08" is unique every
    day by construction, so it never fires and the model is free to invent a
    new page daily. Skip the model call entirely for daily notes and route
    straight to the canonical date-stamped page (matching this vault's
    existing YYYY-MM-DD.md convention).
    """
    if not _is_daily_note(stem):
        return None
    m = _DATE_IN_STEM_PAT.search(stem)
    title = m.group(0) if m else "Daily-Log"
    filename = f"{title}.md"
    for entry in catalog:
        if entry["filename"] == filename:
            return [(entry["title"], Path(entry["path_str"]), False)]
    return [(title, wiki_folder / filename, True)]


# ---------------------------------------------------------------------------
# Catalog-aware routing (Haiku)
# ---------------------------------------------------------------------------

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

    # Build numbered catalog block (capped at config limit, already sorted by title)
    max_in_prompt: int = config.get("catalog_max_pages_in_prompt", 60)
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
        config.get("route_max_tokens", 1024),
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

    # Build lookup: first entry wins on duplicate titles
    by_title: dict[str, dict[str, Any]] = {}
    for p in catalog:
        if p["title"] not in by_title:
            by_title[p["title"]] = p

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
            if title in by_title:
                entry = by_title[title]
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
                title, catalog, config.get("new_page_similarity_threshold", 0.82)
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
    catalog = build_wiki_catalog(wiki_folder, meta_folder)

    routes = route_topics(content, catalog, config, client)
    return [title for (title, _path, _is_new) in routes]


# ---------------------------------------------------------------------------
# Wiki synthesis (Sonnet)
# ---------------------------------------------------------------------------

_WIKILINK_PAT = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|([^\]]*))?\]\]")


def _defuse_unknown_wikilinks(
    text: str,
    topic: str,
    catalog: list[dict[str, Any]] | None,
) -> str:
    """Rewrite any [[wikilink]] the model generated that doesn't resolve to a
    real (or near-duplicate) catalog page into plain `backtick` text instead.

    The CREATE branch's "use [[wikilinks]] to reference related pages"
    instruction gets over-applied: source material often MENTIONS names that
    aren't vault pages at all (e.g. Claude Code memory-file names like
    "project_version_scheme" mentioned in a session note), and the model
    wikilinks them anyway -- producing a permanently broken link every time,
    since nothing by that name will ever exist as a page. Never rely on the
    prompt instruction alone to prevent this -- same deterministic-backstop
    pattern as the daily-note guard and the night-exemption filter elsewhere
    in this codebase. Applied to every synthesis result (merge and create
    alike), not just the branch that introduced the instruction, since a
    merge can just as easily echo a hallucinated link from its input.
    """
    catalog = catalog or []
    known_titles = {p["title"] for p in catalog}
    known_titles.add(topic)  # the page being written links to itself, harmless

    def _replace(m: "re.Match[str]") -> str:
        target = m.group(1).strip()
        alias = m.group(2)
        display = (alias or target).strip()
        if target in known_titles:
            return m.group(0)
        if find_similar_page(target, catalog) is not None:
            return m.group(0)
        return f"`{display}`"

    return _WIKILINK_PAT.sub(_replace, text)


def synthesize_wiki(
    topic: str,
    new_content: str,
    existing_content: str,
    config: dict[str, Any],
    client: anthropic.Anthropic,
    *,
    catalog_entry: dict[str, Any] | None = None,
    catalog: list[dict[str, Any]] | None = None,
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
    """
    max_chars: int = config.get("max_file_chars", 50000)
    large_threshold: int = config.get("large_page_threshold_chars", 35000)

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
        max_tokens: int = config.get("sonnet_max_tokens", 8192)
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
                # Extract the header line (first line of the chunk)
                header_line = chunk.splitlines()[0].rstrip()
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
            # instead: raw file kept, attempt counted, Hermes notified, clean
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
                scored.append((ratio, entry["title"]))
            scored.sort(reverse=True)
            top5 = [t for _, t in scored[:5] if _ > 0.0]
            if top5:
                related_block = (
                    "Related pages in this wiki: "
                    + ", ".join(f"[[{t}]]" for t in top5)
                    + ".\n"
                    "Use [[wikilinks]] ONLY for titles from this exact list -- do not "
                    "wikilink anything else, even if it looks like it should be a page "
                    "(e.g. a tool name, a file name, or something else the source "
                    "material mentions). If in doubt, use plain text instead. "
                    "Do not duplicate content from those pages.\n\n"
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
    max_tokens = config.get("sonnet_max_tokens", 8192)
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

    text = _defuse_unknown_wikilinks(text, topic, catalog)
    return text


# ---------------------------------------------------------------------------
# Hermes notifications
# ---------------------------------------------------------------------------

def _get_hermes_host(config: dict[str, Any]) -> str:
    host = os.environ.get("HERMES_HOST") or config.get("hermes_host", "")
    return "" if host == "http://HERMES_HOST_HERE" else host


def send_hermes_notification(
    config: dict[str, Any],
    message: str,
    priority: str = "normal",
    *,
    http_client: httpx.Client | None = None,
) -> None:
    host = _get_hermes_host(config)
    if not host:
        logging.getLogger("brain_organizer").debug("Hermes host not configured — skipping notification")
        return

    secret = os.environ.get("HERMES_WEBHOOK_SECRET", "")
    headers = {"X-Webhook-Secret": secret} if secret else {}
    payload = {"message": message, "priority": priority}
    try:
        if http_client is not None:
            http_client.post(f"{host}/hermes/notify", json=payload, headers=headers)
        else:
            with httpx.Client(timeout=10.0) as c:
                c.post(f"{host}/hermes/notify", json=payload, headers=headers)
    except Exception as exc:
        logging.getLogger("brain_organizer").warning("Hermes notification failed: %s", exc)


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
) -> list[str]:
    """
    Backup → route to existing/new pages → synthesize ALL wikis first → write all atomically
    → update registry → delete raw.

    Phase 1 (synthesis) must fully succeed before Phase 2 (writes) begins.
    This prevents partial application: if route 2 of 3 fails synthesis, route 1's
    wiki is untouched and the raw file stays for a clean retry.

    catalog is mutated in-place during Phase 2 so later files in the same run
    route against fresh content (prevents same-run duplicate page creation).
    """
    raw_folder = Path(config["vault_path"]) / config["raw_folder"]
    display_name = file_path.relative_to(raw_folder) if file_path.is_relative_to(raw_folder) else file_path.name
    logger.info("Processing: %s", display_name)

    backup_path = backup_file(config, file_path)
    logger.info("Backed up to: %s", backup_path)

    content = file_path.read_text(encoding="utf-8")

    wiki_folder = Path(config["vault_path"]) / config["wiki_folder"]
    wiki_folder.mkdir(parents=True, exist_ok=True)

    if _routes is not None:
        routes = _routes
        logger.info("Routes (pre-computed): %s", [(t, is_new) for (t, _p, is_new) in routes])
    else:
        routes = (
            _daily_note_route(file_path.stem, catalog, wiki_folder)
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
        )
        topic_results.append((topic, wiki_path, wiki_content))
        logger.info("Synthesis complete for route: %s (new=%s)", topic, is_new)

    # Phase 2: all synthesized — write each wiki atomically to its RESOLVED path
    updated_topics: list[str] = []
    for topic, wiki_file, wiki_content in topic_results:
        tmp = _make_temp_path(wiki_folder, f".{wiki_file.stem}_", ".tmp")
        try:
            tmp.write_text(wiki_content, encoding="utf-8")
            os.replace(tmp, wiki_file)
        finally:
            if tmp.exists():
                tmp.unlink(missing_ok=True)
        logger.info("Wiki written: %s", wiki_file)
        updated_topics.append(topic)

        # Refresh the in-memory catalog entry so later files in THIS run route
        # against fresh content (prevents same-run duplicate creation).
        try:
            refreshed = _extract_page_entry(
                wiki_file, config.get("catalog_summary_chars", 300)
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
            update_topics_registry(config, updated_topics, wiki_folder)
    else:
        update_topics_registry(config, updated_topics, wiki_folder)

    # Phase 4: raw file deleted only after all writes confirmed
    # missing_ok=True: iCloud may have evicted the file between read and delete — still a success
    file_path.unlink(missing_ok=True)
    logger.info("Deleted raw file: %s", file_path.name)

    return updated_topics


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
    retention_days: int = config.get("backup_retention_days", 30)
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


def run(
    config_path: Path | None = None,
    *,
    _client: anthropic.Anthropic | None = None,
    _http_client: httpx.Client | None = None,
    _config: dict[str, Any] | None = None,
) -> int:
    """Run the organizer. Returns 0 on full success, 1 if any file failed."""
    config = _config if _config is not None else load_config(config_path)
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
        return 0

    _prune_old_backups(config, logger)

    # Build catalog ONCE after the empty-check so we don't pay the disk scan
    # cost on runs that have nothing to do.
    wiki_folder = Path(config["vault_path"]) / config["wiki_folder"]
    meta_folder = Path(config["vault_path"]) / config["meta_folder"]
    catalog = build_wiki_catalog(wiki_folder, meta_folder)
    logger.info("Wiki catalog: %d page(s)", len(catalog))

    logger.info("Found %d new file(s) to process", len(files))
    start = time.monotonic()
    success_count = 0
    failed_count = 0
    all_topics: set[str] = set()

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
    max_workers = max(1, config.get("max_parallel_files", 1))

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
            routes = _daily_note_route(fp.stem, list(catalog), wiki_folder) or \
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

    def _record_success(sha: str, fp: Path, updated: list[str]) -> None:
        nonlocal success_count
        with state_lock:
            processed[sha] = {
                "filename": fp.name,
                "timestamp": datetime.now(UTC).isoformat(),
                "topics": updated,
            }
            success_count += 1
            all_topics.update(updated)
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
        send_hermes_notification(
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
                updated = process_file(
                    fp, config, client, logger, catalog,
                    _routes=routes,
                    _catalog_lock=catalog_lock,
                    _registry_lock=registry_lock,
                )
                _record_success(sha, fp, updated)
            except _APIUsageCapped as exc:
                logger.error("API hard-capped — aborting run: %s", exc)
                if not aborted.is_set():
                    aborted.set()
                    send_hermes_notification(
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

    if success_count > 0:
        topics_str = ", ".join(sorted(all_topics))
        summary = (
            f"🧠 Brain Organizer — Run complete\n"
            f"✅ Files processed: {success_count}\n"
            f"📝 Topics updated: {topics_str}\n"
            f"⏱ Duration: {duration:.1f}s"
        )
        if failed_count:
            summary += f"\n⚠️ Failed: {failed_count}"
        logger.info(summary)
        send_hermes_notification(config, summary, http_client=_http_client)

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
