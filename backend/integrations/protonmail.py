import json
import logging

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from backend.cache import async_ttl_cache

logger = logging.getLogger(__name__)


class IntegrationError(Exception):
    pass


async def _call_tool(tool_name: str, arguments: dict, *, timeout: float = 20.0) -> str:
    """Open a fresh MCP session, call one tool, close. No persistent session —
    ClientSession is not safe for concurrent calls and a per-call session is
    naturally concurrency-safe and resilient to the remote LXC restarting
    between calls. Runs on the event loop: streamable-HTTP is async httpx/SSE,
    spawns no subprocess, so it's compatible with the forced SelectorEventLoop.
    """
    from backend.config import get_settings
    settings = get_settings()

    async with streamablehttp_client(settings.protonmail_mcp_url, timeout=timeout) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(tool_name, arguments)

    text = "".join(block.text for block in result.content if hasattr(block, "text"))
    if result.isError:
        raise IntegrationError(text or f"{tool_name} returned an error")
    return text


async def list_recent(
    *,
    unread_only: bool = False,
    from_address: str | None = None,
    subject: str | None = None,
    since: str | None = None,
    limit: int = 10,
    mailbox: str | None = None,
) -> str:
    from backend.config import get_settings
    settings = get_settings()
    args: dict = {
        "account_name": settings.protonmail_account,
        "page_size": limit,
        "order": "desc",
    }
    if unread_only:
        args["seen"] = False
    if from_address:
        args["from_address"] = from_address
    if subject:
        args["subject"] = subject
    if since:
        args["since"] = since
    if mailbox:
        args["mailbox"] = mailbox
    return await _call_tool("list_emails_metadata", args)


def _clean_sender(sender: str, cap: int = 40) -> str:
    """Display name only, address stripped -- a raw From header
    ('"Name" <addr@example.com>') is unreadable in a narrow card."""
    sender = sender or "(unknown sender)"
    if "<" in sender:
        sender = sender.split("<", 1)[0].strip().strip('"')
    sender = sender.strip() or "(unknown sender)"
    return sender if len(sender) <= cap else sender[: cap - 1] + "…"


def _clean_subject(subject: str, cap: int = 60) -> str:
    subject = subject or "(no subject)"
    return subject if len(subject) <= cap else subject[: cap - 1] + "…"


def format_inbox_summary(raw_result, limit: int = 7) -> str:
    """Pure formatter: list_recent()'s JSON text (or an Exception, from a
    gather(..., return_exceptions=True) call) -> a compact plain-text unread
    summary, same shape as the old Gmail-via-Hermes summary it replaces on
    the Today page's Inbox card (see inbox_summary() below). Never raises."""
    if isinstance(raw_result, Exception):
        return "(Proton Mail unavailable)"
    try:
        data = json.loads(raw_result)
        emails = [e for e in (data.get("emails") or []) if isinstance(e, dict)]
    except Exception:
        return "(Proton Mail unavailable: malformed response)"

    if not emails:
        return "Inbox: 0 unread"

    total = data.get("total")
    total = total if isinstance(total, int) else len(emails)
    shown = emails[:limit]
    lines = [f"Inbox: {total} unread (showing last {len(shown)}):"]
    for e in shown:
        sender = _clean_sender(e.get("sender"))
        subject = _clean_subject(e.get("subject"))
        lines.append(f"  • {sender} — {subject}")
    return "\n".join(lines)


@async_ttl_cache(120)
async def inbox_summary(limit: int = 7) -> str:
    """Gmail-shaped plain-text unread summary. Best-effort, never raises.

    Cached (matches the TTL the old hermes.get_gmail() used) -- the Today
    page polls on a timer, tab-focus, AND visibility-change, so an uncached
    call here would open a fresh MCP session + IMAP search on every one of
    those. Note: async_ttl_cache ignores call arguments (single shared
    cache slot per decorated function) -- fine here since every caller
    passes the same limit=7, same as _dashboard_inbox's existing precedent
    in backend/api/protonmail.py.
    """
    try:
        raw = await list_recent(unread_only=True, limit=limit, mailbox="inbox")
        return format_inbox_summary(raw, limit=limit)
    except Exception as e:
        return f"(Proton Mail unavailable: {e})"


