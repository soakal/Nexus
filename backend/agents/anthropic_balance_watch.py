"""Monthly watch for whether Anthropic has shipped a public API credit-
balance endpoint yet -- there is none today (confirmed 2026-08-05: a live
probe of GET /v1/organizations/balance returns 404, and the community
feature request anthropics/claude-code#47574 is closed "not planned" with
no reasoning ever given by Anthropic staff). Read-only, no LLM, no write
capability -- same "deterministic trigger, human reads the actual page"
philosophy as backend/agents/outcomes.py's own scope boundaries (no LLM
classification of ambiguous outcomes).

Two independent signals, either one can trigger a notice:
1. The GitHub issue's state/state_reason/comment_count (public API, no
   auth needed) -- catches Anthropic (or anyone) commenting, reopening, or
   closing the issue as completed.
2. A live probe of the one concrete candidate endpoint the community has
   proposed -- catches Anthropic shipping the feature silently, without
   ever touching that GitHub issue.

Never notifies on the FIRST check (no prior persisted baseline) unless the
current read is already clearly "resolved" -- otherwise the very first
scheduled run would page about a gap this module was written specifically
because it already exists. After that, ANY change from the last-seen
signal notifies once, even a "reopened but not yet shipped" transition,
since that's still worth a glance at a once-a-month cadence.
"""
import asyncio
import logging

import httpx

from backend.http_client import SSL_CONTEXT

logger = logging.getLogger(__name__)

_ISSUE_API_URL = "https://api.github.com/repos/anthropics/claude-code/issues/47574"
_ISSUE_HTML_URL = "https://github.com/anthropics/claude-code/issues/47574"
_PROBE_URL = "https://api.anthropic.com/v1/organizations/balance"

# Live-verified 2026-08-05 baselines -- a first check landing exactly on
# these is "nothing new," not a signal to page about.
_KNOWN_UNRESOLVED_SIGNAL = "closed:not_planned"
_KNOWN_UNRESOLVED_PROBE_STATUS = 404


async def _check_github_issue() -> tuple[str, int] | None:
    """Returns (signal, comment_count) or None on any fetch failure --
    a failure here must never be misread as 'the issue disappeared'."""
    try:
        async with httpx.AsyncClient(verify=SSL_CONTEXT, timeout=10) as client:
            resp = await client.get(
                _ISSUE_API_URL,
                headers={"Accept": "application/vnd.github+json", "User-Agent": "nexus-agentic-os"},
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        logger.warning(f"anthropic_balance_watch: GitHub issue check failed (ignored): {e}")
        return None
    state = data.get("state") or "unknown"
    state_reason = data.get("state_reason") or "none"
    comment_count = data.get("comments")
    if not isinstance(comment_count, int):
        comment_count = -1
    return f"{state}:{state_reason}", comment_count


async def _check_direct_probe() -> int | None:
    """Returns the HTTP status code from probing the one concrete candidate
    endpoint the community has proposed, or None on a transport failure or
    unconfigured key -- a network hiccup must never look like the endpoint
    appearing."""
    from backend.config import get_settings

    try:
        api_key = get_settings().anthropic_api_key
    except Exception:
        return None
    try:
        async with httpx.AsyncClient(verify=SSL_CONTEXT, timeout=10) as client:
            resp = await client.get(
                _PROBE_URL,
                headers={"x-api-key": api_key, "anthropic-version": "2023-06-01"},
            )
    except Exception as e:
        logger.warning(f"anthropic_balance_watch: direct probe failed (ignored): {e}")
        return None
    return resp.status_code


async def check_anthropic_balance_feature() -> None:
    """Best-effort monthly check -- never raises, matching every other
    scheduled watch job in this codebase."""
    from backend import events
    from backend.safety import governor

    try:
        issue_result, probe_status = await asyncio.gather(
            _check_github_issue(), _check_direct_probe(),
        )
        issue_signal, comment_count = issue_result if issue_result else (None, None)

        prior = await asyncio.to_thread(governor.get_anthropic_balance_watch_state)
        prior_signal = prior.get("issue_signal")
        prior_comment_count = prior.get("comment_count")
        prior_probe_status = prior.get("probe_status")

        looks_resolved = (
            (issue_signal is not None and issue_signal != _KNOWN_UNRESOLVED_SIGNAL)
            or (probe_status is not None and probe_status != _KNOWN_UNRESOLVED_PROBE_STATUS)
        )
        is_first_check = prior_signal is None and prior_probe_status is None
        changed = (
            issue_signal != prior_signal
            or comment_count != prior_comment_count
            or probe_status != prior_probe_status
        )
        # A run where BOTH checks failed (both None) has nothing to report
        # and nothing to persist as a meaningful change -- stay silent.
        has_any_signal = issue_signal is not None or probe_status is not None
        should_notify = has_any_signal and (looks_resolved if is_first_check else changed)

        if should_notify:
            lines = ["Monthly check: the Anthropic API credit-balance feature status may have changed."]
            if issue_signal is not None:
                lines.append(f"GitHub issue state: {issue_signal} ({comment_count} comments) — {_ISSUE_HTML_URL}")
            if probe_status is not None:
                lines.append(f"Direct probe of {_PROBE_URL}: HTTP {probe_status}")
            lines.append("Worth a look — let me know if this should be wired up for real.")
            await events.notify_phone("\n".join(lines), kind="anthropic_balance_watch")

        # A signal that failed THIS run (None) must not overwrite the last
        # known-good baseline -- otherwise one transient network hiccup
        # wipes persisted history and next month's comparison silently
        # resets to "first check" for that signal.
        await asyncio.to_thread(
            governor.set_anthropic_balance_watch_state,
            issue_signal if issue_signal is not None else prior_signal,
            comment_count if issue_signal is not None else prior_comment_count,
            probe_status if probe_status is not None else prior_probe_status,
        )
    except Exception as e:
        logger.warning(f"anthropic_balance_watch: check failed (ignored): {e}")
