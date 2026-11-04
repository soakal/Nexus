"""Map open flags to graph nodes (plan-v3 §3.7). Pure; Phase 3 grows it."""
from __future__ import annotations

_NEVER_GROUP_SOURCES = frozenset({"watchdog", "contracts", "cron_heartbeat", "vault_signals", "obligation", "manual", "nightshift",
                                  "graph"})  # graph:* flags describe the graph itself; never a parent or a child

# G3b (plan g3/plan-v12-g3b.md §5): the only flags that mean "this node is down for everything on it".
PARENT_CHECKS = frozenset({("homelab_watch", "unraid_array")})
GROUP_WINDOW_MIN = 30
GROUP_DEPTH = 3


def enabled() -> bool:
    """G3b grouping on? Needs the graph too. In-memory only (settings), safe in any send path."""
    from backend.config import get_settings
    from backend.graph import enabled as graph_enabled

    return graph_enabled() and getattr(get_settings(), "graph_correlation_enabled", False) is True


def node_for_flag(source: str, check: str) -> str | None:
    if source in _NEVER_GROUP_SOURCES:
        return None
    if source == "homelab_watch":
        if check.startswith("expected:"):
            check = check[len("expected:"):]
        kind, _, name = check.partition(":")
        if kind in ("vm", "lxc") and name:
            return f"guest:{name}"
        if kind == "docker" and name:
            return f"container:{name}"
        if check == "unraid_array":
            return "host:unraid"
        if check == "vzdump_failed":
            return "host:pve"  # check_backups reads /nodes/pve/tasks; the target storage is live data
    elif source == "briefing":
        if check in ("unraid_array", "unraid_parity"):
            return "host:unraid"
        if check == "adguard_filtering_off":
            return "service:adguard"
    return None


def flags_by_node(flags: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for f in flags:
        n = node_for_flag(f.get("source", ""), f.get("check", ""))
        if n:
            out.setdefault(n, []).append(f)
    return out


# ------------------------------------------------------------------ G3b parent lookup (runs in a g3b worker thread)

_TRAVERSAL = ("runs_on", "depends_on")


def ancestors_on(conn, node_id: str, max_depth: int = GROUP_DEPTH) -> set[str]:
    """store._walk's ancestor CTE on a caller-supplied sqlite3 connection (short busy timeout)."""
    rows = conn.execute(
        "WITH RECURSIVE walk(id, depth) AS (SELECT ?, 0 UNION "
        "SELECT e.dst, w.depth + 1 FROM walk w JOIN graphedge e ON e.src = w.id "
        "WHERE e.type IN (?, ?) AND e.status = 'active' AND w.depth < ?) "
        "SELECT DISTINCT id FROM walk WHERE id != ?",
        (node_id, *_TRAVERSAL, max_depth, node_id),
    ).fetchall()
    return {r[0] for r in rows}


def collect_stale_on(conn, now_utc_naive) -> bool:
    """True when the topology collect has not succeeded for 3 intervals: grouping pauses (plan §5.9)."""
    from datetime import datetime, timedelta

    from backend.graph import freshness, intervals

    r = conn.execute("SELECT last_success FROM graphjob WHERE name = 'collect'").fetchone()
    if r is None or r[0] is None:
        return True
    return now_utc_naive - datetime.fromisoformat(r[0]) >= timedelta(minutes=freshness.STALE_RUNS * intervals()["collect"])


def find_parent_on(conn, child_node: str, child_id: int, now_utc_naive) -> int | None:
    """Newest eligible parent flag whose node is an ancestor of child_node (plan §5.2 eligibility)."""
    from datetime import timedelta

    from backend.agents.g3b import db_ts

    if collect_stale_on(conn, now_utc_naive):
        return None
    anc = ancestors_on(conn, child_node)
    if not anc:
        return None
    window_start = db_ts(now_utc_naive - timedelta(minutes=GROUP_WINDOW_MIN))
    for (src, chk) in PARENT_CHECKS:
        for r in conn.execute(
            "SELECT id, source, \"check\" FROM outcomeflag WHERE source = ? AND \"check\" = ? AND id != ? "
            "AND status IN ('open','needs_follow_up') AND suppressed = 0 AND paged_at IS NOT NULL AND created_at >= ? "
            "ORDER BY id DESC",
            (src, chk, child_id, window_start),
        ).fetchall():
            if node_for_flag(r[1], r[2]) in anc:
                return r[0]
    return None
