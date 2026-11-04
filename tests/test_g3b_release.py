"""G3b phase 4: release reconciler, adoption from the tick, ungroup, doctor (plan §5.4, §5.6, §7)."""
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from g3b_helpers import _child, _cols, _parent, _row, _settings
from sqlalchemy import event, text

import backend.database as bd
from backend.agents import g3b, outcomes, release
from backend.integrations import telegram


@pytest.fixture(autouse=True)
def phone(monkeypatch):
    import backend.config as config_mod
    monkeypatch.setenv("PHONE_NOTIFICATIONS_ENABLED", "true")
    monkeypatch.setattr(config_mod, "_settings_instance", None)


async def _drain_release():
    import asyncio
    while outcomes._release_tasks:
        await asyncio.gather(*list(outcomes._release_tasks))


def _ok_send():
    return patch.object(telegram, "_send_payload", AsyncMock(return_value=(True, False)))


async def test_off_without_grouped_rows_does_no_writes(g3b_db, monkeypatch):
    _settings(monkeypatch, corr="false")
    _parent(status="resolved")
    stmts = []
    event.listen(bd.engine, "before_cursor_execute", lambda conn, cur, st, *a: stmts.append(st.split()[0].upper()))
    res = await release.release_due()
    assert res.get("idle") is True
    assert stmts and set(stmts) == {"SELECT"}


async def test_release_on_resolve_pages_child_once(g3b_on):
    p = _parent()
    d = await outcomes.record_flag_ex("homelab_watch", "docker:demo-app", "stopped")
    assert outcomes.is_grouped(d)
    with _ok_send() as send:
        assert await outcomes.resolve_flag(p, "resolved", note="fixed", by="test") == "resolved"
        await _drain_release()
    send.assert_awaited_once()
    payload = send.await_args.args[0]
    assert payload["type"] == "flag_released" and payload["flag_id"] == d["id"]
    assert any(b["callback_data"] == "docker:restart:demo-app" for b in payload["buttons"])
    assert _cols(d["id"]).paged_at is not None
    with _ok_send() as send2:
        await release.release_due()
    send2.assert_not_awaited()  # once


async def test_release_on_clear_flag(g3b_on):
    p = _parent()
    d = await outcomes.record_flag_ex("homelab_watch", "docker:demo-app", "stopped")
    with _ok_send() as send:
        assert await outcomes.clear_flag("homelab_watch", "unraid_array") == 1
        await _drain_release()
    send.assert_awaited_once()
    assert p and _cols(d["id"]).paged_at is not None


async def test_release_survives_failure_and_retries_next_tick(g3b_on):
    p = _parent()
    d = await outcomes.record_flag_ex("homelab_watch", "docker:demo-app", "stopped")
    with patch.object(telegram, "_send_payload", AsyncMock(side_effect=RuntimeError("boom"))):
        await outcomes.resolve_flag(p, "resolved", note="x", by="test")
        await _drain_release()
    assert _cols(d["id"]).paged_at is None and _cols(d["id"]).send_claim_id is not None  # died mid-send: lease held
    with bd.engine.begin() as c:  # lease expires
        c.execute(text("UPDATE outcomeflag SET send_claimed_at = :t WHERE id = :i"),
                  {"t": g3b.db_ts(datetime.utcnow() - timedelta(minutes=11)), "i": d["id"]})
    with _ok_send():
        res = await release.release_due()
    assert res["paged"] == [d["id"]]


async def test_resolved_child_gets_no_release_page(g3b_on):
    p = _parent()
    d = await outcomes.record_flag_ex("homelab_watch", "docker:demo-app", "stopped")
    await outcomes.resolve_flag(d["id"], "resolved", note="fixed it", by="test")
    with _ok_send() as send:
        await outcomes.resolve_flag(p, "resolved", note="x", by="test")
        await _drain_release()
    send.assert_not_awaited()


async def test_adoption_runs_when_no_group_exists_yet(g3b_on):
    child = await outcomes.record_flag_ex("homelab_watch", "docker:demo-app", "stopped")  # pages first: no parent yet
    assert child["surface"] is True
    with bd.engine.begin() as c:  # its page was accepted
        c.execute(text("UPDATE outcomeflag SET paged_at = :t WHERE id = :i"), {"t": g3b.db_ts(datetime.utcnow()), "i": child["id"]})
    p = _parent()  # parent pages afterwards
    res = await release.release_due()
    assert res["adopted"] == 1 and _row(child["id"]).parent_flag_id == p
    with _ok_send() as send:  # child already paged: nothing owed when the parent closes
        await outcomes.resolve_flag(p, "resolved", note="x", by="test")
        await _drain_release()
    send.assert_not_awaited()


async def test_off_leftover_grouped_child_is_released(g3b_db, monkeypatch):
    _settings(monkeypatch, corr="false")
    p = _parent(status="resolved")
    fid = _child(parent=p)
    with _ok_send() as send:
        res = await release.release_due()
    assert res["paged"] == [fid]
    send.assert_awaited_once()


async def test_pending_first_page_is_left_to_its_replay(g3b_on):
    p = _parent(status="resolved")
    fid = _child(parent=p)
    await _insert_pending(fid)
    with _ok_send() as send:
        res = await release.release_due()
    send.assert_not_awaited()
    assert res["skipped"] == [fid]


