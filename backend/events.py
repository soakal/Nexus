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


async def notify_phone(
    content: str, *, kind: str = "autonomy_alert", buttons: list | None = None
) -> bool:
    """Best-effort phone push via Hermes->Telegram. Gated by phone_notifications_enabled.

    Appends a deep-link to the Safety page when app_base_url is configured so
    Brian can tap straight through to Safety from any alert.

    `buttons` (optional): [{"text": ..., "callback_data": ...}] — forwarded to
    Hermes verbatim so it can render Telegram inline buttons. Older Hermes
    versions ignore the extra key (backward compatible).

    NEVER raises (a notify failure must not affect the caller). Returns delivered bool.
    """
    try:
        from backend.config import get_settings
        settings = get_settings()
        if not getattr(settings, "phone_notifications_enabled", False):
            return False
        suppressed = getattr(settings, "phone_suppressed_kinds", set())
        if kind in suppressed:
            return False
        # Append deep-link when a base URL is configured.
        base = str(getattr(settings, "app_base_url", "") or "").strip().rstrip("/")
        parse_mode = None
        if base:
            url = f"{base}/safety"
            # HTML link so Telegram renders it clickable even for non-dotted hostnames
            # (bare hostnames like win11-vm-proxmox aren't auto-detected as URLs in plain text).
            content = f"{content}\n<a href=\"{url}\">Open Safety</a>"
            parse_mode = "HTML"
        from datetime import datetime
        from backend.integrations import hermes
        payload: dict = {"type": kind, "content": content, "timestamp": datetime.utcnow().isoformat()}
        if parse_mode:
            payload["parse_mode"] = parse_mode
        if buttons:
            payload["buttons"] = buttons
        return await hermes.notify(payload)
    except Exception as e:
        logger.debug(f"events.notify_phone failed (ignored): {e}")
        return False
