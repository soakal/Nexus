"""Incident diagnostician — a fired homelab_watch alert spawns one read-only
investigation instead of leaving Brian to diagnose a bare symptom alone.

Reuses everything that already exists rather than inventing new machinery:
router.run_with_tools (the same read-only tool-use loop the orchestrator's
executor uses) over tools.py's existing read-only registry, delivered via
the same events.notify_phone + broker-gated fix button the original alert
already carries (vm:start:<id> / docker:restart:<name> — no new Telegram
callback namespace, no broker change).

Deliberately a one-shot router.run_with_tools call, NOT a durable Task:
a durable Task would occupy one of worker_pool's 2 workers, fire its own
task_completed/task_failed push (double-paging on top of the diagnosis
message), and durable resumability is anti-value here — if NEXUS restarts
mid-investigation, the original alert already paged; re-running a stale
investigation after the fact is noise, not a feature.

Only 4 of the 6 homelab_watch alert kinds trigger a diagnosis: vm/docker/
array/backup. Garage-open is a physical-world alert with nothing else to
investigate beyond what it already says; disk-temp's only useful correlate
(unraid.mover_running) is a documented dead-default field with nothing
real to read (see CLAUDE.md's contract-canary section).

State is process-local/in-memory, same accepted tradeoff as homelab_watch's
own _active_alerts/_paged_alerts — a restart losing the per-key cooldown
is cheap and acceptable.
"""

import asyncio
import html
import json
import logging
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

_DIAG_COOLDOWN_S = 1800  # ponytail: fixed 30min; make a Settings field if Brian wants tuning
_last_diag_at: dict[str, datetime] = {}
_diag_task: asyncio.Task | None = None

_SYSTEM = (
    "You are NEXUS's incident diagnostician. A homelab alert just fired. Use "
    "the read-only tools to gather evidence — check the affected system's "
    "current state, and look for correlated causes across other systems "
    "(Proxmox backups/updates, Unraid array and disks, UniFi WAN status, "
    "open outcome flags). Then reply with: (1) most likely cause, one "
    "sentence; (2) evidence, max 3 bullets; (3) recommended action, one "
    "line. Under 120 words total. If evidence is inconclusive, say so "
    "plainly — never invent a cause. When an EVIDENCE block is given, "
    "treat changes recorded long after the incident time (also via "
    "what_changed) as unlikely causes, and never present one as the cause "
    "without other evidence; changes it marks UNCERTAIN are possible but "
    "unconfirmed causes."
)


def reset() -> None:
    """Clear all diagnostician state. Test hook — call at the start of each test."""
    global _diag_task
    _last_diag_at.clear()
    _diag_task = None


def _fix_button(key: str) -> list | None:
    """The same broker-gated fix button the original alert already carries —
    no new callback namespace, no broker change."""
    if key.startswith("vm:"):
        vmid = key[len("vm:"):]
        return [{"text": "▶ Start", "callback_data": f"vm:start:{vmid}"}]
    if key.startswith("docker:"):
        name = key[len("docker:"):]
        from backend.integrations import unraid
        if unraid._SAFE_CONTAINER_ID.match(name) and len(f"docker:restart:{name}".encode()) <= 64:
            return [{"text": "↺ Restart", "callback_data": f"docker:restart:{name}"}]
        return None
    return None


def _user_prompt(key: str) -> str:
    if key.startswith("vm:"):
        return f"Alert: {key} — VM/LXC {key[len('vm:'):]} just stopped. Investigate and diagnose."
    if key.startswith("docker:"):
        return f"Alert: {key} — Docker container {key[len('docker:'):]} just stopped. Investigate and diagnose."
    if key == "unraid_array":
        return "Alert: unraid_array — Unraid array is unhealthy. Investigate and diagnose."
    if key == "vzdump_failed":
        return "Alert: vzdump_failed — the latest Proxmox vzdump backup failed. Investigate and diagnose."
    return f"Alert: {key}. Investigate and diagnose."


_EVIDENCE_ROWS = 20


