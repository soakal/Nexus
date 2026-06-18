import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

import httpx

from backend.cache import async_ttl_cache

logger = logging.getLogger(__name__)

# Pending-delivery retry policy. The scheduler calls deliver_pending() every 60s;
# these bound how aggressively a failing row is retried and when it is given up on.
_BACKOFF_BASE_SECONDS = 60      # first retry waits ~60s after the first failure
_BACKOFF_CAP_SECONDS = 3600     # exponential backoff ceiling (1 hour)
_MAX_ATTEMPTS = 8               # dead-letter cap: stop loading a row after this many tries


def _next_eligible(attempts: int, last_attempt: datetime | None) -> datetime:
    """Earliest UTC time a failed delivery may be retried, via exponential backoff.

    A never-attempted row (or one with a non-positive attempt count) is eligible
    immediately. Otherwise the delay doubles per attempt: 60s, 120s, 240s, ...,
    capped at _BACKOFF_CAP_SECONDS.
    """
    if last_attempt is None or attempts <= 0:
        return datetime.min
    delay = min(_BACKOFF_BASE_SECONDS * (2 ** (attempts - 1)), _BACKOFF_CAP_SECONDS)
    return last_attempt + timedelta(seconds=delay)


@dataclass
class HermesStatus:
    alive: bool = False
    last_seen: datetime | None = None
    pending_actions: int = 0


