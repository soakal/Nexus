"""G3b release reconciler: page grouped children whose parent closed (plan g3/plan-v12-g3b.md §5.4, §5.6).

Release debt is derived, never stored: a row is owed a page when it is grouped (or detached by `ungroup`),
still open, unsuppressed, its current surfacing is unpaged, and its parent is no longer open. Runs from the
60 s retry_deliveries tick, at boot, when a parent is resolved/cleared, and from the CLI. A background tick,
not a page path. With correlation OFF and no grouped rows it does one read-only EXISTS and stops.
"""
from __future__ import annotations

import asyncio
import html
import logging
import uuid
from datetime import datetime, timedelta

from sqlalchemy import text

logger = logging.getLogger(__name__)

OVERDUE_MIN = 30
_ACTIVE = "('open','needs_follow_up')"
_GROUPED = "(parent_flag_id IS NOT NULL OR released_from_parent_id IS NOT NULL)"
_DEBT = (
    f"paged_at IS NULL AND status IN {_ACTIVE} AND suppressed = 0 AND ("
    f"(parent_flag_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM outcomeflag p WHERE p.id = outcomeflag.parent_flag_id "
    f"AND p.status IN {_ACTIVE})) OR (parent_flag_id IS NULL AND released_from_parent_id IS NOT NULL))"
)


def _ts(dt: datetime) -> str:
    from backend.agents.g3b import db_ts
    return db_ts(dt)


def _scope(parent_id: int | None) -> tuple[str, dict]:
    if parent_id is None:
        return "", {}
    return " AND (parent_flag_id = :pid OR released_from_parent_id = :pid)", {"pid": parent_id}


def _adopt(gc, now: datetime) -> int:
    """Attach open, unattached flags on descendant nodes to recently PAGED parents (rule 2: only after the
    parent's page was accepted). Children paged before their parent are adopted here (<= 60 s later).
    Discovery is read-only; each attach is ONE fenced primary-key UPDATE in its own autocommit statement on the
    short-timeout connection, so no write lock is held across the pass (plan §5.3.8)."""
    from backend.graph import correlate, freshness, intervals, store

    last = (store.jobs().get("collect") or {}).get("last_success")
    if last is None or now - datetime.fromisoformat(last) >= timedelta(minutes=freshness.STALE_RUNS * intervals()["collect"]):
        return 0  # graph stale: grouping pauses
    window_start = _ts(now - timedelta(minutes=correlate.GROUP_WINDOW_MIN))
    n = 0
    for (src, chk) in correlate.PARENT_CHECKS:
        pnode = correlate.node_for_flag(src, chk)
        down = {i for i, _ in store.descendants(pnode, correlate.GROUP_DEPTH)} if pnode else set()
        if not down:
            continue
        parents = [r[0] for r in gc.execute(
            f"SELECT id FROM outcomeflag WHERE source = :s AND \"check\" = :c AND status IN {_ACTIVE} AND suppressed = 0 "
            "AND paged_at IS NOT NULL AND created_at >= :ws ORDER BY id DESC", {"s": src, "c": chk, "ws": window_start}).fetchall()]
        if not parents:
            continue
        pid = parents[0]  # newest eligible parent
        kids = [r for r in gc.execute(
            f"SELECT id, source, \"check\" FROM outcomeflag WHERE id != :p AND parent_flag_id IS NULL "
            f"AND released_from_parent_id IS NULL AND status IN {_ACTIVE} AND suppressed = 0 AND created_at >= :ws",
            {"p": pid, "ws": window_start}).fetchall()
            if (r[1], r[2]) not in correlate.PARENT_CHECKS and correlate.node_for_flag(r[1], r[2]) in down]
        for kid in kids:
            n += gc.execute(
                f"UPDATE outcomeflag SET parent_flag_id = :p, attach_op = :op, updated_at = :now WHERE id = :c "
                f"AND parent_flag_id IS NULL AND released_from_parent_id IS NULL AND status IN {_ACTIVE} AND suppressed = 0 "
                f"AND EXISTS (SELECT 1 FROM outcomeflag q WHERE q.id = :p AND q.status IN {_ACTIVE} AND q.suppressed = 0 "
                "AND q.paged_at IS NOT NULL AND q.created_at >= :ws)",
                {"p": pid, "op": uuid.uuid4().hex, "now": _ts(now), "c": kid[0], "ws": window_start}).rowcount
    return n


def _db_tick(corr: bool, parent_id: int | None) -> dict | None:
    from backend.agents import g3b
    from backend.database import engine

    now = datetime.utcnow()
    if not corr:
        with engine.connect() as c:  # read-only guard
            if not c.execute(text(f"SELECT EXISTS(SELECT 1 FROM outcomeflag WHERE {_GROUPED} AND status IN {_ACTIVE})")).scalar_one():
                return None  # OFF and nothing grouped: no writes at all
    scope, params = _scope(parent_id)
    gc = g3b.gate_conn()  # autocommit, 1 s busy timeout: every statement below is its own short transaction
    try:
        stamped = gc.execute(f"UPDATE outcomeflag SET debt_since = :now WHERE debt_since IS NULL AND {_DEBT}{scope}",
                             {"now": _ts(now), **params}).rowcount
        adopted = _adopt(gc, now) if corr else 0  # ON: always, even with no grouping pointers yet
        ids = [r[0] for r in gc.execute(
            f"SELECT id FROM outcomeflag WHERE {_DEBT}{scope} AND (send_claim_id IS NULL OR send_claimed_at < :cut) "
            "ORDER BY id", {"cut": _ts(now - timedelta(minutes=10)), **params}).fetchall()]
        rows = [dict(gc.execute(
            "SELECT o.id, o.source, o.\"check\", o.summary, o.surfaced_count, o.parent_flag_id, o.released_from_parent_id, "
            "p.source AS psource, p.\"check\" AS pcheck FROM outcomeflag o LEFT JOIN outcomeflag p "
            "ON p.id = COALESCE(o.parent_flag_id, o.released_from_parent_id) WHERE o.id = :i", {"i": i}).fetchone())
            for i in ids]
    finally:
        gc.close()
    return {"stamped": stamped, "adopted": adopted, "candidates": rows}


