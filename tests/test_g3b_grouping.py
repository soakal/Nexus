"""G3b phase 2: the grouping decision in record_flag_ex (plan g3/plan-v12-g3b.md §5.1-5.2, §5.3.7)."""
import asyncio
import time
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import event, text
from sqlmodel import SQLModel, create_engine

import backend.database as bd
from backend.agents import g3b, outcomes
from backend.graph import now_iso, store
from backend.graph.store import Edge, Node


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "nexus.db"
    monkeypatch.setattr(bd, "DB_PATH", path)
    e = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False, "timeout": 30})
    event.listen(e, "connect", bd._set_sqlite_pragmas)
    monkeypatch.setattr(bd, "engine", e)
    SQLModel.metadata.create_all(e)
    store.upsert_nodes([Node("host:unraid", "host", "unraid"), Node("container:demo-app", "container", "demo-app", source="unraid_api"),
                        Node("guest:150", "guest", "lab-vm", source="proxmox_api")], now_iso())
    store.upsert_edges([Edge("container:demo-app", "host:unraid", "runs_on", "unraid_api")], now_iso())
    store.record_job("collect", ok=True, at=now_iso())
    return e


def _settings(monkeypatch, graph="true", corr="true"):
    import backend.config as config_mod

    monkeypatch.setenv("GRAPH_ENABLED", graph)
    monkeypatch.setenv("GRAPH_CORRELATION_ENABLED", corr)
    monkeypatch.setattr(config_mod, "_settings_instance", None)


@pytest.fixture
def on(db, monkeypatch):
    _settings(monkeypatch)
    yield
    import backend.config as config_mod
    monkeypatch.setattr(config_mod, "_settings_instance", None)


def _parent(paged=True, suppressed=False, status="open", age_min=1):
    with bd.engine.begin() as c:
        c.execute(text(
            "INSERT INTO outcomeflag (source, \"check\", fingerprint, summary, severity, status, surfaced_count, "
            "last_surfaced_at, created_at, updated_at, suppressed, paged_at) VALUES ('homelab_watch', 'unraid_array', "
            "'homelab_watch:unraid_array', 'array down', 'high', :st, 1, :t, :t, :t, :sup, :p)"),
            {"st": status, "t": g3b.db_ts(datetime.utcnow() - timedelta(minutes=age_min)), "sup": int(suppressed),
             "p": g3b.db_ts(datetime.utcnow()) if paged else None})
        return c.execute(text("SELECT max(id) FROM outcomeflag")).scalar_one()


def _row(fid):
    with bd.engine.connect() as c:
        return c.execute(text("SELECT parent_flag_id, attach_op, paged_at, surfaced_count FROM outcomeflag WHERE id = :i"),
                         {"i": fid}).one()


async def test_off_result_unchanged_and_no_g3b_work(db, monkeypatch):
    _settings(monkeypatch, corr="false")
    _parent()
    with patch.object(g3b, "run_presend", AsyncMock()) as rp:
        d = await outcomes.record_flag_ex("homelab_watch", "docker:demo-app", "stopped")
    assert set(d) == {"id", "surface", "reason"} and d["surface"] is True
    rp.assert_not_awaited()
    assert _row(d["id"]).parent_flag_id is None


async def test_child_grouped_under_paged_parent(on):
    p = _parent()
    d = await outcomes.record_flag_ex("homelab_watch", "docker:demo-app", "stopped")
    assert d["surface"] is False and d["reason"] == f"grouped:{p}" and d["surfacing"] == 1
    r = _row(d["id"])
    assert r.parent_flag_id == p and r.attach_op


async def test_no_parent_pages_with_budget(on):
    d = await outcomes.record_flag_ex("homelab_watch", "docker:demo-app", "stopped")
    assert d["surface"] is True and d["surfacing"] == 1 and d["presend_deadline"] > time.monotonic() - 5


@pytest.mark.parametrize("kw", [dict(paged=False), dict(suppressed=True), dict(status="resolved"), dict(age_min=45)])
async def test_ineligible_parent_never_adopts(on, kw):
    _parent(**kw)
    d = await outcomes.record_flag_ex("homelab_watch", "docker:demo-app", "stopped")
    assert d["surface"] is True and _row(d["id"]).parent_flag_id is None


async def test_not_a_descendant_not_grouped(on):
    _parent()
    d = await outcomes.record_flag_ex("homelab_watch", "vm:150", "stopped")  # guest:150 has no edge to host:unraid
    assert d["surface"] is True


async def test_stale_collect_pauses_grouping(on):
    _parent()
    store.record_job("collect", ok=True, at=(datetime.utcnow() - timedelta(hours=4)).replace(microsecond=0).isoformat())
    d = await outcomes.record_flag_ex("homelab_watch", "docker:demo-app", "stopped")
    assert d["surface"] is True


async def test_parent_itself_and_graph_flags_never_grouped(on):
    _parent()
    d = await outcomes.record_flag_ex("graph", "stale:collect", "graph stale", severity="low")
    assert "grouped" not in str(d.get("reason"))


async def test_budget_expiry_fails_open_and_undoes_late_attach(on, monkeypatch):
    p = _parent()
    from backend.graph import correlate

    real = correlate.find_parent_on

    def slow(*a, **k):
        time.sleep(0.4)
        return real(*a, **k)

    monkeypatch.setattr(correlate, "find_parent_on", slow)
    monkeypatch.setattr(g3b, "PRESEND_BUDGET_S", 0.1)
    # make the worker commit despite the abandon (the check->COMMIT window): token never reports abandoned
    monkeypatch.setattr(g3b.AbandonToken, "is_set", lambda self: False)
    t0 = time.monotonic()
    d = await outcomes.record_flag_ex("homelab_watch", "docker:demo-app", "stopped")
    assert d["surface"] is True and time.monotonic() - t0 < 0.35  # page proceeds inside the budget
    await asyncio.sleep(0.6)  # worker finishes and commits; the late handler queues the undo
    assert g3b.drain_post()
    assert _row(d["id"]).parent_flag_id is None  # compensated
    assert p


async def test_admission_refused_fails_open(on, monkeypatch):
    _parent()
    monkeypatch.setattr(g3b, "_admit", lambda: False)
    d = await outcomes.record_flag_ex("homelab_watch", "docker:demo-app", "stopped")
    assert d["surface"] is True and _row(d["id"]).parent_flag_id is None


async def test_refire_resets_delivery_for_new_surfacing(on):
    d1 = await outcomes.record_flag_ex("homelab_watch", "vm:150", "stopped")
    with bd.engine.begin() as c:
        c.execute(text("UPDATE outcomeflag SET paged_at = '2026-01-01 00:00:00.000000', send_claim_id = 'x' WHERE id = :i"),
                  {"i": d1["id"]})
    d2 = await outcomes.record_flag_ex("homelab_watch", "vm:150", "stopped again")
    assert d2["id"] == d1["id"] and d2["surfacing"] == 2
    r = _row(d1["id"])
    assert r.paged_at is None and r.surfaced_count == 2
