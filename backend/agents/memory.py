"""Tier 2.3 — cheap chat memory. Assembles a compact recall block from the
user's vault notes + the latest briefing, injected into the CHAT system prompt.
Best-effort throughout: any failure yields an empty section, never an error."""

import asyncio
import logging

logger = logging.getLogger(__name__)

VAULT_RECALL_MAX_RESULTS = 3
VAULT_RECALL_CHARS = 800
BRIEFING_SEED_CHARS = 600


async def vault_recall(query: str) -> str:
    """Search the vault for notes relevant to the user's message.

    Imports obsidian lazily so the module stays importable even when the
    integration is not configured. Returns "" on any failure or empty result.
    """
    try:
        from backend.integrations import obsidian
        result = await obsidian.vault_search(query, max_results=VAULT_RECALL_MAX_RESULTS)
    except Exception as exc:
        logger.debug(f"vault_recall: search error (ignored): {exc}")
        return ""

    if not result:
        return ""

    # Treat any of these prefixes as "nothing useful found"
    skip_prefixes = (
        "No notes found",
        "Obsidian token not configured",
        "Obsidian vault not found at",
        "Vault search unavailable",
    )
    for prefix in skip_prefixes:
        if result.startswith(prefix):
            return ""

    if len(result) > VAULT_RECALL_CHARS:
        result = result[:VAULT_RECALL_CHARS] + " ...[truncated]"

    return result


def _db_latest_briefing_text() -> str:
    """Sync helper — ONLY call via asyncio.to_thread.

    Opens its own Session, queries the newest Briefing row by created_at desc,
    and returns its content (truncated). Returns "" if no rows exist or on any
    error.
    """
    try:
        from sqlmodel import Session, select
        from backend.database import Briefing, engine

        with Session(engine) as session:
            stmt = select(Briefing).order_by(Briefing.created_at.desc()).limit(1)
            briefing = session.exec(stmt).first()
            if briefing is None or not briefing.content:
                return ""
            content = briefing.content
            if len(content) > BRIEFING_SEED_CHARS:
                content = content[:BRIEFING_SEED_CHARS] + " ...[truncated]"
            return content
    except Exception as exc:
        logger.debug(f"_db_latest_briefing_text: error (ignored): {exc}")
        return ""


async def latest_briefing_seed() -> str:
    """Async wrapper around _db_latest_briefing_text.

    Runs the sync DB read in a thread so the event loop is never blocked.
    Returns "" on any error.
    """
    try:
        return await asyncio.to_thread(_db_latest_briefing_text)
    except Exception as exc:
        logger.debug(f"latest_briefing_seed: error (ignored): {exc}")
        return ""


def assemble(vault_str: str, briefing_str: str, facts_str: str = "") -> str:
    """Pure function: build the memory injection block.

    Returns "" when all inputs are empty so the caller can skip injection
    entirely. Otherwise returns a block with only the non-empty sections.

    facts_str is an optional block of durable known facts (Tier 2.3c). When
    present it is appended after the vault notes and briefing with a header
    noting that live data takes precedence on conflict.
    """
    vault_str = vault_str or ""
    briefing_str = briefing_str or ""
    facts_str = facts_str or ""

    if not vault_str and not briefing_str and not facts_str:
        return ""

    parts = ["RELEVANT MEMORY (from your notes + latest briefing — use if helpful, ignore if not):"]
    if vault_str:
        parts.append("[VAULT NOTES]")
        parts.append(vault_str)
    if briefing_str:
        parts.append("[LATEST BRIEFING]")
        parts.append(briefing_str)
    if facts_str:
        parts.append(
            "[KNOWN FACTS] (durable; may be stale — prefer live data above if it conflicts)"
        )
        parts.append(facts_str)

    return "\n".join(parts)


async def recall(query: str) -> tuple[str, str]:
    """Run vault_recall and latest_briefing_seed concurrently.

    Returns (vault_str, briefing_str). Any Exception result is coerced to "".
    Provided for reuse / tests; the CHAT branch folds the two coroutines into
    its own asyncio.gather instead of calling this.
    """
    results = await asyncio.gather(
        vault_recall(query),
        latest_briefing_seed(),
        return_exceptions=True,
    )
    vault_str = results[0] if not isinstance(results[0], Exception) else ""
    briefing_str = results[1] if not isinstance(results[1], Exception) else ""
    return vault_str, briefing_str
