from fastapi import APIRouter, Depends

from backend.agents import deliveries
from backend.auth import require_api_key

router = APIRouter()


@router.post("/{name}/heartbeat")
async def post_heartbeat(name: str, _=Depends(require_api_key)):
    """A producer pings this on every successful completion. Upserts
    last_heartbeat_at=now, auto-registering `name` (daily interval, 2h grace
    default) on its FIRST heartbeat if it isn't already known — no manual
    pre-registration step, keeps adoption low-friction. Consumed by
    watchdog.check_expected_deliveries()."""
    return await deliveries.record_heartbeat(name)
