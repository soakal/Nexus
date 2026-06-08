import json
import logging
from dataclasses import dataclass
from datetime import datetime

import httpx

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


async def deliver_pending() -> None:
    from datetime import datetime

    from sqlmodel import Session, select

    from backend.database import PendingDelivery, engine

    with Session(engine) as session:
        pending = session.exec(select(PendingDelivery).limit(10)).all()
        for delivery in pending:
            try:
                payload = json.loads(delivery.payload_json)
                from backend.config import get_settings
                settings = get_settings()
                headers = {"Content-Type": "application/json"}
                try:
                    headers["X-Webhook-Secret"] = settings.hermes_webhook_secret
                except Exception:
                    pass

                endpoint = f"{settings.hermes_host}/hermes/{delivery.delivery_type}"
                async with httpx.AsyncClient(timeout=5) as client:
                    resp = await client.post(endpoint, json=payload, headers=headers)
                    if resp.status_code in (200, 201, 204):
                        session.delete(delivery)
                    else:
                        delivery.attempts += 1
                        delivery.last_attempt = datetime.utcnow()
            except Exception as e:
                logger.warning(f"Retry delivery failed: {e}")
                delivery.attempts += 1
                delivery.last_attempt = datetime.utcnow()
        session.commit()
