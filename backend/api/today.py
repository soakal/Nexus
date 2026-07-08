import asyncio

from fastapi import APIRouter, Depends

from backend.auth import require_api_key

router = APIRouter()


@router.get("/")
async def get_today(_=Depends(require_api_key)):
    from backend.integrations.hermes import get_calendar, get_gmail
    calendar, email = await asyncio.gather(get_calendar(), get_gmail(), return_exceptions=True)
    return {
        "calendar": calendar if not isinstance(calendar, Exception) else "(unavailable)",
        "email": email if not isinstance(email, Exception) else "(unavailable)",
    }


@router.get("/home-state")
async def get_home_state(_=Depends(require_api_key)):
    """Passive glance card data: notable locks/doors + alert count. Never 5xx —
    a degraded/unreachable HA integration just reports available=False."""
    from backend.agents.chat import extract_home_state
    from backend.integrations import homeassistant
    try:
        ha = await homeassistant.fetch()
    except Exception:
        ha = Exception("unavailable")
    return extract_home_state(ha)
