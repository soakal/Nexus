"""Best-effort live event publisher (Tier 3 gate-blocker #5).

Publishes JSON events to all connected /ws/logs websocket clients so an away user
sees agent actions in real time. Best-effort: a broadcast failure NEVER propagates
to the broker/governor that called it."""
import json
import logging

logger = logging.getLogger(__name__)

# Every `kind` string that any notify_phone() call site in backend/ actually
# passes today. Hand-maintained on purpose (same discipline as
# backend/safety/contracts.py's CONTRACTS) -- kept honest by
# tests/test_autonomy_notify.py::test_notify_kinds_registry_covers_every_call_site,
# which AST-scans backend/ and fails the suite on an unregistered kind OR on
# any dynamically-built kind. Adding a new notify kind means adding it here.
#
# Used by governor.add_muted_notify_kind to reject a typo'd /mute instead of
# silently muting a string that nothing ever fires.
NOTIFY_KINDS: frozenset[str] = frozenset({
    "agent_message", "anthropic_balance_watch", "anthropic_credit_exhausted",
    "anthropic_usage_limit_exceeded",
    "auth_burst", "auto_approved",
    "autonomy_alert", "autonomy_digest", "backup_failed", "budget_warn",
    "calibration_suppress", "circuit_breaker", "contract_breach",
    "council_postmortem", "dead_letter", "deploy_drift", "flag_followup",
    "goal_criteria_failed", "goal_failed", "goal_proposed", "homelab_array",
    "homelab_backup_failed", "homelab_digest", "homelab_disk_temp",
    "homelab_docker_stopped", "homelab_expected_mismatch", "homelab_garage",
    "homelab_recovered", "homelab_vm_stopped", "incident_diagnosis",
    "mail_draft_created", "needs_confirm", "obligation_due", "scheduler_stall",
    "soak_reminder", "spend_report", "stale_delivery", "throttled",
    "task_completed", "task_failed", "trial_verdict", "weekly_review",
})


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
    """Best-effort phone push via NEXUS's own Telegram bot. Gated by phone_notifications_enabled.

    Appends a deep-link to the Safety page when app_base_url is configured so
    Brian can tap straight through to Safety from any alert.

    `buttons` (optional): [{"text": ..., "callback_data": ...}] — rendered as a
    Telegram inline keyboard on the last chunk of the message.

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
        # Runtime per-kind mute (Telegram /mute), distinct from the static
        # .env-configured phone_suppressed_kinds above. Own try/except: a DB
        # hiccup here must degrade to "not muted", never to "alert dropped" —
        # this gates auth_burst/contract_breach/budget_warn/needs_confirm,
        # exactly the pages documented elsewhere as un-suppressible.
        import asyncio
        from backend.safety import governor
        try:
            muted = await asyncio.to_thread(governor.get_muted_notify_kinds)
        except Exception as e:
            logger.warning(f"Reading muted_notify_kinds failed (treating as unmuted): {e}")
            muted = set()
        if kind in muted:
            return False
        # Append deep-link when a base URL is configured.
        base = str(getattr(settings, "app_base_url", "") or "").strip().rstrip("/")
        parse_mode = None
        if base:
            url = f"{base}/safety"
            # HTML link so Telegram renders it clickable even for non-dotted hostnames
            # (bare hostnames like nexus-lxc aren't auto-detected as URLs in plain text).
            content = f"{content}\n<a href=\"{url}\">Open Safety</a>"
            parse_mode = "HTML"
        from datetime import datetime
        from backend.integrations import telegram
        payload: dict = {"type": kind, "content": content, "timestamp": datetime.utcnow().isoformat()}
        if parse_mode:
            payload["parse_mode"] = parse_mode
        if buttons:
            payload["buttons"] = buttons
        return await telegram.notify(payload)
    except Exception as e:
        logger.debug(f"events.notify_phone failed (ignored): {e}")
        return False
