"""iCal calendar reader (Google + optional Apple/iCloud feeds).

Pure iCal-over-HTTPS + RRULE expansion, no OAuth. Fetches directly via httpx.

Two bugs fixed during an earlier port (do not re-add):
  - the no-pad strftime flag (percent, dash, then I or d) is glibc-only and
    raises ValueError on Windows.
  - date.today() reads the PROCESS timezone; briefing_timezone is used instead
    so this stays correct if NEXUS ever runs on a UTC host.
"""
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from itertools import groupby
from zoneinfo import ZoneInfo

import httpx

from backend.cache import async_ttl_cache
from backend.http_client import SSL_CONTEXT

logger = logging.getLogger(__name__)


@dataclass
class CalendarData:
    events: list[dict] = field(default_factory=list)
    summary: str = ""
    feeds_ok: int = 0
    feeds_total: int = 0


def _ical_unescape(text: str) -> str:
    """Undo iCal text escaping so summaries read cleanly (no literal \\n etc.)."""
    return (
        text.replace("\\n", " ").replace("\\N", " ")
        .replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\")
    ).strip()


def _ical_urls() -> list[str]:
    from backend.config import get_settings
    settings = get_settings()
    urls = []
    if settings.google_calendar_ical_url:
        urls.append(settings.google_calendar_ical_url)
    if settings.apple_calendar_ical_url:
        urls.append(settings.apple_calendar_ical_url)
    return urls


async def _fetch_ical() -> tuple[str, int, int]:
    """Fetch and concatenate every configured feed. VEVENT blocks from all feeds
    end up in one text, which _parse_events then reads as a merged calendar. A
    failed feed is skipped so one bad URL never blocks the others; feeds_ok
    counts successes so a fully-dead feed can be distinguished from a
    genuinely empty calendar."""
    urls = _ical_urls()
    texts = []
    ok = 0
    async with httpx.AsyncClient(verify=SSL_CONTEXT, timeout=10) as client:
        for url in urls:
            fetch_url = "https://" + url[len("webcal://"):] if url.startswith("webcal://") else url
            try:
                r = await client.get(fetch_url)
                r.raise_for_status()
                texts.append(r.text)
                ok += 1
            except Exception as e:
                logger.warning(f"Calendar feed failed, skipping: {e}")
    return "\n".join(texts), ok, len(urls)


# Map iCal BYDAY two-letter codes to Python weekday() integers (Mon=0).
_BYDAY_TO_WEEKDAY = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}


def _parse_dtstart(raw: str, tzid: str | None, tz: ZoneInfo):
    """Parse a DTSTART value into (datetime|date, time_str).

    Returns a tuple of the parsed value and a display time string. The
    datetime is timezone-aware and normalised to `tz`; an all-day value
    returns a plain date and "All day".
    """
    if "T" in raw:
        # datetime — strip any timezone suffix for the bare parse
        clean = re.sub(r"Z$", "", raw)
        clean = re.sub(r"[A-Z]+$", "", clean)
        dt = datetime.strptime(clean[:15], "%Y%m%dT%H%M%S")
        if raw.endswith("Z"):
            dt = dt.replace(tzinfo=ZoneInfo("UTC")).astimezone(tz)
        elif tzid:
            try:
                dt = dt.replace(tzinfo=ZoneInfo(tzid)).astimezone(tz)
            except Exception:
                dt = dt.replace(tzinfo=tz)
        else:
            dt = dt.replace(tzinfo=tz)
        return dt, dt.strftime("%I:%M %p").lstrip("0")
    else:
        return datetime.strptime(raw[:8], "%Y%m%d").date(), "All day"


