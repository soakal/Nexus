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
