"""Knowledge-graph storage: the only module with graph SQL (plan-v3 section 3.2).

All helpers are sync (callers use asyncio.to_thread). Every helper imports
`engine` inside its body so test monkeypatches of backend.database.engine apply.
Write helpers take `conn`; None means "open my own batch()".
"""
from __future__ import annotations

import json
import re
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

from sqlalchemy import bindparam, text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import OperationalError

_TRAVERSAL_TYPES = ("runs_on", "depends_on")
_JOB_NAMES_TABLES = ("graphnode", "graphedge", "graphchange", "graphjob")
_CHUNK = 500


@dataclass(frozen=True)
class Node:
    id: str
    type: str
    name: str
    aliases: tuple[str, ...] = ()
    attrs: dict | None = None
    source: str = "manual"


@dataclass(frozen=True)
class Edge:
    src: str
    dst: str
    type: str
    source: str
    source_hash: str | None = field(default=None)


@contextmanager
def batch() -> Iterator[Connection]:
    from backend.database import engine

    with engine.begin() as conn:
        yield conn


@contextmanager
def _use(conn: Connection | None) -> Iterator[Connection]:
    if conn is not None:
        yield conn
    else:
        with batch() as c:
            yield c


@contextmanager
def _read(conn: Connection | None = None) -> Iterator[Connection]:
    if conn is not None:
        yield conn
        return
    from backend.database import engine

    with engine.connect() as c:
        yield c


def _chunks(items: list, n: int = _CHUNK) -> Iterator[list]:
    for i in range(0, len(items), n):
        yield items[i : i + n]


def _loads(value: str | None):
    return json.loads(value) if value else None


def _node_row(r) -> dict:
    return {
        "id": r.id,
        "type": r.type,
        "name": r.name,
        "aliases": _loads(r.aliases) or [],
        "attrs": _loads(r.attrs),
        "source": r.source,
        "status": r.status,
    }


_NODE_COLS = "id, type, name, aliases, attrs, source, status"

# --------------------------------------------------------------------------- writes

# :patch keeps explicit None so json_patch deletes keys the source no longer reports; :attrs (insert) drops them
_MERGE_ATTRS = "json_patch(COALESCE(graphnode.attrs, '{}'), COALESCE(:patch, '{}'))"


def upsert_nodes(
    nodes: Iterable[Node],
    seen_at: str,
    conn: Connection | None = None,
    *,
    gen: int | None = None,
    merge_attrs: bool = True,
) -> int:
    rows = [
        {
            "id": n.id,
            "type": n.type,
            "name": n.name,
            "aliases": json.dumps(list(n.aliases)) if n.aliases else None,
            "attrs": json.dumps({k: v for k, v in n.attrs.items() if v is not None}, sort_keys=True) if n.attrs is not None else None,
            "patch": json.dumps(n.attrs, sort_keys=True) if n.attrs is not None else None,
            "source": n.source,
            "ts": seen_at,
            "gen": gen,
        }
        for n in nodes
    ]
    if not rows:
        return 0
    attrs_expr = _MERGE_ATTRS if merge_attrs else "excluded.attrs"
    stmt = text(
        "INSERT INTO graphnode(id, type, name, aliases, attrs, source, status, first_seen, last_seen, gen) "
        "VALUES(:id, :type, :name, :aliases, :attrs, :source, 'active', :ts, :ts, :gen) "
        "ON CONFLICT(id) DO UPDATE SET name = excluded.name, aliases = excluded.aliases, "
        f"attrs = {attrs_expr}, status = 'active', last_seen = excluded.last_seen, gen = excluded.gen"
    )
    with _use(conn) as c:
        c.execute(stmt, rows)
    return len(rows)


def upsert_edges(
    edges: Iterable[Edge], seen_at: str, conn: Connection | None = None, *, gen: int | None = None
) -> int:
    rows = [
        {"src": e.src, "dst": e.dst, "type": e.type, "source": e.source, "hash": e.source_hash, "ts": seen_at, "gen": gen}
        for e in edges
    ]
    if not rows:
        return 0
    stmt = text(
        "INSERT INTO graphedge(src, dst, type, source, status, source_hash, first_seen, last_seen, gen) "
        "VALUES(:src, :dst, :type, :source, 'active', :hash, :ts, :ts, :gen) "
        "ON CONFLICT(src, dst, type, source) DO UPDATE SET status = 'active', "
        "last_seen = excluded.last_seen, gen = excluded.gen, source_hash = excluded.source_hash"
    )
    with _use(conn) as c:
        c.execute(stmt, rows)
    return len(rows)