def _expand_rrule(start, rrule: str, today: date, cutoff: date) -> list[date]:
    """Generate occurrence dates of a recurring event within [today, cutoff).

    Handles FREQ=DAILY/WEEKLY/MONTHLY/YEARLY with optional INTERVAL, BYDAY,
    UNTIL, and COUNT. `start` is the master event's start (datetime or date).

    COUNT counts occurrences from `start_date` (per RFC5545), not from
    `today` — a COUNT=2 event that already fired twice in the past must NOT
    show up again, even though the window-bounded loops below only ever look
    at dates >= today. So when COUNT is present, each branch instead
    enumerates exactly `count_limit` occurrences from start_date (a loop
    bounded by COUNT itself, however far in the past start_date is) and
    THEN filters to the [today, cutoff) window.
    """
    parts = {}
    for piece in rrule.split(";"):
        if "=" in piece:
            k, v = piece.split("=", 1)
            parts[k.strip().upper()] = v.strip()

    freq = parts.get("FREQ", "").upper()
    try:
        interval = int(parts.get("INTERVAL", "1"))
    except ValueError:
        interval = 1
    if interval < 1:
        interval = 1

    count_limit = None
    try:
        if "COUNT" in parts:
            count_limit = max(0, int(parts["COUNT"]))
    except ValueError:
        count_limit = None

    start_date = start.date() if isinstance(start, datetime) else start

    # An UNTIL clause caps the recurrence; ignore occurrences past it.
    until_date = None
    until_raw = parts.get("UNTIL")
    if until_raw:
        try:
            until_date = datetime.strptime(until_raw[:8], "%Y%m%d").date()
        except ValueError:
            until_date = None

    occurrences: list[date] = []
    effective_cutoff = cutoff
    if until_date and until_date < effective_cutoff:
        effective_cutoff = until_date + timedelta(days=1)

    if freq == "DAILY":
        if count_limit is not None:
            for i in range(count_limit):
                cand = start_date + timedelta(days=i * interval)
                if today <= cand < effective_cutoff:
                    occurrences.append(cand)
        else:
            d = start_date
            if d < today:
                delta = (today - d).days
                steps = (delta + interval - 1) // interval
                d = start_date + timedelta(days=steps * interval)
            while d < effective_cutoff:
                if d >= today:
                    occurrences.append(d)
                d += timedelta(days=interval)

    elif freq == "WEEKLY":
        byday = parts.get("BYDAY", "")
        weekdays = [_BYDAY_TO_WEEKDAY[c] for c in byday.split(",")
                    if c.strip().upper()[-2:] in _BYDAY_TO_WEEKDAY] if byday else [start_date.weekday()]
        start_week_monday = start_date - timedelta(days=start_date.weekday())
        if count_limit is not None:
            d = start_date
            found = 0
            # Bounded by count_limit (weekdays is never empty), not by the window.
            while found < count_limit:
                if d >= start_date and d.weekday() in weekdays:
                    d_week_monday = d - timedelta(days=d.weekday())
                    weeks_between = (d_week_monday - start_week_monday).days // 7
                    if weeks_between >= 0 and weeks_between % interval == 0:
                        found += 1
                        if today <= d < effective_cutoff:
                            occurrences.append(d)
                d += timedelta(days=1)
        else:
            d = today
            while d < effective_cutoff:
                if d >= start_date and d.weekday() in weekdays:
                    d_week_monday = d - timedelta(days=d.weekday())
                    weeks_between = (d_week_monday - start_week_monday).days // 7
                    if weeks_between >= 0 and weeks_between % interval == 0:
                        occurrences.append(d)
                d += timedelta(days=1)

    elif freq == "MONTHLY":
        y, mo = start_date.year, start_date.month
        if count_limit is not None:
            found = 0
            c = 0
            while found < count_limit:
                try:
                    cand = date(y, mo, start_date.day)
                except ValueError:
                    cand = None  # e.g. day 31 in a short month — skip
                if cand is not None and c % interval == 0:
                    found += 1
                    if today <= cand < effective_cutoff:
                        occurrences.append(cand)
                mo += 1
                if mo > 12:
                    mo = 1
                    y += 1
                c += 1
        else:
            count = 0
            while True:
                try:
                    cand = date(y, mo, start_date.day)
                except ValueError:
                    cand = None  # e.g. day 31 in a short month — skip
                if cand is not None:
                    if cand >= effective_cutoff:
                        break
                    if cand >= today and count % interval == 0:
                        occurrences.append(cand)
                mo += 1
                if mo > 12:
                    mo = 1
                    y += 1
                count += 1
                if date(y, mo, 1) >= effective_cutoff and date(y, mo, 1) > cutoff:
                    break

    elif freq == "YEARLY":
        if count_limit is not None:
            found = 0
            yy = start_date.year
            guard = 0
            guard_limit = count_limit * 40 + 400  # generous bound even for an all-Feb-29 rule
            while found < count_limit and guard < guard_limit:
                try:
                    cand = date(yy, start_date.month, start_date.day)
                except ValueError:
                    cand = None  # e.g. Feb 29 in a non-leap year — doesn't count
                if cand is not None:
                    found += 1
                    if today <= cand < effective_cutoff:
                        occurrences.append(cand)
                yy += interval
                guard += 1
        else:
            for yy in range(max(start_date.year, today.year), effective_cutoff.year + 1):
                if (yy - start_date.year) % interval != 0:
                    continue
                try:
                    cand = date(yy, start_date.month, start_date.day)
                except ValueError:
                    continue  # e.g. Feb 29 in a non-leap year
                if today <= cand < effective_cutoff:
                    occurrences.append(cand)

    return occurrences


