import secrets as _secrets

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

router = APIRouter()

_ALLOWED_SETUP_KEYS = {
    "OPENROUTER_API_KEY", "HASS_TOKEN", "UNIFI_PASSWORD", "UNRAID_API_KEY",
    "ADGUARD_PASS", "CHANNELS_HOST", "GITHUB_TOKEN", "OPENWEATHER_API_KEY",
    "HERMES_WEBHOOK_SECRET", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
    "GOOGLE_CALENDAR_ICAL_URL", "APPLE_CALENDAR_ICAL_URL",
}


def _needs_setup() -> bool:
    """True only when the secret store is reachable AND holds no NEXUS_API_KEY.

    Fail CLOSED: /complete is unauthenticated (it has to be — there is no key
    yet on a fresh install), so anything other than a definitive "the store
    answered and the key is absent" must read as "already configured". A
    KeyError is that definitive answer; any other error (store unreachable,
    vault undecryptable, config invalid) previously reported needs_setup=True
    and reopened an unauthenticated write path over ANTHROPIC_API_KEY and
    NEXUS_API_KEY on an already-provisioned install.
    """
    from backend.secrets.manager import get_secret
    try:
        return not bool(get_secret("NEXUS_API_KEY"))
    except KeyError:
        return True
    except Exception:
        return False


@router.get("/status")
async def setup_status():
    return {"needs_setup": _needs_setup()}


class SetupPayload(BaseModel):
    anthropic_api_key: str
    secrets: dict = {}


@router.post("/complete")
async def setup_complete(body: SetupPayload):
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

    return {"ok": True, "nexus_api_key": nexus_api_key}
