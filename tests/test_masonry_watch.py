from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.agents import masonry_watch


def _settings(**overrides):
    s = MagicMock()
    s.masonry_watch_enabled = True
    s.masonry_watch_location = "Southgate, MI"
    s.masonry_watch_best_pick_day = "sun"
    s.briefing_timezone = "America/Detroit"
    for k, v in overrides.items():
        setattr(s, k, v)
    return s


_SAMPLE_REPLY = (
    "1. Above All Masonry (Wyandotte) -- 4.9/5, 40 reviews on Angi\n"
    "2. It's The Brick Guys (Royal Oak) -- 4.7/5, 90 reviews on HomeAdvisor\n"
    "3. Modern Brick (New Boston) -- unrated, strong direct reviews\n"
    "4. Brickworks Property Restoration (Clinton Twp) -- 4.9/5, 1084 reviews on Birdeye\n"
    "5. Brick & Level Masonry Restoration (Chesterfield) -- 4.9/5, 100 reviews on Google\n"
    "BEST PICK: Above All Masonry -- closest to Southgate with a strong multi-platform rating."
)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def test_split_top5_and_pick_separates_marker():
    top5, pick = masonry_watch._split_top5_and_pick(_SAMPLE_REPLY)
    assert "1. Above All Masonry" in top5
    assert "BEST PICK:" not in top5
    assert pick.startswith("BEST PICK: Above All Masonry")


def test_split_top5_and_pick_no_marker_returns_whole_text_and_none():
    top5, pick = masonry_watch._split_top5_and_pick("just a list, no marker")
    assert top5 == "just a list, no marker"
    assert pick is None


def test_is_best_pick_day_matches_configured_weekday():
    # Every real day is exactly one of these three-letter abbreviations —
    # matching against "today's" own abbreviation must be True, and every
    # other day name must be False.
    import datetime as real_datetime
    from zoneinfo import ZoneInfo

    today_abbr = real_datetime.datetime.now(ZoneInfo("America/Detroit")).strftime("%a").lower()[:3]
    other_abbr = next(d for d in ["mon", "tue", "wed", "thu", "fri", "sat", "sun"] if d != today_abbr)
    assert masonry_watch._is_best_pick_day(today_abbr, "America/Detroit") is True
    assert masonry_watch._is_best_pick_day(other_abbr, "America/Detroit") is False


def test_is_best_pick_day_bad_timezone_falls_back_without_raising():
    # Must not raise on a bad tz name -- degrades to datetime.utcnow() rather
    # than crashing the whole daily job over a typo'd setting.
    masonry_watch._is_best_pick_day("sun", "Not/A_Real_Zone")


# ---------------------------------------------------------------------------
# run_masonry_watch -- full integration
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_masonry_watch_skips_when_disabled():
    with patch("backend.config.get_settings", return_value=_settings(masonry_watch_enabled=False)):
        result = await masonry_watch.run_masonry_watch()
    assert result == {"skipped": "disabled"}


@pytest.mark.asyncio
async def test_run_masonry_watch_llm_failure_skips_send_without_fabricating_data():
    notify_mock = AsyncMock(return_value=True)
    with patch("backend.config.get_settings", return_value=_settings()), \
         patch("backend.agents.router.sonnet", new_callable=AsyncMock, side_effect=RuntimeError("boom")), \
         patch("backend.events.notify_phone", notify_mock):
        result = await masonry_watch.run_masonry_watch()
    assert result == {"skipped": "llm_error"}
    notify_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_masonry_watch_delivers_daily_list_without_best_pick_on_a_non_pick_day():
    sonnet_mock = AsyncMock(return_value=_SAMPLE_REPLY)
    notify_mock = AsyncMock(return_value=True)
    # Configure the pick-day to a day that is never "today" in this test.
    with patch("backend.config.get_settings", return_value=_settings(masonry_watch_best_pick_day="__never__")), \
         patch("backend.agents.router.sonnet", sonnet_mock), \
         patch("backend.events.notify_phone", notify_mock):
        result = await masonry_watch.run_masonry_watch()

    assert result == {"skipped": None, "best_pick_shown": False, "delivered": True}
    sonnet_mock.assert_awaited_once()
    assert sonnet_mock.await_args.kwargs.get("label") == "masonry_watch"
    assert sonnet_mock.await_args.kwargs.get("web_search") is True
    notify_mock.assert_awaited_once()
    assert notify_mock.await_args.kwargs["kind"] == "masonry_watch"
    sent = notify_mock.await_args.args[0]
    assert "Above All Masonry" in sent
    assert "BEST PICK" not in sent


@pytest.mark.asyncio
async def test_run_masonry_watch_includes_best_pick_on_the_configured_day():
    import datetime as real_datetime
    from zoneinfo import ZoneInfo
    today_abbr = real_datetime.datetime.now(ZoneInfo("America/Detroit")).strftime("%a").lower()[:3]

    sonnet_mock = AsyncMock(return_value=_SAMPLE_REPLY)
    notify_mock = AsyncMock(return_value=True)
    with patch("backend.config.get_settings", return_value=_settings(masonry_watch_best_pick_day=today_abbr)), \
         patch("backend.agents.router.sonnet", sonnet_mock), \
         patch("backend.events.notify_phone", notify_mock):
        result = await masonry_watch.run_masonry_watch()

    assert result == {"skipped": None, "best_pick_shown": True, "delivered": True}
    sent = notify_mock.await_args.args[0]
    assert "BEST PICK" in sent


@pytest.mark.asyncio
async def test_run_masonry_watch_escapes_before_truncating():
    # A literal "&" in the LLM's reply must survive as the HTML entity
    # &amp; rather than being cut off mid-entity by truncation -- truncating
    # BEFORE escaping could slice an entity in half and make Telegram's
    # parse_mode=HTML reject the whole message (see proposer.py's _esc()
    # for the same lesson this mirrors).
    reply = "1. Above All Masonry & Construction (Wyandotte) -- 4.9/5\nBEST PICK: same, & good."
    notify_mock = AsyncMock(return_value=True)
    with patch("backend.config.get_settings", return_value=_settings(masonry_watch_best_pick_day="__never__")), \
         patch("backend.agents.router.sonnet", new_callable=AsyncMock, return_value=reply), \
         patch("backend.events.notify_phone", notify_mock):
        await masonry_watch.run_masonry_watch()
    sent = notify_mock.await_args.args[0]
    assert "&amp;" in sent
    assert " & " not in sent


def test_masonry_watch_kind_registered():
    from backend.events import NOTIFY_KINDS
    assert "masonry_watch" in NOTIFY_KINDS
