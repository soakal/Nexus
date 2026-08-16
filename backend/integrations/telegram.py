"""Direct Telegram Bot API transport. No telegram library — the Bot API is
plain JSON over HTTPS, via httpx.

Also owns the PendingDelivery retry queue — it lives here alongside notify()
since three other modules depend on it (scheduler.py, api/safety.py,
agents/watchdog.py).
"""
import asyncio
import json
import logging
from datetime import datetime, timedelta

import httpx

from backend.http_client import SSL_CONTEXT

logger = logging.getLogger(__name__)

_API = "https://api.telegram.org"
LIMIT = 4000  # headroom under Telegram's 4096 hard limit, for parse_mode entity expansion

# A 4xx here is a deterministic request/auth error — a byte-identical retry
# cannot succeed, so these are terminal (logged, never queued). 429/5xx/
# transport errors are retryable.
_TERMINAL_STATUS_CODES = {400, 401, 403, 404}

# Pending-delivery retry policy. The
# scheduler calls deliver_pending() every 60s; these bound how aggressively a
# failing row is retried and when it is given up on.
_BACKOFF_BASE_SECONDS = 60      # first retry waits ~60s after the first failure
_BACKOFF_CAP_SECONDS = 3600     # exponential backoff ceiling (1 hour)
_MAX_ATTEMPTS = 8               # dead-letter cap: stop loading a row after this many tries


class TelegramConflict(Exception):
    """Raised when Telegram reports 409 on getUpdates (another poller is active)."""


def chunk_text(text: str, limit: int = LIMIT) -> list[str]:
    """Split on paragraph boundaries, then lines, then hard-split.

    Every returned chunk is <= limit. Preserves order; drops nothing.
    """
    text = text or ""
    if len(text) <= limit:
        return [text] if text else []

    chunks: list[str] = []
    current = ""

    def _flush():
        nonlocal current
        if current:
            chunks.append(current)
            current = ""

    for para in text.split("\n\n"):
        candidate = f"{current}\n\n{para}" if current else para
        if len(candidate) <= limit:
            current = candidate
            continue
        _flush()
        if len(para) <= limit:
            current = para
            continue
        # Paragraph itself too big — split on lines.
        for line in para.split("\n"):
            candidate = f"{current}\n{line}" if current else line
            if len(candidate) <= limit:
                current = candidate
                continue
            _flush()
            # Single line too big — hard-split (accept mid-word break).
            while len(line) > limit:
                chunks.append(line[:limit])
                line = line[limit:]
            current = line
    _flush()
    return chunks


async def _call(method: str, params: dict, *, timeout: float = 30) -> httpx.Response:
    """Raw Bot API POST. Raises KeyError if TELEGRAM_BOT_TOKEN is unset (loud,
    same posture as every other required-secret property)."""
    from backend.config import get_settings
    token = get_settings().telegram_bot_token
    async with httpx.AsyncClient(verify=SSL_CONTEXT, timeout=timeout) as client:
        return await client.post(f"{_API}/bot{token}/{method}", json=params)


async def send_message(
    text: str, *, parse_mode: str | None = None, buttons: list | None = None,
    chat_id: str | None = None,
) -> httpx.Response:
    """Single sendMessage call (one chunk of a possibly-chunked notify).

    Raises KeyError (missing TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID) or a
    transport exception — callers classify the outcome.
    """
    from backend.config import get_settings
    target_chat = chat_id if chat_id is not None else get_settings().telegram_chat_id
    params: dict = {"chat_id": target_chat, "text": text}
    if parse_mode:
        params["parse_mode"] = parse_mode
    if buttons:
        try:
            params["reply_markup"] = {"inline_keyboard": [[
                {"text": str(b["text"]), "callback_data": str(b["callback_data"])}
                for b in buttons
            ]]}
        except Exception:
            pass  # malformed buttons never block the text
    return await _call("sendMessage", params)


async def answer_callback_query(cq_id: str, text: str | None = None, show_alert: bool = False) -> bool:
    """answerCallbackQuery is SINGLE-USE per query — call exactly once. Never
    raises (a failed answer must not block the outcome edit that follows)."""
    try:
        params: dict = {"callback_query_id": cq_id}
        if text:
            params["text"] = text[:190]
        if show_alert:
            params["show_alert"] = True
        resp = await _call("answerCallbackQuery", params, timeout=10)
        return resp.status_code == 200
    except Exception as e:
        logger.warning(f"answerCallbackQuery failed (ignored): {e}")
        return False


