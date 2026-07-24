"""Proton Mail voice-profile learning + scheduled auto-draft-reply job (Tasks 4-5).

Never sends anything — save_draft (backend/integrations/protonmail.py) is pure
IMAP, drafts land in Brian's real Proton Drafts folder for him to review/edit/send
himself. This module must never invoke the mail-sending integration function.
"""

import asyncio
import json
import logging
import re
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

VOICE_REFRESH_DAYS = 14
DEFAULT_VOICE = "Concise, friendly, plain-text replies."
MAX_CLASSIFIED_PER_TICK = 10
JUNK_REFRESH_DAYS = 30
MAX_TRASHED_PER_TICK = 5

# Zero-LLM-cost pre-filter for obviously-automated mail. Brian's promotional
# traffic (CVS/Newegg/Amazon-style offers) arrives via SimpleLogin alias
# forwards, so those domains are included alongside the usual noreply patterns.
_AUTOMATED_SENDER_PATTERNS = (
    "noreply", "no-reply", "no_reply", "donotreply", "do-not-reply",
    "notification", "notifications", "mailer-daemon", "bounce",
    "newsletter", "marketing", "alerts@", "updates@", "news@", "receipt",
    "@simplelogin.co", "@aleeas.com", "@slmail.me",
)


def _is_automated_sender(sender: str) -> bool:
    s = (sender or "").lower()
    return any(p in s for p in _AUTOMATED_SENDER_PATTERNS)


def _extract_email_address(sender: str) -> str:
    """"Name" <addr> or Name <addr> -> addr; a bare address is returned as-is."""
    m = re.search(r"<([^<>]+)>", sender or "")
    return m.group(1).strip() if m else (sender or "").strip()


# ---------------------------------------------------------------------------
# Voice profile (Task 4) — singleton MailVoiceProfile row (id=1)
# ---------------------------------------------------------------------------

def _db_get_voice_row() -> dict | None:
    from sqlmodel import Session
    from backend.database import MailVoiceProfile, engine
    with Session(engine) as session:
        row = session.get(MailVoiceProfile, 1)
        if row is None:
            return None
        return {"summary": row.summary, "sample_count": row.sample_count, "updated_at": row.updated_at}


def _db_upsert_voice_row(summary: str, sample_count: int) -> None:
    from sqlmodel import Session
    from backend.database import MailVoiceProfile, engine
    with Session(engine) as session:
        row = session.get(MailVoiceProfile, 1)
        if row is None:
            row = MailVoiceProfile(id=1)
            session.add(row)
        row.summary = summary
        row.sample_count = sample_count
        row.updated_at = datetime.utcnow()
        session.commit()


async def _rebuild_voice_profile() -> str:
    """One-time (per refresh window) Sonnet distill of Brian's writing voice from
    his Sent folder. Raises on any failure — get_voice_profile handles fallback.
    """
    from backend.integrations import protonmail
    from backend.agents.router import sonnet

    listed = await protonmail.list_recent(mailbox="Sent", limit=25)
    data = json.loads(listed)
    emails = data.get("emails") or []

    bodies = []
    for e in emails:
        if (e.get("subject") or "").lower().startswith("fwd:"):
            continue
        email_id = e.get("email_id")
        if not email_id:
            continue
        try:
            content = await protonmail.read_email(email_id, mailbox="Sent")
            content_data = json.loads(content)
            content_emails = content_data.get("emails") or []
            body = (content_emails[0].get("body") if content_emails else "") or ""
        except Exception:
            continue
        if len(body.strip()) < 20:
            continue
        bodies.append(body.strip()[:2000])
        if len(bodies) >= 8:
            break

    if not bodies:
        raise RuntimeError("no usable Sent-folder samples found")

    sample_block = "\n\n---\n\n".join(bodies)
    prompt = f"""Here are {len(bodies)} emails Brian actually sent, verbatim:

{sample_block}

Distill Brian's writing voice into a short style guide (<=1200 chars) another writer could
follow to sound exactly like him: typical greeting, typical sign-off, tone/formality register,
typical length, phrasing quirks. Style guide text only, no preamble."""

    summary = (await sonnet(prompt, label="mail_voice_distill")).strip()[:1200]
    if not summary:
        raise RuntimeError("distill returned empty summary")

    await asyncio.to_thread(_db_upsert_voice_row, summary, len(bodies))
    return summary


