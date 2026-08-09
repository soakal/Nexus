"""Telegram bot command handlers.

Phase 2a: chat + read-only status commands. Phase 2b (this build): memory
(/remember /facts /forget), goals/tasks (/goals /task /tasks), /digest,
runtime per-kind notify muting (/mute /unmute /muted), and voice-message
transcription (wired in telegram_poller.py, dispatches through this same
module once transcribed).

Used by telegram_poller.py for every non-callback inbound message. Bare text
(no leading "/") is treated as a chat message; /nx is kept as an explicit
alias for the same handler. Every handler is wrapped by dispatch() so one
handler's exception can never kill the poll loop.

Deliberately NOT built here: /model (no NEXUS equivalent — its model tiers
are .env-configured, not chat-switchable) and the background homelab alert
watcher (VM/docker/garage/doorbell — Phase 2c, not a command at all, not yet
scoped).
"""
import logging
import re
from collections.abc import Awaitable, Callable
from datetime import datetime

from backend.integrations import telegram

logger = logging.getLogger(__name__)

# Recognized second-token statuses for /resolve — anything else in that
# position is treated as the start of a free-text note instead (spec
# §3.2/§2.2-D: "/resolve 12 fixed it" must not become an "invalid_status"
# error). Deliberately excludes "open" (not a meaningful resolve target).
_RESOLVE_STATUSES = frozenset({"resolved", "false_positive", "deferred", "needs_follow_up"})

Handler = Callable[[str, dict], Awaitable[str | None]]


def _chat_id(msg: dict):
    return (msg.get("chat") or {}).get("id")


async def _cmd_chat(args: str, msg: dict) -> str:
    """Bare text and /nx both land here — NEXUS's own chat(), in-process."""
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


async def _cmd_remember(args: str, msg: dict) -> str:
    """Free-text -> the same Haiku fact-extractor chat()/briefing() already
    use (source='telegram'), not a strict subject|value format — consistent
    with the rest of NEXUS, at the cost of being non-deterministic (it can
    extract zero, one, or several facts from one message)."""
    from backend.agents import facts

    text = args.strip()
    if not text:
        return "Usage: /remember <something to remember>"
    await facts.extract_and_store(text, conversation_id=None, source="telegram")
    return "Noted — check /facts to see what I picked up (extraction isn't always 1:1 with what you typed)."


async def _cmd_facts(args: str, msg: dict) -> str:
    import asyncio
    from backend.agents import facts

    rows = await asyncio.to_thread(facts._db_list_facts_for_audit)
    if not rows:
        return "No facts stored yet."
    lines = [f"#{r['id']} {r['subject']}: {r['predicate']} {r['value']} ({r['confidence']:.1f})" for r in rows[:30]]
    if len(rows) > 30:
        lines.append(f"...and {len(rows) - 30} more.")
    return "\n".join(lines)


async def _cmd_forget(args: str, msg: dict) -> str:
    from backend.agents import facts

    try:
        fact_id = int(args.strip())
    except ValueError:
        return "Usage: /forget <fact id> — see /facts for ids"
    ok = await facts.dismiss_fact(fact_id)
    return f"Forgot fact #{fact_id}." if ok else f"No fact #{fact_id} found."


async def _cmd_goals(args: str, msg: dict) -> str:
    import asyncio
    from backend.agents import goals as goals_agent

    rows = await asyncio.to_thread(goals_agent._db_list_goals, None, 20)
    if not rows:
        return "No goals yet."
    return "\n".join(f"#{g['id']} [{g['status']}] {g['title']}" for g in rows)


async def _cmd_task(args: str, msg: dict) -> str:
    """Durable orchestrator task — the /research successor. No auto-emailed
    report (protonmail_send is IRREVERSIBLE and hard-forbidden to non-user
    actors, so that would need its own separate design decision); a Telegram
    message now arrives here on completion (worker_pool._notify_task_finished)
    — check /tasks or the Tasks page for progress in the meantime."""
    import asyncio

    prompt = args.strip()
    if not prompt:
        return "Usage: /task <what you want done>"

    def _create() -> int:
        from sqlmodel import Session
        from backend.database import Task, engine
        with Session(engine) as session:
            task = Task(prompt=prompt, status="pending")
            session.add(task)
            session.commit()
            session.refresh(task)
            return task.id

    task_id = await asyncio.to_thread(_create)
    from backend.agents.worker_pool import get_pool
    await get_pool().enqueue(task_id)
    return f"Task #{task_id} queued — I'll message you here when it finishes. Check /tasks or the Tasks page for progress meanwhile."


