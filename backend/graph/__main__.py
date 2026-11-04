"""python -m backend.graph <cmd> — see plan-v3 §3.8 / §5.

stdout = payload only; diagnostics go to stderr. Exit: 0 ok, 1 ran but wrong, 2 refused.
"""
import argparse
import json
import logging
import os
import sqlite3
import sys
from datetime import datetime, timedelta

from backend.graph import now_iso

_RESOLVED_MIN_PCT = 85  # real vault measures 89% (targets in excluded _meta/backups dirs count as unresolved)


def _err(msg: str) -> None:
    print(msg, file=sys.stderr)


def _guard() -> None:
    """Refuse a missing/decoy DB without ever touching it (read-only open)."""
    import backend.database as bd

    p = bd.DB_PATH
    size = p.stat().st_size if p.exists() else None
    _err(f"db={p.resolve()} size={size if size is not None else 'missing'}")
    if size is None:
        sys.exit(2)
    if size < int(os.environ.get("NEXUS_GRAPH_MIN_DB_BYTES", "1000000")):
        _err("refusing: database too small (decoy?)")
        sys.exit(2)
    try:
        con = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
        try:
            ok = con.execute("SELECT 1 FROM sqlite_master WHERE name='outcomeflag'").fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        ok = None
    if not ok:
        _err("refusing: no outcomeflag table (not the NEXUS database)")
        sys.exit(2)


def _fresh(job: dict | None, minutes: int) -> tuple[bool, str]:
    if not job or not job.get("last_success"):
        return False, "never ran"
    last = datetime.fromisoformat(job["last_success"])
    age = datetime.fromisoformat(now_iso()) - last
    return age < timedelta(minutes=2 * minutes), f"last_success {job['last_success']} (< 2x{minutes} min)"


def _doctor() -> int:
    from backend.config import get_settings
    from backend.graph import enabled, intervals, store

    s = get_settings()
    h = store.health()
    bad = 0

    def line(ok: bool, msg: str) -> None:
        nonlocal bad
        bad += not ok
        print(("✓ " if ok else "✗ ") + msg)

    line(enabled(), f"graph_enabled={str(enabled()).lower()}")
    if h.get("tables_missing"):
        line(False, "tables present: missing (create_all has not run)")
    else:
        line(True, "tables present: graphnode graphedge graphchange graphjob")
    jobs = h["jobs"]
    vj = jobs.get("vault_index")
    mins = intervals(s)["vault_index"]
    ok, msg = _fresh(vj, mins)
    line(ok, f"vault index fresh: {msg}" if ok or vj else "vault index fresh: vault_index never ran")
    err = (vj or {}).get("last_error")
    line(not err, f"vault index last_error: {err or 'none'}")
    types = ("note", "tag", "host", "guest", "storage", "container", "service", "endpoint", "external")
    line(True, "nodes: " + " ".join(f"{t}={h['nodes'].get(t, 0)}" for t in types))
    etypes = ("links_to", "tagged", "runs_on", "depends_on", "backs_up_to", "documents", "resolves_via")
    line(True, "edges: " + " ".join(f"{t}={h['edges'].get(t, 0)}" for t in etypes))
    v = h["vault"]
    total = h["edges"].get("links_to", 0) + v["unresolved_links"]
    extra = f"unresolved={v['unresolved_links']}, ambiguous={v['ambiguous_stems']}, skipped_oversize={v['skipped_oversize']}, unreadable={v['unreadable']}"
    if total == 0:
        line(True, "links resolved n/a (no links indexed)")
    else:
        pct = 100 * h["edges"].get("links_to", 0) / total
        line(pct >= _RESOLVED_MIN_PCT, f"links resolved {pct:.0f}% ({extra})")
    cj = jobs.get("collect")
    ok, msg = _fresh(cj, intervals(s)["collect"])
    line(ok, f"topology fresh: {msg}" if ok or cj else "topology fresh: collect never ran")
    cerr = (cj or {}).get("last_error")
    line(not cerr, f"collect last_error: {cerr or 'none'}")
    parts, sources_ok = [], True
    for name in ("proxmox_api", "unraid_api", "uptime_targets"):
        j = jobs.get(f"source:{name}") or {}
        if j.get("last_error"):
            parts.append(f"{name} error: {j['last_error']}")
            sources_ok = False
        elif not j.get("last_success"):
            parts.append(f"{name} never ran")
            sources_ok = False
        else:
            parts.append(f"{name} ok")
    line(sources_ok, "sources: " + " · ".join(parts))
    st = h["stale"]
    line(not (st["nodes"] or st["edges"]), f"stale: nodes={st['nodes']} edges={st['edges']} (deleted after 7 days unseen)")
    ch = h["changes"]
    line(True, f"changes: rows={ch['rows']} oldest={ch['oldest'] or 'none'}")
    import backend.database as bd
    from backend.agents import release
    from backend.graph import correlate

    corr = correlate.enabled()
    line(True, f"correlation_enabled={str(corr).lower()}")
    missing = bd.missing_g3b_columns()
    line(not missing, "outcomeflag G3b columns + index" + (f": MISSING {missing}" if missing else ""))
    if not missing:
        dc = release.debt_counts()
        line(not (dc["debt"] and not corr), f"release debt: {dc['debt']}" + (" (correlation off: run `ungroup`)" if dc["debt"] and not corr else ""))
        line(dc["overdue"] == 0, f"release debt overdue (> {release.OVERDUE_MIN} min): {dc['overdue']}")
        line(True, f"grouped children not yet paged: {dc['grouped_unpaged']}")
    dang = store.dangling_edges() if h["dangling_edges"] else []
    line(not dang, "dangling edges: " + (f"{h['dangling_edges']} (e.g. {'; '.join(dang)})" if dang else "0"))
    try:
        import asyncio

        from backend.graph import topology

        res = asyncio.run(topology.audit(5))
        okn = sum(1 for _, ok, _ in res if ok)
        line(okn == len(res), f"audit {okn}/{len(res)}")
    except Exception as e:
        line(False, f"audit failed: {e}")
    return 1 if bad else 0


