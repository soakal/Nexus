import asyncio
import json
import logging
import re
from datetime import datetime

logger = logging.getLogger(__name__)

# Sections sourced from single-source, unverified third-party content (a real
# email subject line) rather than a cross-checked NEXUS integration --
# extract_and_store would otherwise store an unread subject as a 0.9+
# confidence "durable fact", and the goal proposer's KNOWN FACTS context
# treats durable facts as grounds for an autonomous investigation + phone
# notification (the original incident: "Dropbox Storage Limit Hit" from one
# unread email got echoed into Priority Actions and then fact-extracted as
# fact). Strip these sections before fact extraction; they stay in the
# stored/displayed briefing. "Inbox" is a Gmail-era LLM-narrated heading
# retired when mail moved to Proton (see _build_protonmail_section) -- kept
# here defensively in case it's ever reintroduced or appears in an old stored
# briefing. "Proton Mail" is appended AFTER the LLM call (never reaches the
# prompt at all), but its raw subject lines still must not reach fact
# extraction, same reasoning.
_UNVERIFIED_FACT_SECTIONS = ("Priority Actions", "Inbox", "Proton Mail")


def _strip_unverified_sections(text: str) -> str:
    for heading in _UNVERIFIED_FACT_SECTIONS:
        text = re.sub(rf"## {re.escape(heading)}.*?(?=\n## |\Z)", "", text, flags=re.DOTALL)
    return text


def _truncate_subject(subject: str, cap: int = 80) -> str:
    subject = subject or "(no subject)"
    return subject if len(subject) <= cap else subject[: cap - 1] + "…"


def _parse_email_list(result) -> list | None:
    """result is either an Exception (integration failure) or the JSON text
    from protonmail.list_recent(). Returns the parsed emails list, [] if the
    shape is unexpected, or None if the source itself was unavailable."""
    if isinstance(result, Exception):
        return None
    try:
        data = json.loads(result)
        emails = data.get("emails")
        if not isinstance(emails, list):
            return []
        return [e for e in emails if isinstance(e, dict)]
    except Exception:
        return None


def _needing_reply(unread_result, drafted_ids: set, cap: int = 5) -> list | None:
    unread_emails = _parse_email_list(unread_result)
    if unread_emails is None:
        return None
    return [e for e in unread_emails if e.get("email_id") in drafted_ids][:cap]


def _build_protonmail_section(unread_result, drafts_result, drafted_ids: set) -> str:
    """Deterministic, factual '## Proton Mail' section, assembled in Python and
    appended AFTER the LLM call — never fed into the prompt. Mail data here is
    already a finished judgment (the auto-draft scheduler's conservative
    classifier already decided which unread emails need a reply), so there is
    nothing for the LLM to synthesize; feeding it in would only risk the same
    manufactured-urgency problem _UNVERIFIED_FACT_SECTIONS exists to contain.
    Never raises for any input shape (Exception, malformed JSON, missing keys).
    """
    unread_emails = _parse_email_list(unread_result)
    draft_emails = _parse_email_list(drafts_result)

    if unread_emails is None and draft_emails is None:
        return "## Proton Mail\nProton Mail data unavailable."

    needing_reply = [e for e in (unread_emails or []) if e.get("email_id") in drafted_ids][:5]
    drafts = (draft_emails or [])[:10]

    if not needing_reply and not drafts and unread_emails is not None and draft_emails is not None:
        return "## Proton Mail\nNothing needing attention."

    lines = ["## Proton Mail"]

    if unread_emails is None:
        lines.append("Unread-mail data unavailable.")
    elif needing_reply:
        lines.append(
            f"{len(needing_reply)} unread email(s) previously judged (conservative classifier) to need a personal reply:"
        )
        for e in needing_reply:
            sender = e.get("sender") or "(unknown sender)"
            lines.append(f'- {sender} — "{_truncate_subject(e.get("subject"))}"')

    if draft_emails is None:
        lines.append("Drafts-folder data unavailable.")
    elif drafts:
        lines.append(f"{len(drafts)} draft(s) awaiting your review in Proton Drafts:")
        for e in drafts:
            subject = _truncate_subject(e.get("subject"))
            recipients = e.get("recipients")
            if recipients:
                to = ", ".join(recipients) if isinstance(recipients, list) else recipients
                lines.append(f'- "{subject}" → {to}')
            else:
                lines.append(f'- "{subject}"')

    if len(lines) == 1:
        lines.append("Nothing needing attention.")

    return "\n".join(lines)


