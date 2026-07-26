import inspect
from datetime import date, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import httpx
import pytest

from backend.integrations import calendar

TZ = ZoneInfo("America/Detroit")


def _today() -> date:
    return datetime.now(TZ).date()


# ---------------------------------------------------------------------------
# Regression guard — the exact bug that was almost ported verbatim from
# hermes-agent's gcal.py: %-I / %-d strftime flags are glibc-only and raise
# ValueError on Windows.
# ---------------------------------------------------------------------------

def test_no_unpadded_strftime_directive_in_module():
    source = inspect.getsource(calendar)
    assert "%-" not in source, "found a glibc-only %- strftime flag — raises on Windows"


# ---------------------------------------------------------------------------
# _ical_unescape
# ---------------------------------------------------------------------------

def test_ical_unescape_handles_all_escapes():
    assert calendar._ical_unescape("Line1\\nLine2") == "Line1 Line2"
    assert calendar._ical_unescape("A\\, B\\; C") == "A, B; C"
    assert calendar._ical_unescape("back\\\\slash") == "back\\slash"


# ---------------------------------------------------------------------------
# _parse_dtstart
# ---------------------------------------------------------------------------

def test_parse_dtstart_z_suffix_normalizes_to_tz():
    dt, time_str = calendar._parse_dtstart("20260801T140000Z", None, TZ)
    assert isinstance(dt, datetime)
    assert dt.tzinfo is not None
    assert time_str != "All day"


def test_parse_dtstart_tzid_normalizes_to_tz():
    dt, time_str = calendar._parse_dtstart("20260801T090000", "America/New_York", TZ)
    assert isinstance(dt, datetime)
    assert dt.tzinfo is not None


def test_parse_dtstart_all_day():
    d, time_str = calendar._parse_dtstart("20260801", None, TZ)
    assert d == date(2026, 8, 1)
    assert time_str == "All day"


# ---------------------------------------------------------------------------
# _parse_events + _expand_rrule — RRULE FREQ branches
# ---------------------------------------------------------------------------

def _vevent(summary: str, dtstart: date, *, all_day=False, rrule: str | None = None) -> str:
    if all_day:
        dtstart_line = f"DTSTART;VALUE=DATE:{dtstart.strftime('%Y%m%d')}"
    else:
        # Noon UTC (not midnight) so the UTC->America/Detroit conversion in
        # _parse_dtstart can never shift the calendar date to the day before.
        dtstart_line = f"DTSTART:{dtstart.strftime('%Y%m%d')}T120000Z"
    lines = ["BEGIN:VEVENT", f"SUMMARY:{summary}", dtstart_line]
    if rrule:
        lines.append(f"RRULE:{rrule}")
    lines.append("END:VEVENT")
    return "\n".join(lines)


def test_non_recurring_event_inside_window():
    today = _today()
    ical = _vevent("Dentist", today + timedelta(days=2))
    events = calendar._parse_events(ical, days_ahead=7, tz=TZ)
    assert len(events) == 1
    assert events[0]["summary"] == "Dentist"
    assert events[0]["date"] == today + timedelta(days=2)


def test_non_recurring_event_outside_window():
    today = _today()
    ical = _vevent("Someday", today + timedelta(days=30))
    events = calendar._parse_events(ical, days_ahead=7, tz=TZ)
    assert events == []


def test_all_day_event():
    today = _today()
    ical = _vevent("Birthday", today + timedelta(days=1), all_day=True)
    events = calendar._parse_events(ical, days_ahead=7, tz=TZ)
    assert len(events) == 1
    assert events[0]["time"] == "All day"


def test_daily_interval_2():
    today = _today()
    ical = _vevent("Every other day", today, rrule="FREQ=DAILY;INTERVAL=2")
    events = calendar._parse_events(ical, days_ahead=6, tz=TZ)
    dates = {e["date"] for e in events}
    assert today in dates
    assert (today + timedelta(days=1)) not in dates
    assert (today + timedelta(days=2)) in dates


