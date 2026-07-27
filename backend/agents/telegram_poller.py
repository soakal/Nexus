"""Long-poll Telegram getUpdates for inline-button callbacks AND (Phase 2a)
text commands/chat.

Polling, not a webhook — same choice Hermes made (main.py's run_polling), and
it means NEXUS needs no inbound exposure. Runs as an asyncio task on the
lifespan loop, NOT a daemon thread: the poll is a single idle httpx socket
(pure async I/O), so it never blocks the loop, and the forced Windows
SelectorEventLoop handles sockets fine (its limits are subprocess transports,
which this never touches). memo_watcher needs a thread only because
watchdog.Observer.join() is blocking; this has no such constraint.

Button dispatch calls the SAME internal functions the REST confirm/reject/
approve/reject endpoints already use (goals.approve/reject,
broker.confirm_action/reject_action) — all already enforce single-use/TTL/
kill-switch server-side, untouched by this module. Text commands dispatch
through backend/agents/telegram_commands.py.

Authorization is fail-CLOSED and asymmetric by design: an unauthorized
CALLBACK gets a "Not authorized" popup (that user already has a button, so
they're already known to be someone who received an alert). An unauthorized
MESSAGE gets NO reply at all — only a log line. Replying to a stranger who
messages the bot would confirm it's live and invite probing.

This matters more than it looks: chat() has no actor parameter — every
branch it dispatches through the broker with hardcodes actor="user", which
the broker always-allows with NO confirm gate and NO kill-switch check
(both are agent/autonomous-only guards). That's the intended trust model —
a chat_id that passes IS Brian, same as a valid Bearer key — but it means
this authorization check is the ONLY gate in front of the three highest-
trust things NEXUS can do: HOME_CONTROL, a free-text hermes_relay (which
broker.py forbids to agents even when "confirmed" — this is the one path
where a human can still use it), and TASK intent's full orchestrator
plan+execute loop. A hole here is not "an inconvenience", it's full control.
"""
import asyncio
import logging
import time

from backend.integrations import telegram

logger = logging.getLogger(__name__)

_task: asyncio.Task | None = None
_stop_event: asyncio.Event | None = None
# Gates concurrent message handling — a slow chat() call (20-40s with web
# search) never blocks getUpdates or a button tap, but two messages still
# serialize relative to each other rather than racing each other's replies.
_message_semaphore = asyncio.Semaphore(1)
# Strong references to in-flight message tasks — asyncio.create_task() only
# holds a WEAK reference internally, so a task with nothing else referencing
# it can be garbage-collected mid-run (silently: no reply, no log, nothing).
# Every task is added here and removed via its own done-callback.
_message_tasks: set[asyncio.Task] = set()


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


def _authorized(chat_id) -> bool:
    """Fail CLOSED: an unreadable/missing TELEGRAM_CHAT_ID (absent, or a
    transient secret-store error) refuses everything rather than treating
    "no check configured" as "no check needed" — this replaced a Bearer-authed
    REST endpoint, so an unset/unreadable chat_id must refuse, not admit."""
    from backend.config import get_settings
    try:
        allowed_chat_id = get_settings().telegram_chat_id
    except Exception:
        allowed_chat_id = None
    return allowed_chat_id is not None and str(chat_id) == str(allowed_chat_id)


def _handle_unknown_message(msg: dict) -> None:
    """Logs any inbound message from a chat_id that isn't the configured
    TELEGRAM_CHAT_ID, so Brian can self-serve discovery without needing to
    stop the poller first (removes the manual-step ordering trap). Never
    replies — see the module docstring on why silence is the safe default."""
    chat = msg.get("chat") or {}
    logger.info(
        f"Telegram message from unrecognized chat_id={chat.get('id')} — "
        "add it as TELEGRAM_CHAT_ID if this is you"
    )


async def _handle_message(msg: dict) -> None:
    """Authorized -> command/chat dispatch, fire-and-forget + semaphore-gated
    so a slow chat() call never blocks getUpdates or a button tap.
    Unauthorized -> log only, no reply (module docstring explains why)."""
    chat_id = (msg.get("chat") or {}).get("id")
    if not _authorized(chat_id):
        _handle_unknown_message(msg)
        return

    text = msg.get("text")
    if not text:
        return  # non-text message (photo, sticker, voice, ...) — not handled in Phase 2a

    async def _run():
        async with _message_semaphore:
            from backend.agents import telegram_commands
            await telegram_commands.dispatch(text, msg)

    task = asyncio.create_task(_run())
    _message_tasks.add(task)
    task.add_done_callback(_message_tasks.discard)


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

    if not _authorized(chat_id):
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
    or other exceptions — run_poller classifies them.

    A NEXUS restart replays up to 24h of un-acked updates. Harmless for
    single-use buttons (goals.approve/broker.confirm_action are idempotent on
    a second call — see Phase 1's verify pass), but NOT harmless for a
    command: a queued "/briefing" or bare-text HOME_CONTROL message
    re-executing on boot is a real correctness issue this phase introduces.
    Messages older than telegram_command_max_age_s are dropped; callbacks are
    never age-filtered (their own idempotency already covers replay)."""
    from backend.config import get_settings
    settings = get_settings()
    updates = await telegram.get_updates(
        offset,
        timeout=settings.telegram_poll_timeout_s,
        allowed_updates=["callback_query", "message"],
    )
    max_age = getattr(settings, "telegram_command_max_age_s", 300)
    new_offset = offset
    for update in updates:
        new_offset = update["update_id"] + 1
        try:
            cq = update.get("callback_query")
            if cq:
                await handle_callback(cq)
                continue
            msg = update.get("message")
            if not msg:
                continue
            raw_date = msg.get("date")
            if not isinstance(raw_date, (int, float)):
                # Fail CLOSED: a missing/malformed date can't be proven fresh,
                # so treat it as stale rather than defaulting to "now" — that
                # would fail OPEN, exactly what this guard exists to prevent.
                logger.warning(f"Telegram message missing/invalid 'date' field — dropping: {raw_date!r}")
                continue
            age = time.time() - raw_date
            if age > max_age:
                logger.info(f"Dropping stale Telegram message (age={age:.0f}s > {max_age}s) — likely replayed after a restart")
                continue
            await _handle_message(msg)
        except Exception as e:
            # A malformed single update must not wedge the whole poll cycle —
            # new_offset is already advanced above, so it won't be refetched.
            logger.warning(f"poll_once: error processing update {update.get('update_id')}: {e}")
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
    from backend.agents import telegram_commands
    await telegram.set_my_commands(telegram_commands.command_menu())

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
    # In-flight message-handling tasks (e.g. a slow chat() call) don't stop on
    # their own — cancel any still running rather than leaving them orphaned
    # past shutdown. _message_tasks.discard is already wired as each task's
    # done-callback, so this loop naturally shrinks as cancellation lands.
    for task in list(_message_tasks):
        task.cancel()
    _stop_event = None
