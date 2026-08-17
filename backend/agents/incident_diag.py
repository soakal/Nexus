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
import logging
from datetime import datetime

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
    "plainly — never invent a cause."
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


async def diagnose(key: str) -> None:
    """Run one read-only investigation for `key` and page the diagnosis.
    Never raises — the caller (maybe_diagnose) fires this as a background
    task with no one to propagate to."""
    try:
        from backend.agents import router, tools
        from backend.config import get_settings
        from backend import events

        s = get_settings()
        text = await router.run_with_tools(
            s.orchestrator_executor_model,
            1500,
            _user_prompt(key),
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
    _diag_task = asyncio.create_task(diagnose(key))
