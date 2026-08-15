import asyncio
import logging
import math
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

import httpx

from backend.cache import async_ttl_cache
from backend.http_client import SSL_CONTEXT

logger = logging.getLogger(__name__)

_BRAIN_SUBDIR = "Brain"
# Directories to skip entirely during vault_search -- these hold raw session
# transcripts / backups / app internals, never curated notes, and (being long
# and dense) are exactly what won under the old raw-frequency scoring.
_EXCLUDED_DIRS = frozenset({"backups", "_meta", ".trash", ".obsidian", "templates"})
MAX_NOTE_BYTES = 200_000        # curated notes are never this big; skips 1MB+ backup transcripts
LENGTH_REF_CHARS = 4000.0       # note length at which the length penalty is 1.0 (no penalty)
FILENAME_MATCH_WEIGHT = 3.0
BODY_MATCH_WEIGHT = 1.0
RECENCY_WEIGHT = 0.25
MIN_COVERAGE = 0.5              # a note must match >= half the query's content tokens
MAX_CONTEXT_CHARS = 300

# Function words only -- never add content words. This is deliberately a
# DIFFERENT (larger) list than backend.agents.facts._CANONICAL_STOPWORDS,
# which serves a different purpose (dedup-key normalization) -- do not merge.
_QUERY_STOPWORDS = frozenset("""
about after again all also and any are because been before being both but can could did
does done during each few for from get got had has have her here his how into its just let
many more most much nor not now off one only other our out over own said same say she
should some still such tell than that the their them then there these they this those told
too two under upon very was were what when where which while who whom whose why will with
would you your
""".split())


def _vault() -> Path:
    from backend.config import get_settings
    return Path(get_settings().obsidian_vault_path)


def _search_root(vault: Path) -> Path:
    """Scope search to the curated Brain/ folder when it exists; fall back to
    the full vault root otherwise (defensive -- matches this module's existing
    degrade-gracefully convention)."""
    brain = vault / _BRAIN_SUBDIR
    return brain if brain.is_dir() else vault


def _tokenize_query(query: str) -> set[str]:
    """Content-word tokens: lowercase, split, drop short/stopword tokens."""
    return {t for t in query.lower().split() if len(t) > 2 and t not in _QUERY_STOPWORDS}


def _hit(term: str, haystack: str, word_bound: bool) -> bool:
    if word_bound:
        return re.search(rf"\b{re.escape(term)}\b", haystack) is not None
    return term in haystack


def _score_note(
    query_tokens: set[str],
    filename: str,
    text_lower: str,
    mtime: float,
    now: float,
    *,
    word_bound: bool = False,
) -> float:
    """Pure, no I/O. Coverage-gated (a note must match >= MIN_COVERAGE of the
    query's tokens or scores 0.0 outright) so raw body-frequency in a long
    file can never swamp relevance -- every remaining component is bounded,
    so no note can win purely by repeating a term. Filename precision
    (fraction of the FILENAME's own tokens matched) is what makes a precisely-
    named note beat a long note whose name happens to contain the term once."""
    if not query_tokens:
        return 0.0

    name_lower = filename.lower()
    name_base = name_lower[:-3] if name_lower.endswith(".md") else name_lower
    name_tokens = [t for t in re.split(r"[^a-z0-9]+", name_base) if t] or ["_"]

    name_matched = {t for t in query_tokens if _hit(t, name_lower, word_bound)}
    body_matched = {t for t in query_tokens if _hit(t, text_lower, word_bound)}

    coverage = len(name_matched | body_matched) / len(query_tokens)
    if coverage < MIN_COVERAGE:
        return 0.0

    length_penalty = 1.0 / (1.0 + max(0.0, math.log10(max(len(text_lower), 1) / LENGTH_REF_CHARS)))

    name_precision = min(1.0, len(name_matched) / len(name_tokens))
    name_score = FILENAME_MATCH_WEIGHT * (len(name_matched) / len(query_tokens)) * (0.25 + 0.75 * name_precision)
    body_score = BODY_MATCH_WEIGHT * (len(body_matched) / len(query_tokens)) * length_penalty

    age_days = max(0.0, (now - mtime) / 86400)
    recency = RECENCY_WEIGHT / (1.0 + age_days)

    return name_score + body_score + recency