def test_weekly_byday():
    # Start on a Monday so BYDAY=MO,WE lands predictably within the window.
    today = _today()
    monday = today - timedelta(days=today.weekday())
    ical = _vevent("Standup", monday, rrule="FREQ=WEEKLY;BYDAY=MO,WE")
    events = calendar._parse_events(ical, days_ahead=14, tz=TZ)
    weekdays = {e["date"].weekday() for e in events}
    assert weekdays <= {0, 2}
    assert len(events) >= 2


def test_monthly():
    today = _today()
    ical = _vevent("Rent", today, rrule="FREQ=MONTHLY")
    events = calendar._parse_events(ical, days_ahead=40, tz=TZ)
    assert any(e["date"] == today for e in events)
    assert len(events) >= 1


def test_yearly_until_caps_recurrence():
    today = _today()
    start = date(today.year - 5, today.month, today.day)
    until = (today - timedelta(days=10)).strftime("%Y%m%d")
    ical = _vevent("Anniversary", start, rrule=f"FREQ=YEARLY;UNTIL={until}")
    events = calendar._parse_events(ical, days_ahead=30, tz=TZ)
    assert events == []  # UNTIL is before today, so this year's occurrence is excluded


def test_yearly_without_until_recurs():
    today = _today()
    start = date(today.year - 3, today.month, today.day)
    ical = _vevent("Anniversary", start, rrule="FREQ=YEARLY")
    events = calendar._parse_events(ical, days_ahead=3, tz=TZ)
    assert any(e["date"] == today for e in events)


def test_daily_count_exhausted_in_the_past_never_recurs():
    """COUNT is measured from start_date, not from today — a finished
    limited recurrence must not show up again just because today is still
    inside what would have been an unbounded window."""
    today = _today()
    start = today - timedelta(days=30)
    ical = _vevent("Two-day trial", start, rrule="FREQ=DAILY;COUNT=2")
    events = calendar._parse_events(ical, days_ahead=7, tz=TZ)
    assert events == []


def test_daily_count_still_shows_occurrence_within_limit():
    today = _today()
    start = today - timedelta(days=1)
    ical = _vevent("Two-day trial", start, rrule="FREQ=DAILY;COUNT=2")
    events = calendar._parse_events(ical, days_ahead=7, tz=TZ)
    assert [e["date"] for e in events] == [today]


def test_weekly_count_limits_across_byday():
    today = _today()
    monday = today - timedelta(days=today.weekday())
    ical = _vevent("Standup", monday, rrule="FREQ=WEEKLY;BYDAY=MO,WE;COUNT=2")
    events = calendar._parse_events(ical, days_ahead=60, tz=TZ)
    assert len(events) <= 2


def test_yearly_count_limits_recurrence():
    today = _today()
    start = date(today.year - 5, today.month, today.day)
    ical = _vevent("Anniversary", start, rrule="FREQ=YEARLY;COUNT=2")
    events = calendar._parse_events(ical, days_ahead=30, tz=TZ)
    assert events == []  # only 2 occurrences ever, both years ago


# ---------------------------------------------------------------------------
# Sort order — by actual time, not the display string
# ---------------------------------------------------------------------------

def test_events_sort_by_time_not_display_string():
    """'9:00 AM' sorts after '2:00 PM' lexicographically -- events must sort
    by actual time of day, not the rendered string."""
    today = _today()
    ical = "\n".join([
        _vevent("Afternoon review", today).replace("T120000Z", "T180000Z"),  # 2pm ET
        _vevent("Morning standup", today).replace("T120000Z", "T130000Z"),   # 9am ET
    ])
    events = calendar._parse_events(ical, days_ahead=7, tz=TZ)
    assert [e["summary"] for e in events] == ["Morning standup", "Afternoon review"]


def test_all_day_event_sorts_before_timed_events():
    today = _today()
    ical = "\n".join([
        _vevent("Morning meeting", today),
        _vevent("Birthday", today, all_day=True),
    ])
    events = calendar._parse_events(ical, days_ahead=7, tz=TZ)
    assert events[0]["summary"] == "Birthday"


