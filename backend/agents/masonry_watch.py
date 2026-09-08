"""Daily masonry / tuck-pointing contractor watch for Southgate, MI --
Brian asked (2026-09-08, via a scheduled research task) for a running
top-5 list of highly-rated local contractors, with a weekly "here's my
best pick" callout, delivered to his Telegram.

Unlike every other watch job in this file, there is no durable local
dataset or deterministic API to probe here -- contractor ratings live on
Yelp/Angi/BBB/Google, not in NEXUS's own DB. The "check" IS an LLM call
with Anthropic's hosted web_search tool, the same pattern chat.py already
uses for a web-search reply (`sonnet(..., web_search=True)`), not a
deterministic probe like anthropic_balance_watch.py's GitHub/endpoint
checks.

Deliberately NOT wired into homelab_watch.py/outcomes.py's OutcomeFlag
machinery -- there's no recurring/resolvable condition here (a
contractor's rating doesn't "clear" the way a stopped VM does), so
record_flag()'s fingerprint-dedup model doesn't fit a research digest
that is *supposed* to repeat daily.

On a genuine LLM/web-search failure this skips the day's send outright
rather than fabricating or reusing a stale hardcoded contractor list --
there's no deterministic fallback data source the way weekly_review.py
has its own DB aggregates to fall back on.
"""
import html
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

_SYSTEM = (
    "You are researching tuck pointing / brick and masonry repair "
    "contractors serving {location}. Use web search to find REAL, "
    "currently-operating contractors with genuine customer ratings "
    "(Google, Yelp, Angi, HomeAdvisor, or BBB) that serve or are near "
    "{location}. Prefer ones physically closer to {location} when ratings "
    "are otherwise comparable, and note the city each is based in. Return "
    "EXACTLY this format, plain text, no markdown headers or asterisk "
    "bullets, under 1200 characters total:\n"
    "1. <name> (<their city>) -- <rating>/5, <review count> reviews on <platform>\n"
    "2. ...\n"
    "3. ...\n"
    "4. ...\n"
    "5. ...\n"
    "BEST PICK: <name> -- <one short sentence why, weighing rating, review "
    "count, and distance from {location}>\n"
    "If you can't confirm a numeric rating for a contractor, say so instead "
    "of inventing one. Never fabricate a contractor that didn't show up in "
    "search results."
)

_BEST_PICK_MARKER = "BEST PICK:"

# Telegram message cap is 4096 chars; leave headroom for the emoji header
# and (when app_base_url is configured) notify_phone's own appended link.
_MAX_MESSAGE_CHARS = 3800


def _is_best_pick_day(day_setting: str, tz_name: str) -> bool:
    """True on the one configured weekday the message should add the
    BEST PICK callout on top of the daily top-5 list."""
    try:
        now = datetime.now(ZoneInfo(tz_name))
    except Exception:
        now = datetime.utcnow()
    return now.strftime("%a").lower()[:3] == (day_setting or "sun").strip().lower()[:3]


def _split_top5_and_pick(text: str) -> tuple[str, str | None]:
    idx = text.find(_BEST_PICK_MARKER)
    if idx == -1:
        return text.strip(), None
    return text[:idx].strip(), text[idx:].strip()


async def run_masonry_watch() -> dict:
    """Top-level entry point called by the scheduler daily. Never raises --
    any exception is caught and logged. Returns a summary dict."""
    try:
        from backend.config import get_settings
        settings = get_settings()
        if not getattr(settings, "masonry_watch_enabled", True):
            return {"skipped": "disabled"}

        location = getattr(settings, "masonry_watch_location", "Southgate, MI")
        best_pick_day = getattr(settings, "masonry_watch_best_pick_day", "sun")
        tz_name = getattr(settings, "briefing_timezone", "America/Detroit")

        try:
            from backend.agents import router
            system = _SYSTEM.format(location=location)
            reply = await router.sonnet(
                f"Research masonry/tuck-pointing contractors near {location} now.",
                system=system,
                web_search=True,
                label="masonry_watch",
            )
        except Exception as e:
            logger.warning(f"masonry_watch: LLM/web-search call failed, skipping today's send: {e}")
            return {"skipped": "llm_error"}

        top5_text, best_pick_line = _split_top5_and_pick(reply)
        if not top5_text:
            logger.warning("masonry_watch: empty research result, skipping send")
            return {"skipped": "empty_result"}

        message = f"\U0001f9f1 Masonry/tuck-pointing near {location}\n\n{top5_text}"
        show_best_pick = _is_best_pick_day(best_pick_day, tz_name)
        if show_best_pick and best_pick_line:
            message += f"\n\n⭐ {best_pick_line}"

        from backend import events
        # Escape BEFORE truncating -- truncating first can cut an HTML
        # entity in half, which Telegram's parse_mode=HTML rejects as a 400
        # and silently drops the whole message (see the goal-notify fix in
        # proposer.py's _esc() for the same lesson).
        delivered = await events.notify_phone(html.escape(message)[:_MAX_MESSAGE_CHARS], kind="masonry_watch")
        return {
            "skipped": None,
            "best_pick_shown": bool(show_best_pick and best_pick_line),
            "delivered": delivered,
        }
    except Exception as e:
        logger.error(f"run_masonry_watch error (ignored): {e}")
        return {"skipped": "error"}