async def edit_message_text(chat_id, message_id: int, text: str) -> bool:
    """Edits WITHOUT reply_markup — omitting it removes the inline keyboard,
    so a tapped button becomes single-use. Never raises."""
    try:
        resp = await _call(
            "editMessageText",
            {"chat_id": chat_id, "message_id": message_id, "text": text},
            timeout=10,
        )
        return resp.status_code == 200
    except Exception as e:
        logger.warning(f"editMessageText failed (ignored): {e}")
        return False


async def send_reply(text: str, *, chat_id: str | None = None, parse_mode: str | None = None) -> bool:
    """Chunked command/chat reply. Deliberately does NOT queue into
    PendingDelivery on failure (unlike notify()) — a command reply is only
    meaningful right now; replaying it an hour later is confusing noise, not
    resilience. Returns True only if every chunk sent. Never raises."""
    chunks = chunk_text(text)
    if not chunks:
        return True
    try:
        for chunk in chunks:
            resp = await send_message(chunk, parse_mode=parse_mode, chat_id=chat_id)
            if resp.status_code != 200:
                logger.warning(f"Telegram reply chunk failed: HTTP {resp.status_code}: {resp.text[:200]}")
                return False
        return True
    except Exception as e:
        logger.warning(f"Telegram reply failed: {e}")
        return False


async def send_chat_action(action: str = "typing", *, chat_id: str | None = None) -> bool:
    """Best-effort 'typing...' indicator. Telegram auto-expires it after ~5s —
    fired once per command, no refresh loop. Never raises."""
    from backend.config import get_settings
    try:
        target_chat = chat_id if chat_id is not None else get_settings().telegram_chat_id
        resp = await _call("sendChatAction", {"chat_id": target_chat, "action": action}, timeout=10)
        return resp.status_code == 200
    except Exception as e:
        logger.debug(f"sendChatAction failed (ignored): {e}")
        return False


async def send_photo(photo: bytes, *, caption: str | None = None, chat_id: str | None = None) -> bool:
    """Multipart sendPhoto — the one Bot API call _call() can't carry (JSON
    body vs. file upload). Never raises."""
    from backend.config import get_settings
    try:
        token = get_settings().telegram_bot_token
        target_chat = chat_id if chat_id is not None else get_settings().telegram_chat_id
        data = {"chat_id": str(target_chat)}
        if caption:
            data["caption"] = caption[:1024]
        async with httpx.AsyncClient(verify=SSL_CONTEXT, timeout=60) as client:
            resp = await client.post(
                f"{_API}/bot{token}/sendPhoto",
                data=data,
                files={"photo": ("image.jpg", photo, "image/jpeg")},
            )
        if resp.status_code != 200:
            logger.warning(f"sendPhoto failed: HTTP {resp.status_code}: {resp.text[:200]}")
            return False
        return True
    except Exception as e:
        logger.warning(f"sendPhoto failed: {e}")
        return False


async def set_my_commands(commands: list[dict]) -> bool:
    """commands: [{"command": "status", "description": "..."}, ...] -> the "/"
    autocomplete menu. Called once at poller start. Never raises."""
    try:
        resp = await _call("setMyCommands", {"commands": commands}, timeout=10)
        return resp.status_code == 200
    except Exception as e:
        logger.warning(f"setMyCommands failed (ignored): {e!r}")
        return False


async def get_file_bytes(file_id: str, *, max_bytes: int = 20 * 1024 * 1024) -> bytes:
    """getFile -> download from the FILE endpoint. Telegram uses a DIFFERENT
    base path for file downloads than every other Bot API call:
    /file/bot<token>/<file_path>, not /bot<token>/<method>. Size-capped.
    Raises on failure — caller (voice transcription) handles it."""
    from backend.config import get_settings
    token = get_settings().telegram_bot_token

    resp = await _call("getFile", {"file_id": file_id}, timeout=15)
    resp.raise_for_status()
    file_path = resp.json()["result"]["file_path"]

    async with httpx.AsyncClient(verify=SSL_CONTEXT, timeout=30) as client:
        file_resp = await client.get(f"{_API}/file/bot{token}/{file_path}")
        file_resp.raise_for_status()
        content = file_resp.content
        if len(content) > max_bytes:
            raise ValueError(f"Telegram file too large: {len(content)} bytes > {max_bytes} cap")
        return content


async def get_updates(offset: int | None, *, timeout: int, allowed_updates: list[str]) -> list[dict]:
    """Long-poll getUpdates. Raises TelegramConflict on 409 (another consumer
    is polling this bot), httpx.HTTPStatusError on other non-2xx, or a
    transport exception — the poller loop classifies each."""
    params: dict = {"timeout": timeout, "allowed_updates": allowed_updates}
    if offset is not None:
        params["offset"] = offset
    resp = await _call("getUpdates", params, timeout=timeout + 15)
    if resp.status_code == 409:
        raise TelegramConflict("another getUpdates consumer is polling this bot")
    resp.raise_for_status()
    return resp.json().get("result", [])