def _incident_at(key: str, flag: dict | None, fired_at: datetime | None) -> tuple[str | None, str]:
    """(naive-UTC ISO or None, label). vzdump: the failing job's START, never when NEXUS noticed it --
    without a valid start time there is no reliable cutoff, so None (change evidence is then omitted).
    Other keys: the flag's created_at, else the watcher tick that fired the alert."""
    from backend.graph import to_utc_iso

    if key == "vzdump_failed":
        try:
            st = json.loads((flag or {}).get("detail") or "{}").get("starttime")
            return (to_utc_iso(st), "job start") if st else (None, "no job start time")
        except (ValueError, TypeError, AttributeError):
            return None, "no valid job start time"
    for raw in ((flag or {}).get("created_at"), fired_at):
        if raw:
            try:
                return to_utc_iso(raw), "alert raised"
            except (ValueError, TypeError):
                continue
    return None, "alert time unknown"


def _collect_minutes() -> int:
    try:
        from backend.graph import intervals

        return intervals()["collect"]  # same clamp as the scheduler's graph_collect job
    except (TypeError, ValueError):
        return 60


def _change_line(r: dict) -> str:
    return f"- {r['ts']}Z {r['node_id']} {r['field']}: {r['old']} -> {r['new']} [{r['source']}]"


async def _evidence(key: str, fired_at: datetime | None = None) -> str:
    """Deterministic graph facts for the prompt (G3a). "" when the graph is off or knows nothing. Never raises.

    graphchange.ts is when the COLLECTOR saw a change (every graph_collect_minutes), not when it happened,
    so rows recorded within two collect intervals after the incident are listed separately as uncertain."""
    try:
        from backend.agents import outcomes
        from backend.graph import correlate, enabled, store, to_utc_iso

        if not enabled():
            return ""
        node = correlate.node_for_flag("homelab_watch", key)
        if node is None:
            return ""
        flag = await asyncio.to_thread(outcomes._db_find_active_by_fingerprint, f"homelab_watch:{key}")
        at, label = _incident_at(key, flag, fired_at)
        ids = [node] + [i for i, _ in await asyncio.to_thread(store.ancestors, node, 3)]
        if key == "vzdump_failed":
            # shortcut: guests covered by ANY backup job, not just the failing task (fetch_backups does not return the
            # task's vmids); fine while there is one all-guests job, read the task log when jobs become selective
            backed = await asyncio.to_thread(store.edge_sources, "backs_up_to")
            ids += [i for i, _ in await asyncio.to_thread(store.descendants, node, 1) if i in backed]
        lines = ["EVIDENCE (deterministic, from the knowledge graph)"]
        if at is None:
            lines.append(f"Incident time unknown ({label}) \u2014 change history omitted, there is no reliable cutoff.")
        else:
            lag = _collect_minutes()
            window = 2 * lag  # tolerates one failed/missed collect; not a hard bound
            t = datetime.fromisoformat(at)
            since = to_utc_iso(t - timedelta(hours=24))
            until = to_utc_iso(t + timedelta(minutes=window))
            before = await asyncio.to_thread(lambda: store.changes_for(ids, since=since, before=at, limit=_EVIDENCE_ROWS))
            near = await asyncio.to_thread(lambda: store.changes_for(ids, since=at, before=until, limit=_EVIDENCE_ROWS))
            lines.append(
                f"Incident at {at}Z UTC ({label}). Tracked changes are RECORDED when the collector next runs "
                f"(about every {lag} min), not when they happened. Recorded in the 24 h before the incident:")
            lines += [_change_line(r) for r in before] or [
                "- none recorded (only tracked fields are recorded; this is not proof nothing changed)"]
            if len(before) >= _EVIDENCE_ROWS:
                lines.append(f"- (showing newest {_EVIDENCE_ROWS})")
            if near:
                lines.append(f"UNCERTAIN \u2014 recorded within {window} min AFTER the incident time; may have changed "
                             "just before it (collector lag) or after it:")
                lines += [_change_line(r) for r in near]
            lines.append(
                f"Changes recorded more than {window} min after the incident time are not listed: a change made before "
                "the incident is normally recorded within one collect, so they are unlikely causes unless the collector "
                "was failing (check graph health).")
        from backend.graph import freshness

        warn = await asyncio.to_thread(freshness.note)
        if warn:
            lines.insert(1, warn)
        lines.append(f"Graph scope: {', '.join(ids)}")
        by_node = correlate.flags_by_node(await outcomes.open_flags(limit=1000))
        own = (flag or {}).get("id")
        others = [f for i in ids for f in by_node.get(i, []) if f.get("id") != own]
        if others:
            lines.append("Other open flags in scope: " + "; ".join(f"#{f['id']} {f['source']}:{f['check']}" for f in others[:10]))
        if own:  # G3b: children grouped under this incident share its one diagnosis
            kids = [f for f in await outcomes.open_flags(limit=1000) if f.get("parent_flag_id") == own]
            if kids:
                lines.append("Grouped under this incident: " + ", ".join(f"#{f['id']} {f['source']}:{f['check']}" for f in kids[:20]))
        docs = await asyncio.to_thread(store.documents_for, ids)
        notes = sorted({n for v in docs.values() for n in v})
        if notes:
            lines.append("Runbook notes: " + ", ".join(notes[:10]))
        return "\n".join(lines)
    except Exception as e:
        logger.warning(f"incident_diag: evidence block failed for {key} (ignored): {e}")
        return ""


