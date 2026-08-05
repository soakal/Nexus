import json

import pytest

from backend.integrations import claude_usage as _claude_usage_module

# Captured at collection time, before conftest's autouse
# _isolate_claude_rate_limits fixture monkeypatches _rate_limits_path for the
# rest of the suite -- same pattern as test_infisical_client.py's
# `_real_request = ic._request`. Lets the test below call the REAL function
# body directly without touching monkeypatch.undo(), which would also revert
# every other autouse fixture sharing this test's monkeypatch instance
# (mock_secrets, the infisical network guard, action_judge_off_by_default,
# etc. -- a real landmine given this repo's history of a test firing real
# Telegram traffic after an overly broad patch revert).
_real_rate_limits_path = _claude_usage_module._rate_limits_path


def _write(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj), encoding="utf-8")


@pytest.mark.asyncio
async def test_fetch_returns_no_capture_when_file_missing(_isolate_claude_rate_limits):
    from backend.integrations.claude_usage import fetch

    result = await fetch()
    assert result.available is False
    assert result.five_hour is None
    assert result.seven_day is None
    assert result.captured_at is None
    assert "no capture" in result.error


@pytest.mark.asyncio
async def test_fetch_raises_on_malformed_json(_isolate_claude_rate_limits):
    from backend.integrations.claude_usage import fetch

    _isolate_claude_rate_limits.parent.mkdir(parents=True, exist_ok=True)
    _isolate_claude_rate_limits.write_text('{"captured_at":17859667', encoding="utf-8")

    with pytest.raises(ValueError):
        await fetch()
    # Raising (not swallowing) is correct here: refresh_collector's
    # store_failure preserves the last successful payload and marks it
    # stale, so a torn read never blanks the dashboard card.


@pytest.mark.asyncio
async def test_fetch_raises_on_empty_file(_isolate_claude_rate_limits):
    from backend.integrations.claude_usage import fetch

    _isolate_claude_rate_limits.parent.mkdir(parents=True, exist_ok=True)
    _isolate_claude_rate_limits.write_text("", encoding="utf-8")

    with pytest.raises(ValueError):
        await fetch()


@pytest.mark.asyncio
async def test_fetch_parses_both_windows(_isolate_claude_rate_limits):
    from backend.integrations.claude_usage import fetch

    _write(_isolate_claude_rate_limits, {
        "captured_at": 1785966702.124308,
        "rate_limits": {
            "five_hour": {"used_percentage": 45, "resets_at": 1785967200},
            "seven_day": {"used_percentage": 7.000000000000001, "resets_at": 1786514400},
        },
    })

    result = await fetch()
    assert result.available is True
    assert result.captured_at == 1785966702.124308
    assert result.five_hour.used_percentage == 45.0
    assert result.five_hour.resets_at == 1785967200
    assert result.seven_day.used_percentage == pytest.approx(7.0)
    assert result.seven_day.resets_at == 1786514400


@pytest.mark.asyncio
async def test_fetch_handles_one_window_absent(_isolate_claude_rate_limits):
    from backend.integrations.claude_usage import fetch

    _write(_isolate_claude_rate_limits, {
        "captured_at": 1.0,
        "rate_limits": {"five_hour": {"used_percentage": 10, "resets_at": 100}},
    })

    result = await fetch()
    assert result.available is True
    assert result.five_hour is not None
    assert result.seven_day is None


@pytest.mark.asyncio
async def test_fetch_handles_empty_rate_limits(_isolate_claude_rate_limits):
    from backend.integrations.claude_usage import fetch

    _write(_isolate_claude_rate_limits, {"captured_at": 1.0, "rate_limits": {}})

    result = await fetch()
    assert result.available is True
    assert result.five_hour is None
    assert result.seven_day is None


@pytest.mark.asyncio
async def test_fetch_ignores_garbage_window_values(_isolate_claude_rate_limits):
    from backend.integrations.claude_usage import fetch

    _write(_isolate_claude_rate_limits, {
        "captured_at": 1.0,
        "rate_limits": {
            "five_hour": {"used_percentage": "abc", "resets_at": None},
            "seven_day": {"used_percentage": 12, "resets_at": 500},
        },
    })

    result = await fetch()
    assert result.available is True
    assert result.five_hour is None
    assert result.seven_day is not None
    assert result.seven_day.used_percentage == 12.0


@pytest.mark.asyncio
async def test_health_check_true_when_file_readable(_isolate_claude_rate_limits):
    from backend.integrations.claude_usage import health_check

    _write(_isolate_claude_rate_limits, {"captured_at": 1.0, "rate_limits": {}})
    assert await health_check() is True


@pytest.mark.asyncio
async def test_health_check_false_when_file_missing(_isolate_claude_rate_limits):
    from backend.integrations.claude_usage import health_check

    assert await health_check() is False


@pytest.mark.asyncio
async def test_health_check_false_on_malformed_json(_isolate_claude_rate_limits):
    from backend.integrations.claude_usage import health_check

    _isolate_claude_rate_limits.parent.mkdir(parents=True, exist_ok=True)
    _isolate_claude_rate_limits.write_text("not json", encoding="utf-8")
    assert await health_check() is False


def test_rate_limits_path_resolves_under_home(tmp_path, monkeypatch):
    import pathlib

    # Calls the REAL function body (captured at module load, before the
    # autouse _isolate_claude_rate_limits fixture patches
    # claude_usage._rate_limits_path for this test) directly -- see the
    # module-level comment above _real_rate_limits_path for why this avoids
    # monkeypatch.undo().
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: tmp_path))
    assert _real_rate_limits_path() == tmp_path / ".claude" / "rate-limits.json"


def test_module_hardcodes_no_absolute_home_path():
    # Regression guard: a future "fix" that pastes Brian's real path back in
    # would make this module machine-specific again.
    import pathlib
    src = pathlib.Path("backend/integrations/claude_usage.py").read_text(encoding="utf-8")
    assert "C:\\Users" not in src
    assert "/home/" not in src