async def health_check() -> bool:
    try:
        resp = await _call("getMe", {}, timeout=5)
        return resp.status_code == 200 and bool(resp.json().get("ok"))
    except Exception:
        return False


async def _send_payload(payload: dict) -> tuple[bool, bool]:
    """Send one notify-shaped payload (chunked + buttons on the last chunk).

    Returns (delivered, terminal). terminal=True means retrying is pointless
    (auth/bad-request/missing-secret) — the caller must NOT queue. Never
    raises.
    """
    text = payload.get("content") or payload.get("message") or payload.get("text") or ""
    if not text:
        return False, True
    parse_mode = payload.get("parse_mode")
    buttons = payload.get("buttons")

    chunks = chunk_text(text)
    if not chunks:
        return True, False

    try:
        for i, chunk in enumerate(chunks):
            resp = await send_message(
                chunk, parse_mode=parse_mode,
                buttons=buttons if i == len(chunks) - 1 else None,
            )
            if resp.status_code == 200:
                continue
            if resp.status_code in _TERMINAL_STATUS_CODES:
                logger.error(
                    f"Telegram send FAILED (HTTP {resp.status_code}): {resp.text[:300]} — "
                    "retrying will NOT help."
                )
                return False, True
            if resp.status_code == 429:
                try:
                    retry_after = resp.json().get("parameters", {}).get("retry_after")
                except Exception:
                    retry_after = None
                logger.warning(f"Telegram send rate-limited (429), retry_after={retry_after}s")
            else:
                logger.warning(f"Telegram send returned HTTP {resp.status_code}")
            return False, False
        return True, False
    except KeyError as e:
        logger.error(f"Telegram send: missing secret {e} — retrying will NOT help.")
        return False, True
    except Exception as e:
        logger.warning(f"Telegram send failed: {e}")
        return False, False


async def notify(payload: dict) -> bool:
    """Send a phone notification. Payload shape: content/message/text,
    parse_mode, buttons. NEVER raises.

    401/403/404/400 and a missing secret are TERMINAL — logged at ERROR and
    NOT queued (a byte-identical retry cannot succeed). 429/5xx/transport
    errors are retryable and get queued for deliver_pending().
    """
    delivered, terminal = await _send_payload(payload)
    if delivered:
        return True
    if not terminal:
        _queue_delivery(payload, "notify")
    return False


def _next_eligible(attempts: int, last_attempt: datetime | None) -> datetime:
    """Earliest UTC time a failed delivery may be retried, via exponential backoff.

    A never-attempted row (or one with a non-positive attempt count) is eligible
    immediately. Otherwise the delay doubles per attempt: 60s, 120s, 240s, ...,
    capped at _BACKOFF_CAP_SECONDS.
    """
    if last_attempt is None or attempts <= 0:
        return datetime.min
    delay = min(_BACKOFF_BASE_SECONDS * (2 ** (attempts - 1)), _BACKOFF_CAP_SECONDS)
    return last_attempt + timedelta(seconds=delay)


def _queue_delivery(payload: dict, delivery_type: str) -> None:
    try:
        from sqlmodel import Session

        from backend.database import PendingDelivery, engine
        with Session(engine) as session:
            session.add(PendingDelivery(payload_json=json.dumps(payload), delivery_type=delivery_type))
            session.commit()
    except Exception as e:
        logger.error(f"Failed to queue delivery: {e}")


def _load_pending() -> list[dict]:
    """Read up to 10 retry-eligible pending deliveries off the event loop. Returns
    plain dicts so no ORM objects (or DB session) cross the await boundary.

    Rows are scanned oldest-first (FIFO by created_at). A row is skipped if it has
    hit the dead-letter cap (attempts >= _MAX_ATTEMPTS) or if its exponential-backoff
    window has not yet elapsed. This stops a permanently-failing 'poison' row from
    occupying a delivery slot every cycle and starving newer deliveries."""
    from sqlmodel import Session, select

    from backend.database import PendingDelivery, engine

    now = datetime.utcnow()
    with Session(engine) as session:
        rows = session.exec(
            select(PendingDelivery).order_by(PendingDelivery.created_at).limit(50)
        ).all()
        eligible: list[dict] = []
        for r in rows:
            if r.attempts >= _MAX_ATTEMPTS:
                continue
            if _next_eligible(r.attempts, r.last_attempt) > now:
                continue
            eligible.append(
                {
                    "id": r.id,
                    "payload_json": r.payload_json,
                    "delivery_type": r.delivery_type,
                    "attempts": r.attempts,
                    "last_attempt": r.last_attempt,
                }
            )
            if len(eligible) >= 10:
                break
        return eligible


