"""Best-effort live event publisher (Tier 3 gate-blocker #5).

Publishes JSON events to all connected /ws/logs websocket clients so an away user
sees agent actions in real time. Best-effort: a broadcast failure NEVER propagates
to the broker/governor that called it."""
import json
import logging

logger = logging.getLogger(__name__)


async def publish(event_type: str, payload: dict) -> None:
    try:
        from backend.api.agents import ws_manager  # lazy import avoids an import cycle
        msg = json.dumps({"type": event_type, **payload})
        await ws_manager.broadcast(msg)
    except Exception as e:  # never break the caller
        logger.debug(f"events.publish failed (ignored): {e}")