UNGROUP_WAIT_S = 90


async def _ungroup() -> int:
    """Rollback step 1 (plan §5.6/§8.5): detach every grouped unpaged child into release debt, page it, and
    exit 0 only when each one is stamped (release pages are never queued)."""
    import asyncio
    import time

    from sqlalchemy import text

    import backend.database as bd
    from backend.agents import release

    ids = await asyncio.to_thread(release.detach_all)
    res = await release.release_due(in_process=False)
    owed = set(ids) | set(res.get("failed", [])) | set(res.get("skipped", []))
    end = time.monotonic() + UNGROUP_WAIT_S
    while owed and time.monotonic() < end:
        with bd.engine.connect() as c:
            done = {r[0] for r in c.execute(text("SELECT id FROM outcomeflag WHERE paged_at IS NOT NULL OR status NOT IN "
                                                 "('open','needs_follow_up')")).all()}
        owed -= done
        if owed:
            await asyncio.sleep(min(5, UNGROUP_WAIT_S))
            await release.release_due(in_process=False)
    print(json.dumps({"detached": ids, "paged": res.get("paged", []), "still_owed": sorted(owed)}, indent=2))
    _err("note: this CLI cannot seed the backend's in-memory recovery notices; the backend's next tick does for the "
         "rows it pages itself")
    return 1 if owed else 0


