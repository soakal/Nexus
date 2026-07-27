"""Telegram bot command handlers — Phase 2a (chat + read-only status commands).

Used by telegram_poller.py for every non-callback inbound message. Bare text
(no leading "/") is treated as a chat message — matches how Hermes's bot
behaved, so there's no new habit to learn; /nx is kept as an explicit alias
for the same handler. Every handler is wrapped by dispatch() so one handler's
exception can never kill the poll loop.

Deliberately NOT built here (see the Phase 2 plan): /model (no NEXUS
equivalent — see plan fork F1), /remember /facts /forget /research (need
their own design decisions, Phase 2b), and the background alert watcher
(Phase 2c, not a command at all).
"""
import logging
from collections.abc import Awaitable, Callable

from backend.integrations import telegram

logger = logging.getLogger(__name__)

Handler = Callable[[str, dict], Awaitable[str | None]]


def _chat_id(msg: dict):
    return (msg.get("chat") or {}).get("id")


async def _cmd_chat(args: str, msg: dict) -> str:
    """Bare text and /nx both land here — NEXUS's own chat(), in-process
    (Hermes's /nx made an HTTP round-trip to the same endpoint)."""
    import asyncio
    from backend.agents.chat import chat
    from backend.safety import governor

    text = args.strip()
    if not text:
        return 'Ask me something, e.g. "what\'s the weather" or "is the garage open".'

    await telegram.send_chat_action("typing", chat_id=_chat_id(msg))
    conversation_id = await asyncio.to_thread(governor.get_telegram_conversation_id)
    result = await chat(conversation_id, text)
    await asyncio.to_thread(governor.set_telegram_conversation_id, result["conversation_id"])
    return result["reply"]


async def _cmd_clear(args: str, msg: dict) -> str:
    import asyncio
    from backend.safety import governor

    await asyncio.to_thread(governor.set_telegram_conversation_id, None)
    return "Conversation cleared — starting fresh."


async def _cmd_help(args: str, msg: dict) -> str:
    lines = ["Available commands:"]
    for name, (_, desc) in sorted(COMMANDS.items()):
        lines.append(f"/{name} — {desc}")
    lines.append("\nOr just type a message to chat.")
    return "\n".join(lines)


async def _cmd_status(args: str, msg: dict) -> str:
    """Native, no-LLM homelab status snapshot — costs zero AI spend, unlike
    routing through chat()'s STATUS branch."""
    import asyncio
    from backend.integrations import adguard, calendar, homeassistant, proxmox, unifi, unraid
    from backend.safety import governor

    results = await asyncio.gather(
        proxmox.fetch(), unraid.fetch(), unifi.fetch(), adguard.fetch(),
        homeassistant.fetch(), calendar.fetch(),
        return_exceptions=True,
    )
    px, ur, uf, ag, ha, cal = results

    lines = []

    if isinstance(px, Exception):
        lines.append("Proxmox: unavailable")
    else:
        lines.append(
            f"Proxmox: {px.node_status}, {px.cpu_pct:.0f}% cpu, "
            f"{px.mem_used_gb:.0f}/{px.mem_total_gb:.0f} GiB, {len(px.vms)} VMs/LXCs"
        )

    if isinstance(ur, Exception):
        lines.append("Unraid: unavailable")
    else:
        free_gb = ur.storage_total_gb - ur.storage_used_gb
        lines.append(
            f"Unraid: array {ur.array_status}, {free_gb:.0f} GB free, "
            f"{len(ur.docker_containers)} containers"
        )

    if isinstance(uf, Exception):
        lines.append("UniFi: unavailable")
    else:
        lines.append(f"UniFi: {uf.client_count} clients")

    if isinstance(ag, Exception):
        lines.append("AdGuard: unavailable")
    else:
        filt = "unknown" if ag.filtering_enabled is None else ("on" if ag.filtering_enabled else "off")
        lines.append(f"AdGuard: filtering {filt}, {ag.blocked_pct:.0f}% blocked today")

    if isinstance(ha, Exception):
        lines.append("Home Assistant: unavailable")
    else:
        lines.append(f"Home Assistant: {len(ha.alerts)} entities unavailable")

    if isinstance(cal, Exception):
        lines.append("Calendar: unavailable")
    else:
        lines.append(f"Calendar: {len(cal.events)} events in the next 7 days")

    state = await asyncio.to_thread(governor.get_system_state)
    spend = await asyncio.to_thread(governor.today_spend_usd)
    lines.append(f"Spend: ${spend:.2f} / ${state['daily_budget_usd']:.2f}")
    lines.append(f"Autonomy: {'enabled' if state['autonomy_enabled'] else 'PAUSED'}")

    return "\n".join(lines)


