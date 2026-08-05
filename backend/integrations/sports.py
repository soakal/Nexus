"""Tigers/Lions last-game scores for the homelab digest (Phase 3).

Ported from hermes-agent/sports.py. Both are unauthenticated public HTTP
GETs (MLB Stats API, ESPN's site API) -- no vault secret needed.
"""
import datetime
import logging
from zoneinfo import ZoneInfo

import httpx

from backend.http_client import SSL_CONTEXT

logger = logging.getLogger(__name__)


def _today() -> datetime.date:
    """briefing_timezone-aware 'today', not the host process's local date --
    matches the convention backend/integrations/calendar.py already
    established (a prior port bug fixed there for the same reason)."""
    from backend.config import get_settings
    try:
        tz = ZoneInfo(get_settings().briefing_timezone)
    except Exception:
        tz = None
    return datetime.datetime.now(tz).date()


async def get_tigers_last_game() -> str | None:
    """Yesterday's Tigers score if they played and the game is final, else None."""
    yesterday = (_today() - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
    try:
        async with httpx.AsyncClient(verify=SSL_CONTEXT, timeout=8) as client:
            resp = await client.get(
                "https://statsapi.mlb.com/api/v1/schedule",
                params={"sportId": 1, "date": yesterday, "teamId": 116},
            )
        if resp.status_code != 200:
            return None
        for date_entry in resp.json().get("dates", []):
            for game in date_entry.get("games", []):
                if game.get("status", {}).get("abstractGameState") != "Final":
                    continue
                home = game["teams"]["home"]
                away = game["teams"]["away"]
                home_name = home.get("team", {}).get("name", "?")
                away_name = away.get("team", {}).get("name", "?")
                home_score = home.get("score", "?")
                away_score = away.get("score", "?")
                return f"{away_name} {away_score}, {home_name} {home_score} (Final)"
    except Exception as e:
        logger.debug(f"get_tigers_last_game failed (ignored): {e}")
    return None


async def get_lions_last_game() -> str | None:
    """Most recent completed Lions score within the last 7 days, else None."""
    try:
        async with httpx.AsyncClient(verify=SSL_CONTEXT, timeout=8) as client:
            for days_ago in range(1, 8):
                date_str = (_today() - datetime.timedelta(days=days_ago)).strftime("%Y%m%d")
                try:
                    resp = await client.get(
                        "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard",
                        params={"dates": date_str},
                    )
                except Exception as e:
                    logger.debug(f"get_lions_last_game request failed (ignored): {e}")
                    continue
                if resp.status_code != 200:
                    continue
                for event in resp.json().get("events", []):
                    for comp in event.get("competitions", []):
                        if not comp.get("status", {}).get("type", {}).get("completed"):
                            continue
                        teams = {c["team"]["abbreviation"]: c for c in comp.get("competitors", [])}
                        if "DET" not in teams:
                            continue
                        det = teams["DET"]
                        opp_abbr = next((k for k in teams if k != "DET"), None)
                        if opp_abbr is None:
                            continue
                        opp = teams[opp_abbr]
                        det_score = det.get("score", "?")
                        opp_score = opp.get("score", "?")
                        if det.get("homeAway") == "home":
                            return f"Lions {det_score}, {opp_abbr} {opp_score} (Final)"
                        return f"{opp_abbr} {opp_score}, Lions {det_score} (Final)"
    except Exception as e:
        logger.debug(f"get_lions_last_game failed (ignored): {e}")
    return None
