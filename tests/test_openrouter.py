import httpx
import pytest

from backend.integrations import openrouter

# conftest.py's autouse mock_secrets fixture already makes
# get_secret("OPENROUTER_API_KEY") return "test-openrouter-key" for every test
# in this file -- no per-test settings patch needed.


def _patch_transport(monkeypatch, models_response, key_response):
    """Same MockTransport pattern as test_infisical_client.py -- routes by
    path so one handler can stand in for both real endpoints fetch() calls."""
    real_client = httpx.AsyncClient

    def handler(request):
        if request.url.path == "/api/v1/key":
            return key_response
        return models_response

    def fake_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", fake_client)


@pytest.mark.asyncio
async def test_fetch_parses_credit_and_usage_fields(monkeypatch):
    _patch_transport(
        monkeypatch,
        httpx.Response(200, json={"data": [{"id": "a"}, {"id": "b"}]}),
        httpx.Response(200, json={"data": {
            "limit": 5, "limit_remaining": 0, "usage": 5.05146565,
            "usage_daily": 0, "is_free_tier": False,
        }}),
    )

    result = await openrouter.fetch()
    assert result.available is True
    assert result.model_count == 2
    assert result.credit_limit == 5
    assert result.credit_remaining == 0
    assert result.usage == pytest.approx(5.05146565)
    assert result.usage_daily == 0.0
    assert result.is_free_tier is False


@pytest.mark.asyncio
async def test_fetch_unlimited_key_keeps_limit_as_none_not_zero(monkeypatch):
    """OpenRouter's own convention: limit/limit_remaining are null for a key
    with no cap -- coercing that to 0 would misrepresent 'no cap' as 'no
    credit left'."""
    _patch_transport(
        monkeypatch,
        httpx.Response(200, json={"data": []}),
        httpx.Response(200, json={"data": {"limit": None, "limit_remaining": None, "usage": 12.3}}),
    )

    result = await openrouter.fetch()
    assert result.credit_limit is None
    assert result.credit_remaining is None
    assert result.usage == pytest.approx(12.3)


@pytest.mark.asyncio
async def test_fetch_raises_on_key_endpoint_failure(monkeypatch):
    """Matches this module's pre-existing raise-on-any-failure contract -- a
    bad /key response must not silently zero-default the credit fields."""
    _patch_transport(monkeypatch, httpx.Response(200, json={"data": []}), httpx.Response(401))

    with pytest.raises(httpx.HTTPStatusError):
        await openrouter.fetch()


@pytest.mark.asyncio
async def test_fetch_raises_when_key_not_configured(monkeypatch):
    def _missing(key, fallback_env=True):
        raise KeyError(key)
    monkeypatch.setattr("backend.secrets.manager.get_secret", _missing)
    with pytest.raises(Exception, match="OPENROUTER_API_KEY not configured"):
        await openrouter.fetch()


@pytest.mark.asyncio
async def test_health_check_true_on_success(monkeypatch):
    _patch_transport(monkeypatch, httpx.Response(200, json={"data": []}), httpx.Response(200, json={"data": {}}))
    assert await openrouter.health_check() is True


@pytest.mark.asyncio
async def test_health_check_false_on_failure(monkeypatch):
    _patch_transport(monkeypatch, httpx.Response(500), httpx.Response(200, json={"data": {}}))
    assert await openrouter.health_check() is False


@pytest.mark.asyncio
async def test_fetch_is_cached_matching_other_integrations(monkeypatch):
    """Regression guard (Opus verification pass, 2026-08-05): fetch() gained
    real consumers beyond zero (dashboard.openrouter collector, the contract
    canary) and now makes TWO requests instead of one -- uncached, that's up
    to 3x the schedulers x 2x the requests of the old single-consumer,
    single-request state. A second immediate call must hit the cache, same
    @async_ttl_cache(30) convention every other integration's fetch() uses."""
    calls = {"n": 0}
    real_client = httpx.AsyncClient

    def handler(request):
        calls["n"] += 1
        if request.url.path == "/api/v1/key":
            return httpx.Response(200, json={"data": {"limit": 5, "limit_remaining": 1, "usage": 4.0}})
        return httpx.Response(200, json={"data": []})

    def fake_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)
    monkeypatch.setattr(httpx, "AsyncClient", fake_client)

    await openrouter.fetch()
    calls_after_first = calls["n"]
    await openrouter.fetch()
    assert calls["n"] == calls_after_first, "second fetch() call should have hit the cache, not the network"


@pytest.mark.asyncio
async def test_fetch_does_not_orphan_sibling_request_on_partial_failure(monkeypatch):
    """Regression guard: return_exceptions=True on the gather means both
    requests complete before the client closes, instead of a bare gather()
    propagating the first failure while the client's `async with` block
    closes out from under a still-in-flight sibling call."""
    real_client = httpx.AsyncClient

    def handler(request):
        if request.url.path == "/api/v1/key":
            return httpx.Response(500)
        return httpx.Response(200, json={"data": []})

    def fake_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)
    monkeypatch.setattr(httpx, "AsyncClient", fake_client)

    with pytest.raises(httpx.HTTPStatusError):
        await openrouter.fetch()
