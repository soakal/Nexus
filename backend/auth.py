import hmac
import html
import re

from fastapi import HTTPException, Request, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

security = HTTPBearer(auto_error=False)

_SOURCE_CHARSET = re.compile(r"[^A-Za-z0-9.:_-]")
_LOOPBACK_HOSTS = {"127.0.0.1", "::1"}


def _client_source(request: Request | None) -> str:
    """Best-effort client identity for 401-burst attribution.

    uvicorn runs WITHOUT proxy_headers (run.py), so a request arriving via
    `tailscale serve` has request.client.host == 127.0.0.1. Only trust
    X-Forwarded-For when the direct peer IS loopback (i.e. actually came
    through that proxy) — trusting it unconditionally would let ANY caller
    spoof the header to evade detection, evict a real offender out of the
    bounded source table, or misattribute the alert (found live 2026-07-25).
    Falls back to the peer address otherwise. Charset-restricted + truncated
    because this string is attacker-controlled and ends up in a Telegram
    (HTML parse mode) message via the auth-burst watchdog alert.
    """
    if request is None:
        return "unknown"
    try:
        peer = request.client.host if request.client else None
        fwd = request.headers.get("x-forwarded-for")
        if fwd and peer in _LOOPBACK_HOSTS:
            source = fwd.split(",")[0].strip()
        elif peer:
            source = peer
        else:
            source = "unknown"
    except Exception:
        source = "unknown"
    return _SOURCE_CHARSET.sub("", source)[:45] or "unknown"


def _client_path(request: Request | None) -> str:
    """Request path for 401-burst attribution. HTML-escaped: like the source
    above, this is attacker-controlled and ends up in an HTML-parse-mode
    Telegram message — unescaped, a crafted path (e.g. containing an <a>
    tag) renders as a live link in an alert Brian trusts, and malformed HTML
    can also fail the Telegram send outright, which would suppress this and
    every other pending alert for the source (found live 2026-07-25)."""
    try:
        path = request.url.path if request is not None else "?"
    except Exception:
        return "?"
    return html.escape(path)


async def require_api_key(
    credentials: HTTPAuthorizationCredentials = Security(security),
    request: Request = None,
) -> str:
    from backend.config import get_settings
    try:
        expected_key = get_settings().nexus_api_key
    except (KeyError, RuntimeError):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Vault not configured",
        )
    # Compare as bytes: str compare_digest raises TypeError on non-ASCII input
    # (a malformed token must 401, not 500).
    if (credentials is None or not expected_key
            or not hmac.compare_digest(
                credentials.credentials.encode(), expected_key.encode())):
        # Feed the 401-burst watchdog. Wrapped so a counter bug can NEVER turn
        # a 401 into a 500 — this branch's only contract is "reject cleanly".
        try:
            from backend.safety import authfail
            authfail.record_failure(_client_source(request), _client_path(request))
        except Exception:
            pass
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return credentials.credentials
