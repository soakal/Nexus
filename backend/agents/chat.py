import asyncio
import json
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

_CONTROLLABLE_DOMAINS = {"light", "switch", "fan", "input_boolean"}

CHAT_SYSTEM = """You are NEXUS, a direct, technical personal-AI assistant for a homelab power user.
You have live access to homelab data shown in the snapshot below. Use it to answer questions about
home systems, storage, recordings, network/DNS, and weather.

You also have a web_search tool — use it whenever a question needs current, real-world, or factual
information you are not certain of (news, prices, versions, sports scores, documentation, anything
recent). Search proactively rather than guessing, and cite what you find. For settled general
knowledge or code questions, answer directly without searching.

If you genuinely cannot see homelab data that was asked for, say so in one short sentence — do not
hedge or apologise. Never say "as of my last update" or similar. Be concise; the user is technical
and time-constrained.

{memory}LIVE HOMELAB SNAPSHOT:
{snapshot}"""


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

        new_summary = (await haiku(prompt)).strip()
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

    # 2. Classify intent with haiku (fast)
    classify_prompt = f"""Classify this user message and return JSON only.

User message: "{user_message}"

Return exactly:
{{"intent": "HOME_CONTROL|TASK|CHAT|HERMES|NOTE", "reason": "brief reason"}}

HOME_CONTROL = user wants to change a Home Assistant device state (turn on/off/toggle a light/switch/fan).
TASK = a multi-step OPERATION that requires DOING several things in sequence (e.g. "research X then save a note", "summarise my PRs and email me"). Not for a plain question.
CHAT = any question or request for information — including current events, prices, news, versions, weather, homelab status, follow-ups, and general/coding questions. The chat can search the web itself, so questions needing live info still go here.
HERMES = a request that targets the Hermes homelab bot specifically — controlling Proxmox VMs/LXCs, Jellyfin, the garage door, restarting a service, sending a Telegram message, or changing/extending Hermes itself; or anything the user explicitly addresses to "Hermes".
NOTE = user wants to save something to their Obsidian notes/vault — "save this to my vault", "make a note: ...", "remember that ...", "save that to my notes"."""

    # Budget-reached degrades gracefully at any point below: the haiku classify
    # and every routing branch can raise BudgetExceeded (router's daily brake).
    # We catch it, reply with a friendly message, and persist normally — no
    # exception escapes to FastAPI.
    try:
        raw_intent = await haiku(classify_prompt)
        intent = "CHAT"
        try:
            start = raw_intent.find("{")
            end = raw_intent.rfind("}") + 1
            if start >= 0 and end > start:
                parsed = json.loads(raw_intent[start:end])
                intent = parsed.get("intent", "CHAT")
                if intent not in ("HOME_CONTROL", "TASK", "CHAT", "HERMES", "NOTE"):
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
            memory_inject = (memory_block + "\n\n") if memory_block else ""
            system = CHAT_SYSTEM.format(memory=memory_inject, snapshot=snapshot)
            user_prompt = (f"Conversation so far:\n{transcript}\n\nUser: {user_message}" if transcript
                           else f"User: {user_message}")
            try:
                if token_queue is not None:
                    reply = ""
                    async for token in stream_sonnet(user_prompt, system=system, web_search=True):
                        reply += token
                        await token_queue.put(token)
                else:
                    reply = await sonnet(user_prompt, system=system, web_search=True)
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
                    reply = await sonnet(user_prompt, system=system)

        elif intent == "HOME_CONTROL":
            from backend.integrations import homeassistant

            try:
                ha_data = await homeassistant.fetch()
                controllable = [
                    {
                        "entity_id": e["entity_id"],
                        "friendly_name": (e.get("attributes") or {}).get("friendly_name", e["entity_id"]),
                        "state": e.get("state", "unknown"),
                    }
                    for e in ha_data.entities
                    if e.get("entity_id", "").split(".")[0] in _CONTROLLABLE_DOMAINS
                ][:60]

                if not controllable:
                    reply = "No controllable devices found in Home Assistant."
                else:
                    entity_list = json.dumps(controllable, indent=2)
                    pick_prompt = f"""The user wants to control a Home Assistant device.

User request: "{user_message}"

Available entities (entity_id, friendly_name, current state):
{entity_list}

Return JSON only — pick the best matching entity and service:
{{"entity_id": "domain.entity_name", "service": "turn_on|turn_off|toggle"}}

If no entity matches, return:
{{"entity_id": null, "service": null}}"""

                    raw_pick = await haiku(pick_prompt)
                    entity_id = None
                    service = None
                    try:
                        ps = raw_pick.find("{")
                        pe = raw_pick.rfind("}") + 1
                        if ps >= 0 and pe > ps:
                            pick = json.loads(raw_pick[ps:pe])
                            entity_id = pick.get("entity_id")
                            service = pick.get("service")
                    except Exception:
                        pass

                    if not entity_id or not service:
                        reply = "I couldn't identify which device you want to control. Could you be more specific? For example: \"turn off the office light\" or \"toggle the living room fan\"."
                    else:
                        domain = entity_id.split(".")[0]
                        friendly = next(
                            (e["friendly_name"] for e in controllable if e["entity_id"] == entity_id),
                            entity_id,
                        )
                        from backend.safety.broker import Decision, execute_action
                        res = await execute_action(
                            actor="user",
                            kind="ha_service",
                            target=entity_id,
                            payload={"domain": domain, "service": service},
                        )
                        if res.decision == Decision.EXECUTED:
                            action_word = {"turn_on": "Turned on", "turn_off": "Turned off", "toggle": "Toggled"}.get(service, service)
                            reply = f"{action_word} {friendly}."
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
            menu = json.dumps(hermes_actions.allowed_verbs(), indent=2)
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
                raw_verb = await haiku(verb_prompt)
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
            from backend.integrations.obsidian import create_note

            extract_prompt = f"""The user wants to save a note to their Obsidian vault.

User request: "{user_message}"

Recent conversation (use this if the user says "save this"/"save that" to refer to a prior message):
{transcript or "(none)"}

Return JSON only:
{{"title": "short descriptive title, max 8 words", "content": "the full note body in markdown"}}

If they're saving something from the conversation, use the relevant prior assistant message as the content. Otherwise use what they dictated."""

            title, content = "Chat Note", user_message
            try:
                raw_note = await haiku(extract_prompt)
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
            try:
                path = await create_note(title=title, content=body, folder="NEXUS/Chat Notes")
                reply = f'Saved "{title}" to your vault ({path}).'
            except Exception as e:
                reply = f"Couldn't save the note: {e}"

    except BudgetExceeded:
        # Spending cap reached anywhere above — degrade gracefully. The reply is
        # persisted below like any other; no exception escapes to FastAPI.
        logger.info("Chat hit budget cap; returning friendly budget-reached reply")
        reply = _BUDGET_REACHED_REPLY

    # 4. Persist reply and update conversation timestamp
    await asyncio.to_thread(_db_add_message, conversation_id, "assistant", reply)
    await asyncio.to_thread(_db_touch_conversation, conversation_id)

    # 5. Rolling summarization (best-effort; swallows its own errors)
    await _maybe_summarize(conversation_id, get_settings().chat_history_limit)

    # 6. Fact extraction (best-effort; guards itself, never raises; runs for ALL intents)
    from backend.agents import facts
    await facts.extract_and_store(user_message, conversation_id)

    if token_queue is not None:
        await token_queue.put(None)  # sentinel: stream done

    return {"conversation_id": conversation_id, "reply": reply}
