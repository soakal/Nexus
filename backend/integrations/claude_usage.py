"""Claude Code subscription rate-limit usage, read from the statusline capture file.

There is no Anthropic API for subscription usage. Claude Code's own statusline
command receives `rate_limits.five_hour` / `.seven_day` on stdin; ~/.claude/
statusline-command.sh persists that to ~/.claude/rate-limits.json on every
render. Consequence, and the thing every consumer of this module MUST surface
honestly: the file only updates while an interactive Claude Code session is
running. It goes STALE, not wrong, whenever no session is open -- freshness is
computed from the payload's own `captured_at`, never from the state_store
snapshot's `freshness` field (which only reports how recently we polled the
file).
"""

import asyncio
import json
import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


def _rate_limits_path() -> Path:
    return Path.home() / ".claude" / "rate-limits.json"


@dataclass
class ClaudeUsageWindow:
    used_percentage: float | None = None
    resets_at: int | None = None


@dataclass
class ClaudeUsageData:
    available: bool = False
    captured_at: float | None = None
    five_hour: ClaudeUsageWindow | None = None
    seven_day: ClaudeUsageWindow | None = None
    error: str | None = None


def _coerce_window(raw) -> ClaudeUsageWindow | None:
    if not isinstance(raw, dict):
        return None
    try:
        used_percentage = float(raw["used_percentage"])
    except (KeyError, TypeError, ValueError):
        used_percentage = None
    try:
        resets_at = int(float(raw["resets_at"]))
    except (KeyError, TypeError, ValueError):
        resets_at = None
    if used_percentage is None and resets_at is None:
        return None
    return ClaudeUsageWindow(used_percentage=used_percentage, resets_at=resets_at)


def _read_sync() -> ClaudeUsageData:
    path = _rate_limits_path()
    if not path.exists():
        return ClaudeUsageData(available=False, error="no capture yet")

    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("rate-limits.json root is not an object")
    except (json.JSONDecodeError, ValueError, UnicodeDecodeError, OSError) as e:
        # Every active Claude Code session on this machine (including this one)
        # overwrites this file on every statusline render -- a reader CAN
        # observe a torn/empty write. Raise rather than silently returning
        # empty data: refresh_collector's store_failure preserves the last
        # successful payload and marks it stale, so a torn read shows the
        # last-good numbers (stale) and self-heals next tick, instead of
        # blanking the card. Do NOT add a retry loop here -- the next
        # scheduled tick already covers it.
        raise ValueError(f"rate-limits.json unreadable: {e}") from e

    rl = data.get("rate_limits")
    if not isinstance(rl, dict):
        rl = {}

    try:
        captured_at = float(data["captured_at"])
    except (KeyError, TypeError, ValueError):
        captured_at = None

    return ClaudeUsageData(
        available=True,
        captured_at=captured_at,
        five_hour=_coerce_window(rl.get("five_hour")),
        seven_day=_coerce_window(rl.get("seven_day")),
    )


async def fetch() -> ClaudeUsageData:
    # Deliberately NOT @async_ttl_cache'd, unlike every other integration in
    # this directory: that cache exists to stop many concurrent dashboard
    # polls from fanning out over the network, and there is no network call
    # here -- the only callers are one scheduled collector and the once-daily
    # briefing. async_ttl_cache would also cache the ValueError above for
    # falsy_ttl seconds, pinning a transient torn-read failure for no benefit.
    # asyncio.to_thread is used for the same reason state_workers.py's
    # _latest_briefing uses it: trivially cheap normally, but this host has a
    # documented Defender-induced file-I/O stall pattern, and nothing may
    # block the loop.
    return await asyncio.to_thread(_read_sync)


async def health_check() -> bool:
    try:
        return (await fetch()).available
    except Exception:
        return False
