"""Tier 2.4 — write tools for the task executor, every one gated by the broker.

Unlike backend/agents/tools.py (read-only, must never import the broker), this
module dispatches side effects EXCLUSIVELY through backend.safety.broker.execute_action
with actor="agent", so the policy gate + kill switch + immutable ActionLog apply.
A HIGH/irreversible action comes back needs_confirm/forbidden and is NOT performed.
Every dispatch returns a compact string for the LLM and NEVER raises.
"""

import contextvars
import hashlib
import json

from backend.agents.tools import ReadTool, tool_specs, dispatcher_map, planner_tool_block

MAX_WRITE_RESULT_CHARS = 600

# ---------------------------------------------------------------------------
# Durable write-context — threaded idempotency key for broker replays
# ---------------------------------------------------------------------------

_write_ctx: contextvars.ContextVar = contextvars.ContextVar("nexus_write_ctx", default=None)


def set_write_context(task_id, step_index):
    """Bind the durable step identity so write dispatchers can compute a stable
    idempotency_key. Returns a token; pass it to reset_write_context in a finally."""
    return _write_ctx.set((task_id, step_index))


def reset_write_context(token):
    try:
        _write_ctx.reset(token)
    except Exception:
        pass


def _idem_key_for(tool_name: str, args: dict):
    """Stable key per (task, step, tool, args) so a durable resume that re-calls
    the SAME tool with the SAME args replays via the broker instead of re-firing.
    Returns None when there is no durable context (chat/legacy single-shot — no
    resume risk), so the broker dispatches normally."""
    ctx = _write_ctx.get()
    if not ctx or ctx[0] is None:
        return None
    task_id, step_index = ctx
    raw = f"{task_id}:{step_index}:{tool_name}:{json.dumps(args, sort_keys=True, default=str)}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def _wtruncate(s: str) -> str:
    """Truncate to MAX_WRITE_RESULT_CHARS using the same logic as tools._truncate."""
    _SUFFIX = "\n…[truncated]"
    if s is None:
        return ""
    if len(s) <= MAX_WRITE_RESULT_CHARS:
        return s
    head_len = MAX_WRITE_RESULT_CHARS - len(_SUFFIX)
    if head_len < 0:
        head_len = 0
    return s[:head_len] + _SUFFIX


def _decision_to_str(res) -> str:
    """Map an ActionResult to a compact LLM-readable string.

    Covers every Decision value: EXECUTED, FAILED, NEEDS_CONFIRM, FORBIDDEN,
    and an unknown fallback. Never raises.
    """
    from backend.safety.broker import Decision

    d = res.decision
    if d == Decision.EXECUTED:
        summary = ""
        if res.result:
            # some dispatch results carry "response"; ha results are simple dicts
            resp_text = res.result.get("response") or str(res.result)
            # Truncate the inner detail before embedding it
            summary = _wtruncate(str(resp_text))
        return f"OK — performed. {summary}".strip()
    if d == Decision.FAILED:
        return f"FAILED: {res.error or 'dispatch error'}"
    if d == Decision.NEEDS_CONFIRM:
        return "BLOCKED: this action needs human confirmation and was NOT performed."
    if d == Decision.FORBIDDEN:
        return f"FORBIDDEN: blocked by policy or kill-switch, NOT performed ({res.error or 'policy'})."
    return f"UNKNOWN decision: {res.decision}"


# ---------------------------------------------------------------------------
# Write dispatchers — async (input: dict) -> str, NEVER raise.
# Every one wraps its body in try/except.
# ---------------------------------------------------------------------------

async def _home_control(input: dict) -> str:  # noqa: A002
    try:
        entity_id = (input or {}).get("entity_id", "")
        service = (input or {}).get("service", "")

        # Input validation — return helpful errors WITHOUT calling the broker.
        if not service or service not in {"turn_on", "turn_off", "toggle"}:
            return (
                f"home_control error: 'service' must be one of "
                f"['turn_on', 'turn_off', 'toggle']; got {service!r}"
            )
        if not entity_id or "." not in str(entity_id):
            return (
                "home_control error: 'entity_id' is required and must contain a "
                f"domain prefix (e.g. 'light.office'); got {entity_id!r}"
            )

        domain = str(entity_id).split(".")[0]
        key = _idem_key_for("home_control", {"entity_id": entity_id, "service": service})

        from backend.safety.broker import execute_action
        res = await execute_action(
            actor="agent",
            kind="ha_service",
            target=entity_id,
            payload={"domain": domain, "service": service},
            idempotency_key=key,
        )
        return _wtruncate(_decision_to_str(res))
    except Exception as e:
        return f"home_control error: {e}"


