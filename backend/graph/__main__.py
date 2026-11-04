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
    line(not (st["nodes"] or st["edges"]), f"stale: nodes={st['nodes']} edges={st['edges']}")
    ch = h["changes"]
    line(True, f"changes: rows={ch['rows']} oldest={ch['oldest'] or 'none'}")
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