def _best_match_context(lines: list[str], query_tokens: set[str], *, word_bound: bool = False) -> str:
    """The line matching the MOST distinct query tokens (not just the first
    match), skipping markup/table/code-fence noise lines. +/-1 line of
    context, truncated."""
    best_n = 0
    best_i = -1
    for i, ln in enumerate(lines):
        stripped = ln.strip()
        if not stripped or stripped.startswith(("<", "---", "```", "|")):
            continue
        ln_lower = ln.lower()
        n = sum(1 for t in query_tokens if _hit(t, ln_lower, word_bound))
        if n > best_n:
            best_n = n
            best_i = i

    if best_i < 0:
        return ""

    start = max(0, best_i - 1)
    end = min(len(lines), best_i + 2)
    ctx = " ... ".join(
        l.strip() for l in lines[start:end]
        if l.strip() and not l.strip().startswith(("<", "---", "```", "|"))
    )
    return ctx[:MAX_CONTEXT_CHARS]


def _mcp_url() -> str:
    from backend.config import get_settings
    return get_settings().brain_mcp_url.rstrip("/")


def _mcp_headers() -> dict:
    from backend.config import get_settings
    token = get_settings().brain_mcp_token
    return {"Authorization": f"Bearer {token}"} if token else {}


@dataclass
class ObsidianData:
    daily_note: str | None = None
    recent_notes: list = field(default_factory=list)
    open_tasks: list = field(default_factory=list)


def _fetch_sync() -> ObsidianData:
    vault = _vault()
    if not vault.exists():
        raise Exception(f"Obsidian vault not found at {vault}")

    md_files = sorted(vault.rglob("*.md"), key=lambda f: f.stat().st_mtime, reverse=True)
    recent_notes = [str(f.relative_to(vault)) for f in md_files[:10]]

    today = date.today().strftime("%Y-%m-%d")
    daily_note = None
    open_tasks = []
    for candidate in [vault / "Brain" / "raw" / f"{today}.md", vault / f"{today}.md"]:
        if candidate.exists():
            content = candidate.read_text(encoding="utf-8")
            daily_note = content
            open_tasks = [ln.strip() for ln in content.splitlines() if ln.strip().startswith("- [ ]")]
            break

    return ObsidianData(daily_note=daily_note, recent_notes=recent_notes, open_tasks=open_tasks)


@async_ttl_cache(60)
async def fetch() -> ObsidianData:
    return await asyncio.to_thread(_fetch_sync)


@async_ttl_cache(30)
async def health_check() -> bool:
    try:
        async with httpx.AsyncClient(verify=SSL_CONTEXT, timeout=3) as client:
            resp = await client.get(f"{_mcp_url()}/health")
            return resp.status_code == 200
    except Exception:
        return False


async def write_daily_note(content: str) -> None:
    today = date.today().strftime("%Y-%m-%d")
    await _post_raw(content, filename=f"{today}.md")


async def complete_task(note_path: str, task_text: str) -> None:
    vault = _vault().resolve()
    path = (vault / note_path).resolve()
    if not path.is_relative_to(vault):
        # note_path is an LLM tool-call arg (write_tools.py) -- an absolute path
        # or a "../" sequence would otherwise let Path's own "/" operator escape
        # the vault entirely (an absolute right-hand side replaces the left side).
        logger.warning(f"complete_task: rejected note_path outside vault: {note_path!r}")
        return
    if path.exists():
        content = path.read_text(encoding="utf-8")
        updated = content.replace(f"- [ ] {task_text}", f"- [x] {task_text}")
        path.write_text(updated, encoding="utf-8")


def _search_sync(query: str, max_results: int) -> str:
    vault = _vault()
    if not vault.exists():
        return f"Obsidian vault not found at {vault}."

    query_tokens = _tokenize_query(query)
    word_bound = False
    if not query_tokens:
        # All tokens were too short/stopwords (or the query is empty). Fall
        # back to a single whole-word match on the raw query instead of
        # returning nothing -- a raw substring fallback was tried and
        # measurably worse (matches "AI" inside "Tailscale"/"Brain").
        q = query.strip().lower()
        if not q:
            return f"No notes found matching '{query}'."
        query_tokens = {q}
        word_bound = True

    root = _search_root(vault)
    now = time.time()
    candidates = []
    try:
        for md_file in root.rglob("*.md"):
            try:
                rel_to_root = md_file.relative_to(root)
                if any(p.lower() in _EXCLUDED_DIRS for p in rel_to_root.parts[:-1]):
                    continue
                st = md_file.stat()
                if st.st_size > MAX_NOTE_BYTES:
                    continue
                text = md_file.read_text(encoding="utf-8", errors="ignore")
                text_lower = text.lower()
                score = _score_note(
                    query_tokens, md_file.name, text_lower, st.st_mtime, now, word_bound=word_bound
                )
                if score <= 0.0:
                    continue
                lines = text.splitlines()
                ctx = _best_match_context(lines, query_tokens, word_bound=word_bound)
                rel = str(md_file.relative_to(vault))
                candidates.append((score, st.st_mtime, rel, ctx))
            except Exception:
                continue
    except Exception as e:
        logger.warning(f"Vault search failed: {e}")
        return f"Vault search unavailable: {e}"

    if not candidates:
        return f"No notes found matching '{query}'."

    # Sort: score desc, then mtime desc (most recent first on ties)
    candidates.sort(key=lambda x: (x[0], x[1]), reverse=True)
    results = []
    for _, _, rel, ctx in candidates[:max_results]:
        results.append(f"**{rel}**\n{ctx}" if ctx else f"**{rel}**")
    return "\n\n".join(results)