# Unraid parity_status values that mean "no parity concern" (a check finished
# clean or none is running) -- distinct from "unknown" (a read problem, not a
# breach, same reasoning as check_unraid_array's own status handling). Real
# values observed are disk_ok/unknown; existing test fixtures use "idle".
# Kept as its own allowlist constant (not folded into a single "not unknown"
# check) so a genuinely-bad status (e.g. "failed"/a running check) is never
# silently swallowed by loosening this set later.
_UNRAID_PARITY_HEALTHY_STATUSES = {"idle", "disk_ok"}


async def _record_briefing_flags(context: dict) -> None:
    """Deterministic, structured flag write path for the briefing (spec
    docs/outcome-tracker-spec.md §2.2-C, rollout step 5 of 7). Reads only
    from the already-built `context` dict -- never re-fetches, never touches
    the LLM prompt/response, matching _build_protonmail_section's "assembled
    in Python, never in the prompt" discipline. Records/clears exactly six
    flags; symmetric clear_flag calls let yesterday's flag (e.g. a stale PR)
    auto-resolve once the underlying condition clears.

    Write path only this cycle -- no BRIEFING_PROMPT change, no '## Open
    Items' section, no _UNVERIFIED_FACT_SECTIONS edit, no open_flags/
    recently_closed wiring into the gather step (all rollout step 6).

    Best-effort: the call site in run_briefing() wraps this whole function in
    try/except so a DB hiccup can never fail or delay the briefing.
    outcomes.record_flag already guarantees it NEVER raises itself
    (backend/agents/outcomes.py); outcomes.clear_flag does not carry that
    same top-level guard, so it is individually wrapped below, mirroring
    backend/agents/homelab_watch.py's _clear_flag_safe helper.
    """
    from backend.agents import outcomes

    async def _clear(check: str) -> None:
        try:
            await outcomes.clear_flag("briefing", check)
        except Exception as e:
            logger.warning(f"_record_briefing_flags: clear_flag failed for {check!r} (ignored): {e}")

    ha_alerts = context["home_assistant"]["alerts"] or []
    if ha_alerts:
        summary = f"{len(ha_alerts)} Home Assistant alert(s): {'; '.join(str(a) for a in ha_alerts[:5])}"
        await outcomes.record_flag("briefing", "ha_unavailable_entities", summary[:300])
    else:
        await _clear("ha_unavailable_entities")

    array_status = context["unraid"]["array_status"]
    if array_status not in ("started", "unknown"):
        summary = f"Unraid array status is '{array_status}' (expected 'started')."
        await outcomes.record_flag("briefing", "unraid_array", summary[:300])
    else:
        await _clear("unraid_array")

    parity_status = context["unraid"]["parity_status"]
    if parity_status != "unknown" and parity_status not in _UNRAID_PARITY_HEALTHY_STATUSES:
        summary = f"Unraid parity status is '{parity_status}'."
        await outcomes.record_flag("briefing", "unraid_parity", summary[:300])
    else:
        await _clear("unraid_parity")

    stale_prs = context["github"]["stale_prs"] or []
    if stale_prs:
        summary = f"{len(stale_prs)} stale PR(s): {'; '.join(str(p) for p in stale_prs[:5])}"
        await outcomes.record_flag("briefing", "github_stale_prs", summary[:300])
    else:
        await _clear("github_stale_prs")

    new_devices = context["unifi"]["new_devices"] or []
    if new_devices:
        summary = f"{len(new_devices)} new UniFi device(s): {'; '.join(str(d) for d in new_devices[:5])}"
        await outcomes.record_flag("briefing", "unifi_new_devices", summary[:300])
    else:
        await _clear("unifi_new_devices")

    # AdGuard: flag only on a confirmed False reading, clear only on a
    # confirmed True reading. filtering_enabled is coerced to the string
    # "unknown" above (a real read failure -- see that block's comment) when
    # the raw value was None; both None and "unknown" must fall through here
    # untouched (neither flag nor clear) to preserve the 2026-07-26
    # unknown-vs-off fix -- an `is False`/`is True` check does this for free
    # since neither matches a bool.
    filtering_enabled = context["adguard"]["filtering_enabled"]
    if filtering_enabled is False:
        await outcomes.record_flag("briefing", "adguard_filtering_off", "AdGuard filtering is OFF.")
    elif filtering_enabled is True:
        await _clear("adguard_filtering_off")