async def _cmd_tasks(args: str, msg: dict) -> str:
    import asyncio

    def _list():
        from sqlmodel import Session, select
        from backend.database import Task, engine
        with Session(engine) as session:
            rows = session.exec(select(Task).order_by(Task.created_at.desc()).limit(10)).all()
            return [(t.id, t.status, t.prompt) for t in rows]

    rows = await asyncio.to_thread(_list)
    if not rows:
        return "No tasks yet."
    return "\n".join(f"#{tid} [{status}] {prompt[:60]}" for tid, status, prompt in rows)


async def _cmd_digest(args: str, msg: dict) -> str:
    from backend.agents.digest import build_autonomy_digest
    return await build_autonomy_digest()


async def _cmd_prune(args: str, msg: dict) -> str:
    """Prune dangling Docker images on Unraid (Phase 7d, native SSH, no args).

    Goes through the broker as actor="user" (a Telegram command from an
    authorized chat IS the human decision, same precedent as every other
    user-actor command here) — never lets a RuntimeError from the SSH path
    (missing credential, connection failure, non-zero exit) crash the handler."""
    from backend.safety.broker import Decision, execute_action

    try:
        res = await execute_action(
            actor="user",
            kind="unraid_docker_prune",
            target="unraid",
            payload={},
        )
    except Exception as e:
        return f"Docker prune failed: {e}"

    if res.decision == Decision.EXECUTED and res.result:
        return res.result.get("reclaimed", "Docker prune completed.")
    return f"Docker prune failed: {res.error or res.decision.value}"


def _mute_kind_menu() -> str:
    """Sorted, human-readable list of every mutable kind for the no-args
    /mute reply. Never-mutable kinds are shown with a marker rather than
    hidden -- Brian should be able to see WHY /mute auth_burst is refused."""
    from backend.events import NOTIFY_KINDS
    from backend.safety.governor import _NEVER_MUTABLE_NOTIFY_KINDS
    lines = []
    for k in sorted(NOTIFY_KINDS):
        lines.append(f"  {k}  (never mutable)" if k in _NEVER_MUTABLE_NOTIFY_KINDS else f"  {k}")
    return "\n".join(lines)


async def _cmd_mute(args: str, msg: dict) -> str:
    import asyncio
    from backend.safety import governor

    kind = args.strip()
    if not kind:
        return (
            "Usage: /mute <kind> — e.g. /mute homelab_garage\n"
            "See /muted for what's currently muted.\n\nValid kinds:\n"
            + _mute_kind_menu()
        )
    try:
        await asyncio.to_thread(governor.add_muted_notify_kind, kind)
    except ValueError as e:
        return f"{e}\nRun /mute with no arguments to see every valid kind."
    return f"Muted notifications of kind '{kind}'."


async def _cmd_unmute(args: str, msg: dict) -> str:
    import asyncio
    from backend.safety import governor

    kind = args.strip()
    if not kind:
        return "Usage: /unmute <kind>"
    was_muted = await asyncio.to_thread(governor.remove_muted_notify_kind, kind)
    if not was_muted:
        return f"'{kind}' wasn't muted — nothing changed. /muted lists what is."
    return f"Unmuted '{kind}'."


async def _cmd_muted(args: str, msg: dict) -> str:
    import asyncio
    from backend.safety import governor

    kinds = await asyncio.to_thread(governor.get_muted_notify_kinds)
    if not kinds:
        return "Nothing muted."
    return "Muted: " + ", ".join(sorted(kinds))


async def _cmd_image(args: str, msg: dict) -> str | None:
    """Generates via Pollinations.ai and sends the photo directly, returning
    None (dispatch()'s existing 'handler already replied' convention) —
    keeps Handler/dispatch() untouched rather than adding a photo-reply type."""
    from backend.integrations import image_gen

    prompt = args.strip()
    if not prompt:
        return "Usage: /image <prompt> — e.g. /image a neon cyberpunk cat"

    await telegram.send_chat_action("upload_photo", chat_id=_chat_id(msg))
    img = await image_gen.generate_image(prompt)
    if img is None:
        return "Image generation is unavailable right now — try again in a minute."

    ok = await telegram.send_photo(img, caption=prompt[:200], chat_id=_chat_id(msg))
    if not ok:
        return "Generated the image but Telegram rejected the upload."
    return None