def _parse_events(ical_text: str, days_ahead: int, tz: ZoneInfo) -> list[dict]:
    """Parse iCal text and return events within the next `days_ahead` days."""
    today = datetime.now(tz).date()
    cutoff = today + timedelta(days=days_ahead)
    events = []

    blocks = re.findall(r"BEGIN:VEVENT(.*?)END:VEVENT", ical_text, re.DOTALL)
    for block in blocks:
        def field_(name):
            """Return (value, tzid) for an iCal field, stripping any params."""
            m = re.search(rf"^{name}([^:]*):(.*)", block, re.MULTILINE)
            if not m:
                return "", None
            params, value = m.group(1), m.group(2).strip()
            tz_m = re.search(r"TZID=([^;:]+)", params)
            return value, (tz_m.group(1) if tz_m else None)

        summary, _ = field_("SUMMARY")
        dtstart_raw, dtstart_tzid = field_("DTSTART")
        rrule, _ = field_("RRULE")

        if not summary or not dtstart_raw:
            continue

        summary = _ical_unescape(summary)

        try:
            start, time_str = _parse_dtstart(dtstart_raw, dtstart_tzid, tz)
        except Exception:
            continue

        master_date = start.date() if isinstance(start, datetime) else start
        # Sortable key, separate from the display string `time_str` ("9:00 AM"
        # sorts after "2:00 PM" lexicographically) — minutes-since-midnight,
        # with all-day events first.
        sort_minutes = start.hour * 60 + start.minute if isinstance(start, datetime) else -1

        if rrule:
            occurrence_dates = _expand_rrule(start, rrule, today, cutoff)
        else:
            occurrence_dates = [master_date] if today <= master_date < cutoff else []

        for ev_date in occurrence_dates:
            events.append({
                "date": ev_date,
                "time": time_str,
                "summary": summary,
                "is_today": ev_date == today,
                "sort_minutes": sort_minutes,
            })

    events.sort(key=lambda e: (e["date"], e["sort_minutes"]))
    return events


def _format_events(events: list[dict], tz: ZoneInfo, days_ahead: int) -> str:
    """Format a parsed event list grouped by day. Extracted from gcal.py's
    get_today_events so fetch()/get_today_events() can share it."""
    if not events:
        return f"No events in the next {days_ahead} days."

    today = datetime.now(tz).date()
    tomorrow = today + timedelta(days=1)
    lines = []
    for ev_date, group in groupby(events, key=lambda e: e["date"]):
        if ev_date == today:
            label = "Today"
        elif ev_date == tomorrow:
            label = "Tomorrow"
        else:
            label = f"{ev_date.strftime('%a %b')} {ev_date.day}"
        lines.append(f"{label}:")
        for e in group:
            lines.append(f"  {e['time']} — {e['summary']}")

    return "\n".join(lines)


async def _load(days_ahead: int) -> CalendarData:
    """Raises RuntimeError when no feed is configured, or when every configured
    feed fails — never silently returns an empty-looking calendar for either
    case (a genuinely empty calendar is events=[] with feeds_ok > 0)."""
    from backend.config import get_settings
    settings = get_settings()
    tz = ZoneInfo(settings.briefing_timezone)

    urls = _ical_urls()
    if not urls:
        raise RuntimeError("No calendar feed configured (GOOGLE_CALENDAR_ICAL_URL/APPLE_CALENDAR_ICAL_URL)")

    ical_text, feeds_ok, feeds_total = await _fetch_ical()
    if feeds_ok == 0:
        raise RuntimeError(f"All {feeds_total} calendar feed(s) failed to fetch")

    events = _parse_events(ical_text, days_ahead, tz)
    summary = _format_events(events, tz, days_ahead)
    return CalendarData(events=events, summary=summary, feeds_ok=feeds_ok, feeds_total=feeds_total)


@async_ttl_cache(120)  # matches the dashboard poll pattern
async def fetch() -> CalendarData:
    """Raises RuntimeError when no feed is configured, or when every configured
    feed fails — never silently returns an empty-looking calendar for either
    case (a genuinely empty calendar is events=[] with feeds_ok > 0)."""
    from backend.config import get_settings
    return await _load(get_settings().calendar_days_ahead)


MAX_DAYS_AHEAD = 90


async def upcoming(days_ahead: int) -> CalendarData:
    """Windowed calendar query for callers that need further out than the
    fixed `calendar_days_ahead` default (e.g. chat answering "when is my
    dr appointment"). Deliberately NOT @async_ttl_cache'd: backend/cache.py's
    cache slot is per-function, not per-argument, so caching here would let
    one caller's window silently override what fetch()'s fixed-window callers
    (Today page, briefing, uptime, the contract canary) see for up to 120s."""
    try:
        days_ahead = max(1, min(int(days_ahead), MAX_DAYS_AHEAD))
    except (TypeError, ValueError):
        days_ahead = 7
    return await _load(days_ahead)


def format_events(events: list[dict], days_ahead: int) -> str:
    """Public formatting delegate so callers outside this module (chat) don't
    duplicate the Today:/Tomorrow:/<weekday> house style."""
    from backend.config import get_settings
    tz = ZoneInfo(get_settings().briefing_timezone)
    return _format_events(events, tz, days_ahead)


async def health_check() -> bool:
    try:
        await fetch()
        return True
    except Exception:
        return False


async def get_today_events() -> str:
    """String-returning convenience for consumers that render calendar text
    directly (briefing/today) — degrades to a string instead of raising."""
    try:
        data = await fetch()
        return data.summary
    except Exception as e:
        return f"(Calendar unavailable: {e})"
