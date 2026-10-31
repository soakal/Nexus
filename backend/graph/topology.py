"""Topology collector (plan-v3 §3.5/§4.2): Proxmox + Unraid + uptime targets + manual_edges.toml -> graph.

build() is pure; collect() does the I/O. Staleness counts successful source snapshots (graphjob.generation),
never elapsed time; an unavailable source is skipped, not staled.
"""
from __future__ import annotations

import asyncio
import logging
import time
import tomllib
from pathlib import Path

from backend.graph import INFRA_TYPES, now_iso, store, vault_index
from backend.graph.store import Edge, Node

logger = logging.getLogger(__name__)

MANUAL_PATH = Path(__file__).with_name("manual_edges.toml")
STALE_CYCLES = 2
_SOURCES = ("proxmox_api", "unraid_api", "uptime_targets")
_EDGE_TYPES = frozenset({"links_to", "tagged", "runs_on", "depends_on", "backs_up_to", "resolves_via", "documents", "mentions"})
_NODE_KEYS = {"id", "type", "name", "aliases", "attrs"}


def load_manual(path: Path = MANUAL_PATH) -> tuple[list[Node], list[Edge]]:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    extra = set(data) - {"node", "edge"}
    if extra:
        raise ValueError(f"manual_edges: unknown top-level table(s) {sorted(extra)}")
    nodes: list[Node] = []
    seen: set[str] = set()
    for n in data.get("node", []):
        nid, typ = n.get("id"), n.get("type")
        if not nid or not n.get("name") or set(n) - _NODE_KEYS:
            raise ValueError(f"manual_edges: bad node {n!r}")
        if typ not in INFRA_TYPES:
            raise ValueError(f"manual_edges: node {nid!r} has unknown type {typ!r}")
        if not nid.startswith(typ + ":"):
            raise ValueError(f"manual_edges: node id {nid!r} does not match type {typ!r}")
        if nid in seen:
            raise ValueError(f"manual_edges: duplicate node id {nid!r}")
        seen.add(nid)
        nodes.append(Node(nid, typ, n["name"], tuple(n.get("aliases", ())), n.get("attrs"), "manual"))
    if "host:pve" not in seen:
        raise ValueError("manual_edges: host:pve must be declared")
    edges: list[Edge] = []
    for e in data.get("edge", []):
        if not (e.get("src") and e.get("dst")) or e.get("type") not in _EDGE_TYPES or set(e) - {"src", "dst", "type"}:
            raise ValueError(f"manual_edges: bad edge {e!r}")
        edges.append(Edge(e["src"], e["dst"], e["type"], "manual"))
    return nodes, edges


def storage_for_volume(volume: str, storages: set[str]) -> str | None:
    if volume.startswith("/mnt/pve/"):
        sid = volume.split("/")[3] if volume.count("/") >= 3 else ""
        return sid if sid in storages else None
    if volume.startswith("/") or ":" not in volume:
        return None  # bind mount
    sid = volume.split(":", 1)[0]
    return sid if sid in storages else None


def _clean(d: dict) -> dict:
    return {k: v for k, v in d.items() if v is not None}


def build(prox, configs, storages, backup_jobs, containers, targets, manual) -> tuple[list[Node], list[Edge]]:
    man_nodes, man_edges = manual
    nodes: dict[str, Node] = {}
    edges: list[Edge] = []
    for n in man_nodes:
        nodes[n.id] = n
    host_ips = {n.attrs["ip"]: n.id for n in man_nodes if n.type == "host" and n.attrs and n.attrs.get("ip")}

    if prox is not None and configs is not None and storages is not None and backup_jobs is not None:
        pve = nodes["host:pve"]
        nodes["host:pve"] = Node(pve.id, pve.type, pve.name, pve.aliases, {**(pve.attrs or {}), "status": prox.node_status}, "manual")
        guest_ids: set[int] = set()
        sids = {s["storage"] for s in storages}
        for vm in prox.vms:
            vmid = int(vm["vmid"])
            guest_ids.add(vmid)
            cfg = configs.get(vmid, {})
            name = vm.get("name") or cfg.get("name") or str(vmid)
            kind = "CT" if vm.get("type") == "lxc" else "VM"
            gid = f"guest:{vmid}"
            attrs = _clean({
                "vmid": vmid, "type": vm.get("type"), "name": name, "status": vm.get("status"),
                **{k: cfg.get(k) for k in ("onboot", "cores", "memory", "tags", "hookscript", "volumes", "net")},
            })
            nodes[gid] = Node(gid, "guest", name, (name, f"{kind} {vmid}", str(vmid)), attrs, "proxmox_api")
            edges.append(Edge(gid, "host:pve", "runs_on", "proxmox_api"))
            for sid in sorted({storage_for_volume(v["volume"], sids) for v in cfg.get("volumes", [])} - {None}):
                edges.append(Edge(gid, f"storage:{sid}", "depends_on", "proxmox_api"))
        for s in storages:
            sid = f"storage:{s['storage']}"
            nodes[sid] = Node(sid, "storage", s["storage"], (), _clean({
                "plugintype": s.get("type"), "server": s.get("server"), "export": s.get("export"),
                "content": s.get("content"), "disabled": bool(s.get("disabled")),
            }), "proxmox_api")
            server = s.get("server")
            if server:
                host = host_ips.get(server)
                if host is None:
                    host = f"external:{server}"
                    nodes[host] = Node(host, "external", server, (), {"ip": server}, "proxmox_api")
                edges.append(Edge(sid, host, "depends_on", "proxmox_api"))
        for job in backup_jobs:
            if not job.get("storage"):
                logger.warning("backup job %s has no storage; no backs_up_to emitted", job.get("id"))
                continue
            for vmid in job.get("vmids", []):
                if int(vmid) in guest_ids:  # skip guests destroyed between the two GETs
                    edges.append(Edge(f"guest:{int(vmid)}", f"storage:{job['storage']}", "backs_up_to", "proxmox_api"))

    for c in containers or []:
        cid = f"container:{c['name']}"
        nodes[cid] = Node(cid, "container", c["name"], (), _clean({"state": c.get("state"), "status": c.get("status"), "id": c.get("id")}), "unraid_api")
        edges.append(Edge(cid, "host:unraid", "runs_on", "unraid_api"))

    svc_alias = {a.lower(): n.id for n in man_nodes if n.type == "service" for a in n.aliases}
    for name, url, expect in targets or []:
        eid = f"endpoint:{name}"
        nodes[eid] = Node(eid, "endpoint", name, (), {"url": url, "expect": expect}, "uptime_targets")
        if name.lower() in svc_alias:
            edges.append(Edge(eid, svc_alias[name.lower()], "resolves_via", "uptime_targets"))

    return list(nodes.values()), edges + man_edges


