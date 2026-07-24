import asyncio
import json
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator

from backend.auth import require_api_key
from backend.cache import async_ttl_cache

logger = logging.getLogger(__name__)

router = APIRouter()


@async_ttl_cache(30)
async def _dashboard_inbox() -> dict:
    """Slim, arg-less (async_ttl_cache doesn't key by arguments) snapshot for the
    Dashboard mail card: up to 10 recent emails + an unread count.
    """
    from backend.integrations import protonmail

    list_text, unread_text = await asyncio.gather(
        protonmail.list_recent(limit=10),
        protonmail.list_recent(unread_only=True, limit=1),
    )
    data = json.loads(list_text)
    unread_data = json.loads(unread_text)
    emails = [
        {
            "email_id": e.get("email_id"),
            "subject": e.get("subject"),
            "sender": e.get("sender"),
            "date": e.get("date"),
        }
        for e in (data.get("emails") or [])
    ]
    return {
        "emails": emails,
        "total": data.get("total", len(emails)),
        "unread": unread_data.get("total", 0),
    }


class SendEmail(BaseModel):
    recipients: list[str]
    subject: str
    body: str
    cc: list[str] | None = None
    bcc: list[str] | None = None
    in_reply_to: str | None = None
    references: str | None = None

    @field_validator("recipients")
    @classmethod
    def _recipients_non_empty(cls, v):
        if not v:
            raise ValueError("recipients must not be empty")
        return v

    @field_validator("subject", "body")
    @classmethod
    def _non_blank(cls, v):
        if not v or not v.strip():
            raise ValueError("must not be blank")
        return v


@router.post("/send")
async def send_email(body: SendEmail, _=Depends(require_api_key)):
    """Send a Proton Mail email (broker-gated, actor=user -> always allowed + audit-logged)."""
    from backend.safety.broker import Decision, execute_action

    payload = {
        "recipients": body.recipients,
        "subject": body.subject,
        "body": body.body,
        "cc": body.cc,
        "bcc": body.bcc,
        "in_reply_to": body.in_reply_to,
        "references": body.references,
    }
    res = await execute_action(
        actor="user",
        kind="protonmail_send",
        target=body.recipients[0],
        payload=payload,
    )
    if res.decision == Decision.EXECUTED:
        return {"ok": True, "result": res.result}
    raise HTTPException(
        status_code=502,
        detail=f"Send failed: {res.error or res.decision.value}",
    )


class EmailAction(BaseModel):
    email_id: str
    mailbox: str | None = None

    @field_validator("email_id")
    @classmethod
    def _non_blank_id(cls, v):
        if not v or not v.strip():
            raise ValueError("email_id must not be blank")
        return v


@router.post("/archive")
async def archive_email(body: EmailAction, _=Depends(require_api_key)):
    """Archive a Proton Mail email (broker-gated, LOW/REVERSIBLE_BY_INVERSE)."""
    from backend.safety.broker import Decision, execute_action

    res = await execute_action(
        actor="user",
        kind="protonmail_archive",
        target=body.email_id,
        payload={"email_id": body.email_id, "mailbox": body.mailbox},
    )
    if res.decision == Decision.EXECUTED:
        _dashboard_inbox.invalidate()
        return {"ok": True, "result": res.result}
    raise HTTPException(status_code=502, detail=f"Archive failed: {res.error or res.decision.value}")


@router.post("/delete")
async def delete_email(body: EmailAction, _=Depends(require_api_key)):
    """Move a Proton Mail email to Trash (broker-gated, LOW/REVERSIBLE_BY_INVERSE — see broker.py)."""
    from backend.safety.broker import Decision, execute_action

    res = await execute_action(
        actor="user",
        kind="protonmail_delete",
        target=body.email_id,
        payload={"email_id": body.email_id, "mailbox": body.mailbox},
    )
    if res.decision == Decision.EXECUTED:
        _dashboard_inbox.invalidate()
        return {"ok": True, "result": res.result}
    raise HTTPException(status_code=502, detail=f"Delete failed: {res.error or res.decision.value}")


@router.get("/status")
async def protonmail_status(_=Depends(require_api_key)):
    from backend.integrations import protonmail

    return {"reachable": await protonmail.health_check()}


@router.get("/inbox")
async def protonmail_inbox(_=Depends(require_api_key)):
    try:
        return await _dashboard_inbox()
    except Exception as e:
        logger.warning(f"Proton Mail inbox fetch failed: {e}")
        raise HTTPException(status_code=502, detail=f"Proton Mail unreachable: {e}")


@router.get("/email/{email_id}")
async def protonmail_email(email_id: str, mailbox: str | None = None, page: int = 1, _=Depends(require_api_key)):
    from backend.integrations import protonmail

    try:
        content = await protonmail.read_email(email_id, page=page, mailbox=mailbox)
        data = json.loads(content)
    except Exception as e:
        logger.warning(f"Proton Mail email fetch failed: {e}")
        raise HTTPException(status_code=502, detail=f"Proton Mail unreachable: {e}")

    emails = data.get("emails") or []
    if not emails:
        raise HTTPException(status_code=404, detail="Email not found")
    return emails[0]