def _pending_for_flag(flag_id: int, surfacing: int) -> bool:
    """A LIVE queued first page for this flag's CURRENT surfacing (that replay owns the delivery). A dead-lettered
    row (attempts at the cap: deliver_pending never loads it again) or a stale surfacing does not count, so the
    reconciler takes the debt over instead of skipping it forever."""
    from backend.database import engine
    from backend.integrations.telegram import _MAX_ATTEMPTS

    with engine.connect() as c:
        return bool(c.execute(text(
            "SELECT 1 FROM pendingdelivery WHERE delivery_type = 'notify' AND attempts < :cap "
            "AND json_extract(payload_json, '$.flag_id') = :i AND json_extract(payload_json, '$.flag_surfacing') = :s LIMIT 1"),
            {"i": flag_id, "s": surfacing, "cap": _MAX_ATTEMPTS}).first())


def _message(c: dict) -> tuple[str, list]:
    from backend.agents.incident_diag import _fix_button

    parent = c.get("parent_flag_id") or c.get("released_from_parent_id")
    how = "which closed" if c.get("parent_flag_id") else "released by ungroup"
    ptxt = f" ({html.escape(str(c.get('psource')))}:{html.escape(str(c.get('pcheck')))})" if c.get("psource") else ""
    text_ = f"#{c['id']} {html.escape(c['summary'])} — was grouped under #{parent}{ptxt}, {how}."
    buttons = list(_fix_button(c["check"]) or []) if c["source"] == "homelab_watch" else []
    buttons += [{"text": "✓ Resolved", "callback_data": f"flag:resolved:{c['id']}"},
                {"text": "✗ False alarm", "callback_data": f"flag:false_positive:{c['id']}"}]
    return text_, buttons


async def release_due(parent_id: int | None = None, *, in_process: bool = True) -> dict:
    """Page every child owed a release page (optionally only one parent's). Never raises."""
    out = {"paged": [], "skipped": [], "failed": []}
    try:
        from backend.graph import correlate

        tick = await asyncio.to_thread(_db_tick, correlate.enabled(), parent_id)
        if tick is None:
            return {**out, "idle": True}
        out.update(stamped=tick["stamped"], adopted=tick["adopted"])
        from backend import events

        for c in tick["candidates"]:
            if await asyncio.to_thread(_pending_for_flag, c["id"], c["surfaced_count"]):
                out["skipped"].append(c["id"])  # its own first page is still queued: that replay owns it
                continue
            msg, buttons = _message(c)
            ok = await events.notify_phone(msg, kind="flag_released", buttons=buttons, flag_id=c["id"],
                                           flag_surfacing=c["surfaced_count"], release=True)
            (out["paged"] if ok else out["failed"]).append(c["id"])
            if ok and in_process and c["source"] == "homelab_watch":
                try:  # recovery notice when the child's condition clears (backend process only)
                    from backend.agents import homelab_watch
                    homelab_watch._paged_alerts.add(c["check"])
                except Exception:
                    pass
        return out
    except Exception as e:
        logger.warning(f"release_due failed (retried next tick): {e}")
        return {**out, "error": str(e)}


def detach_all() -> list[int]:
    """`ungroup`: turn every grouped, open, unpaged child into durable release debt (plan §5.6)."""
    from backend.database import engine

    now = _ts(datetime.utcnow())
    with engine.begin() as c:
        return list(c.execute(text(
            f"UPDATE outcomeflag SET released_from_parent_id = parent_flag_id, parent_flag_id = NULL, attach_op = NULL, "
            f"debt_since = COALESCE(debt_since, :now), updated_at = :now WHERE parent_flag_id IS NOT NULL "
            f"AND status IN {_ACTIVE} AND paged_at IS NULL RETURNING id"), {"now": now}).scalars().all())


def debt_counts() -> dict:
    """For doctor: open release debt and how much of it is overdue (debt_since older than OVERDUE_MIN)."""
    from backend.database import engine

    cut = _ts(datetime.utcnow() - timedelta(minutes=OVERDUE_MIN))
    with engine.connect() as c:
        debt = c.execute(text(f"SELECT COUNT(*) FROM outcomeflag WHERE {_DEBT}")).scalar_one()
        overdue = c.execute(text(f"SELECT COUNT(*) FROM outcomeflag WHERE {_DEBT} AND debt_since < :cut"), {"cut": cut}).scalar_one()
        grouped = c.execute(text(f"SELECT COUNT(*) FROM outcomeflag WHERE {_GROUPED} AND status IN {_ACTIVE} AND paged_at IS NULL")).scalar_one()
    return {"debt": debt, "overdue": overdue, "grouped_unpaged": grouped}
