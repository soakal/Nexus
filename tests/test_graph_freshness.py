"""Graph freshness: stale jobs -> graph:stale:<job> flag (watchdog) + warning on graph answers."""
import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlmodel import SQLModel, create_engine
from sqlmodel.pool import StaticPool

from backend.graph import freshness, intervals, store

NOW = datetime(2026, 11, 4, 12, 0)
IV = {"collect": 60, "vault_index": 15}


def test_intervals_match_scheduler_clamps():
    assert intervals(MagicMock(graph_collect_minutes=5, graph_vault_index_minutes=1)) == {"collect": 15, "vault_index": 5}
    assert intervals(MagicMock(graph_collect_minutes=60, graph_vault_index_minutes=15)) == {"collect": 60, "vault_index": 15}


def test_stale_rules():
    jobs = {
        "collect": {"last_success": "2026-11-04T09:00:01", "last_error": "boom"},    # 2h59m59s: fresh
        "vault_index": {"last_success": "2026-11-04T11:15:00", "last_error": None},  # exactly 3x15 min: stale (>=)
        "source:proxmox_api": {"last_success": None, "last_error": "pve down"},      # never succeeded
        # source:unraid_api / source:uptime_targets: no row at all
    }
    recent_boot = NOW - timedelta(minutes=30)
    got = freshness.stale(jobs, IV, NOW, recent_boot)
    assert set(got) == {"vault_index"}  # never-succeeded / missing jobs get the grace period from boot
    long_ago = NOW - timedelta(hours=3)
    got = freshness.stale(jobs, IV, NOW, long_ago)
    assert set(got) == {"vault_index", "source:proxmox_api", "source:unraid_api", "source:uptime_targets"}
    assert got["source:proxmox_api"] == {"last_success": None, "last_error": "pve down"}
    assert got["source:unraid_api"] == {"last_success": None, "last_error": None}  # job never even ran
    assert set(freshness.stale(jobs, IV, NOW, None)) == {"vault_index"}  # no boot baseline (CLI): only real timestamps
    jobs["collect"]["last_success"] = "2026-11-04T09:00:00"  # exactly 3 h
    assert "collect" in freshness.stale(jobs, IV, NOW, recent_boot)


@pytest.fixture
def eng(monkeypatch):
    import backend.database  # noqa: F401 - register models

    e = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(e)
    monkeypatch.setattr("backend.database.engine", e)
    monkeypatch.setattr(freshness, "_now", lambda: NOW)
    monkeypatch.setattr(freshness, "_started", NOW - timedelta(minutes=10))
    monkeypatch.setattr("backend.graph.intervals", lambda settings=None: IV)
    return e


def _fresh_all():
    for name in freshness.WATCHED:
        store.record_job(name, ok=True, at="2026-11-04T11:50:00")


def test_note(eng):
    assert freshness.note() is None  # nothing ran yet, but only 10 min since boot
    _fresh_all()
    assert freshness.note() is None
    store.record_job("collect", ok=True, at="2026-11-04T07:00:00")
    assert freshness.note() == ("⚠ graph data may be incomplete (collect last succeeded 2026-11-04T07:00:00Z) "
                                "— recent changes may be missing")


@pytest.fixture
def graph_on(monkeypatch):
    import backend.config as config_mod

    monkeypatch.setenv("GRAPH_ENABLED", "true")
    monkeypatch.setattr(config_mod, "_settings_instance", None)
    yield
    monkeypatch.setattr(config_mod, "_settings_instance", None)


