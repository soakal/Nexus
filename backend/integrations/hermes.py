import asyncio
import json
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


@async_ttl_cache(12)
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
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.post(f"{settings.hermes_host}/hermes/notify", json=payload, headers=headers)
            if resp.status_code in (200, 201, 204):
                return True
    except Exception as e:
        logger.warning(f"Hermes notify failed, queuing: {e}")

    # Queue for retry
    _queue_delivery(payload, "notify")
    return False


async def action(payload: dict) -> bool:
    from backend.config import get_settings
    settings = get_settings()
    try:
        headers = {"X-Webhook-Secret": settings.hermes_webhook_secret, "Content-Type": "application/json"}
    except Exception:
        headers = {"Content-Type": "application/json"}

    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.post(f"{settings.hermes_host}/hermes/action", json=payload, headers=headers)
            return resp.status_code in (200, 201, 204)
    except Exception:
        _queue_delivery(payload, "action")
        return False


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
    """Read pending deliveries off the event loop. Returns plain dicts so no
    ORM objects (or DB session) cross the await boundary."""
    from sqlmodel import Session, select

    from backend.database import PendingDelivery, engine

    with Session(engine) as session:
        rows = session.exec(select(PendingDelivery).limit(10)).all()
        return [
            {
                "id": r.id,
                "payload_json": r.payload_json,
                "delivery_type": r.delivery_type,
            }
            for r in rows
        ]


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

    async with httpx.AsyncClient(timeout=5) as client:
        for delivery in pending:
            try:
                payload = json.loads(delivery["payload_json"])
                endpoint = f"{settings.hermes_host}/hermes/{delivery['delivery_type']}"
                resp = await client.post(endpoint, json=payload, headers=headers)
                if resp.status_code in (200, 201, 204):
                    delivered_ids.append(delivery["id"])
                else:
                    failed_ids.append(delivery["id"])
            except Exception as e:
                logger.warning(f"Retry delivery failed: {e}")
                failed_ids.append(delivery["id"])

    await asyncio.to_thread(_apply_pending_results, delivered_ids, failed_ids)
