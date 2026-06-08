import logging
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)


@dataclass
class OpenRouterData:
    available: bool = False
    model_count: int = 0


async def fetch() -> OpenRouterData:
    return await _get_data()


async def _get_data() -> OpenRouterData:
    from backend.config import get_settings
    settings = get_settings()
    try:
        api_key = settings.openrouter_api_key
    except Exception:
        raise Exception("OPENROUTER_API_KEY not configured")

    headers = {"Authorization": f"Bearer {api_key}"}
    async with httpx.AsyncClient(timeout=5) as client:
        resp = await client.get("https://openrouter.ai/api/v1/models", headers=headers)
        resp.raise_for_status()
        models = resp.json().get("data", [])
    return OpenRouterData(available=True, model_count=len(models))


async def health_check() -> bool:
    try:
        data = await _get_data()
        return data.available
    except Exception:
        return False


async def complete(prompt: str, model: str = "openai/gpt-4o-mini") -> str:
    from backend.config import get_settings
    settings = get_settings()
    headers = {
        "Authorization": f"Bearer {settings.openrouter_api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
    }
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post("https://openrouter.ai/api/v1/chat/completions", json=payload, headers=headers)
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]
