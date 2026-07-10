import asyncio
import json
import logging
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

_BRIEFING_FOLLOWUP_KEYWORDS = frozenset([
    "briefing", "this morning", "you said", "you mentioned", "earlier you",
    "the briefing", "in the briefing", "from the briefing",
])


def _db_latest_briefing(max_age_hours: int = 12) -> dict | None:
    """Return the most recent Briefing row if it's within max_age_hours. Sync — call via to_thread."""
    try:
        from sqlmodel import Session, select
        from backend.database import Briefing, engine

        cutoff = datetime.utcnow() - timedelta(hours=max_age_hours)
        with Session(engine) as session:
            stmt = (
                select(Briefing)
                .where(Briefing.created_at >= cutoff)
                .order_by(Briefing.created_at.desc())
                .limit(1)
            )
            row = session.exec(stmt).first()
            if row is None:
                return None
            return {"content": row.content, "context_json": row.context_json, "created_at": row.created_at.isoformat()}
    except Exception as e:
        logger.debug(f"_db_latest_briefing: error (ignored): {e}")
        return None


_CONTROLLABLE_DOMAINS = {"light", "switch", "fan", "input_boolean", "climate", "input_number", "input_select", "automation", "lock", "cover"}

# Static instructions block — cache_control candidate. Currently ~300 tokens, below the
# 4096-token Sonnet/Opus cache minimum, so cache_read_input_tokens will be 0 for now.
# The split is structurally correct; caching activates automatically if the prefix grows.
CHAT_SYSTEM_STATIC = """You are Carl, a direct, high-conviction personal AI assistant for a homelab power user — founder-to-founder, not a support rep. You have live access to homelab data shown in the snapshot below. Use it to answer questions about
home systems, storage, recordings, network/DNS, and weather.

You also have a web_search tool — use it whenever a question needs current, real-world, or factual
information you are not certain of (news, prices, versions, sports scores, documentation, anything
recent). Search proactively rather than guessing, and cite what you find. For settled general
knowledge or code questions, answer directly without searching.

No hedging language — cut "try," "hope," "maybe," "should probably," "I think." State what is true
and what you're doing about it. Challenge him with love: if a request has a flaw or a cheaper path,
say so plainly once, then solve it — don't debate it. For anything with a clearly-best answer,
decide and execute rather than listing options; reserve real choices for genuinely open decisions
or anything destructive/hard-to-reverse.

If you genuinely cannot see homelab data that was asked for, say so in one short sentence — do not
hedge or apologise. Never say "as of my last update" or similar. Be concise; the user is technical
and time-constrained."""

_CHAT_SYSTEM_DYNAMIC = "{memory}LIVE HOMELAB SNAPSHOT:\n{snapshot}"


def extract_home_state(ha) -> dict:
    """Pull notable locks/doors + alert count out of an HA fetch() result.

    Shared by _build_snapshot (chat's live-state block) and the Today page's
    passive home-state card (backend/api/today.py) so both read the exact same
    entities the same way. Returns {"available", "locks", "doors", "alert_count"} --
    locks/doors are capped strings like "Front Door=locked", newest-first from
    the HA entity list, same 12-item cap _build_snapshot always used.
    """
    if isinstance(ha, Exception):
        return {"available": False, "locks": [], "doors": [], "alert_count": 0, "truncated": False}

    alerts = getattr(ha, "alerts", []) or []
    locks, doors = [], []
    for e in getattr(ha, "entities", []) or []:
        if not isinstance(e, dict):
            continue
        eid = e.get("entity_id") or ""
        raw_attrs = e.get("attributes")
        attrs = raw_attrs if isinstance(raw_attrs, dict) else {}
        label = (attrs.get("friendly_name") or eid or "").strip() or eid
        state = (e.get("state") or "unknown").strip() or "unknown"
        if eid.startswith("lock."):
            locks.append(f"{label}={state}")
        elif eid.startswith("cover.") or (
            (eid.startswith("binary_sensor.") or eid.startswith("sensor."))
            and any(k in label.lower() for k in ("door", "window"))
        ):
            doors.append(f"{label}={state}")

    cap = 12
    n_locks = min(len(locks), cap)
    n_doors = min(len(doors), cap - n_locks)
    truncated = len(locks) > n_locks or len(doors) > n_doors
    return {
        "available": True,
        "locks": locks[:n_locks],
        "doors": doors[:n_doors],
        "alert_count": len(alerts),
        "truncated": truncated,
    }


