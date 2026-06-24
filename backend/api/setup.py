import secrets as _secrets

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

router = APIRouter()

_ALLOWED_SETUP_KEYS = {
    "OPENROUTER_API_KEY", "HASS_TOKEN", "UNIFI_PASSWORD", "UNRAID_API_KEY",
    "ADGUARD_PASS", "CHANNELS_HOST", "GITHUB_TOKEN", "OPENWEATHER_API_KEY",
    "HERMES_WEBHOOK_SECRET",
}


def _needs_setup() -> bool:
    try:
        from backend.secrets.vault import get_secret
        val = get_secret("NEXUS_API_KEY")
        return not bool(val)
    except Exception:
        return True


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

    from backend.secrets.vault import set_secret
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