async def _channels_record(input: dict) -> str:  # noqa: A002
    """Trigger a Channels DVR recording by program_id.

    Goes through the safety broker (Risk.LOW → agent ALLOWED automatically).
    """
    try:
        program_id = (input or {}).get("program_id", "")
        if not program_id or not str(program_id).strip():
            return (
                "channels_record error: 'program_id' is required and must be a non-empty string; "
                f"got {program_id!r}"
            )

        program_id = str(program_id).strip()
        key = _idem_key_for("channels_record", {"program_id": program_id})

        from backend.safety.broker import execute_action
        res = await execute_action(
            actor="agent",
            kind="channels_record",
            target=program_id,
            payload={"program_id": program_id},
            idempotency_key=key,
        )
        return _wtruncate(_decision_to_str(res))
    except Exception as e:
        return f"channels_record error: {e}"


async def _unraid_docker_restart(input: dict) -> str:  # noqa: A002
    """Restart a Docker container on Unraid by container_id.

    Goes through the safety broker (Risk.HIGH → agent gets NEEDS_CONFIRM;
    a human must confirm before the restart executes).
    Dispatches direct from this PC.
    """
    try:
        container_id = (input or {}).get("container_id", "")
        if not container_id or not str(container_id).strip():
            return (
                "unraid_docker_restart error: 'container_id' is required and must be a non-empty string; "
                f"got {container_id!r}"
            )

        container_id = str(container_id).strip()
        key = _idem_key_for("unraid_docker_restart", {"container_id": container_id})

        from backend.safety.broker import Decision, execute_action
        res = await execute_action(
            actor="agent",
            kind="unraid_docker",
            target=container_id,
            payload={"container_id": container_id},
            idempotency_key=key,
        )
        # _decision_to_str's generic EXECUTED branch would render a failed or
        # half-restart as "OK — performed. {'success': False, ...}" -- it only
        # looks at the Decision enum, not this dispatcher's result shape.
        # Success must be read off the result, and a stop-succeeded/
        # start-failed half-restart (the container is now DOWN) must never
        # be readable as "OK".
        if res.decision == Decision.EXECUTED:
            result = res.result or {}
            if result.get("success"):
                return "OK — container restarted."
            if result.get("stopped"):
                return _wtruncate(f"STOPPED but failed to restart: {result.get('error', '')}")
            return _wtruncate(f"FAILED: {result.get('error', 'restart failed')}")
        return _wtruncate(_decision_to_str(res))
    except Exception as e:
        return f"unraid_docker_restart error: {e}"


async def _unraid_docker_prune(input: dict) -> str:  # noqa: A002
    """Prune dangling Docker images on Unraid (no args).

    Goes through the safety broker (Risk.HIGH — agent gets NEEDS_CONFIRM;
    a human must confirm before the prune executes). Dispatches direct from
    this PC over native SSH.
    """
    try:
        key = _idem_key_for("unraid_docker_prune", {})

        from backend.safety.broker import execute_action
        res = await execute_action(
            actor="agent",
            kind="unraid_docker_prune",
            target="unraid",
            payload={},
            idempotency_key=key,
        )
        return _wtruncate(_decision_to_str(res))
    except Exception as e:
        return f"unraid_docker_prune error: {e}"


async def _vm_power(input: dict) -> str:  # noqa: A002
    """Start/stop/reboot a Proxmox VM or LXC by vmid.

    Goes through the safety broker (Risk.HIGH → agent gets NEEDS_CONFIRM;
    a human must confirm before the power action executes).
    Dispatches direct from this PC.
    """
    try:
        vmid_raw = (input or {}).get("vmid")
        action = (input or {}).get("action", "")
        try:
            vmid = int(vmid_raw)
        except (TypeError, ValueError):
            return f"vm_power error: 'vmid' must be an integer; got {vmid_raw!r}"
        if action not in ("start", "stop", "reboot"):
            return f"vm_power error: 'action' must be one of start/stop/reboot; got {action!r}"

        key = _idem_key_for("vm_power", {"vmid": vmid, "action": action})

        from backend.safety.broker import execute_action
        res = await execute_action(
            actor="agent",
            kind="vm_power",
            target=str(vmid),
            payload={"vmid": vmid, "action": action},
            idempotency_key=key,
        )
        return _wtruncate(_decision_to_str(res))
    except Exception as e:
        return f"vm_power error: {e}"


async def _unifi_block(input: dict) -> str:  # noqa: A002
    """Block a client from the UniFi network by MAC address.

    Goes through the safety broker (Risk.HIGH → agent gets NEEDS_CONFIRM;
    a human must confirm before the block executes — a wrong MAC is a
    self-lockout risk).
    Dispatches direct from this PC.
    """
    try:
        mac = (input or {}).get("mac", "")
        if not mac or not str(mac).strip():
            return f"unifi_block error: 'mac' is required and must be a non-empty string; got {mac!r}"

        mac = str(mac).strip()
        key = _idem_key_for("unifi_block", {"mac": mac})

        from backend.safety.broker import execute_action
        res = await execute_action(
            actor="agent",
            kind="unifi_block",
            target=mac,
            payload={"mac": mac},
            idempotency_key=key,
        )
        return _wtruncate(_decision_to_str(res))
    except Exception as e:
        return f"unifi_block error: {e}"