def extract_temperature_sensors(ha) -> list[dict]:
    """Every sensor.*temperature* entity currently reporting a numeric value, as
    [{"label", "entity_id", "value_f"}]. Shared by the goal proposer's dynamic
    room-temperature discovery (proposer.py::_ha_entity_summary) and the
    homeassistant_temperatures tool (tools.py) so both read HA the same way --
    e.g. a newly-added sensor like "First Air Quality Monitor" shows up in
    both without any per-sensor wiring."""
    if isinstance(ha, Exception):
        return []
    result = []
    for e in getattr(ha, "entities", []) or []:
        if not isinstance(e, dict):
            continue
        eid = e.get("entity_id") or ""
        if not eid.startswith("sensor.") or "temperature" not in eid:
            continue
        if e.get("state") in ("unavailable", "unknown", None):
            continue
        try:
            value_f = float(e["state"])
        except (ValueError, TypeError):
            continue
        label = (
            eid.replace("sensor.", "")
            .replace("_current_temperature", "")
            .replace("_temperature", "")
            .replace("_", " ")
        )
        result.append({"label": label, "entity_id": eid, "value_f": value_f})
    return result


def _build_snapshot(ha, unraid_d, channels, ag, wx) -> str:
    def safe(obj, attr, default="N/A"):
        if isinstance(obj, Exception):
            return default
        return getattr(obj, attr, default)

    lines = []

    # Home Assistant
    entity_count = len(safe(ha, "entities", []))
    alerts = safe(ha, "alerts", [])
    lines.append(f"Home Assistant: {entity_count} entities, {len(alerts)} alert(s)" +
                 (f" [{', '.join(alerts[:3])}]" if alerts else ""))
    home_state = extract_home_state(ha)
    if home_state["available"]:
        _locks, _doors, _truncated = home_state["locks"], home_state["doors"], home_state["truncated"]
        _parts = []
        if _locks:
            _parts.append("Locks: " + ", ".join(_locks))
        if _doors:
            _parts.append("Doors/covers: " + ", ".join(_doors))
        if _parts:
            lines.append("Notable HA: " + " | ".join(_parts) + ("…" if _truncated else ""))

    # Unraid
    if not isinstance(unraid_d, Exception):
        docker_count = len(safe(unraid_d, "docker_containers", []))
        lines.append(
            f"Unraid: array={safe(unraid_d, 'array_status', 'unknown')}, "
            f"storage={safe(unraid_d, 'storage_used_gb', 0)}/{safe(unraid_d, 'storage_total_gb', 0)} GB, "
            f"docker={docker_count} containers"
        )
    else:
        lines.append("Unraid: unavailable")

    # Channels DVR
    if not isinstance(channels, Exception):
        rec_now = safe(channels, "recording_now", [])
        rec_str = ", ".join(r.get("title", "") for r in rec_now) if rec_now else "nothing"
        lines.append(
            f"Channels DVR: recording={rec_str}, "
            f"storage={safe(channels, 'storage_used_gb', 0)}/{safe(channels, 'storage_total_gb', 0)} GB"
        )
    else:
        lines.append("Channels DVR: unavailable")

    # AdGuard
    if not isinstance(ag, Exception):
        lines.append(
            f"AdGuard: {safe(ag, 'blocked_today', 0)} blocked today "
            f"({safe(ag, 'blocked_pct', 0)}%), filtering={safe(ag, 'filtering_enabled', True)}"
        )
    else:
        lines.append("AdGuard: unavailable")

    # Weather
    if not isinstance(wx, Exception):
        lines.append(f"Weather: {safe(wx, 'summary', 'unavailable')}")
    else:
        lines.append("Weather: unavailable")

    return "\n".join(lines)


def _db_create_conversation(title: str) -> int:
    from sqlmodel import Session
    from backend.database import Conversation, engine
    with Session(engine) as session:
        conv = Conversation(title=title[:40])
        session.add(conv)
        session.commit()
        session.refresh(conv)
        return conv.id


