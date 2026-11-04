"""Graph freshness: stale jobs -> graph:stale:<job> flag + warning on graph answers."""
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlmodel import SQLModel, create_engine
from sqlmodel.pool import StaticPool

from backend.graph import freshness, intervals, store

NOW = datetime(2026, 11, 4, 12, 0)
IV = {"collect": 60, "vault_index": 15}


def test_intervals_match_scheduler_clamps():
    s = MagicMock(graph_collect_minutes=5, graph_vault_index_minutes=1)
    assert intervals(s) == {"collect": 15, "vault_index": 5}
    s = MagicMock(graph_collect_minutes=60, graph_vault_index_minutes=15)
    assert intervals(s) == {"collect": 60, "vault_index": 15}


def test_stale_rules():
    jobs = {
        "collect": {"last_success": "2026-11-04T09:01:00", "last_error": "boom"},       # 2h59m: < 3 runs, fresh
        "vault_index": {"last_success": "2026-11-04T11:14:00", "last_error": None},     # 46 min > 3x15: stale
        "source:proxmox_api": {"last_success": None, "last_error": "pve down"},         # never succeeded: stale
        "source:unraid_api": {"last_success": None, "last_error": None},                # row, no outcome yet: not judged
        "unrelated": {"last_success": "2020-01-01T00:00:00"},
    }
    got = freshness.stale(jobs, IV, NOW)
    assert set(got) == {"vault_index", "source:proxmox_api"}
    assert got["source:proxmox_api"] == {"last_success": None, "last_error": "pve down"}
    jobs["collect"]["last_success"] = "2026-11-04T08:59:00"  # 3h01m
    assert "collect" in freshness.stale(jobs, IV, NOW)
    assert freshness.stale({}, IV, NOW) == {}  # graph never ran: nothing to judge


@pytest.fixture
def eng(monkeypatch):
    import backend.database  # noqa: F401 - register models

    e = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(e)
    monkeypatch.setattr("backend.database.engine", e)
    monkeypatch.setattr(freshness, "_now", lambda: NOW)
    monkeypatch.setattr("backend.graph.intervals", lambda settings=None: IV)
    return e


def test_note(eng):
    assert freshness.note() is None
    store.record_job("collect", ok=True, at="2026-11-04T11:30:00")
    assert freshness.note() is None
    store.record_job("collect", ok=True, at="2026-11-04T07:00:00")
    assert freshness.note() == ("⚠ graph data may be incomplete (collect last succeeded 2026-11-04T07:00:00Z) "
                                "— recent changes may be missing")


async def test_update_flags_opens_and_clears(eng):
    rec, clr = AsyncMock(), AsyncMock()
    store.record_job("collect", ok=True, at="2026-11-04T07:00:00")
    store.record_job("collect", ok=False, error="proxmox timeout", at="2026-11-04T11:00:00")
    with patch("backend.agents.outcomes.record_flag_ex", rec), patch("backend.agents.outcomes.clear_flag", clr):
        await freshness.update_flags()
    assert rec.await_count == 1
    src, check, summary = rec.await_args.args
    assert (src, check, rec.await_args.kwargs["severity"]) == ("graph", "stale:collect", "low")
    assert "since 2026-11-04T07:00:00Z" in summary and "proxmox timeout" in summary and len(summary) <= 300
    assert {c.args[1] for c in clr.await_args_list} == {f"stale:{n}" for n in freshness.WATCHED} - {"stale:collect"}
    rec.reset_mock()
    clr.reset_mock()
    store.record_job("collect", ok=True, at="2026-11-04T11:55:00")  # recovered
    with patch("backend.agents.outcomes.record_flag_ex", rec), patch("backend.agents.outcomes.clear_flag", clr):
        await freshness.update_flags()
    rec.assert_not_awaited()
    assert ("graph", "stale:collect") in {c.args for c in clr.await_args_list}


async def test_update_flags_never_raises():
    with patch("backend.graph.freshness.current", side_effect=RuntimeError("db gone")):
        await freshness.update_flags()


async def test_jobs_update_flags_even_when_they_fail():
    from backend import scheduler

    upd = AsyncMock()
    with patch("backend.graph.freshness.update_flags", upd), \
         patch("backend.graph.topology.collect", AsyncMock(side_effect=RuntimeError("x"))), \
         patch("backend.graph.vault_index.refresh", side_effect=RuntimeError("y")):
        await scheduler._graph_collect()
        await scheduler._graph_vault_index()
    assert upd.await_count == 2


async def test_what_changed_and_evidence_carry_the_warning(eng, monkeypatch):
    import backend.config as config_mod
    from backend.agents import incident_diag
    from backend.agents.tools import dispatcher_map
    from backend.graph.store import Node

    monkeypatch.setenv("GRAPH_ENABLED", "true")
    monkeypatch.setattr(config_mod, "_settings_instance", None)
    store.upsert_nodes([Node("guest:42", "guest", "lab-vm")], "2026-11-01T00:00:00")
    store.record_job("collect", ok=True, at="2026-11-04T07:00:00")
    out = await dispatcher_map()["what_changed"]({"node": "guest:42", "since": "2026-11-01T00:00:00"})
    assert out.startswith("⚠ graph data may be incomplete (collect last succeeded 2026-11-04T07:00:00Z)")
    with patch("backend.agents.outcomes.open_flags", new=AsyncMock(return_value=[])):
        ev = await incident_diag._evidence("vm:42", datetime(2026, 11, 4, 11, 0))
    assert ev.splitlines()[1].startswith("⚠ graph data may be incomplete")
    monkeypatch.setattr(config_mod, "_settings_instance", None)