async def test_reconcile_opens_and_clears(eng, graph_on):
    _fresh_all()
    store.record_job("collect", ok=True, at="2026-11-04T07:00:00")
    store.record_job("collect", ok=False, error="proxmox timeout", at="2026-11-04T11:00:00")
    rec, clr = AsyncMock(), AsyncMock()
    with patch("backend.agents.outcomes.record_flag_ex", rec), patch("backend.agents.outcomes.clear_flag", clr):
        assert await freshness.reconcile() == ["collect"]
    src, check, summary = rec.await_args.args
    assert (src, check, rec.await_args.kwargs["severity"]) == ("graph", "stale:collect", "low")
    assert "since 2026-11-04T07:00:00Z" in summary and "proxmox timeout" in summary and len(summary) <= 300
    assert {c.args[1] for c in clr.await_args_list} == {f"stale:{n}" for n in freshness.WATCHED} - {"stale:collect"}
    rec.reset_mock()
    clr.reset_mock()
    store.record_job("collect", ok=True, at="2026-11-04T11:55:00")  # recovered
    with patch("backend.agents.outcomes.record_flag_ex", rec), patch("backend.agents.outcomes.clear_flag", clr):
        assert await freshness.reconcile() == []
    rec.assert_not_awaited()
    assert ("graph", "stale:collect") in {c.args for c in clr.await_args_list}


async def test_reconcile_with_real_flags(eng, graph_on):
    from backend.agents import outcomes

    _fresh_all()
    store.record_job("vault_index", ok=True, at="2026-11-04T08:00:00")
    await freshness.reconcile()
    await freshness.reconcile()  # dedups onto the same row
    open_ = [f for f in await outcomes.open_flags(limit=50) if f["source"] == "graph"]
    assert [f["check"] for f in open_] == ["stale:vault_index"]
    store.record_job("vault_index", ok=True, at="2026-11-04T11:59:00")
    await freshness.reconcile()
    assert not [f for f in await outcomes.open_flags(limit=50) if f["source"] == "graph"]


async def test_reconcile_clears_when_graph_disabled(eng, monkeypatch):
    import backend.config as config_mod

    monkeypatch.setenv("GRAPH_ENABLED", "false")
    monkeypatch.setattr(config_mod, "_settings_instance", None)
    store.record_job("collect", ok=True, at="2026-01-01T00:00:00")  # would be stale if the graph were on
    rec, clr = AsyncMock(), AsyncMock()
    with patch("backend.agents.outcomes.record_flag_ex", rec), patch("backend.agents.outcomes.clear_flag", clr):
        assert await freshness.reconcile() == []
    rec.assert_not_awaited()
    assert {c.args[1] for c in clr.await_args_list} == {f"stale:{n}" for n in freshness.WATCHED}
    monkeypatch.setattr(config_mod, "_settings_instance", None)


async def test_reconcile_bounded_and_never_raises(monkeypatch):
    async def hang():
        await asyncio.sleep(10)

    monkeypatch.setattr(freshness, "_RECONCILE_TIMEOUT_S", 0.05)
    with patch("backend.graph.freshness._reconcile", hang):
        assert await freshness.reconcile() == []
    with patch("backend.graph.freshness._reconcile", AsyncMock(side_effect=RuntimeError("db gone"))):
        assert await freshness.reconcile() == []


async def test_watchdog_runs_freshness_check():
    from backend.agents import watchdog

    with patch("backend.graph.freshness.reconcile", AsyncMock(return_value=["collect"])) as rc:
        assert await watchdog.check_graph_freshness() == ["collect"]
    rc.assert_awaited_once()


async def test_what_changed_and_evidence_carry_the_warning(eng, graph_on):
    from backend.agents import incident_diag
    from backend.agents.tools import dispatcher_map
    from backend.graph.store import Node

    store.upsert_nodes([Node("guest:42", "guest", "lab-vm")], "2026-11-01T00:00:00")
    _fresh_all()
    store.record_job("collect", ok=True, at="2026-11-04T07:00:00")
    out = await dispatcher_map()["what_changed"]({"node": "guest:42", "since": "2026-11-01T00:00:00"})
    assert out.startswith("⚠ graph data may be incomplete (collect last succeeded 2026-11-04T07:00:00Z)")
    with patch("backend.agents.outcomes.open_flags", new=AsyncMock(return_value=[])):
        ev = await incident_diag._evidence("vm:42", datetime(2026, 11, 4, 11, 0))
    assert ev.splitlines()[1].startswith("⚠ graph data may be incomplete")