async def get_voice_profile() -> str:
    """Cached voice summary; rebuilds when missing/stale. Never raises — mirrors
    goals._distill_completed_goal's never-blocks discipline."""
    try:
        row = await asyncio.to_thread(_db_get_voice_row)
    except Exception:
        row = None

    is_stale = row is None or (datetime.utcnow() - row["updated_at"]) > timedelta(days=VOICE_REFRESH_DAYS)
    if not is_stale:
        return row["summary"]

    try:
        return await _rebuild_voice_profile()
    except Exception as e:
        logger.warning(f"Voice profile rebuild failed (best-effort, ignoring): {e}")
        if row is not None and row["summary"]:
            return row["summary"]
        return DEFAULT_VOICE


async def compose_reply(sender: str, subject: str, body: str, voice: str) -> str:
    from backend.agents.router import sonnet

    truncated_body = (body or "")[:3000]
    prompt = f"""Draft a reply email as Brian, in his own voice.

BRIAN'S WRITING VOICE:
{voice}

ORIGINAL EMAIL
From: {sender}
Subject: {subject}
Body:
{truncated_body}

Write ONLY the reply body text (no subject line). Rules:
- Write as Brian, using his voice above (greeting, sign-off, tone, length).
- Never invent commitments, facts, availability, or details not present in the original email.
- No placeholder tokens like [Name] or [insert X] — if something is genuinely unknown, write around it naturally.
- Plain text only."""

    return (await sonnet(prompt, label="mail_reply_draft")).strip()


# ---------------------------------------------------------------------------
# Junk profile — singleton MailJunkProfile row (id=1), distilled from Brian's
# real Trash folder (sender+subject only — junk is identified by envelope, and
# the classifier that consumes this profile only ever sees sender+subject too,
# so training on bodies would teach signals the classifier can't use).
# ---------------------------------------------------------------------------

def _db_get_junk_row() -> dict | None:
    from sqlmodel import Session
    from backend.database import MailJunkProfile, engine
    with Session(engine) as session:
        row = session.get(MailJunkProfile, 1)
        if row is None:
            return None
        return {"summary": row.summary, "sample_count": row.sample_count, "updated_at": row.updated_at}


def _db_upsert_junk_row(summary: str, sample_count: int) -> None:
    from sqlmodel import Session
    from backend.database import MailJunkProfile, engine
    with Session(engine) as session:
        row = session.get(MailJunkProfile, 1)
        if row is None:
            row = MailJunkProfile(id=1)
            session.add(row)
        row.summary = summary
        row.sample_count = sample_count
        row.updated_at = datetime.utcnow()
        session.commit()


async def _rebuild_junk_profile() -> str:
    """One-time (per refresh window) Sonnet distill of what Brian actually
    deletes, from his real Trash folder. Metadata only (sender + subject, no
    read_email body fetch). Raises on any failure — get_junk_profile handles
    fallback."""
    from backend.integrations import protonmail
    from backend.agents.router import sonnet

    listed = await protonmail.list_recent(mailbox="Trash", limit=50)
    data = json.loads(listed)
    emails = data.get("emails") or []

    lines = []
    for e in emails:
        sender = (e.get("sender") or "").strip()
        subject = (e.get("subject") or "").strip()
        if not sender:
            continue
        lines.append(f"From: {sender} | Subject: {subject}")

    if not lines:
        raise RuntimeError("no usable Trash-folder samples found")

    sample_block = "\n".join(lines)
    prompt = f"""Here are {len(lines)} emails Brian has actually deleted (sender + subject only):

{sample_block}

Distill a short profile (<=1200 chars) describing the RECURRING kind of mail Brian deletes —
sender domain/address patterns, subject-line patterns, and content categories (e.g. promotional
retail offers, political/newsletter mailing lists, account/order notifications). IGNORE any
one-off personal or transactional email that merely happens to be in this list — Trash also
holds ordinary handled mail, not only junk; describe only clearly recurring patterns. Profile
text only, no preamble — written so another process can compare a NEW email's sender+subject
against it and judge whether it matches."""

    summary = (await sonnet(prompt, label="mail_junk_distill")).strip()[:1200]
    if not summary:
        raise RuntimeError("distill returned empty summary")

    await asyncio.to_thread(_db_upsert_junk_row, summary, len(lines))
    return summary


async def get_junk_profile() -> str | None:
    """Cached junk summary; rebuilds when missing/stale. Never raises. Unlike
    get_voice_profile, the terminal fallback is None, not a fabricated default —
    a guessed junk profile would cause real Trash-moves, so no profile means
    callers must not auto-trash this tick (fail-safe in the safe direction)."""
    try:
        row = await asyncio.to_thread(_db_get_junk_row)
    except Exception:
        row = None

    is_stale = row is None or (datetime.utcnow() - row["updated_at"]) > timedelta(days=JUNK_REFRESH_DAYS)
    if not is_stale:
        return row["summary"]

    try:
        return await _rebuild_junk_profile()
    except Exception as e:
        logger.warning(f"Junk profile rebuild failed (best-effort, ignoring): {e}")
        if row is not None and row["summary"]:
            return row["summary"]
        return None


