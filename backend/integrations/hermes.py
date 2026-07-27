"""Hermes action-relay bridge + liveness probe.

Phase 1 Hermes decoupling (2026-07) moved Telegram notify/inbound-buttons and
calendar reads directly into NEXUS (backend/integrations/telegram.py,
backend/integrations/calendar.py, backend/agents/telegram_poller.py).
Deliberately kept here: NEXUS still asks Hermes to execute action-relay verbs
(vm_action, docker_prune, unifi_block, etc. — see backend/safety/
hermes_actions.py's allowlist) via Hermes's own SSH/Proxmox access, which
NEXUS does not have.
"""
import logging
from dataclasses import dataclass
from datetime import datetime

import httpx

from backend.cache import async_ttl_cache

logger = logging.getLogger(__name__)


@dataclass
class HermesStatus:
    alive: bool = False
    last_seen: datetime | None = None
    pending_actions: int = 0


@async_ttl_cache(30)
async def get_status() -> HermesStatus:
    from backend.config import get_settings
    settings = get_settings()
    try:
        headers = {"X-Webhook-Secret": settings.hermes_webhook_secret}
    except Exception as e:
        logger.warning(f"hermes status: webhook secret unavailable, probing unauthenticated: {e}")
        headers = {}
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(f"{settings.hermes_host}/hermes/status", headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                last_seen_str = data.get("last_seen")
                last_seen = datetime.fromisoformat(last_seen_str) if last_seen_str else None
                return HermesStatus(alive=True, last_seen=last_seen, pending_actions=data.get("pending_actions", 0))
            logger.warning(f"hermes status: HTTP {resp.status_code}")
    except Exception as e:
        logger.warning(f"hermes status probe failed: {e}")
    return HermesStatus(alive=False)


@async_ttl_cache(30)
async def health_check() -> bool:
    status = await get_status()
    return status.alive


def _ok_from_action_json(data: dict) -> bool:
    """Read Hermes's structured-action success signal.

    Newer Hermes (the #2 response-contract change) returns {"ok": bool, ...} on
    /hermes/action. Older Hermes returns only {"response": str}. So: trust an
    explicit "ok" when present; otherwise fall back to the same prefix heuristic
    Hermes uses — a result is failed only if the response text starts with
    "error" (case-insensitive). Absent/blank body degrades to True (HTTP 2xx
    already gated the call) to preserve back-compat before Brian deploys #2.
    """
    if not isinstance(data, dict):
        return True
    if isinstance(data.get("ok"), bool):
        return data["ok"]
    text = (data.get("response") or "").strip().lower()
    if not text:
        return True
    return not text.startswith("error")


async def relay_action(message: str, idempotency_key: str | None = None) -> dict:
    """Structured relay used by the broker for agent/autonomous actions.

    Unlike relay() (which returns a human string and swallows transport errors
    INTO that string, so the broker can't tell a real failure from a normal
    reply), this returns Hermes's structured contract:
        {"ok": bool, "response": str, "intent": str | None}
    A transport error or non-200 yields ok=False with the detail in "response",
    so the broker records the action FAILED instead of silently "succeeding" on
    an error string. Back-compatible with pre-#2 Hermes via _ok_from_action_json.

    When idempotency_key is given it is sent as the Idempotency-Key header so a
    retry that races the broker's own dedup can't double-execute on Hermes
    (Hermes-side #7). Older Hermes simply ignores the unknown header.
    """
    from backend.config import get_settings
    settings = get_settings()
    headers = {"Content-Type": "application/json"}
    try:
        headers["X-Webhook-Secret"] = settings.hermes_webhook_secret
    except Exception as e:
        # Hermes will reject an unsigned action — log why before we send it.
        logger.warning(f"relay_action: webhook secret unavailable: {e}")
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{settings.hermes_host}/hermes/action", json={"message": message}, headers=headers
            )
            if resp.status_code != 200:
                return {"ok": False, "response": f"Hermes returned HTTP {resp.status_code}.", "intent": None}
            try:
                data = resp.json()
            except Exception as e:
                logger.warning(f"relay_action: Hermes returned a non-JSON 200 body: {e}")
                return {"ok": True, "response": "(Hermes returned a non-JSON response.)", "intent": None}
            return {
                "ok": _ok_from_action_json(data),
                "response": data.get("response") or "(Hermes returned no response.)",
                "intent": data.get("intent"),
            }
    except Exception as e:
        logger.warning(f"relay_action transport failure: {e}")
        return {"ok": False, "response": f"Hermes is not reachable right now: {e}", "intent": None}


async def relay(message: str) -> str:
    from backend.config import get_settings
    settings = get_settings()
    headers = {"Content-Type": "application/json"}
    try:
        headers["X-Webhook-Secret"] = settings.hermes_webhook_secret
    except Exception as e:
        logger.warning(f"relay: webhook secret unavailable: {e}")
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(f"{settings.hermes_host}/hermes/action", json={"message": message}, headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                return data.get("response") or "(Hermes returned no response.)"
            logger.warning(f"relay: Hermes returned HTTP {resp.status_code}")
            return f"Hermes returned HTTP {resp.status_code}."
    except Exception as e:
        logger.warning(f"relay transport failure: {e}")
        return f"Hermes is not reachable right now: {e}"
