"""G3b phase 4: release reconciler, adoption from the tick, ungroup, doctor (plan §5.4, §5.6, §7)."""
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import event, text

import backend.database as bd
from backend.agents import g3b, outcomes, release
from backend.integrations import telegram
from test_g3b_grouping import _parent, _row, _settings, db, on  # noqa: F401
from test_g3b_send import _child, _cols


@pytest.fixture(autouse=True)
def phone(monkeypatch):
    import backend.config as config_mod
    monkeypatch.setenv("PHONE_NOTIFICATIONS_ENABLED", "true")
    monkeypatch.setattr(config_mod, "_settings_instance", None)


def _ok_send():
    return patch.object(telegram, "_send_payload", AsyncMock(return_value=(True, False)))


async def test_off_without_grouped_rows_does_no_writes(db, monkeypatch):
    _settings(monkeypatch, corr="false")
    _parent(status="resolved")
    stmts = []
    event.listen(bd.engine, "before_cursor_execute", lambda conn, cur, st, *a: stmts.append(st.split()[0].upper()))
    res = await release.release_due()
    assert res.get("idle") is True
    assert stmts and set(stmts) == {"SELECT"}


async def test_release_on_resolve_pages_child_once(on):
    p = _parent()
    d = await outcomes.record_flag_ex("homelab_watch", "docker:demo-app", "stopped")
    assert outcomes.is_grouped(d)
    with _ok_send() as send:
        assert await outcomes.resolve_flag(p, "resolved", note="fixed", by="test") == "resolved"
    send.assert_awaited_once()
    payload = send.await_args.args[0]
    assert payload["type"] == "flag_released" and payload["flag_id"] == d["id"]
    assert any(b["callback_data"] == "docker:restart:demo-app" for b in payload["buttons"])
    assert _cols(d["id"]).paged_at is not None
    with _ok_send() as send2:
        await release.release_due()
    send2.assert_not_awaited()  # once


async def test_release_on_clear_flag(on):
    p = _parent()
    d = await outcomes.record_flag_ex("homelab_watch", "docker:demo-app", "stopped")
    with _ok_send() as send:
        assert await outcomes.clear_flag("homelab_watch", "unraid_array") == 1
    send.assert_awaited_once()
    assert p and _cols(d["id"]).paged_at is not None


async def test_release_survives_failure_and_retries_next_tick(on):
    p = _parent()
    d = await outcomes.record_flag_ex("homelab_watch", "docker:demo-app", "stopped")
    with patch.object(telegram, "_send_payload", AsyncMock(side_effect=RuntimeError("boom"))):
        await outcomes.resolve_flag(p, "resolved", note="x", by="test")
    assert _cols(d["id"]).paged_at is None and _cols(d["id"]).send_claim_id is not None  # died mid-send: lease held
    with bd.engine.begin() as c:  # lease expires
        c.execute(text("UPDATE outcomeflag SET send_claimed_at = :t WHERE id = :i"),
                  {"t": g3b.db_ts(datetime.utcnow() - timedelta(minutes=11)), "i": d["id"]})
    with _ok_send():
        res = await release.release_due()
    assert res["paged"] == [d["id"]]


async def test_resolved_child_gets_no_release_page(on):
    p = _parent()
    d = await outcomes.record_flag_ex("homelab_watch", "docker:demo-app", "stopped")
    await outcomes.resolve_flag(d["id"], "resolved", note="fixed it", by="test")
    with _ok_send() as send:
        await outcomes.resolve_flag(p, "resolved", note="x", by="test")
    send.assert_not_awaited()


async def test_adoption_runs_when_no_group_exists_yet(on):
    child = await outcomes.record_flag_ex("homelab_watch", "docker:demo-app", "stopped")  # pages first: no parent yet
    assert child["surface"] is True
    with bd.engine.begin() as c:  # its page was accepted
        c.execute(text("UPDATE outcomeflag SET paged_at = :t WHERE id = :i"), {"t": g3b.db_ts(datetime.utcnow()), "i": child["id"]})
    p = _parent()  # parent pages afterwards
    res = await release.release_due()
    assert res["adopted"] == 1 and _row(child["id"]).parent_flag_id == p
    with _ok_send() as send:  # child already paged: nothing owed when the parent closes
        await outcomes.resolve_flag(p, "resolved", note="x", by="test")
    send.assert_not_awaited()


async def test_off_leftover_grouped_child_is_released(db, monkeypatch):
    _settings(monkeypatch, corr="false")
    p = _parent(status="resolved")
    fid = _child(parent=p)
    with _ok_send() as send:
        res = await release.release_due()
    assert res["paged"] == [fid]
    send.assert_awaited_once()


async def test_pending_first_page_is_left_to_its_replay(on):
    p = _parent(status="resolved")
    fid = _child(parent=p)
    with bd.engine.begin() as c:
        c.execute(text("INSERT INTO pendingdelivery (delivery_type, payload_json, attempts, created_at) "
                       "VALUES ('notify', :pj, 1, :t)"), {"pj": f'{{"flag_id": {fid}, "flag_surfacing": 1}}',
                                                         "t": g3b.db_ts(datetime.utcnow())})
    with _ok_send() as send:
        res = await release.release_due()
    send.assert_not_awaited()
    assert res["skipped"] == [fid]


async def test_debt_counts_and_overdue(on):
    p = _parent(status="resolved")
    fid = _child(parent=p)
    with patch.object(telegram, "_send_payload", AsyncMock(return_value=(False, True))):  # terminal failure
        await release.release_due()
    assert release.debt_counts()["debt"] == 1 and release.debt_counts()["overdue"] == 0
    with bd.engine.begin() as c:
        c.execute(text("UPDATE outcomeflag SET debt_since = :t WHERE id = :i"),
                  {"t": g3b.db_ts(datetime.utcnow() - timedelta(minutes=31)), "i": fid})
    assert release.debt_counts()["overdue"] == 1


async def test_ungroup_detaches_and_pages_children_of_open_parent(on, monkeypatch):
    from backend.graph import __main__ as cli

    p = _parent()
    d = await outcomes.record_flag_ex("homelab_watch", "docker:demo-app", "stopped")
    with _ok_send():
        assert await cli._ungroup() == 0
    r = _row(d["id"])
    assert r.parent_flag_id is None and r.paged_at is not None
    with bd.engine.connect() as c:
        assert c.execute(text("SELECT released_from_parent_id FROM outcomeflag WHERE id = :i"), {"i": d["id"]}).scalar_one() == p


async def test_ungroup_exit_1_when_still_owed(on, monkeypatch):
    from backend.graph import __main__ as cli

    monkeypatch.setattr(cli, "UNGROUP_WAIT_S", 0.2)
    _parent()
    d = await outcomes.record_flag_ex("homelab_watch", "docker:demo-app", "stopped")
    with patch.object(telegram, "_send_payload", AsyncMock(return_value=(False, False))):
        assert await cli._ungroup() == 1
    assert _row(d["id"]).paged_at is None and release.debt_counts()["debt"] == 1  # stays debt, tick retries
