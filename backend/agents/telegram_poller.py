"""Long-poll Telegram getUpdates for inline-button callbacks.

Polling, not a webhook — same choice Hermes made (main.py's run_polling), and
it means NEXUS needs no inbound exposure. Runs as an asyncio task on the
lifespan loop, NOT a daemon thread: the poll is a single idle httpx socket
(pure async I/O), so it never blocks the loop, and the forced Windows
SelectorEventLoop handles sockets fine (its limits are subprocess transports,
which this never touches). memo_watcher needs a thread only because
watchdog.Observer.join() is blocking; this has no such constraint.

Dispatch calls the SAME internal functions the REST confirm/reject/approve/
reject endpoints already use (goals.approve/reject, broker.confirm_action/
reject_action) — all already enforce single-use/TTL/kill-switch server-side,
untouched by this module.
"""
import asyncio
import logging

from backend.integrations import telegram

logger = logging.getLogger(__name__)

_task: asyncio.Task | None = None
_stop_event: asyncio.Event | None = None


async def _dispatch(namespace: str, verb: str, obj_id: int) -> tuple[bool, str]:
    """Returns (definitive, human_result). definitive=False means an internal/
    dispatch error -> alert popup, KEEP the buttons (parity with Hermes's
    transport-error handling)."""
    try:
        if namespace == "goal":
            from backend.agents import goals
            if verb == "approve":
                r = await goals.approve(obj_id)
                default_msg = "Approved."
            elif verb == "reject":
                r = await goals.reject(obj_id, reason="rejected via Telegram")
                default_msg = "Rejected."
            else:
                return False, f"Unknown goal verb: {verb}"
            mapping = {
                "not_found": "Goal not found.",
                "conflict": f"Goal already {r.get('current')}.",
                "expired": "Goal proposal expired.",
            }
            return True, mapping.get(r.get("status"), default_msg)

        if namespace == "safety":
            from backend.config import get_settings
            from backend.safety import broker
            if verb == "confirm":
                ttl = get_settings().action_confirm_ttl_seconds
                status, _res = await broker.confirm_action(obj_id, ttl_seconds=ttl)
            elif verb == "reject":
                status, _res = await broker.reject_action(obj_id)
            else:
                return False, f"Unknown safety verb: {verb}"
            mapping = {
                "not_found": "Action not found.",
                "not_confirmable": "Action is not awaiting confirmation.",
                "expired": "Confirmation window expired.",
                "forbidden": "Blocked: autonomy is paused.",
                "executed": "Executed.",
                "failed": "Failed.",
                "rejected": "Rejected.",
            }
            return True, mapping.get(status, status)

        return False, f"Unknown namespace: {namespace}"
    except Exception as e:
        logger.warning(f"telegram_poller dispatch error: {e}")
        return False, str(e)


def _handle_unknown_message(msg: dict) -> None:
    """Logs any inbound message from a chat_id that isn't the configured
    TELEGRAM_CHAT_ID, so Brian can self-serve discovery without needing to
    stop the poller first (removes the manual-step ordering trap)."""
    chat = msg.get("chat") or {}
    logger.info(
        f"Telegram message from unrecognized chat_id={chat.get('id')} — "
        "add it as TELEGRAM_CHAT_ID if this is you"
    )


