"""Tests for backend/agents/expected_resources.py -- the declared "what
should be running" baseline homelab_watch.check_expected_resources diffs
live Docker/VM/LXC state against.

Pattern: in-memory StaticPool engine monkeypatched onto backend.database.engine,
matching tests/test_outcome_flags.py.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel import SQLModel, create_engine
from sqlmodel.pool import StaticPool

import backend.database  # noqa: F401 -- register all models on metadata


@pytest.fixture
def eng(monkeypatch):
    e = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(e)
    monkeypatch.setattr("backend.database.engine", e)
    return e


@pytest.mark.asyncio
async def test_upsert_creates_then_updates_same_row(eng):
    from backend.agents import expected_resources

    created = await expected_resources.upsert("docker", "sonarr", "running", note="media stack")
    assert created["kind"] == "docker"
    assert created["identifier"] == "sonarr"
    assert created["desired_state"] == "running"
    assert created["note"] == "media stack"

    updated = await expected_resources.upsert("docker", "sonarr", "stopped")
    assert updated["id"] == created["id"]  # same row, not a new one
    assert updated["desired_state"] == "stopped"
    assert updated["note"] == "media stack"  # note not overwritten by note=None

    rows = await expected_resources.list_expected()
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_upsert_rejects_unknown_kind_or_state(eng):
    from backend.agents import expected_resources

    with pytest.raises(ValueError):
        await expected_resources.upsert("potato", "x", "running")
    with pytest.raises(ValueError):
        await expected_resources.upsert("docker", "x", "sideways")


@pytest.mark.asyncio
async def test_list_expected_empty_and_sorted(eng):
    from backend.agents import expected_resources

    assert await expected_resources.list_expected() == []

    await expected_resources.upsert("vm", "102", "running")
    await expected_resources.upsert("docker", "sonarr", "running")
    rows = await expected_resources.list_expected()
    assert [r["kind"] for r in rows] == ["docker", "vm"]  # ordered by kind then identifier


@pytest.mark.asyncio
async def test_set_desired_state_never_raises_on_bad_input(eng):
    """Best-effort variant used by outcomes.resolve_flag -- an unknown kind
    must degrade to None, not propagate a ValueError into the flag-resolve
    path."""
    from backend.agents import expected_resources

    result = await expected_resources.set_desired_state("potato", "x", "running")
    assert result is None


@pytest.mark.asyncio
async def test_set_desired_state_updates_existing_row(eng):
    from backend.agents import expected_resources

    await expected_resources.upsert("docker", "sonarr", "running")
    result = await expected_resources.set_desired_state("docker", "sonarr", "stopped")
    assert result["desired_state"] == "stopped"
    rows = await expected_resources.list_expected()
    assert rows[0]["desired_state"] == "stopped"


@pytest.mark.asyncio
async def test_seed_from_live_snapshots_docker_and_vm_lxc(eng):
    from backend.agents import expected_resources

    unraid_data = SimpleNamespace(docker_containers=[
        {"name": "sonarr", "state": "RUNNING"},
        {"name": "radarr", "state": "EXITED"},
    ])
    proxmox_data = SimpleNamespace(vms=[
        {"vmid": 201, "name": "plex-lxc", "status": "running", "type": "lxc"},
        {"vmid": 102, "name": "win11", "status": "stopped", "type": "qemu"},
    ])
    with patch("backend.integrations.unraid.fetch", new_callable=AsyncMock, return_value=unraid_data), \
         patch("backend.integrations.proxmox.fetch", new_callable=AsyncMock, return_value=proxmox_data):
        summary = await expected_resources.seed_from_live()

    assert summary["docker"] == 2
    assert summary["lxc"] == 1
    assert summary["vm"] == 1
    assert summary["errors"] == []

    rows = {r["kind"] + ":" + r["identifier"]: r for r in await expected_resources.list_expected()}
    assert rows["docker:sonarr"]["desired_state"] == "running"
    assert rows["docker:radarr"]["desired_state"] == "stopped"
    assert rows["lxc:201"]["desired_state"] == "running"
    assert rows["vm:102"]["desired_state"] == "stopped"


@pytest.mark.asyncio
async def test_seed_from_live_degrades_independently_on_fetch_failure(eng):
    from backend.agents import expected_resources

    with patch("backend.integrations.unraid.fetch", new_callable=AsyncMock, side_effect=RuntimeError("down")), \
         patch("backend.integrations.proxmox.fetch", new_callable=AsyncMock,
               return_value=SimpleNamespace(vms=[{"vmid": 1, "name": "x", "status": "running", "type": "qemu"}])):
        summary = await expected_resources.seed_from_live()

    assert summary["docker"] == 0
    assert summary["vm"] == 1
    assert len(summary["errors"]) == 1
    assert "docker" in summary["errors"][0]


@pytest.mark.asyncio
async def test_seed_from_live_is_idempotent_reruns_upsert_not_duplicate(eng):
    from backend.agents import expected_resources

    unraid_data = SimpleNamespace(docker_containers=[{"name": "sonarr", "state": "RUNNING"}])
    with patch("backend.integrations.unraid.fetch", new_callable=AsyncMock, return_value=unraid_data), \
         patch("backend.integrations.proxmox.fetch", new_callable=AsyncMock, return_value=SimpleNamespace(vms=[])):
        await expected_resources.seed_from_live()
        await expected_resources.seed_from_live()

    rows = await expected_resources.list_expected()
    assert len(rows) == 1
