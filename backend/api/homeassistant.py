import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from backend.auth import require_api_key

logger = logging.getLogger(__name__)

router = APIRouter()


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