def _db_write(manual, nodes, edges, ok_sources, failed, all_ok, seen_at, t0) -> dict:
    pruned = 0
    with store.batch() as conn:
        for src in ok_sources:
            g = store.bump_generation(src, conn)
            store.upsert_nodes([n for n in nodes if n.source == src], seen_at, conn, gen=g)
            store.upsert_edges([e for e in edges if e.source == src], seen_at, conn, gen=g)
            store.mark_stale(src, g - STALE_CYCLES, conn)
        store.upsert_nodes([n for n in nodes if n.source == "manual"], seen_at, conn)
        store.upsert_edges([e for e in edges if e.source == "manual"], seen_at, conn)
        if all_ok:
            declared = {(e.src, e.dst, e.type) for e in manual[1]}
            pruned = store.delete_edges(source="manual", not_in=declared, conn=conn)
            ids = {n.id for n in manual[0]}
            pruned += store.delete_nodes([n["id"] for n in store.list_nodes(source="manual", conn=conn) if n["id"] not in ids], conn)
        else:
            logger.info("prune skipped: %s unavailable", ", ".join(failed))
        vault_index.reconcile_documents(seen_at, conn)
        for src in ok_sources:
            store.record_job(f"source:{src}", ok=True, at=seen_at, conn=conn)
        for src, err in failed.items():
            store.record_job(f"source:{src}", ok=False, error=err, at=seen_at, conn=conn)
        stats = {"nodes": len(nodes), "edges": len(edges), "pruned_manual": pruned, "seconds": round(time.monotonic() - t0, 3)}
        store.record_job("collect", ok=True, stats=stats, at=seen_at, conn=conn)
    return stats


async def collect() -> dict:
    from backend.agents import outcomes
    from backend.integrations import proxmox, unraid

    t0 = time.monotonic()
    manual = await asyncio.to_thread(load_manual)  # file I/O off the loop; raises -> whole run fails (the TOML is code)
    failed: dict[str, str] = {}
    prox = configs = storages = jobs = containers = targets = None
    try:
        prox = await proxmox.fetch()
        configs = await proxmox.fetch_guest_configs()
        storages = await proxmox.fetch_storages()
        jobs = await proxmox.fetch_backup_jobs()
    except Exception as e:
        prox = configs = storages = jobs = None
        failed["proxmox_api"] = str(e)
    try:
        containers = await unraid.fetch_containers()
    except Exception as e:
        failed["unraid_api"] = str(e)
    try:
        # lazy: importing backend.scheduler builds an unstarted scheduler
        from backend.scheduler import _parse_uptime_targets

        targets = _parse_uptime_targets()
    except Exception as e:
        failed["uptime_targets"] = str(e)
    for s, err in failed.items():
        logger.warning("graph collect: source %s skipped: %s", s, err)

    seen_at = now_iso()
    nodes, edges = build(prox, configs, storages, jobs, containers, targets, manual)
    ok_sources = [s for s in _SOURCES if s not in failed]
    try:
        stats = await asyncio.to_thread(_db_write, manual, nodes, edges, ok_sources, failed, not failed, seen_at, t0)
    except Exception as e:
        await asyncio.to_thread(store.record_job, "collect", ok=False, error=str(e), at=now_iso())
        raise

    stale = await asyncio.to_thread(store.stale_counts)
    # the batch is already committed: a flag hiccup must not turn a populated graph into "collect failed"
    try:
        if stale["edges"] > 0:
            await outcomes.record_flag_ex(
                "graph", "stale_edges",
                f"{stale['edges']} infra edges not re-observed for 2 collection cycles — run `python -m backend.graph doctor`",
                severity="low",
            )
        else:
            await outcomes.clear_flag("graph", "stale_edges")
    except Exception as e:
        logger.warning("graph collect: stale_edges flag update failed: %s", e)
    return {
        "sources": {s: ("ok" if s not in failed else f"skipped: {failed[s]}") for s in _SOURCES},
        "nodes": stats["nodes"], "edges": stats["edges"], "stale": stale,
        "pruned_manual": stats["pruned_manual"], "seconds": round(time.monotonic() - t0, 3),
    }