async def _is_junk(sender: str, subject: str, profile: str) -> bool:
    """Conservative TRASH/KEEP verdict against the junk profile. Any failure or
    ambiguity -> KEEP (leave in inbox)."""
    from backend.safety.governor import BudgetExceeded
    from backend.agents.router import haiku

    prompt = f"""Does this email match the kind of mail Brian deletes?

BRIAN'S JUNK PROFILE (recurring patterns he actually deletes):
{profile}

CANDIDATE EMAIL
From: {sender}
Subject: {subject}

Answer TRASH only if this confidently matches a recurring pattern in the profile above.
Answer KEEP for anything that looks like a receipt, order/shipping confirmation, security or
account notice, anything personal, or anything you are not certain about — when uncertain,
always answer KEEP.

Reply with exactly one word: TRASH or KEEP."""

    try:
        raw = await haiku(prompt, label="mail_junk_classify")
    except BudgetExceeded:
        raise
    except Exception:
        return False
    return raw.strip().upper().startswith("TRASH")


# ---------------------------------------------------------------------------
# Auto-draft job (Task 5) — ProcessedMailId dedup + conservative classifier
# ---------------------------------------------------------------------------

def _db_already_processed(email_id: str) -> bool:
    from sqlmodel import Session, select
    from backend.database import ProcessedMailId, engine
    with Session(engine) as session:
        row = session.exec(select(ProcessedMailId).where(ProcessedMailId.email_id == email_id)).first()
        return row is not None


def _db_claim_email(email_id: str) -> bool:
    """Insert a claim row. True if newly claimed, False if already processed
    (IntegrityError on the unique index) — same race-safety idiom as
    Goal.fingerprint's partial unique index."""
    from sqlmodel import Session
    from sqlalchemy.exc import IntegrityError
    from backend.database import ProcessedMailId, engine
    with Session(engine) as session:
        try:
            session.add(ProcessedMailId(email_id=email_id))
            session.commit()
            return True
        except IntegrityError:
            session.rollback()
            return False


def _db_mark_drafted(email_id: str) -> None:
    from sqlmodel import Session, select
    from backend.database import ProcessedMailId, engine
    with Session(engine) as session:
        row = session.exec(select(ProcessedMailId).where(ProcessedMailId.email_id == email_id)).first()
        if row:
            row.drafted = True
            session.commit()


def _db_mark_trashed(email_id: str) -> None:
    from sqlmodel import Session, select
    from backend.database import ProcessedMailId, engine
    with Session(engine) as session:
        row = session.exec(select(ProcessedMailId).where(ProcessedMailId.email_id == email_id)).first()
        if row:
            row.trashed = True
            session.commit()


def _db_drafted_email_ids(within_days: int = 14) -> set[str]:
    """email_ids the auto-draft job already judged as genuine correspondence
    (drafted=True) within the last within_days. Used by the briefing to reuse
    that judgment instead of re-classifying at briefing time."""
    from sqlmodel import Session, select
    from backend.database import ProcessedMailId, engine
    cutoff = datetime.utcnow() - timedelta(days=within_days)
    with Session(engine) as session:
        rows = session.exec(
            select(ProcessedMailId.email_id).where(
                ProcessedMailId.drafted == True,  # noqa: E712 (SQLAlchemy needs == not is)
                ProcessedMailId.processed_at >= cutoff,
            )
        ).all()
        return set(rows)


async def _warrants_reply(sender: str, subject: str, snippet: str) -> bool:
    """Conservative REPLY/SKIP verdict. Any failure or ambiguity -> SKIP."""
    from backend.safety.governor import BudgetExceeded
    from backend.agents.router import haiku

    prompt = f"""Does this email genuinely warrant a personal reply from Brian?

From: {sender}
Subject: {subject}
Body snippet: {(snippet or "")[:800]}

Answer REPLY only if this is genuine one-to-one correspondence from a real human/business
contact who is waiting on Brian's response. Answer SKIP for anything promotional, marketing,
a newsletter, a receipt/transactional/order/shipping notice, an automated notification, or
anything you are not certain about — when uncertain, always answer SKIP.

Reply with exactly one word: REPLY or SKIP."""

    try:
        raw = await haiku(prompt, label="mail_reply_classify")
    except BudgetExceeded:
        raise
    except Exception:
        return False
    return raw.strip().upper().startswith("REPLY")


