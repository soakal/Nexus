"""Map open flags to graph nodes (plan-v3 §3.7). Pure; Phase 3 grows it."""
from __future__ import annotations

_NEVER_GROUP_SOURCES = frozenset({"watchdog", "contracts", "cron_heartbeat", "vault_signals", "obligation", "manual", "nightshift"})


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