async def diagnose(key: str, fired_at: datetime | None = None) -> None:
    """Run one read-only investigation for `key` and page the diagnosis.
    Never raises — the caller (maybe_diagnose) fires this as a background
    task with no one to propagate to."""
    try:
        from backend.agents import router, tools
        from backend.config import get_settings
        from backend import events

        s = get_settings()
        evidence = await _evidence(key, fired_at)
        text = await router.run_with_tools(
            s.orchestrator_executor_model,
            1500,
            f"{evidence}\n\n{_user_prompt(key)}" if evidence else _user_prompt(key),
            system=_SYSTEM,
            tool_specs=tools.tool_specs(),
            dispatch=tools.dispatcher_map(),
            label="incident_diag",
            max_rounds=4,
        )
        await events.notify_phone(
            f"🔎 NEXUS diagnosis — {html.escape(key)}:\n{html.escape(text)[:1000]}",
            kind="incident_diagnosis",
            buttons=_fix_button(key),
        )
    except Exception as e:
        from backend.safety.governor import BudgetExceeded
        if isinstance(e, BudgetExceeded):
            logger.info(f"incident_diag: budget exceeded, skipping diagnosis for {key}")
        else:
            logger.warning(f"incident_diag: diagnosis failed for {key} (ignored): {e}")


def maybe_diagnose(result: dict) -> None:
    """Called from run_homelab_watch with its full result dict. Diagnoses
    only the FIRST qualifying fired key this tick -- a burst of alerts in
    one tick usually shares one root cause, and this keeps at most one
    investigation in flight at a time."""
    global _diag_task
    from backend.config import get_settings
    if not getattr(get_settings(), "incident_diag_enabled", True):
        return

    if _diag_task and not _diag_task.done():
        return

    candidates = (
        (result.get("vms") or [])
        + (result.get("docker") or [])
        + (result.get("array") or [])
        + (result.get("backups") or [])
    )
    if not candidates:
        return

    now = datetime.utcnow()
    key = None
    for k in candidates:
        last = _last_diag_at.get(k)
        if last and (now - last).total_seconds() < _DIAG_COOLDOWN_S:
            continue
        key = k
        break
    if key is None:
        return

    _last_diag_at[key] = now
    # asyncio.create_task alone holds only a weak ref (same reasoning as
    # telegram_poller._message_tasks / homelab_watch._event_propose_task) --
    # the module-level var is what keeps this alive, and also doubles as
    # the already-running guard above.
    _diag_task = asyncio.create_task(diagnose(key, now))  # now = the watcher tick that fired it (naive UTC)