def delete_edges(
    src: str | None = None,
    *,
    source: str | tuple[str, ...] | None = None,
    type: str | None = None,
    not_in: set[tuple] | None = None,
    conn: Connection | None = None,
) -> int:
    where, params = [], {}
    if src is not None:
        where.append("src = :src")
        params["src"] = src
    if type is not None:
        where.append("type = :type")
        params["type"] = type
    sources = (source,) if isinstance(source, str) else source
    if sources is not None:
        where.append("source IN :sources")
    clause = (" WHERE " + " AND ".join(where)) if where else ""

    def _bind(stmt):
        return stmt.bindparams(bindparam("sources", expanding=True)) if sources is not None else stmt

    if sources is not None:
        params["sources"] = list(sources)
    with _use(conn) as c:
        if not_in is None:
            return c.execute(_bind(text("DELETE FROM graphedge" + clause)), params).rowcount
        # shortcut: select-then-delete-by-PK instead of a row-value NOT IN; fine at this scale.
        found = c.execute(_bind(text("SELECT src, dst, type, source FROM graphedge" + clause)), params).all()
        doomed = [
            {"src": r.src, "dst": r.dst, "type": r.type, "source": r.source}
            for r in found
            if (r.src, r.dst, r.type) not in not_in
        ]
        if doomed:
            c.execute(text("DELETE FROM graphedge WHERE src = :src AND dst = :dst AND type = :type AND source = :source"), doomed)
        return len(doomed)


def delete_nodes(ids: Iterable[str], conn: Connection | None = None) -> int:
    id_list = list(ids)
    if not id_list:
        return 0
    n = 0
    with _use(conn) as c:
        for part in _chunks(id_list):
            c.execute(
                text("DELETE FROM graphedge WHERE src IN :ids OR dst IN :ids").bindparams(bindparam("ids", expanding=True)),
                {"ids": part},
            )
            n += c.execute(
                text("DELETE FROM graphnode WHERE id IN :ids").bindparams(bindparam("ids", expanding=True)), {"ids": part}
            ).rowcount
    return n


def delete_orphan_tags(conn: Connection | None = None) -> int:
    with _use(conn) as c:
        return c.execute(
            text("DELETE FROM graphnode WHERE type = 'tag' AND id NOT IN (SELECT dst FROM graphedge WHERE type = 'tagged')")
        ).rowcount


def bump_generation(source: str, conn: Connection) -> int:
    return conn.execute(
        text(
            "INSERT INTO graphjob(name, generation) VALUES ('source:' || :source, 1) "
            "ON CONFLICT(name) DO UPDATE SET generation = generation + 1 RETURNING generation"
        ),
        {"source": source},
    ).scalar_one()


def mark_stale(
    source: str, max_gen: int, conn: Connection | None = None
) -> tuple[list[str], list[tuple[str, str, str]]]:
    """Rows that transitioned active -> stale in this call (node ids, (src, dst, type) edges)."""
    p = {"source": source, "g": max_gen}
    with _use(conn) as c:
        n = c.execute(
            text(
                "UPDATE graphnode SET status = 'stale' WHERE source = :source AND status = 'active' AND gen <= :g "
                "RETURNING id"
            ),
            p,
        ).scalars().all()
        e = c.execute(
            text(
                "UPDATE graphedge SET status = 'stale' WHERE source = :source AND status = 'active' AND gen <= :g "
                "RETURNING src, dst, type"
            ),
            p,
        ).all()
    return sorted(n), sorted((r.src, r.dst, r.type) for r in e)


def insert_changes(rows: list[dict], conn: Connection | None = None) -> int:
    """rows: {ts, node_id, field, old, new, source}."""
    if not rows:
        return 0
    stmt = text(
        "INSERT INTO graphchange(ts, node_id, field, old, new, source) VALUES(:ts, :node_id, :field, :old, :new, :source)"
    )
    with _use(conn) as c:
        for part in _chunks(rows):
            c.execute(stmt, part)
    return len(rows)


def insert_change(ts: str, node_id: str, field: str, old: str | None, new: str | None, source: str,
                  conn: Connection | None = None) -> int:
    with _use(conn) as c:
        return c.execute(
            text(
                "INSERT INTO graphchange(ts, node_id, field, old, new, source) "
                "VALUES(:ts, :n, :f, :o, :w, :s) RETURNING id"
            ),
            {"ts": ts, "n": node_id, "f": field, "o": old, "w": new, "s": source},
        ).scalar_one()


def prune_changes(before_ts: str) -> int:
    with batch() as c:
        return c.execute(text("DELETE FROM graphchange WHERE ts < :b"), {"b": before_ts}).rowcount