BRIEFING_PROMPT = """You are Carl, a direct, high-conviction personal AI assistant, briefing a solo power user starting their day.
Be direct. No filler, no hedging ("try," "hope," "maybe"). Assume high technical literacy. Flag anomalies clearly.
Never say "as of my last update" or similar hedges — this is live data.

DATA SNAPSHOT as of {timestamp}:
{json_context}

Produce a morning brief with these exact sections:

## Priority Actions (max 3)
Items requiring action TODAY, ranked by urgency. If nothing urgent, say so.

## Weather
{weather_summary}
[Flag if rain > 50% or temperature extreme]

## System Health
One line per system: Unraid, UniFi, Home Assistant, AdGuard.
Flag parity check if running. Flag mover if active. Flag new unknown devices on network.

## Network Security
Queries today: {blocked_today} blocked ({blocked_pct}%). Flag any spike vs 7-day average.
Filtering: {filtering_enabled}.

## GitHub Pulse
PRs/issues needing attention. Call out any stale PRs explicitly.

## Media
{recording_now}. Notable upcoming in next 24h.
DVR storage: {dvr_used}/{dvr_total} GB.

## Calendar
{calendar_block}

## From Your Vault
Relevant open tasks from Obsidian. Surface anything tagged #today or #urgent.

## Today's Focus
Single paragraph. What should this person focus on and why, given everything above."""