def _apply_pending_results(delivered_ids: list[int], failed_ids: list[int]) -> None:
    """Apply delivery outcomes off the event loop in a single short transaction."""
    from sqlmodel import Session, select

    from backend.database import PendingDelivery, engine

    if not delivered_ids and not failed_ids:
        return

    with Session(engine) as session:
        if delivered_ids:
            for delivery in session.exec(
                select(PendingDelivery).where(PendingDelivery.id.in_(delivered_ids))
            ).all():
                session.delete(delivery)
        if failed_ids:
            now = datetime.utcnow()
            for delivery in session.exec(
                select(PendingDelivery).where(PendingDelivery.id.in_(failed_ids))
            ).all():
                delivery.attempts += 1
                delivery.last_attempt = now
                # Fires exactly once: once at/over the cap the row is no longer
                # loaded by _load_pending(), so it can't be incremented again.
                if delivery.attempts >= _MAX_ATTEMPTS:
                    logger.warning(
                        f"Dead-lettering pending delivery id={delivery.id} "
                        f"type={delivery.delivery_type} after {delivery.attempts} attempts — purging"
                    )
                    session.delete(delivery)
        session.commit()


def delivery_queue_health() -> dict:
    """Sync helper: delivery queue stats for the safety status endpoint.

    Run via asyncio.to_thread — never called directly from the event loop.
    Returns pending_count, oldest_age_seconds, dead_lettered_count, secret_present.
    Never raises.
    """
    try:
        from sqlmodel import Session, select
        from backend.database import PendingDelivery, engine
        from backend.config import get_settings

        settings = get_settings()
        try:
            secret_present = bool(settings.telegram_bot_token)
        except Exception:
            secret_present = False

        with Session(engine) as session:
            rows = session.exec(select(PendingDelivery)).all()
            pending_count = len(rows)
            dead_lettered_count = sum(1 for r in rows if r.attempts >= _MAX_ATTEMPTS)
            oldest_age_seconds = (
                int((datetime.utcnow() - min(r.created_at for r in rows)).total_seconds())
                if rows else None
            )

        return {
            "pending_count": pending_count,
            "oldest_age_seconds": oldest_age_seconds,
            "dead_lettered_count": dead_lettered_count,
            "secret_present": secret_present,
        }
    except Exception:
        return {
            "pending_count": 0,
            "oldest_age_seconds": None,
            "dead_lettered_count": 0,
            "secret_present": False,
        }


async def deliver_pending() -> None:
    """Retries queued deliveries. Sends directly (not via notify()) so a
    retryable failure updates attempts/backoff through _apply_pending_results
    exactly once, instead of also inserting a duplicate row via notify()'s
    own _queue_delivery call."""
    pending = await asyncio.to_thread(_load_pending)
    if not pending:
        return

    delivered_ids: list[int] = []
    failed_ids: list[int] = []

    for delivery in pending:
        if delivery["delivery_type"] != "notify":
            # Only "notify" rows are ever queued going forward. A row of any
            # other type is a legacy row type that is no longer produced and
            # cannot be replayed to Telegram — drop it rather than
            # dead-lettering forever.
            logger.warning(
                f"Retry delivery id={delivery['id']} has unsupported "
                f"delivery_type={delivery['delivery_type']!r} — dropping"
            )
            delivered_ids.append(delivery["id"])
            continue
        try:
            payload = json.loads(delivery["payload_json"])
        except Exception as e:
            logger.warning(f"Retry delivery id={delivery['id']} payload corrupt, dropping: {e}")
            delivered_ids.append(delivery["id"])
            continue

        delivered, _terminal = await _send_payload(payload)
        if delivered:
            delivered_ids.append(delivery["id"])
        else:
            failed_ids.append(delivery["id"])

    total = len(pending)
    d, f = len(delivered_ids), len(failed_ids)
    if f and not d:
        logger.error(f"Telegram delivery cycle: 0/{total} delivered, {f} still failing")
    elif f:
        logger.warning(f"Telegram delivery cycle: {d}/{total} delivered, {f} failed")
    else:
        logger.info(f"Telegram delivery cycle: {d}/{total} delivered")

    await asyncio.to_thread(_apply_pending_results, delivered_ids, failed_ids)
