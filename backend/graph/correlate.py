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