async def _drill(a) -> int:
    """Synthetic grouping drill (plan §8.3): two clearly labelled flag rows on real node ids, nothing physical."""
    import asyncio
    import time

    from sqlalchemy import text

    import backend.database as bd
    from backend import events
    from backend.agents import outcomes, release
    from backend.graph import correlate

    if not a.yes:
        _err("refusing: pass --yes (this sends real Telegram pages)")
        return 2
    if not correlate.enabled():
        _err("refusing: GRAPH_CORRELATION_ENABLED is not on in this process")
        return 2
    if not a.child.startswith("docker:"):
        _err("--child must be docker:<name>")
        return 2

    def row(i):
        with bd.engine.connect() as c:
            return c.execute(text("SELECT id, status, parent_flag_id, paged_at, send_attempts FROM outcomeflag WHERE id = :i"),
                             {"i": i}).mappings().first()

    open_parent = await outcomes.open_flags(limit=1000)
    if any(f["source"] == "homelab_watch" and f["check"] in ("unraid_array", a.child) for f in open_parent):
        _err("baseline failed: an open unraid_array or child flag already exists")
        return 2
    out = {"parent": None, "child": None, "grouped": False, "released": False}
    try:
        if not a.no_parent:
            p = await outcomes.record_flag_ex("homelab_watch", "unraid_array", "G3 DRILL — synthetic parent (no real array problem)", severity="low")
            out["parent"] = p["id"]
            await events.notify_phone("G3 DRILL — synthetic parent", kind="homelab_array",
                                      **outcomes.coord_kwargs(p, "homelab_watch", "unraid_array"))
            end = time.monotonic() + 120
            while not (row(p["id"]) or {}).get("paged_at") and time.monotonic() < end:
                await asyncio.sleep(1)
            if not row(p["id"])["paged_at"]:
                _err("parent page was not accepted in time: abort")
                return 1
        c = await outcomes.record_flag_ex("homelab_watch", a.child, "G3 DRILL — synthetic child", severity="low")
        out["child"] = c["id"]
        out["grouped"] = outcomes.is_grouped(c)
        if a.no_parent:
            print(json.dumps(out, indent=2))
            return 0 if c["surface"] and not out["grouped"] else 1
        if not out["grouped"]:
            _err(f"child was not grouped: {c}")
            return 1
        if a.via == "resolve":
            await outcomes.resolve_flag(out["parent"], "resolved", note="G3 drill", by="drill")
        else:
            await asyncio.to_thread(release.detach_all)
        await release.release_due(in_process=False)
        out["released"] = bool((row(c["id"]) or {}).get("paged_at"))
        out["child_send_attempts"] = (row(c["id"]) or {}).get("send_attempts")
        print(json.dumps(out, indent=2))
        return 0 if out["released"] else 1
    finally:
        for fid in (out.get("child"), out.get("parent")):
            if fid:
                await outcomes.resolve_flag(fid, "resolved", note="G3 drill cleanup", by="drill")


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(stream=sys.stderr)
    ap = argparse.ArgumentParser(prog="python -m backend.graph")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("index").add_argument("--full", action="store_true")
    sub.add_parser("health")
    sub.add_parser("doctor")
    sub.add_parser("drop").add_argument("--yes", action="store_true")
    sub.add_parser("rebuild")
    sub.add_parser("collect")
    sub.add_parser("audit").add_argument("--sample", type=int, default=5)
    sub.add_parser("ungroup")
    d = sub.add_parser("drill")
    d.add_argument("--child", required=True, help="docker:<running container name>")
    d.add_argument("--yes", action="store_true")
    d.add_argument("--no-parent", action="store_true")
    d.add_argument("--via", choices=("resolve", "ungroup"), default="resolve")
    try:
        a = ap.parse_args(argv)
    except SystemExit as e:
        return 2 if e.code else 0
    _guard()
    from sqlmodel import SQLModel

    import backend.database as bd
    from backend.graph import store

    if a.cmd == "drop" and not a.yes:  # refuse before create_all touches the schema
        _err("refusing: pass --yes")
        return 2
    SQLModel.metadata.create_all(bd.engine)
    if a.cmd == "drop":
        store.drop_tables()
        _err("stop nexus-backend first or set GRAPH_ENABLED=false — create_all recreates empty tables at next boot")
        return 0
    if a.cmd == "health":
        print(json.dumps(store.health(), indent=2))
        return 0
    if a.cmd == "doctor":
        return _doctor()
    if a.cmd == "ungroup":
        import asyncio

        return asyncio.run(_ungroup())
    if a.cmd == "drill":
        import asyncio

        return asyncio.run(_drill(a))
    from backend.graph import vault_index

    if a.cmd == "audit":
        import asyncio

        from backend.graph import topology

        try:
            res = asyncio.run(topology.audit(a.sample))
        except Exception as e:
            _err(f"audit failed: {e}")
            return 1
        for label, ok, detail in res:
            if detail.startswith("source unavailable"):
                print(f"? {label} ({detail})")
            else:
                print(("\u2713 " if ok else "\u2717 ") + label + ("" if ok else f" (live: {detail})"))
        return 0 if all(ok for _, ok, _ in res) else 1

    if a.cmd in ("rebuild", "collect"):
        import asyncio

        from backend.graph import topology

        try:
            out: dict = {}
            if a.cmd == "rebuild":
                store.drop_all()
                out["index"] = vault_index.refresh(full=True)
            out["collect"] = asyncio.run(topology.collect())
        except Exception as e:
            _err(f"{a.cmd} failed: {e}")
            return 1
        print(json.dumps(out if a.cmd == "rebuild" else out["collect"], indent=2))
        return 1 if any(v != "ok" for v in out["collect"]["sources"].values()) else 0

    try:
        print(json.dumps(vault_index.refresh(full=a.full), indent=2))
    except Exception as e:
        _err(f"index failed: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