def record_job(
    name: str, *, ok: bool, error: str | None = None, stats: dict | None = None, at: str, conn: Connection | None = None
) -> None:
    with _use(conn) as c:
        if ok:
            c.execute(
                text(
                    "INSERT INTO graphjob(name, last_success, last_error, last_error_at, stats, generation) "
                    "VALUES(:n, :at, NULL, NULL, :stats, 0) "
                    "ON CONFLICT(name) DO UPDATE SET last_success = excluded.last_success, "
                    "last_error = NULL, last_error_at = NULL, stats = excluded.stats"
                ),
                {"n": name, "at": at, "stats": json.dumps(stats) if stats is not None else None},
            )
        else:
            c.execute(
                text(
                    "INSERT INTO graphjob(name, last_error, last_error_at, generation) VALUES(:n, :err, :at, 0) "
                    "ON CONFLICT(name) DO UPDATE SET last_error = excluded.last_error, last_error_at = excluded.last_error_at"
                ),
                {"n": name, "err": error, "at": at},
            )


def drop_all(conn: Connection | None = None) -> None:
    with _use(conn) as c:
        for t in ("graphnode", "graphedge", "graphchange"):
            c.execute(text(f"DELETE FROM {t}"))


def drop_tables() -> None:
    with batch() as c:
        for t in _JOB_NAMES_TABLES:
            c.execute(text(f"DROP TABLE IF EXISTS {t}"))


# --------------------------------------------------------------------------- reads


def stale_counts() -> dict[str, int]:
    with _read() as c:
        return {
            "nodes": c.execute(text("SELECT COUNT(*) FROM graphnode WHERE status = 'stale'")).scalar_one(),
            "edges": c.execute(text("SELECT COUNT(*) FROM graphedge WHERE status = 'stale'")).scalar_one(),
        }


def jobs() -> dict[str, dict]:
    try:
        with _read() as c:
            rows = c.execute(
                text("SELECT name, last_success, last_error, last_error_at, stats, generation FROM graphjob")
            ).all()
    except OperationalError:
        return {}
    return {
        r.name: {
            "last_success": r.last_success,
            "last_error": r.last_error,
            "last_error_at": r.last_error_at,
            "stats": _loads(r.stats),
            "generation": r.generation,
        }
        for r in rows
    }


def get_node(node_id: str) -> dict | None:
    with _read() as c:
        r = c.execute(text(f"SELECT {_NODE_COLS} FROM graphnode WHERE id = :id"), {"id": node_id}).first()
    return _node_row(r) if r else None


def list_nodes(
    *,
    types: Iterable[str] | None = None,
    source: str | None = None,
    status: str | None = None,
    conn: Connection | None = None,
) -> list[dict]:
    where, params = [], {}
    stmt_binds = []
    if types is not None:
        where.append("type IN :types")
        params["types"] = sorted(types)
        stmt_binds.append(bindparam("types", expanding=True))
    if source is not None:
        where.append("source = :source")
        params["source"] = source
    if status is not None:
        where.append("status = :status")
        params["status"] = status
    sql = f"SELECT {_NODE_COLS} FROM graphnode" + ((" WHERE " + " AND ".join(where)) if where else "") + " ORDER BY id"
    stmt = text(sql).bindparams(*stmt_binds)
    with _read(conn) as c:
        return [_node_row(r) for r in c.execute(stmt, params).all()]


def edge_keys(*, source: str, status: str | None = "active", conn: Connection | None = None) -> dict[tuple[str, str, str], str]:
    sql = "SELECT src, dst, type, status FROM graphedge WHERE source = :source"
    params = {"source": source}
    if status is not None:
        sql += " AND status = :status"
        params["status"] = status
    with _read(conn) as c:
        return {(r.src, r.dst, r.type): r.status for r in c.execute(text(sql), params).all()}


def changes_for(node_ids: Iterable[str], *, since: str, before: str | None = None, limit: int = 100) -> list[dict]:
    """Newest first. ts strings are naive-UTC ISO, so string order == time order."""
    ids = list(dict.fromkeys(node_ids))
    if not ids:
        return []
    sql = "SELECT id, ts, node_id, field, old, new, source FROM graphchange WHERE node_id IN :ids AND ts >= :since"
    params: dict = {"since": since, "limit": limit}
    if before is not None:
        sql += " AND ts < :before"
        params["before"] = before
    stmt = text(sql + " ORDER BY ts DESC, id DESC LIMIT :limit").bindparams(bindparam("ids", expanding=True))
    out: list[dict] = []
    with _read() as c:
        for part in _chunks(ids):
            out += [dict(r._mapping) for r in c.execute(stmt, {**params, "ids": part}).all()]
    out.sort(key=lambda r: (r["ts"], r["id"]), reverse=True)
    return out[:limit]