async def _unifi_unblock(input: dict) -> str:  # noqa: A002
    """Unblock a client on the UniFi network by MAC address.

    Goes through the safety broker (Risk.HIGH → agent gets NEEDS_CONFIRM).
    Dispatches direct from this PC.
    """
    try:
        mac = (input or {}).get("mac", "")
        if not mac or not str(mac).strip():
            return f"unifi_unblock error: 'mac' is required and must be a non-empty string; got {mac!r}"

        mac = str(mac).strip()
        key = _idem_key_for("unifi_unblock", {"mac": mac})

        from backend.safety.broker import execute_action
        res = await execute_action(
            actor="agent",
            kind="unifi_unblock",
            target=mac,
            payload={"mac": mac},
            idempotency_key=key,
        )
        return _wtruncate(_decision_to_str(res))
    except Exception as e:
        return f"unifi_unblock error: {e}"


async def _obsidian_complete_task(input: dict) -> str:  # noqa: A002
    """Check off a task in an Obsidian vault note.

    Goes through the safety broker (Risk.LOW → agent ALLOWED automatically).
    Dispatches direct from this PC.
    """
    try:
        note_path = (input or {}).get("note_path", "")
        task_text = (input or {}).get("task_text", "")
        if not note_path or not str(note_path).strip():
            return (
                "obsidian_complete_task error: 'note_path' is required and must be a non-empty string; "
                f"got {note_path!r}"
            )
        if not task_text or not str(task_text).strip():
            return (
                "obsidian_complete_task error: 'task_text' is required and must be a non-empty string; "
                f"got {task_text!r}"
            )

        note_path = str(note_path).strip()
        task_text = str(task_text).strip()
        key = _idem_key_for("obsidian_complete_task", {"note_path": note_path, "task_text": task_text})

        from backend.safety.broker import execute_action
        res = await execute_action(
            actor="agent",
            kind="obsidian_task",
            target=note_path,
            payload={"note_path": note_path, "task_text": task_text},
            idempotency_key=key,
        )
        return _wtruncate(_decision_to_str(res))
    except Exception as e:
        return f"obsidian_complete_task error: {e}"


async def _send_notification(input: dict) -> str:  # noqa: A002
    """Send a phone (Telegram) notification to the owner.

    Goes through the safety broker (Risk.LOW reversible → agent ALLOWED, but
    per-verb throttled and kill-switch-gated). This is the tool that makes a
    "send a test notification" goal genuinely succeed.
    """
    try:
        content = (input or {}).get("content", "")
        if not content or not str(content).strip():
            return (
                "send_notification error: 'content' is required and must be a non-empty string; "
                f"got {content!r}"
            )

        content = str(content).strip()
        key = _idem_key_for("send_notification", {"content": content})

        from backend.safety.broker import execute_action
        res = await execute_action(
            actor="agent",
            kind="send_notification",
            target="owner",
            payload={"content": content},
            idempotency_key=key,
        )
        return _wtruncate(_decision_to_str(res))
    except Exception as e:
        return f"send_notification error: {e}"


# ---------------------------------------------------------------------------
# Write tool registry
# ---------------------------------------------------------------------------

