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
    from backend.graph import enabled, store

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
    mins = max(5, s.graph_vault_index_minutes)
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
    return 1 if bad else 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(stream=sys.stderr)
    ap = argparse.ArgumentParser(prog="python -m backend.graph")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("index").add_argument("--full", action="store_true")
    sub.add_parser("health")
    sub.add_parser("doctor")
    sub.add_parser("drop").add_argument("--yes", action="store_true")
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

    try:
        print(json.dumps(vault_index.refresh(full=a.full), indent=2))
    except Exception as e:
        _err(f"index failed: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
