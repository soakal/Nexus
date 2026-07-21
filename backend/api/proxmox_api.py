from fastapi import APIRouter, Depends

from backend.auth import require_api_key

router = APIRouter()


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