_VMID_REF = re.compile(r"^\s*(?:ct|vm|lxc|qemu)?\s*(\d+)\s*$", re.IGNORECASE)


def resolve_node(ref: str) -> list[dict]:
    """exact id -> case-insensitive name/alias -> 'CT 209' / 'VM 109' / bare vmid -> []. Returns all candidates."""
    with _read() as c:
        r = c.execute(text(f"SELECT {_NODE_COLS} FROM graphnode WHERE id = :r"), {"r": ref}).first()
        if r:
            return [_node_row(r)]
        low = ref.strip().lower()
        rows = c.execute(
            text(
                f"SELECT {_NODE_COLS} FROM graphnode WHERE lower(name) = :r "
                "OR EXISTS (SELECT 1 FROM json_each(graphnode.aliases) WHERE lower(value) = :r) ORDER BY id"
            ),
            {"r": low},
        ).all()
        if rows:
            return [_node_row(x) for x in rows]
        m = _VMID_REF.match(ref)
        if m:
            r = c.execute(text(f"SELECT {_NODE_COLS} FROM graphnode WHERE id = :r"), {"r": f"guest:{int(m.group(1))}"}).first()
            if r:
                return [_node_row(r)]
    return []


def _walk(node_id: str, max_depth: int, forward: bool) -> list[tuple[str, int]]:
    frm, to = ("src", "dst") if forward else ("dst", "src")
    sql = (
        "WITH RECURSIVE walk(id, depth) AS ("
        "  SELECT :start, 0 "
        "  UNION "
        f"  SELECT e.{to}, w.depth + 1 FROM walk w JOIN graphedge e ON e.{frm} = w.id "
        "  WHERE e.type IN :types AND e.status = 'active' AND w.depth < :maxd"
        ") SELECT id, MIN(depth) AS d FROM walk WHERE id != :start GROUP BY id ORDER BY d, id"
    )
    stmt = text(sql).bindparams(bindparam("types", expanding=True))
    with _read() as c:
        rows = c.execute(stmt, {"start": node_id, "types": list(_TRAVERSAL_TYPES), "maxd": max_depth}).all()
    return [(r.id, r.d) for r in rows]


def ancestors(node_id: str, max_depth: int = 3) -> list[tuple[str, int]]:
    """What node_id runs on / depends on (follows src -> dst)."""
    return _walk(node_id, max_depth, True)


def descendants(node_id: str, max_depth: int = 3) -> list[tuple[str, int]]:
    """What depends on / runs on node_id (follows dst -> src)."""
    return _walk(node_id, max_depth, False)


def neighbors(node_id: str, depth: int = 1, types: tuple[str, ...] = ("links_to",), limit: int = 50) -> list[dict]:
    """Breadth-first over both edge directions; {id, type, name, edge_type, direction, depth}."""
    seen = {node_id}
    frontier = [node_id]
    out: list[dict] = []
    stmt = text(
        "SELECT e.src, e.dst, e.type AS etype, n.id, n.type, n.name, "
        "CASE WHEN e.src = :cur THEN 'out' ELSE 'in' END AS direction "
        "FROM graphedge e JOIN graphnode n ON n.id = CASE WHEN e.src = :cur THEN e.dst ELSE e.src END "
        "WHERE (e.src = :cur OR e.dst = :cur) AND e.type IN :types AND e.status = 'active' ORDER BY n.id"
    ).bindparams(bindparam("types", expanding=True))
    with _read() as c:
        for d in range(1, depth + 1):
            nxt = []
            for cur in frontier:
                for r in c.execute(stmt, {"cur": cur, "types": list(types)}).all():
                    if r.id in seen:
                        continue
                    seen.add(r.id)
                    nxt.append(r.id)
                    out.append(
                        {"id": r.id, "type": r.type, "name": r.name, "edge_type": r.etype, "direction": r.direction, "depth": d}
                    )
                    if len(out) >= limit:
                        return out
            frontier = nxt
    return out


def documents_for(node_ids: Iterable[str]) -> dict[str, list[str]]:
    ids = list(node_ids)
    if not ids:
        return {}
    out: dict[str, list[str]] = {}
    stmt = text("SELECT dst, src FROM graphedge WHERE type = 'documents' AND dst IN :ids ORDER BY dst, src").bindparams(
        bindparam("ids", expanding=True)
    )
    with _read() as c:
        for part in _chunks(ids):
            for r in c.execute(stmt, {"ids": part}).all():
                out.setdefault(r.dst, []).append(r.src)
    return out