async def read_email(email_id: str, *, page: int = 1, mailbox: str | None = None) -> str:
    from backend.config import get_settings
    settings = get_settings()
    args: dict = {"account_name": settings.protonmail_account, "email_ids": [email_id], "page": page}
    if mailbox:
        args["mailbox"] = mailbox
    return await _call_tool("get_emails_content", args)


async def send_email(
    recipients: list[str],
    subject: str,
    body: str,
    *,
    cc: list[str] | None = None,
    bcc: list[str] | None = None,
    in_reply_to: str | None = None,
    references: str | None = None,
    html: bool = False,
) -> dict:
    from backend.config import get_settings
    settings = get_settings()
    args: dict = {
        "account_name": settings.protonmail_account,
        "recipients": recipients,
        "subject": subject,
        "body": body,
        "html": html,
    }
    if cc:
        args["cc"] = cc
    if bcc:
        args["bcc"] = bcc
    if in_reply_to:
        args["in_reply_to"] = in_reply_to
    if references:
        args["references"] = references
    detail = await _call_tool("send_email", args)
    return {"ok": True, "detail": detail}


async def save_draft(
    recipients: list[str],
    subject: str,
    body: str,
    *,
    in_reply_to: str | None = None,
    references: str | None = None,
    html: bool = False,
) -> dict:
    """Compose and save a draft to the Drafts IMAP folder — pure IMAP, NEVER
    touches SMTP, so nothing is ever sent. A draft is trivially deletable, so
    unlike send_email this is LOW-risk/reversible by construction and does NOT
    need broker gating — callable directly from a scheduled job. Do not "fix"
    this into the broker; that's send_email's job, not this one's.
    """
    from backend.config import get_settings
    settings = get_settings()
    args: dict = {
        "account_name": settings.protonmail_account,
        "recipients": recipients,
        "subject": subject,
        "body": body,
        "html": html,
        "mailbox": "Drafts",
    }
    if in_reply_to:
        args["in_reply_to"] = in_reply_to
    if references:
        args["references"] = references
    detail = await _call_tool("save_to_mailbox", args)
    return {"ok": True, "detail": detail}


async def archive_email(email_id: str, *, mailbox: str | None = None) -> dict:
    """Move an email to the Archive folder. Broker-gated (kind='protonmail_archive',
    LOW/REVERSIBLE_BY_INVERSE — cleanly reversible via move_emails back to INBOX)."""
    from backend.config import get_settings
    settings = get_settings()
    args: dict = {"account_name": settings.protonmail_account, "email_ids": [email_id]}
    if mailbox:
        args["mailbox"] = mailbox
    detail = await _call_tool("archive_emails", args)
    return {"ok": True, "detail": detail}


async def trash_email(email_id: str, *, mailbox: str | None = None) -> dict:
    """Move an email to Trash. Broker-gated (kind='protonmail_delete',
    LOW/REVERSIBLE_BY_INVERSE — same band and reasoning as archive_email: moves
    mail between two folders in the same account, disrupts nothing live,
    cleanly reversible via move_emails back to INBOX). Deliberately uses the
    MCP move-between-folders tool rather than the MCP hard-remove tool — the
    latter was verified 2026-07-23 to permanently expunge (a test email removed
    via it never appeared in the real Trash folder, unlike Brian's years of
    normal deletes), which is not what "delete" should mean here. Do not swap
    this back to the hard-remove tool.
    """
    from backend.config import get_settings
    settings = get_settings()
    args: dict = {
        "account_name": settings.protonmail_account,
        "email_ids": [email_id],
        "destination_mailbox": "Trash",
    }
    if mailbox:
        args["source_mailbox"] = mailbox
    detail = await _call_tool("move_emails", args)
    return {"ok": True, "detail": detail}


@async_ttl_cache(30)
async def health_check() -> bool:
    from backend.config import get_settings
    settings = get_settings()
    try:
        # timeout=5, not the 20s default: this is a liveness probe (feeds the
        # dashboard's online/offline pill), not a functional read — every
        # other integration's health_check() bounds its own call the same
        # way. Note this caps each of _call_tool's two round trips
        # (initialize + call_tool) at 5s, so ~10s is the realistic worst
        # case, not a hard 5s wall. A slow/hung bridge used to be able to
        # hold this open for up to 2x20s, which stalled unrelated concurrent
        # requests behind Chrome's per-origin connection cap (found live
        # 2026-07-25).
        text = await _call_tool("list_available_accounts", {}, timeout=5.0)
        return settings.protonmail_account in text
    except Exception:
        return False
