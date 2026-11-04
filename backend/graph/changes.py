"""Graph change recording (G3a, plan g3/plan-v2 §4.1): diff a source's snapshot against what the store holds.

Pure except record(). Source-scoped: a node/edge owned by another source can never look deleted.
Disappearance is NOT diffed here — it is recorded only when store.mark_stale fires (stale_rows()).
"""
from __future__ import annotations

import json

TRACKED = {
    "guest": ("status", "onboot", "cores", "memory", "tags", "hookscript", "volumes", "net.ip"),
    "storage": ("plugintype", "server", "disabled"),
    "container": ("state",),
    "service": ("sha",),
    "note": ("title", "status", "category"),
}
MAX_PER_RUN = 500


def _val(field: str, attrs: dict) -> str | None:
    if field == "volumes":
        v = attrs.get("volumes")
        return None if v is None else json.dumps(sorted(f"{x.get('key')}={x.get('volume')}" for x in v))
    if field == "net.ip":
        v = attrs.get("net")
        return None if v is None else json.dumps(sorted(str(x.get("ip")) for x in v if x.get("ip")))
    v = attrs.get(field)
    if v is None:
        return None
    if isinstance(v, list):
        return json.dumps(sorted(str(x) for x in v))
    return v if isinstance(v, str) else json.dumps(v, sort_keys=True)


def canon(node_type: str, attrs: dict | None) -> dict[str, str | None]:
    a = attrs or {}
    return {f: _val(f, a) for f in TRACKED.get(node_type, ())}


def diff_nodes(node_type: str, old: dict | None, new: dict | None) -> list[tuple[str, str | None, str | None]]:
    o, n = canon(node_type, old), canon(node_type, new)
    return [(f, o[f], n[f]) for f in o if o[f] != n[f]]


def _row(ts, node_id, field, old, new, source) -> dict:
    return {"ts": ts, "node_id": node_id, "field": field, "old": old, "new": new, "source": source}


def diff_source(*, source: str, ts: str, new_nodes, new_edges, stored_nodes: list[dict],
                stored_edges: dict[tuple[str, str, str], str], merge_attrs: bool = True) -> list[dict]:
    """new_nodes: store.Node of this source; stored_nodes: store.list_nodes(source=, status=None);
    stored_edges: store.edge_keys(source=, status=None). merge_attrs mirrors upsert_nodes (None deletes a key).
    Assumes node ids are unique across sources (upsert_nodes never rewrites `source`); a TOML node sharing a
    collector id would make that collector report it as 'appeared' every run."""
    stored = {n["id"]: n for n in stored_nodes}
    was_empty = not stored and not stored_edges  # first run / rebuild: record nothing
    rows: list[dict] = []
    for n in new_nodes:
        s = stored.get(n.id)
        if s is None:
            if not was_empty:
                rows.append(_row(ts, n.id, "node", None, n.type, source))
            continue
        if s.get("status") == "stale":
            rows.append(_row(ts, n.id, "status", "stale", "active", source))
        old = s.get("attrs") or {}
        new = dict(n.attrs or {})
        if merge_attrs:
            new = {k: v for k, v in {**old, **new}.items() if v is not None}
        rows += [_row(ts, n.id, f, o, w, source) for f, o, w in diff_nodes(n.type, old, new)]
    for k in dict.fromkeys((e.src, e.dst, e.type) for e in new_edges):  # two backup jobs can emit the same edge
        st = stored_edges.get(k)
        if st == "stale":
            rows.append(_row(ts, k[0], f"edge:{k[2]}:{k[1]}", "stale", "active", source))
        elif st is None and not was_empty:
            rows.append(_row(ts, k[0], f"edge:{k[2]}:{k[1]}", None, "active", source))
    return rows


def stale_rows(*, source: str, ts: str, node_ids: list[str], edges: list[tuple[str, str, str]]) -> list[dict]:
    """The two-cycle disappearance rows, from store.mark_stale's RETURNING."""
    return [_row(ts, i, "status", "active", "stale", source) for i in node_ids] + [
        _row(ts, s, f"edge:{t}:{d}", "active", "stale", source) for s, d, t in edges
    ]


def removed_rows(*, source: str, ts: str, nodes, edges) -> list[dict]:
    """Rows a prune deletes outright (manual source: no stale phase). nodes: (id, type) pairs. new=None = removed."""
    return [_row(ts, i, "node", t, None, source) for i, t in sorted(nodes)] + [
        _row(ts, s, f"edge:{t}:{d}", "active", None, source) for s, d, t in sorted(edges)
    ]


def record(conn, ts: str, rows: list[dict]) -> int:
    """Insert up to MAX_PER_RUN rows, plus one 'truncated' marker row when more were produced.
    Call once per collect/refresh run: the cap is per run, not per source."""
    from backend.graph import store

    if len(rows) > MAX_PER_RUN:
        dropped = len(rows) - MAX_PER_RUN
        rows = rows[:MAX_PER_RUN] + [_row(ts, "graph:changes", "truncated", None, str(dropped), "graph")]
    return store.insert_changes(rows, conn)
