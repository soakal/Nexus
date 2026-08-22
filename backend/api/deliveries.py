from fastapi import APIRouter, Body, Depends, HTTPException

from backend.agents import deliveries
from backend.auth import require_api_key

router = APIRouter()


@router.get("")
async def list_deliveries_route(_=Depends(require_api_key)):
    """Every registered ExpectedDelivery — needed to find a typo'd auto-
    registered name so it can be DELETEd."""
    return await deliveries.list_deliveries()


@router.post("/{name}/heartbeat")
async def post_heartbeat(name: str, _=Depends(require_api_key)):
    """A producer pings this on every successful completion. Upserts
    last_heartbeat_at=now, auto-registering `name` (daily interval, 2h grace
    default) on its FIRST heartbeat if it isn't already known — no manual
    pre-registration step, keeps adoption low-friction. Consumed by
    watchdog.check_expected_deliveries()."""
    try:
        return await deliveries.record_heartbeat(name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.patch("/{name}")
async def patch_delivery(name: str, body: dict = Body(...), _=Depends(require_api_key)):
    """Tune expected_interval_minutes / grace_minutes for a delivery whose
    real cadence isn't the daily default auto-registration assumed."""
    fields: dict = {}
    for key in ("expected_interval_minutes", "grace_minutes"):
        if key in body:
            v = body[key]
            if isinstance(v, bool) or not isinstance(v, int) or v < 0:
                raise HTTPException(status_code=400, detail=f"{key} must be a non-negative integer")
            fields[key] = v
    if not fields:
        raise HTTPException(
            status_code=400, detail="expected_interval_minutes and/or grace_minutes required"
        )
    row = await deliveries.update_delivery(name, **fields)
    if row is None:
        raise HTTPException(status_code=404, detail="delivery not found")
    return row


@router.delete("/{name}")
async def delete_delivery_route(name: str, _=Depends(require_api_key)):
    """Deregister a delivery. Without this a single typo'd heartbeat URL
    creates a phantom producer that goes overdue and pages forever."""
    if not await deliveries.delete_delivery(name):
        raise HTTPException(status_code=404, detail="delivery not found")
    return {"deleted": True}