# ---------------------------------------------------------------------------
# fetch() / health_check() — feed-level behavior (httpx mocked)
# ---------------------------------------------------------------------------

def _mock_async_client(responses: dict[str, str | Exception]):
    async def _get(url, *a, **kw):
        result = responses[url]
        if isinstance(result, Exception):
            raise result
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.text = result
        return resp

    mock_client = AsyncMock()
    mock_client.__aenter__.return_value.get = AsyncMock(side_effect=_get)
    return mock_client


@pytest.mark.asyncio
async def test_fetch_merges_two_feeds():
    calendar.fetch.invalidate()
    today = _today()
    ical_a = _vevent("From Google", today + timedelta(days=1))
    ical_b = _vevent("From Apple", today + timedelta(days=2))
    responses = {
        "https://calendar.google.com/test.ics": ical_a,
        "https://p.icloud.com/test.ics": ical_b,
    }
    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client_cls.return_value = _mock_async_client(responses)
        data = await calendar.fetch()
    assert data.feeds_ok == 2
    assert data.feeds_total == 2
    summaries = {e["summary"] for e in data.events}
    assert summaries == {"From Google", "From Apple"}


@pytest.mark.asyncio
async def test_fetch_one_feed_fails_other_still_yields_events():
    calendar.fetch.invalidate()
    today = _today()
    ical_b = _vevent("From Apple", today + timedelta(days=1))
    responses = {
        "https://calendar.google.com/test.ics": httpx.ConnectError("refused"),
        "https://p.icloud.com/test.ics": ical_b,
    }
    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client_cls.return_value = _mock_async_client(responses)
        data = await calendar.fetch()
    assert data.feeds_ok == 1
    assert data.feeds_total == 2
    assert [e["summary"] for e in data.events] == ["From Apple"]


@pytest.mark.asyncio
async def test_fetch_raises_when_no_feed_configured(monkeypatch):
    calendar.fetch.invalidate()

    def _no_secret(key, *a, **kw):
        raise KeyError(key)

    monkeypatch.setattr("backend.secrets.manager.get_secret", _no_secret)
    with pytest.raises(RuntimeError, match="No calendar feed configured"):
        await calendar.fetch()


@pytest.mark.asyncio
async def test_fetch_raises_when_all_feeds_fail():
    calendar.fetch.invalidate()
    responses = {
        "https://calendar.google.com/test.ics": httpx.ConnectError("refused"),
        "https://p.icloud.com/test.ics": httpx.ConnectError("refused"),
    }
    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client_cls.return_value = _mock_async_client(responses)
        with pytest.raises(RuntimeError, match="failed to fetch"):
            await calendar.fetch()


@pytest.mark.asyncio
async def test_health_check_true_on_success():
    calendar.fetch.invalidate()
    responses = {
        "https://calendar.google.com/test.ics": "BEGIN:VCALENDAR\nEND:VCALENDAR",
        "https://p.icloud.com/test.ics": "BEGIN:VCALENDAR\nEND:VCALENDAR",
    }
    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client_cls.return_value = _mock_async_client(responses)
        assert await calendar.health_check() is True


@pytest.mark.asyncio
async def test_health_check_false_when_all_feeds_fail():
    calendar.fetch.invalidate()
    responses = {
        "https://calendar.google.com/test.ics": httpx.ConnectError("refused"),
        "https://p.icloud.com/test.ics": httpx.ConnectError("refused"),
    }
    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client_cls.return_value = _mock_async_client(responses)
        assert await calendar.health_check() is False


@pytest.mark.asyncio
async def test_get_today_events_degrades_to_string_on_failure():
    calendar.fetch.invalidate()
    responses = {
        "https://calendar.google.com/test.ics": httpx.ConnectError("refused"),
        "https://p.icloud.com/test.ics": httpx.ConnectError("refused"),
    }
    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client_cls.return_value = _mock_async_client(responses)
        result = await calendar.get_today_events()
    assert result.startswith("(Calendar unavailable:")
