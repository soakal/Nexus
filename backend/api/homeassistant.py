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