async def run_briefing() -> str:
    from sqlmodel import Session

    from backend.agents.router import (
        close_trace,
        open_trace,
        reset_trace_context,
        set_trace_context,
        sonnet,
    )
    from backend.database import Briefing, engine
    from backend.integrations import (
        adguard,
        channels_dvr,
        github,
        homeassistant,
        obsidian,
        unifi,
        unraid,
        weather,
    )
    from backend.integrations.calendar import get_today_events
    from backend.integrations import protonmail
    from backend.agents import mail_drafts

    logger.info("Running morning briefing")

    # Open an AgentTrace (council w-observability) so nested LLM calls made
    # during this run attach TraceSpan rows to it. Mirrors orchestrator's
    # run_task trace wiring, via the generic open_trace/close_trace helper
    # (briefing has no durable task_id -- kind='briefing', task_id=None).
    # trace_id is None on any open failure -- set_trace_context(None) is a
    # safe no-op downstream (router._record_trace_span short-circuits on
    # trace_id is None).
    trace_id = await asyncio.to_thread(open_trace, "briefing", "daily_briefing")
    _trace_token = set_trace_context(trace_id)
    _trace_status = "ok"
    _trace_error = None

    try:
        results = await asyncio.gather(
            homeassistant.fetch(),
            unifi.fetch(),
            unraid.fetch(),
            obsidian.fetch(),
            github.fetch(),
            weather.fetch(),
            channels_dvr.fetch(),
            adguard.fetch(),
            get_today_events(),
            protonmail.list_recent(unread_only=True, limit=25),
            protonmail.list_recent(mailbox="Drafts", limit=10),
            return_exceptions=True,
        )

        ha, unifi_d, unraid_d, obs, gh, wx, channels, ag, cal_data, proton_unread, proton_drafts = results

        cal_str = cal_data if not isinstance(cal_data, Exception) else "Calendar unavailable"

        try:
            drafted_ids = await asyncio.to_thread(mail_drafts._db_drafted_email_ids)
        except Exception:
            drafted_ids = set()

        def safe(obj, attr, default="N/A"):
            if isinstance(obj, Exception):
                return default
            return getattr(obj, attr, default)

        # AdGuard's filtering_enabled is None (not a default True/False) when its
        # own /control/status read failed — a real reading exists but couldn't be
        # taken, distinct from "AdGuard is unreachable" (ag itself would be an
        # Exception then, caught above by safe()). Render that as "unknown", not
        # a bare None, since a bare None reads as a bug in the prompt/JSON either
        # way and used to silently read as "filtering on" instead.
        ag_filtering = safe(ag, "filtering_enabled", None)
        ag_filtering = "unknown" if ag_filtering is None else ag_filtering

        context = {
            "home_assistant": {
                "entity_count": len(safe(ha, "entities", [])),
                "alerts": safe(ha, "alerts", []),
            },
            "unifi": {
                "clients": safe(unifi_d, "client_count", 0),
                "status": safe(unifi_d, "uplink_status", "unknown"),
                "new_devices": safe(unifi_d, "new_devices", []),
            },
            "unraid": {
                "array_status": safe(unraid_d, "array_status", "unknown"),
                "parity_status": safe(unraid_d, "parity_status", "unknown"),
                "mover_running": safe(unraid_d, "mover_running", False),
                "storage_used_gb": safe(unraid_d, "storage_used_gb", 0),
                "storage_total_gb": safe(unraid_d, "storage_total_gb", 0),
                "docker_containers": len(safe(unraid_d, "docker_containers", [])),
            },
            "github": {
                "open_prs": len(safe(gh, "open_prs", [])),
                "assigned_issues": len(safe(gh, "assigned_issues", [])),
                "stale_prs": safe(gh, "stale_prs", []),
            },
            "obsidian": {
                "open_tasks": safe(obs, "open_tasks", []),
            },
            "channels_dvr": {
                "recording_now": safe(channels, "recording_now", []),
                "upcoming": safe(channels, "upcoming", []),
                "storage_used_gb": safe(channels, "storage_used_gb", 0),
                "storage_total_gb": safe(channels, "storage_total_gb", 0),
            },
            "adguard": {
                "queries_today": safe(ag, "queries_today", 0),
                "blocked_today": safe(ag, "blocked_today", 0),
                "blocked_pct": safe(ag, "blocked_pct", 0),
                "filtering_enabled": ag_filtering,
            },
        }

        wx_data = wx if not isinstance(wx, Exception) else None
        weather_summary = wx_data.summary if wx_data else "Weather data unavailable"
        if wx_data:
            weather_summary = f"{wx_data.summary}. High {wx_data.high_f}°F / Low {wx_data.low_f}°F."

        rec_now = safe(channels, "recording_now", [])
        rec_str = ", ".join([r.get("title", "") for r in rec_now]) if rec_now else "Nothing recording"

        prompt = BRIEFING_PROMPT.format(
            timestamp=datetime.utcnow().isoformat(),
            json_context=json.dumps(context, indent=2),
            weather_summary=weather_summary,
            blocked_today=safe(ag, "blocked_today", 0),
            blocked_pct=safe(ag, "blocked_pct", 0),
            filtering_enabled=ag_filtering,
            recording_now=rec_str,
            dvr_used=safe(channels, "storage_used_gb", 0),
            dvr_total=safe(channels, "storage_total_gb", 0),
            calendar_block=cal_str,
        )

        # Added AFTER the prompt is built, on purpose — this must never reach the
        # LLM (see _build_protonmail_section's docstring). It only records what
        # was surfaced, for context_json's own record-keeping.
        proton_section = _build_protonmail_section(proton_unread, proton_drafts, drafted_ids)
        _needing = _needing_reply(proton_unread, drafted_ids)
        _drafts = _parse_email_list(proton_drafts)
        context["protonmail"] = {
            "unread_needing_reply": len(_needing) if _needing is not None else 0,
            "pending_drafts": len(_drafts) if _drafts is not None else 0,
        }

        briefing_text = await sonnet(prompt, label="briefing")
        briefing_text = briefing_text + "\n\n" + proton_section
        logger.info("Briefing generated")

        # Best-effort structured flag write path (spec
        # docs/outcome-tracker-spec.md §2.2-C, rollout step 5) -- write path
        # only, no prompt/read-path change this cycle. Wrapped so a DB
        # hiccup can never fail or delay the briefing; see
        # _record_briefing_flags' own docstring for its internal guards.
        try:
            await _record_briefing_flags(context)
        except Exception as e:
            logger.warning(f"_record_briefing_flags failed (ignored): {e}")

        # Extract durable facts from briefing content (best-effort, never raises).
        # Priority Actions/Inbox are excluded -- see _strip_unverified_sections.
        from backend.agents.facts import extract_and_store as _extract_facts
        await _extract_facts(_strip_unverified_sections(briefing_text), None, source="briefing")

        # Store in DB
        with Session(engine) as session:
            b = Briefing(content=briefing_text, context_json=json.dumps(context))
            session.add(b)
            session.commit()
            session.refresh(b)
            briefing_id = b.id

        # Write to Obsidian
        try:
            today = datetime.now().strftime("%Y-%m-%d")
            await obsidian.create_note(
                title=today,
                content=f"# Morning Briefing — {today}\n\n{briefing_text}",
                folder="NEXUS/Briefings",
            )
            obsidian_path = f"NEXUS/Briefings/{today}.md"
            with Session(engine) as session:
                b = session.get(Briefing, briefing_id)
                if b:
                    b.obsidian_path = obsidian_path
                    session.commit()
        except Exception as e:
            logger.warning(f"Obsidian write failed: {e}")

        # Deliver via Telegram
        try:
            from backend.integrations.telegram import notify
            delivered = await notify({"type": "briefing", "content": briefing_text, "timestamp": datetime.utcnow().isoformat()})
            if delivered:
                # Column name kept as-is (delivered_to_hermes) -- renaming it needs
                # a migration for zero benefit; it now means "delivered via NEXUS's
                # own Telegram notify path".
                with Session(engine) as session:
                    b = session.get(Briefing, briefing_id)
                    if b:
                        b.delivered_to_hermes = True
                        session.commit()
        except Exception as e:
            logger.warning(f"Telegram delivery failed: {e}")

        return briefing_text
    except Exception as exc:
        _trace_status = "error"
        _trace_error = str(exc)
        raise
    finally:
        # Best-effort trace close, wrapped so a bookkeeping failure here can
        # never mask the real return value / exception from run_briefing.
        try:
            await asyncio.to_thread(close_trace, trace_id, _trace_status, _trace_error)
        except Exception as e:
            logger.warning(f"trace close failed (non-fatal): {e}")
        reset_trace_context(_trace_token)
