import logging
import re

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from backend.auth import require_api_key

logger = logging.getLogger(__name__)

router = APIRouter()

# Matches a bare/underscore-separated 12-hex-char MAC embedded in an entity_id
# or an attributes.mac value (e.g. "device_tracker.aa_bb_cc_dd_ee_ff" or
# "AA:BB:CC:DD:EE:FF") -- see _resolve_device_tracker_name below.
_MAC_CANDIDATE_RE = re.compile(r"(?:[0-9a-f]{2}[:_-]?){5}[0-9a-f]{2}", re.IGNORECASE)


async def _resolve_device_tracker_name(entity_id: str, attributes: dict | None) -> str | None:
    """Best-effort cross-reference of a device_tracker.* entity's raw MAC
    against UniFi's already-populated KnownDevice table
    (backend/integrations/unifi.py) -- closes part of the vault-flagged
    "15+ unidentified device tracker entries with raw MACs" gap by
    resolving a hostname where UniFi has already seen the same device on
    the network. Returns None on any miss (not a device_tracker, no MAC
    found, MAC not known to UniFi, or any error) -- this is an
    annotation only and must never break the triage list.
    """
    if not entity_id.startswith("device_tracker."):
        return None
    try:
        from backend.integrations.unifi import _normalize_mac

        candidate = (attributes or {}).get("mac") or entity_id.split(".", 1)[1]
        match = _MAC_CANDIDATE_RE.search(candidate)
        if not match:
            return None
        # _normalize_mac only strips ':'/'-'/'.' separators (real UniFi/MQTT
        # MAC forms) -- HA entity_ids use '_' instead (HA restricts
        # entity_id to lowercase alnum + underscore), so strip everything
        # down to bare hex here before handing it off.
        bare_hex = re.sub(r"[^0-9a-fA-F]", "", match.group(0))
        mac = _normalize_mac(bare_hex)

        from sqlmodel import Session, select

        from backend.database import KnownDevice, engine
        with Session(engine) as session:
            dev = session.exec(select(KnownDevice).where(KnownDevice.mac == mac)).first()
        return dev.hostname if dev and dev.hostname else None
    except Exception:
        return None


class ServiceCall(BaseModel):
    domain: str
    service: str
    entity_id: str
    # Extra service fields (e.g. {"temperature": 72}); entity_id is merged in
    # server-side so the broker's empty-service_data fallback stays untouched.
    service_data: dict | None = None


@router.get("/entities")
async def get_entities(_=Depends(require_api_key)):
    """Return the full Home Assistant entity list."""
    from backend.integrations.homeassistant import IntegrationError, fetch
    try:
        data = await fetch()
    except IntegrationError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        logger.warning(f"HA entities fetch failed: {e}")
        raise HTTPException(status_code=502, detail=f"Home Assistant unreachable: {e}")
    return {
        "entities": data.entities,
        "alerts": data.alerts,
        "cloud_alerts": data.cloud_alerts,
        "last_updated": data.last_updated.isoformat(),
    }


@router.post("/service")
async def call_ha_service(body: ServiceCall, _=Depends(require_api_key)):
    """Invoke a Home Assistant service against a single entity (broker-gated)."""
    from backend.safety.broker import Decision, execute_action
    payload = {"domain": body.domain, "service": body.service}
    if body.service_data:
        payload["service_data"] = {"entity_id": body.entity_id, **body.service_data}
    res = await execute_action(
        actor="user",
        kind="ha_service",
        target=body.entity_id,
        payload=payload,
    )
    if res.decision == Decision.EXECUTED:
        return {"ok": True, "result": res.result}
    raise HTTPException(
        status_code=502,
        detail=f"Service call failed: {res.error or res.decision.value}",
    )


@router.get("/unavailable")
async def get_unavailable(_=Depends(require_api_key)):
    """Triage list: per-entity unavailable duration, from the tracking table
    (backend/database.py::HaEntityUnavailable) -- distinguishes a
    just-restarted transient from a permanently orphaned entity, unlike a
    flat count. Includes suppressed ("known-dead, dismissed") rows too, so
    the UI can render them greyed-out with an undo, not just omit them.
    Each device_tracker.* item also gets a best-effort `resolved_name` from
    UniFi's KnownDevice table (see _resolve_device_tracker_name above).
    """
    from backend.integrations.homeassistant import fetch, unavailable_report
    report = await unavailable_report()
    try:
        ha_data = await fetch()
        by_id = {e.get("entity_id"): e for e in ha_data.entities}
    except Exception as e:
        logger.warning(f"get_unavailable: HA entity fetch failed, resolved_name omitted: {e}")
        by_id = {}
    for item in report["items"]:
        ent = by_id.get(item["entity_id"]) or {}
        item["resolved_name"] = await _resolve_device_tracker_name(
            item["entity_id"], ent.get("attributes"),
        )
    return report


@router.post("/unavailable/{entity_id}/suppress")
async def suppress_unavailable(entity_id: str, _=Depends(require_api_key)):
    """Dismiss a persistently-unavailable entity -- "known-dead, stop
    counting". Excludes it from unavailable_report()'s total/persistent/
    recent counts (and therefore from the digest/briefing text) without
    deleting its tracked history."""
    from backend.integrations.homeassistant import set_unavailable_suppressed
    if not await set_unavailable_suppressed(entity_id, True):
        raise HTTPException(status_code=404, detail="entity not currently tracked as unavailable")
    return {"ok": True}


@router.post("/unavailable/{entity_id}/unsuppress")
async def unsuppress_unavailable(entity_id: str, _=Depends(require_api_key)):
    """Undo a suppress -- the entity resumes counting toward
    unavailable_report() (only meaningful while it's still actually
    unavailable; a recovered entity's row is deleted on its next poll
    regardless of this flag)."""
    from backend.integrations.homeassistant import set_unavailable_suppressed
    if not await set_unavailable_suppressed(entity_id, False):
        raise HTTPException(status_code=404, detail="entity not currently tracked as unavailable")
    return {"ok": True}


@router.post("/reload-cloud")
async def reload_cloud(_=Depends(require_api_key)):
    """Reload the Home Assistant Cloud integration (broker-gated)."""
    from backend.safety.broker import Decision, execute_action

    # First attempt: reload the specific cloud config entry
    res = await execute_action(
        actor="user",
        kind="ha_service",
        target="cloud",
        payload={
            "domain": "homeassistant",
            "service": "reload_config_entry",
            "service_data": {"entry_id": "cloud"},
        },
    )
    if res.decision == Decision.EXECUTED:
        return {"ok": True, "result": res.result}

    logger.warning(f"HA Cloud entry reload failed (broker), falling back: {res.error or res.decision.value}")

    # Fallback: reload the whole HA Cloud component
    res2 = await execute_action(
        actor="user",
        kind="ha_service",
        target="cloud",
        payload={"domain": "cloud", "service": "reload", "service_data": {}},
    )
    if res2.decision == Decision.EXECUTED:
        return {"ok": True, "result": res2.result}
    raise HTTPException(
        status_code=502,
        detail=f"Cloud reload failed: {res2.error or res2.decision.value}",
    )
