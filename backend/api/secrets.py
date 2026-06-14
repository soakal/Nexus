import time

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from backend.auth import require_api_key

router = APIRouter()


class SecretUpdate(BaseModel):
    key: str
    value: str


@router.get("/list")
async def list_secrets(_=Depends(require_api_key)):
    from backend.secrets.vault import list_keys, read_meta
    try:
        keys = list_keys()
        return {"keys": keys, "meta": read_meta()}
    except RuntimeError:
        return {"keys": [], "meta": {}}


@router.post("/set")
async def set_secret_endpoint(body: SecretUpdate, _=Depends(require_api_key)):
    from backend.secrets.vault import set_secret
    set_secret(body.key, body.value)
    return {"ok": True}


@router.post("/test/{key}")
async def test_secret(key: str, _=Depends(require_api_key)):
    start = time.time()
    try:
        result = await _run_test(key)
        latency = int((time.time() - start) * 1000)
        return {"ok": result[0], "latency_ms": latency, "error": result[1]}
    except Exception as e:
        latency = int((time.time() - start) * 1000)
        return {"ok": False, "latency_ms": latency, "error": str(e)}


async def _run_test(key: str) -> tuple:
    from backend.integrations import (
        adguard,
        github,
        hermes,
        homeassistant,
        obsidian,
        openrouter,
        unifi,
        unraid,
        weather,
    )

    TEST_MAP = {
        "ANTHROPIC_API_KEY": _test_anthropic,
        "HASS_TOKEN": homeassistant.health_check,
        "UNIFI_PASSWORD": unifi.health_check,
        "UNRAID_API_KEY": unraid.health_check,
        "OBSIDIAN_TOKEN": obsidian.health_check,
        "GITHUB_TOKEN": github.health_check,
        "OPENWEATHER_API_KEY": weather.health_check,
        "OPENROUTER_API_KEY": openrouter.health_check,
        "ADGUARD_PASS": adguard.health_check,
        "HERMES_WEBHOOK_SECRET": hermes.health_check,
    }

    fn = TEST_MAP.get(key)
    if not fn:
        return (True, None)

    try:
        result = await fn()
        return (bool(result), None)
    except Exception as e:
        return (False, str(e))


async def _test_anthropic() -> bool:
    import anthropic

    from backend.config import get_settings
    client = anthropic.AsyncAnthropic(api_key=get_settings().anthropic_api_key)
    models = await client.models.list()
    return len(models.data) > 0