WRITE_TOOLS: list[ReadTool] = [
    ReadTool(
        name="home_control",
        description=(
            "Control a Home Assistant device (turn_on/turn_off/toggle a light, switch, or fan). "
            "Goes through the safety broker; high-risk devices (locks, garage, climate, alarm) "
            "will be refused without human confirmation."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "entity_id": {
                    "type": "string",
                    "description": "HA entity id, e.g. 'light.office' or 'switch.fan'",
                },
                "service": {
                    "type": "string",
                    "enum": ["turn_on", "turn_off", "toggle"],
                    "description": "HA service to call",
                },
            },
            "required": ["entity_id", "service"],
        },
        dispatch=_home_control,
    ),
    ReadTool(
        name="channels_record",
        description=(
            "Trigger a Channels DVR recording for a program by program_id. "
            "Goes through the safety broker (LOW risk — auto-allowed for agents). "
            "Dispatches direct from this PC."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "program_id": {
                    "type": "string",
                    "description": "Channels DVR program id to record, e.g. '12345'",
                },
            },
            "required": ["program_id"],
        },
        dispatch=_channels_record,
    ),
    ReadTool(
        name="unraid_docker_restart",
        description=(
            "Restart a Docker container on Unraid by container_id. "
            "Goes through the safety broker (HIGH risk — needs human confirmation "
            "before the restart executes for an agent). "
            "Dispatches direct from this PC."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "container_id": {
                    "type": "string",
                    "description": "Docker container id or name on Unraid, e.g. 'plex' or 'abc123def456'",
                },
            },
            "required": ["container_id"],
        },
        dispatch=_unraid_docker_restart,
    ),
    ReadTool(
        name="unraid_docker_prune",
        description=(
            "Prune dangling Docker images on Unraid (no args) — dangling images only, "
            "never containers/volumes/networks. "
            "Goes through the safety broker (HIGH risk — needs human confirmation "
            "before the prune executes for an agent). "
            "Dispatches direct from this PC over native SSH."
        ),
        input_schema={"type": "object", "properties": {}, "required": []},
        dispatch=_unraid_docker_prune,
    ),
    ReadTool(
        name="vm_power",
        description=(
            "Start, stop, or reboot a Proxmox VM or LXC by vmid. "
            "Goes through the safety broker (HIGH risk — needs human confirmation "
            "before the action executes for an agent). "
            "Dispatches direct from this PC."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "vmid": {
                    "type": "integer",
                    "description": "Proxmox VM or LXC id, e.g. 101",
                },
                "action": {
                    "type": "string",
                    "enum": ["start", "stop", "reboot"],
                    "description": "'stop' performs a graceful shutdown, not a hard power-pull",
                },
            },
            "required": ["vmid", "action"],
        },
        dispatch=_vm_power,
    ),
    ReadTool(
        name="unifi_block",
        description=(
            "Block a client from the UniFi network by MAC address. "
            "Goes through the safety broker (HIGH risk — needs human confirmation "
            "before the block executes for an agent; a wrong MAC risks locking out "
            "a real device). "
            "Dispatches direct from this PC."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "mac": {
                    "type": "string",
                    "description": "MAC address to block, any common format (aa:bb:cc:dd:ee:ff, aa-bb-cc-dd-ee-ff, aabbccddeeff)",
                },
            },
            "required": ["mac"],
        },
        dispatch=_unifi_block,
    ),
    ReadTool(
        name="unifi_unblock",
        description=(
            "Unblock a client on the UniFi network by MAC address. "
            "Goes through the safety broker (HIGH risk — needs human confirmation "
            "before the unblock executes for an agent). "
            "Dispatches direct from this PC."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "mac": {
                    "type": "string",
                    "description": "MAC address to unblock, any common format (aa:bb:cc:dd:ee:ff, aa-bb-cc-dd-ee-ff, aabbccddeeff)",
                },
            },
            "required": ["mac"],
        },
        dispatch=_unifi_unblock,
    ),
    ReadTool(
        name="obsidian_complete_task",
        description=(
            "Check off an open task (- [ ] ...) in an Obsidian vault note. "
            "Goes through the safety broker (LOW risk — auto-allowed for agents). "
            "Dispatches direct from this PC."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "note_path": {
                    "type": "string",
                    "description": "Vault-relative path to the note, e.g. '2026-06-17.md' or 'Projects/Todo.md'",
                },
                "task_text": {
                    "type": "string",
                    "description": "Exact text of the task (without the '- [ ] ' prefix), e.g. 'Call dentist'",
                },
            },
            "required": ["note_path", "task_text"],
        },
        dispatch=_obsidian_complete_task,
    ),
    ReadTool(
        name="send_notification",
        description=(
            "Send a phone (Telegram) notification to the owner. "
            "Use this to confirm something, surface a finding, or send a requested message. "
            "Goes through the safety broker (LOW risk — auto-allowed for agents, but rate-limited)."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "The message text to send to the owner's phone.",
                },
            },
            "required": ["content"],
        },
        dispatch=_send_notification,
    ),
]


# ---------------------------------------------------------------------------
# Combined providers — used by the executor and planner when write tools are on
# ---------------------------------------------------------------------------

def all_tool_specs() -> list[dict]:
    """Read specs + write specs — full tool list for the executor."""
    return tool_specs() + [t.anthropic_spec() for t in WRITE_TOOLS]


def all_dispatchers() -> dict:
    """Read dispatchers + write dispatchers — full dispatch map for the executor."""
    return {**dispatcher_map(), **{t.name: t.dispatch for t in WRITE_TOOLS}}


def all_planner_block() -> str:
    """Read tool block + write tool lines — full tool advertisement for the planner."""
    write_lines = "\n".join(f"- {t.name}: {t.description}" for t in WRITE_TOOLS)
    return planner_tool_block() + "\n" + write_lines


def write_tool_names() -> list[str]:
    """Names of the write tools only (for tests/introspection)."""
    return [t.name for t in WRITE_TOOLS]