async def _cmd_calendar(args: str, msg: dict) -> str:
    from backend.integrations.calendar import get_today_events
    return await get_today_events()


async def _cmd_mail(args: str, msg: dict) -> str:
    from backend.integrations import protonmail
    return await protonmail.inbox_summary()


async def _cmd_spend(args: str, msg: dict) -> str:
    import asyncio
    from backend.safety import governor

    state = await asyncio.to_thread(governor.get_system_state)
    spend = await asyncio.to_thread(governor.today_spend_usd)
    daily_cap = state["daily_budget_usd"]
    pct = (spend / daily_cap * 100) if daily_cap else 0.0
    return (
        f"Today: ${spend:.2f} / ${daily_cap:.2f} ({pct:.0f}%)\n"
        f"Per-task cap: ${state['per_task_budget_usd']:.2f}"
    )


async def _cmd_vms(args: str, msg: dict) -> str:
    from backend.integrations import proxmox

    try:
        data = await proxmox.fetch()
    except Exception as e:
        return f"Proxmox unavailable: {e}"
    if not data.vms:
        return "No VMs/LXCs found."
    return "\n".join(f"{v['name'] or v['vmid']}: {v['status']}" for v in data.vms)


async def _cmd_briefing(args: str, msg: dict) -> str:
    import asyncio
    from sqlmodel import Session, select
    from backend.database import Briefing, engine

    def _latest():
        with Session(engine) as session:
            b = session.exec(select(Briefing).order_by(Briefing.created_at.desc()).limit(1)).first()
            return b.content if b else None

    content = await asyncio.to_thread(_latest)
    return content or "No briefing yet — the daily briefing hasn't run."


COMMANDS: dict[str, tuple[Handler, str]] = {
    "nx": (_cmd_chat, "Ask NEXUS anything"),
    "help": (_cmd_help, "List commands"),
    "status": (_cmd_status, "Quick homelab status (no AI, instant)"),
    "clear": (_cmd_clear, "Start a fresh conversation"),
    "calendar": (_cmd_calendar, "Upcoming calendar events"),
    "mail": (_cmd_mail, "Unread email summary"),
    "spend": (_cmd_spend, "Today's AI spend vs budget"),
    "vms": (_cmd_vms, "Proxmox VM/LXC list"),
    "briefing": (_cmd_briefing, "Latest morning briefing"),
}


def command_menu() -> list[dict]:
    """COMMANDS -> Telegram's setMyCommands payload (the "/" autocomplete menu)."""
    return [{"command": name, "description": desc} for name, (_, desc) in COMMANDS.items()]


async def dispatch(text: str, msg: dict) -> None:
    """Parse '/cmd@botname args' or bare text -> run the matching handler ->
    send_reply. Every handler exception is caught here — a single handler
    failure can never kill the poll loop."""
    stripped = text.strip()
    if stripped.startswith("/"):
        parts = stripped[1:].split(maxsplit=1)
        cmd_token = parts[0].split("@", 1)[0].lower() if parts and parts[0] else ""
        args = parts[1] if len(parts) > 1 else ""
        entry = COMMANDS.get(cmd_token)
        if entry is None:
            await telegram.send_reply(f"Unknown command /{cmd_token} — try /help", chat_id=_chat_id(msg))
            return
        handler = entry[0]
    else:
        handler = _cmd_chat
        args = stripped

    try:
        reply = await handler(args, msg)
    except Exception as e:
        logger.warning(f"Telegram command handler error: {e}")
        reply = f"Something went wrong: {e}"

    if reply is not None:
        await telegram.send_reply(reply, chat_id=_chat_id(msg))