def _db_add_message(conversation_id: int, role: str, content: str) -> None:
    from sqlmodel import Session
    from backend.database import ChatMessage, engine
    with Session(engine) as session:
        msg = ChatMessage(conversation_id=conversation_id, role=role, content=content)
        session.add(msg)
        session.commit()


def _db_load_history(conversation_id: int, limit: int = 20) -> list[dict]:
    from sqlmodel import Session, select
    from backend.database import ChatMessage, engine
    with Session(engine) as session:
        # Load the MOST RECENT `limit` messages (DESC), then restore chronological
        # order. Ordering ASC + limit would pin the model to the oldest messages and
        # drop all recent context as a conversation grows.
        msgs = session.exec(
            select(ChatMessage)
            .where(ChatMessage.conversation_id == conversation_id)
            .order_by(ChatMessage.created_at.desc())
            .limit(limit)
        ).all()
        msgs = list(reversed(msgs))
        return [{"role": m.role, "content": m.content} for m in msgs]


def _db_touch_conversation(conversation_id: int) -> None:
    from sqlmodel import Session
    from backend.database import Conversation, engine
    with Session(engine) as session:
        conv = session.get(Conversation, conversation_id)
        if conv:
            conv.updated_at = datetime.utcnow()
            session.commit()


def _db_get_summary(conversation_id: int) -> str:
    from sqlmodel import Session
    from backend.database import Conversation, engine
    with Session(engine) as session:
        conv = session.get(Conversation, conversation_id)
        if conv is None:
            return ""
        return conv.summary or ""


def _db_get_summary_meta(conversation_id: int) -> dict:
    from sqlmodel import Session
    from backend.database import Conversation, engine
    with Session(engine) as session:
        conv = session.get(Conversation, conversation_id)
        if conv is None:
            return {"summary": "", "through_id": None}
        return {"summary": conv.summary or "", "through_id": conv.summarized_through_id}


def _db_messages_after(conversation_id: int, after_id: int | None) -> list[dict]:
    from sqlmodel import Session, select
    from backend.database import ChatMessage, engine
    with Session(engine) as session:
        q = select(ChatMessage).where(ChatMessage.conversation_id == conversation_id)
        if after_id is not None:
            q = q.where(ChatMessage.id > after_id)
        q = q.order_by(ChatMessage.id.asc())
        msgs = session.exec(q).all()
        return [{"id": m.id, "role": m.role, "content": m.content} for m in msgs]


def _db_set_summary(conversation_id: int, summary: str, through_id: int) -> None:
    from sqlmodel import Session
    from backend.database import Conversation, engine
    with Session(engine) as session:
        conv = session.get(Conversation, conversation_id)
        if conv is None:
            return
        conv.summary = summary
        conv.summarized_through_id = through_id
        session.commit()


async def _maybe_summarize(conversation_id: int, history_limit: int) -> None:
    """Best-effort rolling summarizer. Folds oldest out-of-window messages into a
    Haiku summary stored on the Conversation row. NEVER raises — all errors are
    swallowed so summarization never breaks a chat request.
    """
    try:
        from backend.agents.router import haiku

        meta = await asyncio.to_thread(_db_get_summary_meta, conversation_id)
        pending = await asyncio.to_thread(
            _db_messages_after, conversation_id, meta["through_id"]
        )

        # If there are still <= history_limit un-summarized messages, nothing to fold.
        if len(pending) <= history_limit:
            return

        # Fold the oldest messages that fall outside the current window.
        fold = pending[: len(pending) - history_limit]
        if not fold:
            return

        existing = meta["summary"]
        fold_transcript = "\n".join(
            f"{'User' if m['role'] == 'user' else 'Assistant'}: {m['content']}"
            for m in fold
        )

        prompt = (
            "You maintain a running summary of a conversation so older messages can be dropped without losing context.\n\n"
            f"EXISTING SUMMARY:\n{existing or '(none yet)'}\n\n"
            f"NEW MESSAGES TO FOLD IN:\n{fold_transcript}\n\n"
            "Return an UPDATED running summary (<=180 words) capturing durable facts, decisions, preferences, and open threads. Summary text only — no preamble."
        )

        new_summary = (await haiku(prompt, label="chat_summary")).strip()
        if not new_summary:
            return

        await asyncio.to_thread(
            _db_set_summary, conversation_id, new_summary, fold[-1]["id"]
        )
    except Exception as e:
        logger.warning(f"_maybe_summarize failed (best-effort, ignoring): {e}")


