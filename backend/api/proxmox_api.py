from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from backend.auth import require_api_key

router = APIRouter()


class VmPowerAction(BaseModel):
    action: str  # "start" | "stop" | "reboot"


@router.get("/")
async def get_proxmox_data(_=Depends(require_api_key)):
    from backend.integrations.proxmox import fetch
    return await fetch()


@router.get("/maintenance")
async def get_proxmox_maintenance(_=Depends(require_api_key)):
    """Pending-updates + last-backup status for the dashboard card badges.

    Never 5xx's on a PVE-side failure -- each part independently degrades to
    None so one flaky endpoint (e.g. apt) doesn't blank the other (backups)."""
    from backend.integrations.proxmox import fetch_updates, fetch_backups

    try:
        updates = await fetch_updates()
    except Exception:
        updates = None

    try:
        backup = await fetch_backups()
    except Exception:
        backup = None

    return {"updates": updates, "backup": backup}


@router.post("/vm/{vmid}/power")
async def vm_power(vmid: int, body: VmPowerAction, _=Depends(require_api_key)):
    """Start/stop/reboot a Proxmox VM or LXC (broker-gated).

    See backend/safety/broker.py's vm_power dispatcher and
    backend/integrations/proxmox.py::set_vm_power.
    """
    from backend.safety.broker import Decision, execute_action

    res = await execute_action(
        actor="user",
        kind="vm_power",
        target=str(vmid),
        payload={"vmid": vmid, "action": body.action},
    )
    if res.decision == Decision.EXECUTED:
        return {"ok": True, "result": res.result}
    raise HTTPException(
        status_code=502,
        detail=f"VM power action failed: {res.error or res.decision.value}",
    )
