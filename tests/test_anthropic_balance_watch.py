import pytest
import httpx
from unittest.mock import AsyncMock, patch

from backend.agents import anthropic_balance_watch as watch


def _patch_transport(monkeypatch, issue_response, probe_response):
    """Same MockTransport-by-path pattern as test_infisical_client.py /
    test_openrouter.py -- routes GitHub's issue API vs the Anthropic probe
    URL to two independently-controlled responses."""
    real_client = httpx.AsyncClient

    def handler(request):
        if "api.github.com" in str(request.url):
            return issue_response
        return probe_response

    def fake_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", fake_client)


def _issue_json(state="closed", state_reason="not_planned", comments=3):
    return httpx.Response(200, json={"state": state, "state_reason": state_reason, "comments": comments})


@pytest.mark.asyncio
async def test_first_check_stays_silent_on_known_baseline(monkeypatch):
    """A fresh SystemState row (no prior check) at the documented baseline
    (closed:not_planned, 404) must NOT page -- we already know this, that's
    why the module exists."""
    _patch_transport(monkeypatch, _issue_json(), httpx.Response(404))
    with patch("backend.events.notify_phone", new_callable=AsyncMock) as mock_notify, \
         patch("backend.safety.governor.get_anthropic_balance_watch_state",
               return_value={"issue_signal": None, "comment_count": None, "probe_status": None}), \
         patch("backend.safety.governor.set_anthropic_balance_watch_state") as mock_set:
        await watch.check_anthropic_balance_feature()
    mock_notify.assert_not_called()
    mock_set.assert_called_once_with("closed:not_planned", 3, 404)


@pytest.mark.asyncio
async def test_first_check_notifies_if_already_resolved(monkeypatch):
    """If the very first check ever run already shows something other than
    the known baseline, that's genuine news worth telling Brian immediately
    -- don't wait for a 'change' when we have no prior baseline to compare
    against."""
    _patch_transport(monkeypatch, _issue_json(state="open", state_reason="reopened", comments=5), httpx.Response(404))
    with patch("backend.events.notify_phone", new_callable=AsyncMock) as mock_notify, \
         patch("backend.safety.governor.get_anthropic_balance_watch_state",
               return_value={"issue_signal": None, "comment_count": None, "probe_status": None}), \
         patch("backend.safety.governor.set_anthropic_balance_watch_state"):
        await watch.check_anthropic_balance_feature()
    mock_notify.assert_called_once()


@pytest.mark.asyncio
async def test_no_notify_when_nothing_changed_since_last_check(monkeypatch):
    _patch_transport(monkeypatch, _issue_json(comments=3), httpx.Response(404))
    with patch("backend.events.notify_phone", new_callable=AsyncMock) as mock_notify, \
         patch("backend.safety.governor.get_anthropic_balance_watch_state",
               return_value={"issue_signal": "closed:not_planned", "comment_count": 3, "probe_status": 404}), \
         patch("backend.safety.governor.set_anthropic_balance_watch_state"):
        await watch.check_anthropic_balance_feature()
    mock_notify.assert_not_called()


@pytest.mark.asyncio
async def test_notifies_when_comment_count_increases(monkeypatch):
    """A rising comment count is itself worth a glance, even if the issue
    state hasn't changed -- someone (possibly Anthropic) may have said
    something."""
    _patch_transport(monkeypatch, _issue_json(comments=4), httpx.Response(404))
    with patch("backend.events.notify_phone", new_callable=AsyncMock) as mock_notify, \
         patch("backend.safety.governor.get_anthropic_balance_watch_state",
               return_value={"issue_signal": "closed:not_planned", "comment_count": 3, "probe_status": 404}), \
         patch("backend.safety.governor.set_anthropic_balance_watch_state"):
        await watch.check_anthropic_balance_feature()
    mock_notify.assert_called_once()


@pytest.mark.asyncio
async def test_notifies_when_probe_status_changes_away_from_404(monkeypatch):
    """The strongest possible signal: the candidate endpoint itself
    stopped 404ing. Must page regardless of the GitHub issue's state."""
    _patch_transport(monkeypatch, _issue_json(comments=3), httpx.Response(200, json={"balance": "42.00"}))
    with patch("backend.events.notify_phone", new_callable=AsyncMock) as mock_notify, \
         patch("backend.safety.governor.get_anthropic_balance_watch_state",
               return_value={"issue_signal": "closed:not_planned", "comment_count": 3, "probe_status": 404}), \
         patch("backend.safety.governor.set_anthropic_balance_watch_state"):
        await watch.check_anthropic_balance_feature()
    mock_notify.assert_called_once()


@pytest.mark.asyncio
async def test_transient_failure_of_both_checks_never_notifies_or_overwrites(monkeypatch):
    """A run where both the GitHub call and the probe fail (e.g. a network
    blip) must stay completely silent AND must not stomp the persisted
    baseline with None -- that would falsely reset next month's comparison
    to 'first check'."""
    async def _boom(*a, **k):
        raise httpx.ConnectError("boom")

    with patch.object(watch, "_check_github_issue", new_callable=AsyncMock, return_value=None), \
         patch.object(watch, "_check_direct_probe", new_callable=AsyncMock, return_value=None), \
         patch("backend.events.notify_phone", new_callable=AsyncMock) as mock_notify, \
         patch("backend.safety.governor.get_anthropic_balance_watch_state",
               return_value={"issue_signal": "closed:not_planned", "comment_count": 3, "probe_status": 404}), \
         patch("backend.safety.governor.set_anthropic_balance_watch_state") as mock_set:
        await watch.check_anthropic_balance_feature()
    mock_notify.assert_not_called()
    # Prior values preserved verbatim, not overwritten with None.
    mock_set.assert_called_once_with("closed:not_planned", 3, 404)


@pytest.mark.asyncio
async def test_probe_skipped_when_api_key_not_configured(monkeypatch):
    def _missing(key, fallback_env=True):
        raise KeyError(key)
    monkeypatch.setattr("backend.secrets.manager.get_secret", _missing)
    result = await watch._check_direct_probe()
    assert result is None


@pytest.mark.asyncio
async def test_check_anthropic_balance_feature_never_raises():
    """Top-level guard: even a totally unexpected exception inside the body
    (e.g. governor.get_... raising) must not propagate -- matches every
    other best-effort scheduled watch job's contract."""
    with patch.object(watch, "_check_github_issue", new_callable=AsyncMock, return_value=None), \
         patch.object(watch, "_check_direct_probe", new_callable=AsyncMock, return_value=None), \
         patch("backend.safety.governor.get_anthropic_balance_watch_state", side_effect=RuntimeError("db down")):
        await watch.check_anthropic_balance_feature()  # must not raise