_BUDGET_REACHED_REPLY = (
    "I've hit the configured spending limit for now, so I can't run that. "
    "You can raise or reset the budget in Settings (Safety)."
)


async def chat(conversation_id: int | None, user_message: str, *, token_queue=None) -> dict:
    """token_queue: if set (asyncio.Queue), CHAT replies stream tokens into it; None sentinel marks end."""
    from backend.agents.router import haiku, sonnet, stream_sonnet
    from backend.safety.governor import BudgetExceeded

    # 1. Conversation handling — all DB ops off the event loop
    if conversation_id is None:
        conversation_id = await asyncio.to_thread(_db_create_conversation, user_message)

    await asyncio.to_thread(_db_add_message, conversation_id, "user", user_message)
    from backend.config import get_settings
    history = await asyncio.to_thread(
        _db_load_history, conversation_id, get_settings().chat_history_limit
    )
    convo_summary = await asyncio.to_thread(_db_get_summary, conversation_id)

    # 2a. Check for briefing follow-up — load recent briefing context if relevant
    _msg_lower = user_message.lower()
    _is_briefing_followup = any(kw in _msg_lower for kw in _BRIEFING_FOLLOWUP_KEYWORDS)
    _recent_briefing: dict | None = None
    if _is_briefing_followup:
        _recent_briefing = await asyncio.to_thread(_db_latest_briefing, 12)

    # 2. Classify intent with haiku (fast)
    classify_prompt = f"""Classify this user message and return JSON only.

User message: "{user_message}"

Return exactly:
{{"intent": "HOME_CONTROL|TASK|CHAT|HERMES|NOTE|STATUS", "reason": "brief reason"}}

HOME_CONTROL = user is issuing a COMMAND that changes a Home Assistant device — turn on/off/toggle a light/switch/fan/automation, open/close/stop a garage door or cover, lock or unlock a physical door lock, set a thermostat temperature, set a number helper value, or change a select/mode helper. Only use HOME_CONTROL for imperative commands, NOT for asking about device state.
TASK = a multi-step OPERATION that requires DOING several things in sequence (e.g. "research X then save a note", "summarise my PRs and email me"). Not for a plain question.
CHAT = any question or request for information — including current events, prices, news, versions, weather, homelab status, follow-ups, and general/coding questions. IMPORTANT: (1) asking about the STATE of a device (e.g. "is the back door locked?", "is the garage open?", "are any lights on?") is CHAT — the live snapshot answers these; (2) searching or reading your notes/vault ("search my vault for X", "what do my notes say about X", "find X in my brain") is CHAT — vault is searched automatically. The chat can also search the web.
HERMES = a request that targets the Hermes homelab bot specifically — controlling Proxmox VMs/LXCs, Jellyfin, restarting a service, sending a Telegram message, or changing/extending Hermes itself; or anything the user explicitly addresses to "Hermes".
NOTE = user wants to SAVE new content to their Obsidian notes/vault — "save this to my vault", "make a note: ...", "remember that ...", "save that to my notes". NOTE is only for WRITING, not for reading or searching existing notes.
STATUS = user wants a quick homelab status summary — "/status" command or "what's running", "system status", "homelab status"."""

    # Fast-path: /status command bypasses haiku classify
    _msg_stripped = user_message.strip()
    _is_status_cmd = (
        _msg_stripped.lower() == "/status"
        or _msg_stripped.lower().startswith("/status ")
    )

    # Budget-reached degrades gracefully at any point below: the haiku classify
    # and every routing branch can raise BudgetExceeded (router's daily brake).
    # We catch it, reply with a friendly message, and persist normally — no
    # exception escapes to FastAPI.
    intent = "CHAT"  # default; reassigned after Haiku classify (guards BudgetExceeded before classify)
    try:
        if _is_status_cmd:
            raw_intent = '{"intent": "STATUS", "reason": "slash command"}'
        else:
            raw_intent = await haiku(classify_prompt, label="chat_classify")
        intent = "CHAT"
        try:
            start = raw_intent.find("{")
            end = raw_intent.rfind("}") + 1
            if start >= 0 and end > start:
                parsed = json.loads(raw_intent[start:end])
                intent = parsed.get("intent", "CHAT")
                if intent not in ("HOME_CONTROL", "TASK", "CHAT", "HERMES", "NOTE", "STATUS"):
                    intent = "CHAT"
        except Exception:
            intent = "CHAT"

        logger.info(f"Chat intent={intent} conversation_id={conversation_id}")

        # Build history transcript (exclude the last user message — sent separately)
        prior = history[:-1]  # last item is the user message we just persisted
        transcript = "\n".join(
            f"{'User' if m['role'] == 'user' else 'Assistant'}: {m['content']}"
            for m in prior
        )
        if convo_summary:
            transcript = (
                f"[Earlier conversation summary]\n{convo_summary}\n\n[Recent messages]\n{transcript}"
                if transcript
                else f"[Earlier conversation summary]\n{convo_summary}"
            )

        reply = ""

        # 3. Route by intent
        if intent == "CHAT":
            from backend.integrations import adguard, channels_dvr, homeassistant, unraid, weather
            from backend.agents import facts, memory

            results = await asyncio.gather(
                homeassistant.fetch(),
                unraid.fetch(),
                channels_dvr.fetch(),
                adguard.fetch(),
                weather.fetch(),
                memory.vault_recall(user_message),
                memory.latest_briefing_seed(),
                facts.facts_recall(user_message),
                return_exceptions=True,
            )
            ha, unraid_d, channels, ag, wx, vault_str, briefing_str, facts_str = results
            # Coerce any exception results from memory/facts fns to empty string
            if isinstance(vault_str, Exception):
                vault_str = ""
            if isinstance(briefing_str, Exception):
                briefing_str = ""
            if isinstance(facts_str, Exception):
                facts_str = ""
            snapshot = _build_snapshot(ha, unraid_d, channels, ag, wx)

            memory_block = memory.assemble(vault_str, briefing_str, facts_str)

            # Feature 3: inject full briefing context for follow-up questions
            if _recent_briefing and _is_briefing_followup:
                _b_content = _recent_briefing.get("content", "")
                _b_ctx = _recent_briefing.get("context_json")
                _b_ts = _recent_briefing.get("created_at", "")
                _briefing_inject = f"\n\n[TODAY'S BRIEFING ({_b_ts[:16] if _b_ts else 'recent'})]:\n{_b_content}"
                if _b_ctx:
                    try:
                        _ctx_parsed = json.loads(_b_ctx)
                        _briefing_inject += f"\n\n[BRIEFING RAW CONTEXT]:\n{json.dumps(_ctx_parsed, indent=2)}"
                    except Exception:
                        pass
                memory_block = (memory_block + _briefing_inject) if memory_block else _briefing_inject.lstrip("\n\n")

            memory_inject = (memory_block + "\n\n") if memory_block else ""
            system = [
                {"type": "text", "text": CHAT_SYSTEM_STATIC, "cache_control": {"type": "ephemeral"}},
                {"type": "text", "text": _CHAT_SYSTEM_DYNAMIC.format(memory=memory_inject, snapshot=snapshot)},
            ]
            user_prompt = (f"Conversation so far:\n{transcript}\n\nUser: {user_message}" if transcript
                           else f"User: {user_message}")
            try:
                if token_queue is not None:
                    reply = ""
                    async for token in stream_sonnet(user_prompt, system=system, web_search=True):
                        reply += token
                        await token_queue.put(token)
                else:
                    reply = await sonnet(user_prompt, system=system, web_search=True, label="chat_reply_websearch")
            except BudgetExceeded:
                raise
            except Exception as e:
                logger.warning(f"Chat web search unavailable, answering without it: {e}")
                if token_queue is not None:
                    reply = ""
                    async for token in stream_sonnet(user_prompt, system=system):
                        reply += token
                        await token_queue.put(token)
                else:
                    reply = await sonnet(user_prompt, system=system, label="chat_reply")

        elif intent == "HOME_CONTROL":
            from backend.integrations import homeassistant

            try:
                ha_data = await homeassistant.fetch()
                _all_controllable = [
                    {
                        "entity_id": e["entity_id"],
                        "friendly_name": (e.get("attributes") or {}).get("friendly_name", e["entity_id"]),
                        "state": e.get("state", "unknown"),
                    }
                    for e in ha_data.entities
                    if e.get("entity_id", "").split(".")[0] in _CONTROLLABLE_DOMAINS
                ]
                # Rank by token overlap with user message so the intended entity
                # is always in the top-60 even across 1,000+ controllable entities.
                _msg_tokens = {t.lower() for t in user_message.split() if len(t) > 2}
                _all_controllable.sort(
                    key=lambda ent: sum(
                        1 for t in _msg_tokens
                        if t in f"{ent['entity_id']} {ent['friendly_name']}".lower()
                    ),
                    reverse=True,
                )
                controllable = _all_controllable[:12]

                if not controllable:
                    reply = "No controllable devices found in Home Assistant."
                else:
                    entity_list = json.dumps(controllable)
                    pick_prompt = f"""The user wants to control or configure a Home Assistant entity.

User request: "{user_message}"

Available entities (entity_id, friendly_name, current state):
{entity_list}

Return JSON only. Pick the best matching entity and service.
Include "value" ONLY for input_number or climate, "option" ONLY for input_select:
{{"entity_id": "domain.entity_name", "service": "...", "value": null, "option": null}}

Services by domain:
- light / switch / fan / input_boolean / automation: turn_on | turn_off | toggle
- lock: lock (to lock) | unlock (to unlock)
- cover: open_cover (to open) | close_cover (to close) | stop_cover (to stop)

IMPORTANT: `lock.*` entities are PHYSICAL door locks (deadbolts, August locks, etc.). `input_boolean.*` entities are virtual helper toggles — NOT physical locks. When the user's request involves locking, unlocking, or a door/lock/deadbolt, ALWAYS prefer a `lock.*` entity over any `input_boolean.*` entity, even if the input_boolean has a similar-sounding friendly name. Only pick `input_boolean` if the user is explicitly toggling a virtual helper (not a physical lock).
- input_number: set_value  (set "value" to the number the user specified)
- input_select: select_option  (set "option" to the exact option string)
- climate: set_temperature  (set "value" to the temperature the user specified)

If no entity matches, return:
{{"entity_id": null, "service": null, "value": null, "option": null}}"""

                    raw_pick = await haiku(pick_prompt, label="chat_lane_pick")
                    entity_id = None
                    service = None
                    value = None
                    option = None
                    try:
                        ps = raw_pick.find("{")
                        pe = raw_pick.rfind("}") + 1
                        if ps >= 0 and pe > ps:
                            pick = json.loads(raw_pick[ps:pe])
                            entity_id = pick.get("entity_id")
                            service = pick.get("service")
                            value = pick.get("value")
                            option = pick.get("option")
                    except Exception:
                        pass

                    if not entity_id or not service:
                        reply = "I couldn't identify which device you want to control. Could you be more specific? For example: \"turn off the office light\", \"set the thermostat to 72\", or \"disable the away automation\"."
                    else:
                        domain = entity_id.split(".")[0]
                        friendly = next(
                            (e["friendly_name"] for e in controllable if e["entity_id"] == entity_id),
                            entity_id,
                        )

                        # Build service_data for parameterised services.
                        if service == "set_temperature" and value is not None:
                            service_data = {"entity_id": entity_id, "temperature": value}
                        elif service == "set_value" and value is not None:
                            service_data = {"entity_id": entity_id, "value": value}
                        elif service == "select_option" and option:
                            service_data = {"entity_id": entity_id, "option": option}
                        else:
                            service_data = {}

                        # Guard: parameterised services with missing params.
                        if service == "set_temperature" and value is None:
                            reply = f"What temperature would you like to set {friendly} to?"
                        elif service == "set_value" and value is None:
                            reply = f"What value would you like to set {friendly} to?"
                        elif service == "select_option" and not option:
                            reply = f"Which option would you like to select for {friendly}?"
                        else:
                            from backend.safety.broker import Decision, execute_action
                            res = await execute_action(
                                actor="user",
                                kind="ha_service",
                                target=entity_id,
                                payload={"domain": domain, "service": service, "service_data": service_data},
                            )
                            if res.decision == Decision.EXECUTED:
                                reply = {
                                    "turn_on": f"Turned on {friendly}.",
                                    "turn_off": f"Turned off {friendly}.",
                                    "toggle": f"Toggled {friendly}.",
                                    "lock": f"Locked {friendly}.",
                                    "unlock": f"Unlocked {friendly}.",
                                    "open_cover": f"Opened {friendly}.",
                                    "close_cover": f"Closed {friendly}.",
                                    "stop_cover": f"Stopped {friendly}.",
                                    "set_temperature": f"Set {friendly} to {value}°.",
                                    "set_value": f"Set {friendly} to {value}.",
                                    "select_option": f"Set {friendly} to {option}.",
                                }.get(service, f"{service.replace('_', ' ').capitalize()} {friendly}.")
                            elif res.decision == Decision.FAILED:
                                reply = f"Failed to {service.replace('_', ' ')} {friendly}: {res.error}"
                            else:
                                reply = "That action needs confirmation."

            except BudgetExceeded:
                raise  # budget brake reaches the outer handler
            except Exception as e:
                reply = f"Home Assistant is not reachable right now: {e}"

        elif intent == "TASK":
            from backend.agents.orchestrator import run_task
            result = await run_task(user_message)
            if result.success:
                reply = result.output[-1] if result.output else "Task completed."
            else:
                reply = f"I wasn't able to complete that task: {result.reason}"

        elif intent == "HERMES":
            from backend.safety import hermes_actions
            from backend.safety.broker import Decision, execute_action

            # Haiku verb-pick: map the request onto the structured allowlist. A
            # known verb with valid args goes through the structured `hermes_action`
            # path; anything else falls back to free-text relay (kind="hermes_relay"),
            # which is allowed only because this is a USER action.
            menu = json.dumps(hermes_actions.allowed_verbs())
            verb_prompt = f"""The user wants the Hermes homelab bot to do something.

User request: "{user_message}"

Pick the single best matching verb from this allowlist (verb, risk, reversibility,
required_args, enum_args):
{menu}

Return JSON only:
{{"verb": "<one of the allowlist verbs above, or 'unknown'>", "args": {{...}}}}

Fill `args` with every required_arg and enum_arg the chosen verb needs (enum_args
must use one of the listed values). If nothing matches, return:
{{"verb": "unknown", "args": {{}}}}"""

            verb = "unknown"
            args: dict = {}
            try:
                raw_verb = await haiku(verb_prompt, label="chat_hermes_verb")
                vs = raw_verb.find("{")
                ve = raw_verb.rfind("}") + 1
                if vs >= 0 and ve > vs:
                    vd = json.loads(raw_verb[vs:ve])
                    verb = vd.get("verb", "unknown")
                    args = vd.get("args") or {}
                    if not isinstance(args, dict):
                        args = {}
            except BudgetExceeded:
                raise  # budget brake reaches the outer handler
            except Exception:
                verb, args = "unknown", {}

            if hermes_actions.is_allowed(verb) and hermes_actions.validate_args(verb, args) is None:
                res = await execute_action(
                    actor="user",
                    kind="hermes_action",
                    target="hermes",
                    payload={"verb": verb, "args": args},
                )
            else:
                # Fallback: free-text relay (user-only path, behaviour unchanged).
                res = await execute_action(
                    actor="user",
                    kind="hermes_relay",
                    target="hermes",
                    payload={"message": user_message},
                )

            # actor=user always allows, so relay still runs; its return string flows
            # back via res.result["response"] — user-visible reply is unchanged.
            reply = (
                (res.result or {}).get("response")
                if res.decision == Decision.EXECUTED
                else (res.error or "Hermes action could not be completed.")
            )

        elif intent == "NOTE":
            import httpx as _httpx
            from backend.config import get_settings as _get_settings

            extract_prompt = f"""The user wants to save a note to their Obsidian vault.

User request: "{user_message}"

Recent conversation (use this if the user says "save this"/"save that" to refer to a prior message):
{transcript or "(none)"}

Return JSON only:
{{"title": "short descriptive title, max 8 words", "content": "the full note body in markdown"}}

If they're saving something from the conversation, use the relevant prior assistant message as the content. Otherwise use what they dictated."""

            title, content = "Chat Note", user_message
            try:
                raw_note = await haiku(extract_prompt, label="chat_note_extract")
                ns = raw_note.find("{")
                ne = raw_note.rfind("}") + 1
                if ns >= 0 and ne > ns:
                    nd = json.loads(raw_note[ns:ne])
                    title = nd.get("title") or "Chat Note"
                    content = nd.get("content") or user_message
            except BudgetExceeded:
                raise  # budget brake reaches the outer handler
            except Exception:
                pass

            ts = datetime.now().strftime("%Y-%m-%d %H:%M")
            body = f"# {title}\n\n*Saved from NEXUS chat — {ts}*\n\n{content}\n"
            safe_title = title.replace("/", "-").replace("\\", "-")
            _file_ts = datetime.now().strftime("%Y%m%d-%H%M")
            filename = f"{safe_title}-{_file_ts}.md"
            try:
                _settings = _get_settings()
                _mcp_url = _settings.brain_mcp_url.rstrip("/")
                _token = getattr(_settings, "brain_mcp_token", "")
                _headers = {"Authorization": f"Bearer {_token}"} if _token else {}
                async with _httpx.AsyncClient(timeout=10) as _client:
                    _resp = await _client.post(
                        f"{_mcp_url}/raw",
                        json={"content": body, "filename": filename},
                        headers=_headers,
                    )
                    _resp.raise_for_status()
                reply = f'Saved "{title}" to your vault (Brain/raw/{filename}).'
            except Exception as e:
                reply = f"Couldn't save the note: {e}"

        elif intent == "STATUS":
            from backend.integrations import unifi, unraid, channels_dvr, hermes

            status_results = await asyncio.gather(
                unifi.fetch(),
                unraid.fetch(),
                channels_dvr.fetch(),
                hermes.get_status(),
                return_exceptions=True,
            )
            unifi_d, unraid_d, channels_d, hermes_d = status_results

            lines = []

            # UniFi
            if isinstance(unifi_d, Exception):
                lines.append("UniFi: unavailable")
            else:
                lines.append(f"UniFi: {unifi_d.client_count} clients online")

            # Unraid
            if isinstance(unraid_d, Exception):
                lines.append("Unraid: unavailable")
            else:
                free_gb = unraid_d.storage_total_gb - unraid_d.storage_used_gb
                lines.append(
                    f"Unraid: array {unraid_d.array_status}, "
                    f"{free_gb:.1f} GB free, "
                    f"{len(unraid_d.docker_containers)} containers"
                )

            # Channels DVR
            if isinstance(channels_d, Exception):
                lines.append("Channels: unavailable")
            else:
                rec_now = channels_d.recording_now
                rec_str = ", ".join(r.get("title", "?") for r in rec_now) if rec_now else "idle"
                lines.append(f"Channels: recording {rec_str}")

            # Hermes
            if isinstance(hermes_d, Exception):
                lines.append("Hermes: unavailable")
            else:
                lines.append("Hermes: online" if hermes_d.alive else "Hermes: offline")

            reply = "\n".join(lines)

        budget_exceeded = False
    except BudgetExceeded:
        # Spending cap reached anywhere above — degrade gracefully. The reply is
        # persisted below like any other; no exception escapes to FastAPI.
        logger.info("Chat hit budget cap; returning friendly budget-reached reply")
        reply = _BUDGET_REACHED_REPLY
        budget_exceeded = True

    # 4. Persist reply and update conversation timestamp
    await asyncio.to_thread(_db_add_message, conversation_id, "assistant", reply)
    await asyncio.to_thread(_db_touch_conversation, conversation_id)

    # 5. Rolling summarization (best-effort; swallows its own errors). Skipped
    # once the daily cap is already hit this turn — router._run's universal
    # budget brake would just reject this call too, so it's a guaranteed-wasted
    # DB round-trip + LLM attempt on every message for the rest of the day.
    if not budget_exceeded:
        await _maybe_summarize(conversation_id, get_settings().chat_history_limit)

    # 6. Fact extraction — only for conversational intents (not imperatives/status)
    if intent in ("CHAT", "TASK", "NOTE"):
        from backend.agents import facts
        await facts.extract_and_store(user_message, conversation_id)

    if token_queue is not None:
        await token_queue.put(None)  # sentinel: stream done

    return {"conversation_id": conversation_id, "reply": reply}