async def autodraft_tick() -> None:
    """One scan of the inbox: classify candidates, draft+save replies for ones
    that warrant it, trash ones that match Brian's real junk-deletion pattern,
    notify Brian on drafts. Never sends anything.

    save_draft is deliberately NOT broker-gated (pure IMAP, no SMTP, LOW/reversible
    by construction — see protonmail.py::save_draft). But the global kill switch is
    a blanket "no autonomous side effects" switch, and this job is unsupervised
    autonomous behavior, so it honors autonomy_enabled directly here rather than
    only indirectly via a scheduler pause (those are two different mechanisms).

    Three mutually-exclusive outcomes per email, kept disjoint by construction:
    an automated-looking sender (_is_automated_sender) can only ever be
    considered for trash (never drafted a reply); a human-looking sender can
    only ever be considered for a reply draft (never auto-trashed). This makes
    trashing genuine correspondence impossible, not just unlikely.
    """
    from backend.config import get_settings
    from backend.integrations import protonmail
    from backend.safety import governor
    from backend.safety.governor import BudgetExceeded
    from backend import events

    state = await asyncio.to_thread(governor.get_system_state)
    if not state.get("autonomy_enabled", True):
        logger.info("Mail autodraft tick skipped: autonomy disabled (kill switch)")
        return

    listed = await protonmail.list_recent(limit=25)
    try:
        data = json.loads(listed)
    except Exception as e:
        logger.warning(f"autodraft_tick: couldn't parse inbox listing: {e}")
        return
    emails = data.get("emails") or []

    autotrash_enabled = get_settings().mail_autotrash_enabled
    junk_profile = None
    junk_profile_loaded = False

    classified = 0
    trashed_count = 0
    for e in emails:
        email_id = e.get("email_id")
        if not email_id:
            continue
        sender = e.get("sender", "")
        subject = e.get("subject", "")
        try:
            if await asyncio.to_thread(_db_already_processed, email_id):
                continue

            if _is_automated_sender(sender):
                if not autotrash_enabled:
                    # Feature off -- today's behavior: permanent skip, zero LLM cost.
                    await asyncio.to_thread(_db_claim_email, email_id)
                    continue

                if not junk_profile_loaded:
                    junk_profile = await get_junk_profile()
                    junk_profile_loaded = True

                if junk_profile is None:
                    # No profile yet (or rebuild failed with no fallback) -- a
                    # guessed profile would cause real Trash-moves, so don't guess.
                    await asyncio.to_thread(_db_claim_email, email_id)
                    continue

                if classified >= MAX_CLASSIFIED_PER_TICK or trashed_count >= MAX_TRASHED_PER_TICK:
                    # Per-tick cap reached -- leave UNCLAIMED, reconsidered next tick.
                    continue

                claimed = await asyncio.to_thread(_db_claim_email, email_id)
                if not claimed:
                    continue
                classified += 1

                if await _is_junk(sender, subject, junk_profile):
                    from backend.safety.broker import execute_action, Decision
                    res = await execute_action(
                        actor="autonomous",
                        kind="protonmail_delete",
                        target=email_id,
                        payload={"email_id": email_id},
                    )
                    if res.decision == Decision.EXECUTED:
                        await asyncio.to_thread(_db_mark_trashed, email_id)
                        trashed_count += 1
                # KEEP (or a failed dispatch) -- claimed, permanent skip either way.
                continue

            if classified >= MAX_CLASSIFIED_PER_TICK:
                # Per-tick cost cap. Deliberately left UNCLAIMED so this candidate
                # is reconsidered on the next tick rather than lost forever.
                continue

            claimed = await asyncio.to_thread(_db_claim_email, email_id)
            if not claimed:
                continue  # already claimed by a concurrent/prior run
            classified += 1

            content_text = await protonmail.read_email(email_id)
            content_data = json.loads(content_text)
            content_emails = content_data.get("emails") or []
            body = (content_emails[0].get("body") if content_emails else "") or ""
            message_id = content_emails[0].get("message_id") if content_emails else None

            if not await _warrants_reply(sender, subject, body):
                continue  # claimed, drafted=False — permanent skip

            voice = await get_voice_profile()
            reply_body = await compose_reply(sender, subject, body, voice)
            reply_subject = subject if subject.lower().startswith("re:") else f"Re: {subject}"

            await protonmail.save_draft(
                recipients=[_extract_email_address(sender)],
                subject=reply_subject,
                body=reply_body,
                in_reply_to=message_id,
                references=message_id,
            )
            await asyncio.to_thread(_db_mark_drafted, email_id)
            await events.notify_phone(
                f'Drafted a reply to {sender} re "{subject}" — waiting in your Proton Drafts folder.',
                kind="mail_draft_created",
            )
        except BudgetExceeded:
            raise
        except Exception as e:
            logger.warning(f"autodraft_tick: error processing email {email_id}: {e}")
            continue