async def vault_search(query: str, max_results: int = 10) -> str:
    """Runs the filesystem walk off the event loop -- this fires on every
    chat message and the walk is synchronous, so it must never run inline
    on the asyncio loop (project hard rule, see CLAUDE.md)."""
    return await asyncio.to_thread(_search_sync, query, max_results)


async def write_facts_digest(content: str) -> None:
    """Weekly facts-digest note via POST /raw. Filename: facts-digest-{UTC
    timestamp}.md, always into Brain/raw/ (never Brain/wiki/ directly -- the
    nightly brain_organizer job is the sole raw->wiki consumer; the MCP
    server's own collision-rename is the backstop if two land the same
    second). Modeled on create_note, not emit_event: PROPAGATES failures
    (does not swallow) so backend/agents/facts_digest.py's caller can decide
    whether to advance its watermark -- a silently-lost write must never be
    treated as "already digested"."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    await _post_raw(content, filename=f"facts-digest-{ts}.md")


async def write_fragmentation_report(content: str) -> None:
    """Weekly wiki-fragmentation report via POST /raw (Brain/raw/, digested
    into Brain/wiki/ by the next brain_organizer run, same as every other
    raw note) -- replaces wiki_ingest.py's old direct pathlib append straight
    to Brain/wiki/Inbox.md, which bypassed the :8765 MCP write surface every
    other writer in this codebase goes through. Modeled on
    write_facts_digest: propagates failures rather than swallowing them, so
    the caller's own best-effort try/except (weekly_fragmentation_report's)
    is the single place this can fail silently, not two.
    """
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    await _post_raw(content, filename=f"wiki-fragmentation-report-{ts}.md")


async def create_note(title: str, content: str, folder: str = "NEXUS") -> str:
    safe_title = title.replace("/", "-").replace("\\", "-")
    filename = f"{folder}/{safe_title}.md" if folder else f"{safe_title}.md"
    await _post_raw(content, filename=filename)
    return filename


async def _post_raw(content: str, filename: str, timeout: float = 10) -> None:
    async with httpx.AsyncClient(verify=SSL_CONTEXT, timeout=timeout) as client:
        resp = await client.post(
            f"{_mcp_url()}/raw",
            json={"content": content, "filename": filename},
            headers=_mcp_headers(),
        )
        resp.raise_for_status()


def _format_event(
    event_type: str, title: str, body: str, when: datetime | None = None
) -> tuple[str, str]:
    """Pure formatter for a Brain event note. No I/O -- unit-testable without HTTP.

    Returns (content, filename) matching the shared event contract in
    00-OVERVIEW.md: a fixed markdown template with a "Powered by CwiAI"
    footer, and filename `event-nexus-{type-slug}-{yyyyMMddTHHmmssZ}.md`.
    """
    ts = when or datetime.now(timezone.utc)
    when_iso = ts.strftime("%Y-%m-%dT%H:%M:%SZ")
    file_ts = ts.strftime("%Y%m%dT%H%M%SZ")
    type_slug = event_type.replace(".", "-")

    content = (
        f"# Event: {title}\n"
        "\n"
        "- Source: nexus\n"
        f"- Type: {event_type}\n"
        f"- When: {when_iso}\n"
        "\n"
        f"{body}\n"
        "\n"
        "Powered by CwiAI"
    )
    filename = f"event-nexus-{type_slug}-{file_ts}.md"
    return content, filename


async def emit_event(event_type: str, title: str, body: str) -> None:
    """Best-effort Brain event note via POST /raw. Never raises.

    Fire-and-forget: swallows every exception (network errors, timeouts,
    non-2xx responses) and logs at warning level at most, so a failed emit
    never disrupts the calling operation (goal approval, etc).
    """
    try:
        content, filename = _format_event(event_type, title, body)
        await _post_raw(content, filename, timeout=5)
    except Exception as e:
        logger.warning(f"emit_event failed for {event_type!r}: {e}")