async def handle_callback(cq: dict) -> None:
    """Parse -> authorize -> dispatch -> answerCallbackQuery -> editMessageText.

    answerCallbackQuery is SINGLE-USE per query — answered exactly once, after
    the outcome is known, mirroring Hermes's prior discipline.
    """
    cq_id = cq.get("id")
    data = cq.get("data") or ""
    message = cq.get("message") or {}
    chat_id = (message.get("chat") or {}).get("id")
    message_id = message.get("message_id")
    original_text = message.get("text") or ""

    from backend.config import get_settings
    try:
        allowed_chat_id = get_settings().telegram_chat_id
    except Exception:
        allowed_chat_id = None

    # Fail CLOSED: an unreadable TELEGRAM_CHAT_ID (missing, or a transient
    # secret-store error) must never be treated as "no check configured" —
    # this replaced a Bearer-authed REST endpoint, so an unset/unreadable
    # chat_id should refuse every callback, not let them all through.
    if allowed_chat_id is None or str(chat_id) != str(allowed_chat_id):
        await telegram.answer_callback_query(cq_id, "Not authorized", show_alert=True)
        return

    parts = data.split(":", 2)
    if len(parts) != 3:
        await telegram.answer_callback_query(cq_id)
        return
    namespace, verb, obj_id_str = parts
    try:
        obj_id = int(obj_id_str)
    except ValueError:
        await telegram.answer_callback_query(cq_id, "Invalid id.", show_alert=True)
        return

    definitive, result = await _dispatch(namespace, verb, obj_id)

    if not definitive:
        # Internal/dispatch error — alert popup, KEEP the buttons for a retry.
        await telegram.answer_callback_query(cq_id, result[:190], show_alert=True)
        return

    await telegram.answer_callback_query(cq_id)
    icon = "✓" if verb in ("approve", "confirm") else "✗"
    if chat_id is not None and message_id is not None:
        await telegram.edit_message_text(chat_id, message_id, f"{original_text}\n\n{icon} {result}")


async def poll_once(offset: int | None) -> int | None:
    """One getUpdates cycle. Returns the new offset. Raises TelegramConflict
    or other exceptions — run_poller classifies them."""
    from backend.config import get_settings
    settings = get_settings()
    updates = await telegram.get_updates(
        offset,
        timeout=settings.telegram_poll_timeout_s,
        allowed_updates=["callback_query", "message"],
    )
    new_offset = offset
    for update in updates:
        new_offset = update["update_id"] + 1
        cq = update.get("callback_query")
        if cq:
            await handle_callback(cq)
            continue
        msg = update.get("message")
        if msg:
            _handle_unknown_message(msg)
    return new_offset


async def _sleep_or_stop(stop: asyncio.Event, seconds: float) -> None:
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        pass


async def run_poller(stop: asyncio.Event) -> None:
    """The poll loop. Never raises out — every exception is logged and
    backed off, so a Telegram outage degrades to 'no inbound buttons', never
    a crashed task."""
    offset = None
    backoff = 1.0
    logger.info("Telegram poller started (offset=None)")
    while not stop.is_set():
        try:
            offset = await poll_once(offset)
            backoff = 1.0
        except telegram.TelegramConflict as e:
            logger.error(f"Telegram getUpdates conflict: {e} — sleeping 30s")
            await _sleep_or_stop(stop, 30)
        except Exception as e:
            logger.warning(f"Telegram poller error (backing off {backoff:.0f}s): {e}")
            await _sleep_or_stop(stop, backoff)
            backoff = min(backoff * 2, 60)


def start() -> "asyncio.Task | None":
    """Starts the poller task. Returns None (no task created) when disabled or
    the bot token isn't configured yet — a poller failure/absence degrades to
    'no inbound buttons', never blocks NEXUS boot."""
    global _task, _stop_event
    from backend.config import get_settings
    settings = get_settings()
    if not settings.telegram_poll_enabled:
        logger.info("Telegram poller disabled (telegram_poll_enabled=False)")
        return None
    try:
        settings.telegram_bot_token
    except Exception:
        logger.info("Telegram poller not started — TELEGRAM_BOT_TOKEN not configured")
        return None

    _stop_event = asyncio.Event()
    _task = asyncio.create_task(run_poller(_stop_event))
    return _task


async def stop() -> None:
    global _task, _stop_event
    if _stop_event is not None:
        _stop_event.set()
    if _task is not None:
        try:
            await asyncio.wait_for(_task, timeout=5)
        except Exception:
            _task.cancel()
    _task = None
    _stop_event = None