def _format_age(iso_str: str | None) -> str:
    """Best-effort "Ns/Nm/Nh/Nd ago" rendering of an ISO timestamp — never
    raises (a malformed/missing timestamp degrades to "?", matching the
    defensive-read discipline used throughout this module)."""
    if not iso_str:
        return "?"
    try:
        then = datetime.fromisoformat(iso_str)
    except ValueError:
        return "?"
    seconds = (datetime.utcnow() - then).total_seconds()
    if seconds < 60:
        return "just now"
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes}m ago"
    hours = int(minutes // 60)
    if hours < 24:
        return f"{hours}h ago"
    days = int(hours // 24)
    return f"{days}d ago"


def _format_date(iso_str: str | None) -> str:
    """ISO timestamp -> "YYYY-MM-DD" for /calibration's since/re-test dates —
    never raises (mirrors _format_age's defensive-read discipline: a
    malformed/missing timestamp degrades to "?")."""
    if not iso_str:
        return "?"
    try:
        return datetime.fromisoformat(iso_str).date().isoformat()
    except ValueError:
        return "?"


async def _cmd_flags(args: str, msg: dict) -> str:
    """Open + needs_follow_up + deferred-past-due flags — same shape as
    /goals and /tasks."""
    from backend.agents import outcomes

    rows = await outcomes.open_flags()
    if not rows:
        return "No open flags."
    return "\n".join(
        f"#{r['id']} [{r['severity']}] {r['source']}:{r['check']} — "
        f"{r['summary']} ({_format_age(r['created_at'])})"
        for r in rows
    )


async def _cmd_resolve(args: str, msg: dict) -> str:
    """/resolve <id> [status] [note] — default status "resolved" so the
    common case is bare "/resolve 12". The second token is only treated as a
    status if it's one of the recognized words; otherwise the whole
    remainder is a free-text note against the default status (spec
    §2.2-D/§3.2) — "/resolve 12 fixed it" must not error as invalid_status."""
    from backend.agents import outcomes

    parts = args.split(maxsplit=1)
    if not parts:
        return "Usage: /resolve <id> [status] [note]"
    try:
        flag_id = int(parts[0])
    except ValueError:
        return "Usage: /resolve <id> [status] [note] — <id> must be a number"

    remainder = parts[1].strip() if len(parts) > 1 else ""
    status = "resolved"
    note: str | None = remainder or None
    if remainder:
        sub = remainder.split(maxsplit=1)
        candidate = sub[0].lower()
        if candidate in _RESOLVE_STATUSES:
            status = candidate
            note = sub[1].strip() if len(sub) > 1 else None

    result = await outcomes.resolve_flag(flag_id, status, note=note, by="telegram")
    mapping = {
        "not_found": f"Flag #{flag_id} not found.",
        "already_closed": f"Flag #{flag_id} is already closed.",
        "invalid_status": f"Invalid status: {status}",
    }
    return mapping.get(result, f"Flag #{flag_id} marked {result}.")


async def _cmd_defer(args: str, msg: dict) -> str:
    """/defer <id> <days> [note] -> resolve_flag(id, "deferred", defer_days=days)."""
    from backend.agents import outcomes

    parts = args.split(maxsplit=2)
    if len(parts) < 2:
        return "Usage: /defer <id> <days> [note]"
    try:
        flag_id = int(parts[0])
        days = int(parts[1])
    except ValueError:
        return "Usage: /defer <id> <days> [note] — <id> and <days> must be numbers"

    note = parts[2].strip() if len(parts) > 2 else None
    result = await outcomes.resolve_flag(flag_id, "deferred", note=note, by="telegram", defer_days=days)
    mapping = {
        "not_found": f"Flag #{flag_id} not found.",
        "already_closed": f"Flag #{flag_id} is already closed.",
    }
    return mapping.get(result, f"Flag #{flag_id} deferred {days} day(s).")


async def _cmd_flag(args: str, msg: dict) -> str:
    """/flag <text> -> record_flag("manual", <slugified first words>, args,
    severity="medium"). Lets Brian log his own item into the same store.

    A case-insensitive "missed " prefix (spec §6, missed-detection capture)
    instead records check=f"missed:{slug-of-remaining-text}" with
    severity="high", so a self-reported missed detection is distinguishable
    from a routine manual note."""
    from backend.agents import outcomes

    text = args.strip()
    if not text:
        return "Usage: /flag <text> — log your own item into the outcome tracker"

    severity = "medium"
    if text.lower().startswith("missed "):
        remainder = text[len("missed "):].strip()
        slug = re.sub(r"[^a-z0-9]+", "_", remainder.lower()).strip("_")
        check = f"missed:{'_'.join(slug.split('_')[:6]) or 'note'}"
        severity = "high"
    else:
        slug = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
        check = "_".join(slug.split("_")[:6]) or "note"

    flag_id = await outcomes.record_flag("manual", check, text, severity=severity)
    if flag_id is None:
        return "Not recorded (outcome tracking is disabled)."
    return f"Flag #{flag_id} recorded."


async def _cmd_calibration(args: str, msg: dict) -> str:
    """/calibration [unsuppress|suppress] <fingerprint> — the observability
    surface for the calibration loop (spec §4). Bare renders
    calibration.hint_report(30) in full: SUPPRESSED renders first and is
    NEVER truncated (CAL37's no-black-box requirement), WATCHING is capped at
    15. The two subcommands call set_override, mapping its "applied" /
    "not_found" / "invalid" tri-state result the same way _cmd_resolve maps
    resolve_flag's."""
    from backend.agents import calibration

    parts = args.split(maxsplit=1)
    if parts:
        action = parts[0].lower()
        if action not in ("unsuppress", "suppress"):
            return "Usage: /calibration [unsuppress|suppress] <fingerprint>"
        fingerprint = parts[1].strip() if len(parts) > 1 else ""
        if not fingerprint:
            return f"Usage: /calibration {action} <fingerprint>"

        result = await calibration.set_override(fingerprint, active=(action == "suppress"), by="telegram")
        mapping = {
            "not_found": f"No calibration hint found for {fingerprint}.",
            "invalid": f"Invalid fingerprint: {fingerprint}",
        }
        if result in mapping:
            return mapping[result]
        verb = "suppressed" if action == "suppress" else "un-suppressed"
        return f"{fingerprint} {verb}."

    report = await calibration.hint_report(30)
    suppressed = report["suppressed"]
    watching = report["watching"]
    overridden = report["overridden"]
    if not suppressed and not watching and not overridden:
        return "No calibration data yet — flags need to accumulate ✓/✗ verdicts before rules can be judged."

    lines = [f"Flag calibration ({report['window_days']} days)", ""]

    lines.append(f"SUPPRESSED ({len(suppressed)})")
    if suppressed:
        for h in suppressed:
            pct = round(h["fp_rate"] * 100)
            line = (
                f"{h['fingerprint']} — {pct}% false alarm "
                f"({h['false_positive_count']}/{h['verdict_count']} judged)"
            )
            if h["never_auto_suppressed"]:
                line += " · HIGH, never auto-suppressed"
            lines.append(line)
            lines.append(
                f"  since {_format_date(h['since'])}, re-tests {_format_date(h['retest_at'])} "
                f"· {h['suppressed_surfacings']} occurrences silenced"
            )
    else:
        lines.append("(none)")
    lines.append("")

    lines.append("WATCHING (below threshold)")
    if watching:
        for w in watching[:15]:
            pct = round(w["fp_rate"] * 100)
            line = (
                f"{w['fingerprint']} — {pct}% false alarm "
                f"({w['false_positive_count']}/{w['verdict_count']} judged)"
            )
            if w["never_auto_suppressed"]:
                line += " · HIGH, never auto-suppressed"
            if w["auto_cleared_count"]:
                line += f" · {w['auto_cleared_count']} auto-cleared"
            lines.append(line)
    else:
        lines.append("(none)")
    lines.append("")

    lines.append(f"OVERRIDDEN BY YOU ({len(overridden)})")
    if overridden:
        for o in overridden:
            lines.append(
                f"{o['fingerprint']} — un-suppressed {_format_date(o['override_at'])}, "
                f"protected until {_format_date(o['override_until'])}"
            )
    else:
        lines.append("(none)")
    lines.append("")

    threshold_pct = round(report["fp_threshold"] * 100)
    state = "ON" if report["suppression_enabled"] else "OFF"
    lines.append(f"Suppression: {state} (>={threshold_pct}% of >={report['min_verdicts']} judged flags)")
    lines.append("/calibration unsuppress <rule>  ·  /calibration suppress <rule>")

    return "\n".join(lines)


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
    "remember": (_cmd_remember, "Save a fact"),
    "facts": (_cmd_facts, "List stored facts"),
    "forget": (_cmd_forget, "Delete a fact by id"),
    "goals": (_cmd_goals, "List recent goals"),
    "task": (_cmd_task, "Queue a durable task"),
    "tasks": (_cmd_tasks, "List recent tasks"),
    "digest": (_cmd_digest, "Today's autonomy digest"),
    "prune": (_cmd_prune, "Prune dangling Docker images on Unraid"),
    "mute": (_cmd_mute, "Silence a notification kind"),
    "unmute": (_cmd_unmute, "Un-silence a notification kind"),
    "muted": (_cmd_muted, "List muted notification kinds"),
    "image": (_cmd_image, "Generate an image from a prompt"),
    "flags": (_cmd_flags, "List open outcome flags"),
    "resolve": (_cmd_resolve, "Resolve an outcome flag by id"),
    "defer": (_cmd_defer, "Defer an outcome flag for N days"),
    "flag": (_cmd_flag, "Log your own item into the outcome tracker"),
    "calibration": (_cmd_calibration, "Flag calibration status / suppress-unsuppress a rule"),
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
