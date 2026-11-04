"""G3b hard rule, measured: with GRAPH_CORRELATION_ENABLED off, the homelab page paths issue exactly the SQL
statements and thread hops they did on main before G3b (counts pinned from a run of this file on main 7fc26ba).
Set G3B_MEASURE=1 to print the counts instead of asserting them."""
import asyncio
import os
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import event
from sqlmodel import SQLModel, create_engine

import backend.database as bd

BASELINE = {  # measured on main 7fc26ba with G3B_MEASURE=1 (identical on this branch with correlation off)
    "vm": {"stmts": 4, "writes": 1, "hops": 3},
    "docker": {"stmts": 4, "writes": 1, "hops": 3},
    "array": {"stmts": 6, "writes": 2, "hops": 4},
}


@pytest.fixture
def eng(tmp_path, monkeypatch):
    import backend.config as config_mod

    path = tmp_path / "nexus.db"
    monkeypatch.setattr(bd, "DB_PATH", path)
    e = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False, "timeout": 30})
    event.listen(e, "connect", bd._set_sqlite_pragmas)
    monkeypatch.setattr(bd, "engine", e)
    SQLModel.metadata.create_all(e)
    monkeypatch.setenv("GRAPH_ENABLED", "true")
    monkeypatch.delenv("GRAPH_CORRELATION_ENABLED", raising=False)
    monkeypatch.setattr(config_mod, "_settings_instance", None)
    yield e
    monkeypatch.setattr(config_mod, "_settings_instance", None)


async def _measure(e, coro_fn):
    stmts, hops = [], []
    event.listen(e, "before_cursor_execute", lambda conn, cur, st, *a: stmts.append(st.split()[0].upper()))
    real = asyncio.to_thread

    async def counting(fn, *a, **k):
        hops.append(getattr(fn, "__name__", "?"))
        return await real(fn, *a, **k)

    with patch("asyncio.to_thread", counting):
        await coro_fn()
    return {"stmts": len(stmts), "writes": sum(s in ("INSERT", "UPDATE", "DELETE") for s in stmts), "hops": len(hops)}


async def _vm():
    from backend.agents import homelab_watch
    from backend.integrations.proxmox import ProxmoxData

    homelab_watch.reset()
    homelab_watch._vm_states["150"] = "running"
    data = ProxmoxData(node="pve", node_status="online", vms=[{"vmid": 150, "name": "lab-vm", "status": "stopped", "type": "lxc"}])
    with patch("backend.integrations.proxmox.fetch", AsyncMock(return_value=data)), \
         patch("backend.events.notify_phone", AsyncMock(return_value=True)):
        await homelab_watch.check_proxmox_vms()


async def _docker():
    from backend.agents import homelab_watch

    homelab_watch.reset()
    homelab_watch._docker_states["demo-app"] = "RUNNING"
    data = type("D", (), {"docker_containers": [{"name": "demo-app", "state": "EXITED"}]})()
    with patch("backend.integrations.unraid.fetch", AsyncMock(return_value=data)), \
         patch("backend.events.notify_phone", AsyncMock(return_value=True)), \
         patch.object(homelab_watch, "_maybe_auto_restart_docker", AsyncMock(return_value=False)):
        await homelab_watch.check_docker()


async def _array():
    from backend.agents import homelab_watch

    homelab_watch.reset()
    with patch("backend.events.notify_phone", AsyncMock(return_value=True)):
        await homelab_watch._edge_alert("unraid_array", True, "array unhealthy", kind="homelab_array")
        await homelab_watch._edge_alert("unraid_array", False, "array unhealthy", kind="homelab_array")  # clear path too


@pytest.mark.parametrize("name,fn", [("vm", _vm), ("docker", _docker), ("array", _array)])
async def test_off_page_paths_match_main(eng, name, fn):
    got = await _measure(eng, fn)
    if os.environ.get("G3B_MEASURE"):
        print(f"\nBASELINE {name} {got}")
        return
    assert got == BASELINE[name], f"{name}: {got} != main {BASELINE[name]}"