async def test_debt_counts_and_overdue(g3b_on):
    p = _parent(status="resolved")
    fid = _child(parent=p)
    with patch.object(telegram, "_send_payload", AsyncMock(return_value=(False, True))):  # terminal failure
        await release.release_due()
    assert release.debt_counts()["debt"] == 1 and release.debt_counts()["overdue"] == 0
    with bd.engine.begin() as c:
        c.execute(text("UPDATE outcomeflag SET debt_since = :t WHERE id = :i"),
                  {"t": g3b.db_ts(datetime.utcnow() - timedelta(minutes=31)), "i": fid})
    assert release.debt_counts()["overdue"] == 1


async def test_ungroup_detaches_and_pages_children_of_open_parent(g3b_on, monkeypatch):
    from backend.graph import __main__ as cli

    p = _parent()
    d = await outcomes.record_flag_ex("homelab_watch", "docker:demo-app", "stopped")
    with _ok_send():
        assert await cli._ungroup() == 0
    r = _row(d["id"])
    assert r.parent_flag_id is None and r.paged_at is not None
    with bd.engine.connect() as c:
        assert c.execute(text("SELECT released_from_parent_id FROM outcomeflag WHERE id = :i"), {"i": d["id"]}).scalar_one() == p


async def test_ungroup_exit_1_when_still_owed(g3b_on, monkeypatch):
    from backend.graph import __main__ as cli

    monkeypatch.setattr(cli, "UNGROUP_WAIT_S", 0.2)
    _parent()
    d = await outcomes.record_flag_ex("homelab_watch", "docker:demo-app", "stopped")
    with patch.object(telegram, "_send_payload", AsyncMock(return_value=(False, False))):
        assert await cli._ungroup() == 1
    assert _row(d["id"]).paged_at is None and release.debt_counts()["debt"] == 1  # stays debt, tick retries


async def _insert_pending(fid, attempts=1, surfacing=1):
    with bd.engine.begin() as c:
        c.execute(text("INSERT INTO pendingdelivery (delivery_type, payload_json, attempts, created_at) VALUES ('notify', :pj, :a, :t)"),
                  {"pj": f'{{"flag_id": {fid}, "flag_surfacing": {surfacing}}}', "a": attempts, "t": g3b.db_ts(datetime.utcnow())})


async def test_dead_lettered_first_page_is_taken_over(g3b_on):
    from backend.integrations.telegram import _MAX_ATTEMPTS

    p = _parent(status="resolved")
    fid = _child(parent=p)
    await _insert_pending(fid, attempts=_MAX_ATTEMPTS)  # deliver_pending will never load this again
    with _ok_send() as send:
        res = await release.release_due()
    assert res["paged"] == [fid] and send.await_count == 1


async def test_stale_surfacing_pending_does_not_block(g3b_on):
    p = _parent(status="resolved")
    fid = _child(parent=p, surfacing=2)
    await _insert_pending(fid, surfacing=1)
    with _ok_send():
        res = await release.release_due()
    assert res["paged"] == [fid]


async def test_resolve_and_clear_never_wait_for_release(g3b_on, monkeypatch):
    import asyncio
    import time

    p = _parent()
    await outcomes.record_flag_ex("homelab_watch", "docker:demo-app", "stopped")

    async def slow(*a, **k):
        await asyncio.sleep(1.0)
        return {}

    monkeypatch.setattr(release, "release_due", slow)
    t0 = time.monotonic()
    assert await outcomes.clear_flag("homelab_watch", "unraid_array") == 1
    assert time.monotonic() - t0 < 0.5  # the watcher tick is not held up by the release
    p2 = _parent()
    t0 = time.monotonic()
    assert await outcomes.resolve_flag(p2, "resolved", note="x", by="test") == "resolved"
    assert time.monotonic() - t0 < 0.5  # nor is a Telegram resolve tap
    for t in list(outcomes._release_tasks):
        t.cancel()
    assert p


async def test_off_clear_flag_schedules_nothing(g3b_db, monkeypatch):
    _settings(monkeypatch, corr="false")
    _parent()
    before = set(outcomes._release_tasks)
    with patch.object(release, "release_due", AsyncMock()) as rd:
        assert await outcomes.clear_flag("homelab_watch", "unraid_array") == 1
    rd.assert_not_called()
    assert set(outcomes._release_tasks) == before  # nothing new scheduled


async def test_adoption_writes_are_single_statements(g3b_on):
    """No write lock is held across the adoption pass: every write is its own autocommit statement."""
    child = await outcomes.record_flag_ex("homelab_watch", "docker:demo-app", "stopped")
    with bd.engine.begin() as c:
        c.execute(text("UPDATE outcomeflag SET paged_at = :t WHERE id = :i"), {"t": g3b.db_ts(datetime.utcnow()), "i": child["id"]})
    _parent()
    seen = []
    real = g3b.gate_conn

    def spy():
        c = real()
        c.set_trace_callback(seen.append)
        return c

    with patch.object(g3b, "gate_conn", spy):
        await release.release_due()
    assert not any(s.strip().upper().startswith(("BEGIN", "SAVEPOINT")) for s in seen)
    assert any(s.lstrip().upper().startswith("UPDATE OUTCOMEFLAG SET PARENT_FLAG_ID") for s in seen)
