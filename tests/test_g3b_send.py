"""G3b phase 3: send gate in notify / deliver_pending, send_release, homelab call sites (plan §5.3-§5.4)."""
import json
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import text

import backend.database as bd
from backend.agents import g3b, outcomes
from backend.integrations import telegram
from test_g3b_grouping import _parent, _row, _settings, db, on  # noqa: F401  (fixtures)


def _child(parent=None, paged=False, surfacing=1, released_from=None, status="open"):
    with bd.engine.begin() as c:
        c.execute(text(
            "INSERT INTO outcomeflag (source, \"check\", fingerprint, summary, severity, status, surfaced_count, last_surfaced_at,"
            " created_at, updated_at, suppressed, parent_flag_id, released_from_parent_id, paged_at) VALUES ('homelab_watch',"
            " 'docker:demo-app', 'homelab_watch:docker:demo-app', 'stopped', 'medium', :st, :s, :t, :t, :t, 0, :p, :r, :pg)"),
            {"st": status, "s": surfacing, "t": g3b.db_ts(datetime.utcnow()), "p": parent, "r": released_from,
             "pg": g3b.db_ts(datetime.utcnow()) if paged else None})
        return c.execute(text("SELECT max(id) FROM outcomeflag")).scalar_one()


def _cols(fid):
    with bd.engine.connect() as c:
        return c.execute(text("SELECT paged_at, send_claim_id, send_attempts FROM outcomeflag WHERE id = :i"), {"i": fid}).one()


def _payload(fid, s=1, kind="homelab_docker_stopped"):
    return {"type": kind, "content": "x", "flag_id": fid, "flag_surfacing": s}


async def test_off_notify_never_gates(db, monkeypatch):
    _settings(monkeypatch, corr="false")
    fid = _child()
    send = AsyncMock(return_value=(True, False))
    with patch.object(telegram, "_send_payload", send), patch.object(g3b, "run_presend", AsyncMock()) as rp:
        assert await telegram.notify(_payload(fid)) is True  # a stale flag_id while OFF: today's path
    rp.assert_not_awaited()
    assert _cols(fid).paged_at is None


async def test_v1_wins_sends_and_stamps(on):
    fid = _child()
    with patch.object(telegram, "_send_payload", AsyncMock(return_value=(True, False))):
        assert await telegram.notify(_payload(fid)) is True
    assert g3b.drain_post()
    r = _cols(fid)
    assert r.paged_at is not None and r.send_attempts == 1


async def test_v1_superseded_sends_without_stamp(on):
    fid = _child(surfacing=2)
    with patch.object(telegram, "_send_payload", AsyncMock(return_value=(True, False))) as send:
        assert await telegram.notify(_payload(fid, s=1)) is True
    send.assert_awaited_once()
    assert g3b.drain_post() and _cols(fid).paged_at is None


async def test_v1_gate_error_sends_ungated(on, monkeypatch):
    fid = _child()
    monkeypatch.setattr(g3b, "gate_conn", lambda: (_ for _ in ()).throw(RuntimeError("db locked")))
    with patch.object(telegram, "_send_payload", AsyncMock(return_value=(True, False))) as send:
        assert await telegram.notify(_payload(fid)) is True
    send.assert_awaited_once()


async def test_v1_retryable_failure_queues_and_gives_back(on):
    fid = _child()
    with patch.object(telegram, "_send_payload", AsyncMock(return_value=(False, False))), \
         patch.object(telegram, "_queue_delivery") as q:
        assert await telegram.notify(_payload(fid)) is False
    q.assert_called_once()  # today's queue logic untouched
    assert g3b.drain_post()
    r = _cols(fid)
    assert r.send_claim_id is None and r.paged_at is None and r.send_attempts == 1


async def _replay(payload, send_result=(True, False)):
    rows = [{"id": 77, "delivery_type": "notify", "payload_json": json.dumps(payload)}]
    applied = {}
    with patch.object(telegram, "_load_pending", return_value=rows), \
         patch.object(telegram, "_apply_pending_results", side_effect=lambda d, f: applied.update(d=d, f=f)), \
         patch.object(telegram, "_send_payload", AsyncMock(return_value=send_result)) as send:
        await telegram.deliver_pending()
    return send, applied


async def test_v2_drops_already_paged_grouped_surfacing(on):
    p = _parent()
    fid = _child(parent=p, paged=True)
    send, applied = await _replay(_payload(fid))
    send.assert_not_awaited()
    assert applied["d"] == [77]