def edges_of(node_id: str) -> list[dict]:
    with _read() as c:
        rows = c.execute(
            text(
                "SELECT dst AS other, type, 'out' AS direction, source, status FROM graphedge WHERE src = :n "
                "UNION ALL "
                "SELECT src AS other, type, 'in' AS direction, source, status FROM graphedge WHERE dst = :n "
                "ORDER BY type, direction, other"
            ),
            {"n": node_id},
        ).all()
    return [dict(r._mapping) for r in rows]


def health() -> dict:
    """Counts + job freshness. Tolerates dropped tables (returns tables_missing)."""
    empty = {
        "nodes": {},
        "edges": {},
        "stale": {"nodes": 0, "edges": 0},
        "sources": {},
        "jobs": {},
        "vault": {"notes": 0, "unresolved_links": 0, "ambiguous_stems": 0, "skipped_oversize": 0, "unreadable": 0},
        "dangling_edges": 0,
        "changes": {"rows": 0, "oldest": None},
    }
    try:
        with _read() as c:
            nodes = {r.type: r.n for r in c.execute(text("SELECT type, COUNT(*) AS n FROM graphnode GROUP BY type"))}
            edges = {r.type: r.n for r in c.execute(text("SELECT type, COUNT(*) AS n FROM graphedge GROUP BY type"))}
            sources: dict[str, dict] = {}
            for r in c.execute(text("SELECT source, COUNT(*) AS n, MAX(last_seen) AS ls FROM graphnode GROUP BY source")):
                sources[r.source] = {"nodes": r.n, "edges": 0, "last_seen": r.ls}
            for r in c.execute(text("SELECT source, COUNT(*) AS n, MAX(last_seen) AS ls FROM graphedge GROUP BY source")):
                s = sources.setdefault(r.source, {"nodes": 0, "edges": 0, "last_seen": None})
                s["edges"] = r.n
                if r.ls and (s["last_seen"] is None or r.ls > s["last_seen"]):
                    s["last_seen"] = r.ls
            sums = c.execute(
                text(
                    "SELECT COALESCE(SUM(json_extract(attrs, '$.unresolved')), 0) AS u, "
                    "COALESCE(SUM(json_extract(attrs, '$.ambiguous')), 0) AS a FROM graphnode WHERE type = 'note'"
                )
            ).one()
            dangling = c.execute(
                text(
                    "SELECT COUNT(*) FROM graphedge e WHERE NOT EXISTS (SELECT 1 FROM graphnode WHERE id = e.src) "
                    "OR NOT EXISTS (SELECT 1 FROM graphnode WHERE id = e.dst)"
                )
            ).scalar_one()
            ch = c.execute(text("SELECT COUNT(*) AS n, MIN(ts) AS oldest FROM graphchange")).one()
        stale = stale_counts()
    except OperationalError:
        return {"tables_missing": True, **empty}
    j = jobs()
    vstats = (j.get("vault_index") or {}).get("stats") or {}
    return {
        "nodes": nodes,
        "edges": edges,
        "stale": stale,
        "sources": sources,
        "jobs": j,
        "vault": {
            "notes": nodes.get("note", 0),
            "unresolved_links": int(sums.u),
            "ambiguous_stems": int(sums.a),
            "skipped_oversize": vstats.get("skipped_oversize", 0),
            "unreadable": vstats.get("unreadable", 0),
        },
        "dangling_edges": dangling,
        "changes": {"rows": ch.n, "oldest": ch.oldest},
    }


def dangling_edges(limit: int = 5) -> list[str]:
    """'src -type-> dst' for edges whose src or dst has no node row."""
    with _read() as c:
        rows = c.execute(
            text(
                "SELECT src, type, dst FROM graphedge e WHERE NOT EXISTS (SELECT 1 FROM graphnode WHERE id = e.src) "
                "OR NOT EXISTS (SELECT 1 FROM graphnode WHERE id = e.dst) ORDER BY src, dst LIMIT :n"
            ),
            {"n": limit},
        ).all()
    return [f"{r.src} -{r.type}-> {r.dst}" for r in rows]


def sample_edges(sources: tuple[str, ...], n: int) -> list[dict]:
    """Random active edges owned by the given sources (live spot-check input)."""
    stmt = text(
        "SELECT src, dst, type, source FROM graphedge WHERE status = 'active' AND source IN :s ORDER BY RANDOM() LIMIT :n"
    ).bindparams(bindparam("s", expanding=True))
    with _read() as c:
        return [dict(r._mapping) for r in c.execute(stmt, {"s": list(sources), "n": n}).all()]
