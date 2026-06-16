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
