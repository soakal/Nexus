import asyncio
import hmac
import logging
import pathlib
import secrets as _secrets

from fastapi import APIRouter, Depends, HTTPException, Request, Security, status
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel

from backend.auth import _client_path, _client_source, security as _bearer
from backend.secrets.vault import secure_key_file

logger = logging.getLogger(__name__)

router = APIRouter()

_ALLOWED_SETUP_KEYS = {
    "OPENROUTER_API_KEY", "HASS_TOKEN", "UNIFI_PASSWORD", "UNRAID_API_KEY",
    "ADGUARD_PASS", "CHANNELS_HOST", "GITHUB_TOKEN", "OPENWEATHER_API_KEY",
    "HERMES_WEBHOOK_SECRET", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
    "GOOGLE_CALENDAR_ICAL_URL", "APPLE_CALENDAR_ICAL_URL",
}

_BOOTSTRAP_TOKEN_PATH = pathlib.Path(".nexus-setup-token")
_bootstrap_token: str | None = None
_setup_lock = asyncio.Lock()


def _needs_setup() -> bool:
    try:
        from backend.secrets.manager import get_secret
        val = get_secret("NEXUS_API_KEY")
        return not bool(val)
    except Exception:
        return True


def _clear_bootstrap_token() -> None:
    """Destroy the bootstrap token, in memory and on disk. Never raises."""
    global _bootstrap_token
    _bootstrap_token = None
    try:
        _BOOTSTRAP_TOKEN_PATH.unlink(missing_ok=True)
    except Exception as e:
        logger.warning(f"Could not remove {_BOOTSTRAP_TOKEN_PATH}: {e}")


def ensure_bootstrap_token() -> None:
    """Called once from lifespan startup. Setup pending -> mint+publish a
    fresh token, ACL-hardened like .vault.key. Setup done -> clear any stale
    token. Never raises."""
    global _bootstrap_token
    try:
        if not _needs_setup():
            _clear_bootstrap_token()
            return
        _bootstrap_token = _secrets.token_urlsafe(32)
        try:
            _BOOTSTRAP_TOKEN_PATH.write_text(_bootstrap_token, encoding="utf-8")
            secure_key_file(_BOOTSTRAP_TOKEN_PATH)  # same ACL hardening .vault.key gets — this token is equally sensitive (it gates minting the master key)
            where = _BOOTSTRAP_TOKEN_PATH.resolve()
        except Exception as e:
            where = f"(could not write token file: {e})"
        logger.warning(
            "\n"
            "=================== NEXUS FIRST-RUN SETUP ===================\n"
            "  Setup is not complete. The setup wizard requires this\n"
            "  one-time token, which is NOT valid after setup finishes\n"
            "  and changes every time the backend restarts:\n"
            "\n"
            f"      {_bootstrap_token}\n"
            "\n"
            f"  Also written to: {where}\n"
            "=============================================================",
        )
    except Exception as e:
        logger.error(f"Bootstrap token setup failed: {e}")


async def require_setup_token(
    credentials: HTTPAuthorizationCredentials = Security(_bearer),
    request: Request = None,
) -> None:
    """First-run gate. One uniform 401 for every rejection reason."""
    expected = _bootstrap_token
    if (credentials is None or not expected
            or not hmac.compare_digest(
                credentials.credentials.encode(), expected.encode())):
        try:
            from backend.safety import authfail
            authfail.record_failure(_client_source(request), _client_path(request))
        except Exception:
            pass
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing first-run setup token",
            headers={"WWW-Authenticate": "Bearer"},
        )


@router.get("/status")
async def setup_status():
    return {"needs_setup": _needs_setup()}


class SetupPayload(BaseModel):
    anthropic_api_key: str
    secrets: dict = {}


@router.post("/complete")
async def setup_complete(body: SetupPayload, _=Depends(require_setup_token)):
    async with _setup_lock:
        if not _needs_setup():
            return JSONResponse(status_code=409, content={"error": "Already configured"})

        key = body.anthropic_api_key.strip()
        if not key or not key.startswith("sk-ant-"):
            return JSONResponse(status_code=400, content={"error": "Invalid Anthropic API key (must start with sk-ant-)"})

        from backend.secrets.manager import set_secret
        set_secret("ANTHROPIC_API_KEY", key)

        # Write any additional secrets from the wizard — allowlisted only, blanks skipped
        for k, v in body.secrets.items():
            if k in _ALLOWED_SETUP_KEYS and isinstance(v, str) and v.strip():
                set_secret(k, v.strip())

        nexus_api_key = _secrets.token_urlsafe(32)
        set_secret("NEXUS_API_KEY", nexus_api_key)

        import backend.config as _cfg
        _cfg._settings_instance = None

        _clear_bootstrap_token()
        return {"ok": True, "nexus_api_key": nexus_api_key}
