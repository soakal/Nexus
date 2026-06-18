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

from backend.agents.tools import ReadTool, _truncate, tool_specs, dispatcher_map, planner_tool_block

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
            # hermes_action results carry "response"; ha results are simple dicts
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


async def _hermes_command(input: dict) -> str:  # noqa: A002
    try:
        verb = (input or {}).get("verb", "")
        args = (input or {}).get("args") or {}

        from backend.safety import hermes_actions

        if not hermes_actions.is_allowed(verb):
            allowed_names = ", ".join(
                v["verb"] for v in hermes_actions.allowed_verbs()
            )
            return _wtruncate(
                f"unknown verb. Allowed verbs: {allowed_names}"
            )

        err = hermes_actions.validate_args(verb, args)
        if err:
            return f"invalid args: {err}"

        key = _idem_key_for("hermes_command", {"verb": verb, "args": args})

        from backend.safety.broker import execute_action
        res = await execute_action(
            actor="agent",
            kind="hermes_action",
            target="hermes",
            payload={"verb": verb, "args": args},
            idempotency_key=key,
        )
        return _wtruncate(_decision_to_str(res))
    except Exception as e:
        return f"hermes_command error: {e}"


async def _channels_record(input: dict) -> str:  # noqa: A002
    """Trigger a Channels DVR recording by program_id.

    Goes through the safety broker (Risk.LOW → agent ALLOWED automatically).
    Dispatches DIRECT from this PC — not via Hermes.
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
    Dispatches DIRECT from this PC — not via Hermes.
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

        from backend.safety.broker import execute_action
        res = await execute_action(
            actor="agent",
            kind="unraid_docker",
            target=container_id,
            payload={"container_id": container_id},
            idempotency_key=key,
        )
        return _wtruncate(_decision_to_str(res))
    except Exception as e:
        return f"unraid_docker_restart error: {e}"


async def _obsidian_complete_task(input: dict) -> str:  # noqa: A002
    """Check off a task in an Obsidian vault note.

    Goes through the safety broker (Risk.LOW → agent ALLOWED automatically).
    Dispatches DIRECT from this PC — not via Hermes.
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
    """Send a phone (Telegram) notification to the owner via Hermes.

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
        name="hermes_command",
        description=(
            "Command the Hermes homelab bot via a structured allowlisted verb "
            "(e.g. restart a service). Goes through the safety broker; risky verbs "
            "are refused without confirmation. Call with the verb and its required args."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "verb": {
                    "type": "string",
                    "description": "Allowlisted Hermes verb, e.g. 'proxmox_status', 'adguard_control'",
                },
                "args": {
                    "type": "object",
                    "description": "Verb-specific arguments (see allowed_verbs for required/enum args)",
                },
            },
            "required": ["verb"],
        },
        dispatch=_hermes_command,
    ),
    ReadTool(
        name="channels_record",
        description=(
            "Trigger a Channels DVR recording for a program by program_id. "
            "Goes through the safety broker (LOW risk — auto-allowed for agents). "
            "Dispatches direct from this PC, not via Hermes."
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
            "Dispatches direct from this PC, not via Hermes."
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
        name="obsidian_complete_task",
        description=(
            "Check off an open task (- [ ] ...) in an Obsidian vault note. "
            "Goes through the safety broker (LOW risk — auto-allowed for agents). "
            "Dispatches direct from this PC, not via Hermes."
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
