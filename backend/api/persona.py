"""Persona API — read-only access to Brian's distilled writing voice, for the
separate nexus-twin repo (and any future in-NEXUS caller) to build prompts
from. All endpoints require Bearer auth.
"""

from fastapi import APIRouter, Depends

from backend.auth import require_api_key

router = APIRouter()


@router.get("/voice-profile")
async def voice_profile(_: str = Depends(require_api_key)):
    """Cached voice-style summary (mail + Telegram samples, distilled by
    Sonnet). Never raises — degrades to the best available cached value; see
    mail_drafts.get_voice_profile()'s own docstring."""
    from backend.agents.mail_drafts import get_voice_profile

    return {"voice": await get_voice_profile()}