async def test_v2_ungrouped_row_replays_ungated(on):
    fid = _child(paged=True)  # paged but not grouped: today's replay must flow
    send, applied = await _replay(_payload(fid))
    send.assert_awaited_once()
    assert applied["d"] == [77]


async def test_v2_leased_row_left_untouched(on):
    p = _parent()
    fid = _child(parent=p)
    with bd.engine.begin() as c:
        c.execute(text("UPDATE outcomeflag SET send_claim_id = 'other', send_claimed_at = :t WHERE id = :i"),
                  {"t": g3b.db_ts(datetime.utcnow()), "i": fid})
    send, applied = await _replay(_payload(fid))
    send.assert_not_awaited()
    assert applied == {"d": [], "f": []}


async def test_off_stale_flag_id_replay_ungated(db, monkeypatch):
    _settings(monkeypatch, corr="false")
    fid = _child(paged=True)
    with patch.object(g3b, "run_presend", AsyncMock()) as rp:
        send, applied = await _replay(_payload(fid))
    rp.assert_not_awaited()
    send.assert_awaited_once()


async def test_send_release_pages_debt_once_and_never_queues(on):
    p = _parent(status="resolved")
    fid = _child(parent=p)
    with patch.object(telegram, "_send_payload", AsyncMock(return_value=(False, False))), \
         patch.object(telegram, "_queue_delivery") as q:
        assert await telegram.send_release(_payload(fid, kind="flag_released")) is False
    q.assert_not_called()
    assert _cols(fid).send_claim_id is None  # given back: next tick retries
    with patch.object(telegram, "_send_payload", AsyncMock(return_value=(True, False))):
        assert await telegram.send_release(_payload(fid, kind="flag_released")) is True
    with patch.object(telegram, "_send_payload", AsyncMock(return_value=(True, False))) as send:
        assert await telegram.send_release(_payload(fid, kind="flag_released")) is False  # already paged
    send.assert_not_awaited()
    assert _cols(fid).paged_at is not None


async def test_send_release_skips_child_of_open_parent_and_resolved_child(on):
    p = _parent()
    fid = _child(parent=p)
    with patch.object(telegram, "_send_payload", AsyncMock(return_value=(True, False))) as send:
        assert await telegram.send_release(_payload(fid, kind="flag_released")) is False
        q = _parent(status="resolved")
        gone = _child(parent=q, status="resolved")
        assert await telegram.send_release(_payload(gone, kind="flag_released")) is False
    send.assert_not_awaited()


async def test_notify_phone_release_route_keeps_gates(on, monkeypatch):
    from backend import events

    s = type("S", (), {"phone_notifications_enabled": False})()
    with patch("backend.config.get_settings", return_value=s), \
         patch.object(telegram, "send_release", AsyncMock(return_value=True)) as sr:
        assert await events.notify_phone("x", kind="flag_released", flag_id=1, flag_surfacing=1, release=True) is False
    sr.assert_not_awaited()


async def test_grouped_docker_child_not_paged_not_fired(on, monkeypatch):
    from backend.agents import homelab_watch

    _parent()
    homelab_watch.reset()
    homelab_watch._docker_states["demo-app"] = "RUNNING"
    data = type("D", (), {"docker_containers": [{"name": "demo-app", "state": "EXITED"}]})()
    with patch("backend.integrations.unraid.fetch", AsyncMock(return_value=data)), \
         patch("backend.events.notify_phone", AsyncMock(return_value=True)) as n, \
         patch.object(homelab_watch, "_maybe_auto_restart_docker", AsyncMock(return_value=False)):
        fired = await homelab_watch.check_docker()
    n.assert_not_awaited()
    assert fired == []


async def test_off_docker_page_args_unchanged(db, monkeypatch):
    from backend.agents import homelab_watch

    _settings(monkeypatch, corr="false")
    homelab_watch.reset()
    homelab_watch._docker_states["demo-app"] = "RUNNING"
    data = type("D", (), {"docker_containers": [{"name": "demo-app", "state": "EXITED"}]})()
    with patch("backend.integrations.unraid.fetch", AsyncMock(return_value=data)), \
         patch("backend.events.notify_phone", AsyncMock(return_value=True)) as n, \
         patch.object(homelab_watch, "_maybe_auto_restart_docker", AsyncMock(return_value=False)):
        fired = await homelab_watch.check_docker()
    assert fired == ["docker:demo-app"]
    assert set(n.await_args.kwargs) == {"kind", "buttons"}  # no G3b kwargs while off
