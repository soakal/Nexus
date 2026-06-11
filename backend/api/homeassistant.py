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
    """Invoke a Home Assistant service against a single entity."""
    from backend.integrations.homeassistant import call_service
    try:
        result = await call_service(
            body.domain, body.service, {"entity_id": body.entity_id}
        )
    except Exception as e:
        logger.warning(f"HA service call failed: {e}")
        raise HTTPException(status_code=502, detail=f"Service call failed: {e}")
    return {"ok": True, "result": result}


@router.post("/reload-cloud")
async def reload_cloud(_=Depends(require_api_key)):
    """Reload the Home Assistant Cloud integration."""
    from backend.integrations.homeassistant import call_service
    try:
        # Try both known cloud component entry reload approaches
        result = await call_service("homeassistant", "reload_config_entry", {"entry_id": "cloud"})
        return {"ok": True, "result": result}
    except Exception as e:
        logger.warning(f"HA Cloud entry reload failed, falling back: {e}")
        # Fallback: reload the whole HA Cloud component
        try:
            result = await call_service("cloud", "reload", {})
            return {"ok": True, "result": result}
        except Exception as e2:
            raise HTTPException(status_code=502, detail=f"Cloud reload failed: {e2}")