async def get_status() -> HermesStatus:
    from backend.config import get_settings
    settings = get_settings()
    try:
        headers = {"X-Webhook-Secret": settings.hermes_webhook_secret}
    except Exception:
        headers = {}
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(f"{settings.hermes_host}/hermes/status", headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                last_seen_str = data.get("last_seen")
                last_seen = datetime.fromisoformat(last_seen_str) if last_seen_str else None
                return HermesStatus(alive=True, last_seen=last_seen, pending_actions=data.get("pending_actions", 0))
    except Exception:
        pass
    return HermesStatus(alive=False)


async def fetch() -> HermesStatus:
    return await get_status()


@async_ttl_cache(30)
async def health_check() -> bool:
    status = await get_status()
    return status.alive


async def notify(payload: dict) -> bool:
    from backend.config import get_settings
    settings = get_settings()
    try:
        headers = {"X-Webhook-Secret": settings.hermes_webhook_secret, "Content-Type": "application/json"}
    except Exception:
        headers = {"Content-Type": "application/json"}

    try:
        # 30s (not 5s): a full daily briefing is several KB and Hermes forwards it
        # to Telegram before replying, which can exceed 5s on the ~2s-latency LXC
        # link — a false timeout marks it failed, queues it, and the retry re-sends
        # to Telegram (the briefing-spam root cause). 30s comfortably covers it.
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(f"{settings.hermes_host}/hermes/notify", json=payload, headers=headers)
            if resp.status_code in (200, 201, 204):
                return True
            if resp.status_code in (401, 403):
                logger.error(
                    f"Hermes notify AUTH FAILED (HTTP {resp.status_code}) — "
                    "HERMES_WEBHOOK_SECRET is missing or wrong. Retrying will NOT help. NOT queuing."
                )
                return False
            logger.warning(f"Hermes notify returned HTTP {resp.status_code}; queuing for retry")
    except Exception as e:
        logger.warning(f"Hermes notify failed, queuing: {e}")

    # Queue for retry (auth failures return early above and never reach here)
    _queue_delivery(payload, "notify")
    return False


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


async def action(payload: dict) -> bool:
    from backend.config import get_settings
    settings = get_settings()
    try:
        headers = {"X-Webhook-Secret": settings.hermes_webhook_secret, "Content-Type": "application/json"}
    except Exception:
        headers = {"Content-Type": "application/json"}

    try:
        async with httpx.AsyncClient(timeout=30) as client:  # 30s: Hermes->Telegram round-trip can be slow
            resp = await client.post(f"{settings.hermes_host}/hermes/action", json=payload, headers=headers)
            if resp.status_code not in (200, 201, 204):
                return False
            # A 2xx with a structured {"ok": false} body is a Hermes-side action
            # failure (e.g. Proxmox returned 500) — surface it as False even though
            # the HTTP call landed. Body parse failures fall back to the 2xx result.
            try:
                return _ok_from_action_json(resp.json())
            except Exception:
                return True
    except Exception:
        _queue_delivery(payload, "action")
        return False


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
    except Exception:
        pass
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
            except Exception:
                return {"ok": True, "response": "(Hermes returned a non-JSON response.)", "intent": None}
            return {
                "ok": _ok_from_action_json(data),
                "response": data.get("response") or "(Hermes returned no response.)",
                "intent": data.get("intent"),
            }
    except Exception as e:
        return {"ok": False, "response": f"Hermes is not reachable right now: {e}", "intent": None}


def _queue_delivery(payload: dict, delivery_type: str) -> None:
    try:
        from sqlmodel import Session

        from backend.database import PendingDelivery, engine
        with Session(engine) as session:
            session.add(PendingDelivery(payload_json=json.dumps(payload), delivery_type=delivery_type))
            session.commit()
    except Exception as e:
        logger.error(f"Failed to queue delivery: {e}")


def _load_pending() -> list[dict]:
    """Read up to 10 retry-eligible pending deliveries off the event loop. Returns
    plain dicts so no ORM objects (or DB session) cross the await boundary.

    Rows are scanned oldest-first (FIFO by created_at). A row is skipped if it has
    hit the dead-letter cap (attempts >= _MAX_ATTEMPTS) or if its exponential-backoff
    window has not yet elapsed. This stops a permanently-failing 'poison' row from
    occupying a delivery slot every cycle and starving newer deliveries."""
    from sqlmodel import Session, select

    from backend.database import PendingDelivery, engine

    now = datetime.utcnow()
    with Session(engine) as session:
        # Pull a candidate window larger than 10 since some will be filtered out as
        # not-yet-eligible; oldest first so the backlog drains fairly.
        rows = session.exec(
            select(PendingDelivery).order_by(PendingDelivery.created_at).limit(50)
        ).all()
        eligible: list[dict] = []
        for r in rows:
            if r.attempts >= _MAX_ATTEMPTS:
                continue
            if _next_eligible(r.attempts, r.last_attempt) > now:
                continue
            eligible.append(
                {
                    "id": r.id,
                    "payload_json": r.payload_json,
                    "delivery_type": r.delivery_type,
                    "attempts": r.attempts,
                    "last_attempt": r.last_attempt,
                }
            )
            if len(eligible) >= 10:
                break
        return eligible


def _apply_pending_results(delivered_ids: list[int], failed_ids: list[int]) -> None:
    """Apply delivery outcomes off the event loop in a single short transaction."""
    from datetime import datetime

    from sqlmodel import Session, select

    from backend.database import PendingDelivery, engine

    if not delivered_ids and not failed_ids:
        return

    with Session(engine) as session:
        if delivered_ids:
            for delivery in session.exec(
                select(PendingDelivery).where(PendingDelivery.id.in_(delivered_ids))
            ).all():
                session.delete(delivery)
        if failed_ids:
            now = datetime.utcnow()
            for delivery in session.exec(
                select(PendingDelivery).where(PendingDelivery.id.in_(failed_ids))
            ).all():
                delivery.attempts += 1
                delivery.last_attempt = now
                # Fires exactly once: once at/over the cap the row is no longer
                # loaded by _load_pending(), so it can't be incremented again.
                if delivery.attempts >= _MAX_ATTEMPTS:
                    logger.warning(
                        f"Dead-lettering pending delivery id={delivery.id} "
                        f"type={delivery.delivery_type} after {delivery.attempts} attempts"
                    )
        session.commit()


@async_ttl_cache(120)
async def get_gmail() -> str:
    from backend.config import get_settings
    settings = get_settings()
    headers = {}
    try:
        headers["X-Webhook-Secret"] = settings.hermes_webhook_secret
    except Exception:
        pass
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(f"{settings.hermes_host}/hermes/gmail", headers=headers)
            if resp.status_code == 200:
                return resp.json().get("summary") or "Inbox: 0 unread"
            return f"(Gmail unavailable: HTTP {resp.status_code})"
    except Exception as e:
        return f"(Gmail unavailable: {e})"


@async_ttl_cache(120)
async def get_calendar() -> str:
    from backend.config import get_settings
    settings = get_settings()
    headers = {}
    try:
        headers["X-Webhook-Secret"] = settings.hermes_webhook_secret
    except Exception:
        pass
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(f"{settings.hermes_host}/hermes/calendar", headers=headers)
            if resp.status_code == 200:
                return resp.json().get("events") or "No events in the next 2 days."
            return f"(Calendar unavailable: HTTP {resp.status_code})"
    except Exception as e:
        return f"(Calendar unavailable: {e})"


async def relay(message: str) -> str:
    from backend.config import get_settings
    settings = get_settings()
    headers = {"Content-Type": "application/json"}
    try:
        headers["X-Webhook-Secret"] = settings.hermes_webhook_secret
    except Exception:
        pass
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(f"{settings.hermes_host}/hermes/action", json={"message": message}, headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                return data.get("response") or "(Hermes returned no response.)"
            return f"Hermes returned HTTP {resp.status_code}."
    except Exception as e:
        return f"Hermes is not reachable right now: {e}"


def delivery_queue_health() -> dict:
    """Sync helper: delivery queue stats for the safety status endpoint.

    Run via asyncio.to_thread — never called directly from the event loop.
    Returns pending_count, oldest_age_seconds, dead_lettered_count, secret_present.
    Never raises.
    """
    try:
        from sqlmodel import Session, select
        from backend.database import PendingDelivery, engine
        from backend.config import get_settings

        settings = get_settings()
        try:
            secret_present = bool(settings.hermes_webhook_secret)
        except Exception:
            secret_present = False

        with Session(engine) as session:
            rows = session.exec(select(PendingDelivery)).all()
            pending_count = len(rows)
            dead_lettered_count = sum(1 for r in rows if r.attempts >= _MAX_ATTEMPTS)
            oldest_age_seconds = (
                int((datetime.utcnow() - min(r.created_at for r in rows)).total_seconds())
                if rows else None
            )

        return {
            "pending_count": pending_count,
            "oldest_age_seconds": oldest_age_seconds,
            "dead_lettered_count": dead_lettered_count,
            "secret_present": secret_present,
        }
    except Exception:
        return {
            "pending_count": 0,
            "oldest_age_seconds": None,
            "dead_lettered_count": 0,
            "secret_present": False,
        }


async def deliver_pending() -> None:
    # All synchronous SQLite I/O is pushed to a worker thread so the asyncio
    # event loop is never blocked by SQLite contention on Windows.
    pending = await asyncio.to_thread(_load_pending)
    if not pending:
        return

    from backend.config import get_settings
    settings = get_settings()
    headers = {"Content-Type": "application/json"}
    try:
        headers["X-Webhook-Secret"] = settings.hermes_webhook_secret
    except Exception:
        pass

    delivered_ids: list[int] = []
    failed_ids: list[int] = []

    async with httpx.AsyncClient(timeout=30) as client:  # 30s: large queued briefings need headroom (see notify)
        for delivery in pending:
            try:
                payload = json.loads(delivery["payload_json"])
                endpoint = f"{settings.hermes_host}/hermes/{delivery['delivery_type']}"
                resp = await client.post(endpoint, json=payload, headers=headers)
                if resp.status_code in (200, 201, 204):
                    delivered_ids.append(delivery["id"])
                elif resp.status_code in (401, 403):
                    logger.error(
                        f"Retry delivery id={delivery['id']} type={delivery['delivery_type']} "
                        f"AUTH FAILED (HTTP {resp.status_code}) — "
                        "HERMES_WEBHOOK_SECRET missing/wrong; will not succeed on retry"
                    )
                    failed_ids.append(delivery["id"])
                else:
                    logger.warning(
                        f"Retry delivery id={delivery['id']} type={delivery['delivery_type']} "
                        f"got HTTP {resp.status_code} (attempt {delivery['attempts'] + 1})"
                    )
                    failed_ids.append(delivery["id"])
            except Exception as e:
                logger.warning(
                    f"Retry delivery id={delivery['id']} type={delivery['delivery_type']} failed: {e}"
                )
                failed_ids.append(delivery["id"])

    total = len(pending)
    d, f = len(delivered_ids), len(failed_ids)
    if f and not d:
        logger.error(f"Hermes delivery cycle: 0/{total} delivered, {f} still failing")
    elif f:
        logger.warning(f"Hermes delivery cycle: {d}/{total} delivered, {f} failed")
    else:
        logger.info(f"Hermes delivery cycle: {d}/{total} delivered")

    await asyncio.to_thread(_apply_pending_results, delivered_ids, failed_ids)
