"""Declared-baseline resource tracker (Docker containers / Proxmox VMs & LXCs)
-- the "what SHOULD be running" table `homelab_watch.check_expected_resources`
diffs live state against.

Closes a structural gap in homelab_watch.py's existing edge-triggered checks
(check_docker/check_proxmox_vms): those diff against in-memory state cleared
on every NEXUS restart, so a container/VM already stopped BEFORE NEXUS booted
is invisible to them forever -- exactly the "Only 3 Docker containers running
-- verify this is intentional" briefing nag that's been open since 2026-06-08
and could never resolve on its own (backend/agents/briefing.py, System-Health.md
in Brian's vault). This module is the declared, DB-persisted baseline; a
resource with no row here is simply not evaluated -- declaring a baseline is
opt-in, so Brian's intentionally-stopped Unraid containers never generate
noise merely by existing.

`outcomes.resolve_flag` calls `set_desired_state` when a human resolves an
"expected state mismatch" flag ("this is fine now") so the same mismatch
doesn't get flagged again forever.

Follows the same sync-DB-helper-via-asyncio.to_thread shape as outcomes.py:
sync `_db_*` helpers open their own Session (re-importing `engine` from
backend.database on every call so tests that monkeypatch
backend.database.engine are honoured); the public async functions call them
exclusively via asyncio.to_thread. No Session/ORM object crosses an await.
"""
import asyncio
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

VALID_KINDS = {"docker", "vm", "lxc"}
VALID_STATES = {"running", "stopped"}


def _row_to_dict(r) -> dict:
    return {
        "id": r.id,
        "kind": r.kind,
        "identifier": r.identifier,
        "desired_state": r.desired_state,
        "note": r.note,
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "updated_at": r.updated_at.isoformat() if r.updated_at else None,
    }


def _db_upsert(kind: str, identifier: str, desired_state: str, note: str | None) -> dict:
    from sqlmodel import Session, select

    from backend.database import ExpectedResource, engine

    key = f"{kind}:{identifier}"
    with Session(engine) as session:
        row = session.exec(select(ExpectedResource).where(ExpectedResource.key == key)).first()
        now = datetime.utcnow()
        if row is None:
            row = ExpectedResource(
                kind=kind, identifier=identifier, key=key,
                desired_state=desired_state, note=note,
            )
        else:
            row.desired_state = desired_state
            if note is not None:
                row.note = note
            row.updated_at = now
        session.add(row)
        session.commit()
        session.refresh(row)
        return _row_to_dict(row)


def _db_list() -> list[dict]:
    from sqlmodel import Session, select

    from backend.database import ExpectedResource, engine

    with Session(engine) as session:
        rows = session.exec(
            select(ExpectedResource).order_by(ExpectedResource.kind, ExpectedResource.identifier)
        ).all()
        return [_row_to_dict(r) for r in rows]


async def upsert(kind: str, identifier: str, desired_state: str, note: str | None = None) -> dict:
    """Declare (or update) one resource's expected state. Raises ValueError on
    an unknown kind/desired_state -- callers at a trust boundary (the REST
    route) should validate up front, but this is the one place the invariant
    is actually enforced."""
    if kind not in VALID_KINDS:
        raise ValueError(f"unknown ExpectedResource kind: {kind!r} (expected one of {sorted(VALID_KINDS)})")
    if desired_state not in VALID_STATES:
        raise ValueError(f"unknown desired_state: {desired_state!r} (expected one of {sorted(VALID_STATES)})")
    return await asyncio.to_thread(_db_upsert, kind, identifier, desired_state, note)


async def set_desired_state(kind: str, identifier: str, desired_state: str) -> dict | None:
    """Best-effort variant used by outcomes.resolve_flag when a human resolves
    a mismatch flag -- NEVER raises (a DB hiccup here must not surface out of
    the flag-resolve path)."""
    try:
        return await upsert(kind, identifier, desired_state)
    except Exception as e:
        logger.warning(f"set_desired_state failed ({kind}:{identifier} -> {desired_state}): {e}")
        return None


async def list_expected() -> list[dict]:
    """All declared rows. NEVER raises -- callers (briefing, homelab_watch)
    degrade to 'no baseline declared' on any DB error rather than blocking."""
    try:
        return await asyncio.to_thread(_db_list)
    except Exception as e:
        logger.warning(f"list_expected failed: {e}")
        return []


async def seed_from_live() -> dict:
    """One-time (or re-runnable) baseline snapshot: upserts an ExpectedResource
    row for every currently-observed Docker container / Proxmox VM+LXC, with
    desired_state = whatever it's observed at right now -- so the feature has
    a real baseline on day one instead of being useless until manually
    populated. Safe to re-run (upsert, not insert-only). Each integration
    read degrades independently; never raises."""
    seeded = {"docker": 0, "vm": 0, "lxc": 0, "errors": []}

    try:
        from backend.integrations import unraid
        data = await unraid.fetch()
        for c in data.docker_containers:
            name = c.get("name")
            if not name:
                continue
            state = "running" if (c.get("state") or "").upper() == "RUNNING" else "stopped"
            await upsert("docker", name, state, note="seeded from live state")
            seeded["docker"] += 1
    except Exception as e:
        logger.warning(f"seed_from_live: unraid fetch failed (ignored): {e}")
        seeded["errors"].append(f"docker: {e}")

    try:
        from backend.integrations import proxmox
        pdata = await proxmox.fetch()
        for v in pdata.vms:
            vmid = v.get("vmid")
            if vmid is None:
                continue
            kind = "lxc" if v.get("type") == "lxc" else "vm"
            state = "running" if v.get("status") == "running" else "stopped"
            await upsert(kind, str(vmid), state, note="seeded from live state")
            seeded[kind] += 1
    except Exception as e:
        logger.warning(f"seed_from_live: proxmox fetch failed (ignored): {e}")
        seeded["errors"].append(f"vm/lxc: {e}")

    return seeded
