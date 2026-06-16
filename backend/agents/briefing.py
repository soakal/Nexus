import asyncio
import json
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

BRIEFING_PROMPT = """You are a senior intelligence analyst briefing a solo power user starting their day.
Be direct. No filler. Assume high technical literacy. Flag anomalies clearly.
Never say "as of my last update" or similar hedges — this is live data.

DATA SNAPSHOT as of {timestamp}:
{json_context}

TREND DATA (7-day):
{trend_summary}

Produce a morning brief with these exact sections:

## Priority Actions (max 3)
Items requiring action TODAY, ranked by urgency. If nothing urgent, say so.

## Weather
{weather_summary}
[Flag if rain > 50% or temperature extreme]

## System Health
One line per system: Unraid, UniFi, Home Assistant, AdGuard.
Flag parity check if running. Flag mover if active. Flag new unknown devices on network.
If Unraid storage trend projects full within 30 days, call it out with projected date.

## Network Security
Queries today: {blocked_today} blocked ({blocked_pct}%). Flag any spike vs 7-day average.
Filtering: {filtering_enabled}.

## GitHub Pulse
PRs/issues needing attention. Call out any stale PRs explicitly.

## Media
{recording_now}. Notable upcoming in next 24h.
DVR storage: {dvr_used}/{dvr_total} GB.
[If DVR trend projects full within 14 days, flag with projected date]

## Calendar
{calendar_block}

## Inbox
{inbox_block}

## From Your Vault
Relevant open tasks from Obsidian. Surface anything tagged #today or #urgent.

## Today's Focus
Single paragraph. What should this person focus on and why, given everything above."""


async def _get_trend_summary() -> str:
    from datetime import timedelta

    from sqlmodel import Session, select

    from backend.database import TrendSnapshot, engine

    cutoff = datetime.utcnow() - timedelta(days=7)
    with Session(engine) as session:
        snapshots = session.exec(
            select(TrendSnapshot).where(TrendSnapshot.captured_at >= cutoff)
        ).all()

    lines = []
    sources = {}
    for s in snapshots:
        key = f"{s.source}:{s.metric}"
        if key not in sources:
            sources[key] = []
        sources[key].append(s.value)

    for key, values in sources.items():
        if len(values) >= 2:
            trend = "stable"
            delta = values[-1] - values[0]
            if abs(delta) > 1:
                trend = f"increasing by {abs(delta):.1f}" if delta > 0 else f"decreasing by {abs(delta):.1f}"
            lines.append(f"  {key}: {values[-1]:.1f} ({trend} over 7d)")

    return "\n".join(lines) if lines else "  No trend data yet"


async def run_briefing() -> str:
    from sqlmodel import Session

    from backend.agents.router import sonnet
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
    from backend.integrations.hermes import get_calendar, get_gmail

    logger.info("Running morning briefing")

    results = await asyncio.gather(
        homeassistant.fetch(),
        unifi.fetch(),
        unraid.fetch(),
        obsidian.fetch(),
        github.fetch(),
        weather.fetch(),
        channels_dvr.fetch(),
        adguard.fetch(),
        get_calendar(),
        get_gmail(),
        return_exceptions=True,
    )

    ha, unifi_d, unraid_d, obs, gh, wx, channels, ag, cal_data, mail_data = results

    cal_str = cal_data if not isinstance(cal_data, Exception) else "Calendar unavailable"
    mail_str = mail_data if not isinstance(mail_data, Exception) else "Inbox unavailable"

    def safe(obj, attr, default="N/A"):
        if isinstance(obj, Exception):
            return default
        return getattr(obj, attr, default)

    trend_summary = await _get_trend_summary()

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
            "filtering_enabled": safe(ag, "filtering_enabled", True),
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
        trend_summary=trend_summary,
        weather_summary=weather_summary,
        blocked_today=safe(ag, "blocked_today", 0),
        blocked_pct=safe(ag, "blocked_pct", 0),
        filtering_enabled=safe(ag, "filtering_enabled", True),
        recording_now=rec_str,
        dvr_used=safe(channels, "storage_used_gb", 0),
        dvr_total=safe(channels, "storage_total_gb", 0),
        calendar_block=cal_str,
        inbox_block=mail_str,
    )

    briefing_text = await sonnet(prompt)
    logger.info("Briefing generated")

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

    # Deliver via Hermes
    try:
        from backend.integrations.hermes import notify
        delivered = await notify({"type": "briefing", "content": briefing_text, "timestamp": datetime.utcnow().isoformat()})
        if delivered:
            with Session(engine) as session:
                b = session.get(Briefing, briefing_id)
                if b:
                    b.delivered_to_hermes = True
                    session.commit()
    except Exception as e:
        logger.warning(f"Hermes delivery failed: {e}")

    return briefing_text
