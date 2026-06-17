"""Policy-gated action broker — the single chokepoint for side-effecting writes.

Every action that changes the state of an external system (turning a Home
Assistant device on/off, relaying a command to the Hermes production bot, ...)
MUST go through `execute_action`. The broker:

  1. classifies the action's RISK and REVERSIBILITY (`classify`),
  2. decides, based on the ACTOR, whether the action is allowed, needs
     confirmation, or is forbidden (`decide`),
  3. writes an immutable ActionLog row BEFORE the attempt (the intent/gate
     record — it exists with the gate decision even if the process dies), and
  4. dispatches the action only when allowed, then UPDATEs the same row with the
     dispatch outcome.

Two distinct axes are recorded, and they must not be conflated:

  * the GATE outcome — one of {allowed, needs_confirm, forbidden}. This is the
    policy decision about whether the action may run at all.
  * the DISPATCH outcome — one of {executed, failed}. This only applies when the
    gate said `allowed` and we actually attempted the dispatch.

`ActionLog.decision` always holds the FINAL state of the action: a forbidden /
needs_confirm action keeps that decision (no dispatch happened); an allowed
action is overwritten with `executed` or `failed` once dispatch completes.

The broker is idempotent by `idempotency_key`: a re-run whose key already has a
terminal row (executed/failed/forbidden) returns the recorded result with
`replayed=True` and does NOT dispatch again.
"""

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class Actor(str, Enum):
    USER = "user"
    AGENT = "agent"
    AUTONOMOUS = "autonomous"


class Risk(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    UNCLASSIFIABLE = "unclassifiable"


class Reversibility(str, Enum):
    REVERSIBLE = "reversible"
    REVERSIBLE_BY_INVERSE = "reversible_by_inverse"
    IRREVERSIBLE = "irreversible"
    UNKNOWN = "unknown"


class Decision(str, Enum):
    ALLOWED = "allowed"
    NEEDS_CONFIRM = "needs_confirm"
    FORBIDDEN = "forbidden"
    EXECUTED = "executed"
    FAILED = "failed"


# Terminal decisions for idempotency: an existing row with one of these means the
# action already ran to completion (or was permanently refused) and must not be
# re-dispatched. `needs_confirm` is explicitly NOT terminal — it is awaiting a
# confirm that has not yet happened.
_TERMINAL_DECISIONS = {Decision.EXECUTED.value, Decision.FAILED.value, Decision.FORBIDDEN.value}


@dataclass
class ActionResult:
    decision: Decision
    risk: Risk
    reversibility: Reversibility
    log_id: int | None
    result: dict | None = None
    error: str | None = None
    replayed: bool = False


# ---------------------------------------------------------------------------
# Policy — classification + decision
# ---------------------------------------------------------------------------

# HA domains whose state is trivially reversed by applying the inverse service
# (turn_on <-> turn_off). Low blast radius.
_HA_LOW_DOMAINS = {"light", "switch", "fan", "input_boolean"}
# HA domains that can affect physical security/safety; reversibility unknowable
# from the service call alone (a lock, a garage cover, a thermostat, an alarm).
_HA_HIGH_DOMAINS = {"lock", "cover", "climate", "alarm_control_panel"}


def classify(kind: str, payload: dict) -> tuple[Risk, Reversibility]:
    """Map an action (kind + payload) to (Risk, Reversibility).

    Defensive: missing/odd payload keys never raise — an unrecognised shape
    degrades to the most cautious classification rather than crashing the gate.
    """
    payload = payload or {}

    if kind == "ha_service":
        domain = payload.get("domain")
        if domain in _HA_LOW_DOMAINS:
            return Risk.LOW, Reversibility.REVERSIBLE_BY_INVERSE
        if domain in _HA_HIGH_DOMAINS:
            return Risk.HIGH, Reversibility.UNKNOWN
        # Any other/missing HA domain — we can't reason about its blast radius.
        return Risk.MEDIUM, Reversibility.UNKNOWN

    if kind == "hermes_relay":
        # A relay posts raw natural language straight to a live PRODUCTION bot
        # that can restart LXCs, open the garage, send Telegram messages, etc.
        # The effect (and thus its reversibility) is unknowable from here, so it
        # is HIGH by construction. Tier 1.4 quarantines this for non-user actors
        # in `decide` (FORBIDDEN); the classification itself is unchanged.
        return Risk.HIGH, Reversibility.UNKNOWN

    if kind == "hermes_action":
        # A structured allowlist verb — classification comes from the verb spec.
        # Lazy import to avoid a load cycle (hermes_actions imports Risk/Reversibility
        # from this module at its top).
        from backend.safety import hermes_actions
        return hermes_actions.classify_verb((payload or {}).get("verb", ""))

    if kind == "channels_record":
        # Trigger a DVR recording — low blast radius, deletable via the inverse.
        return Risk.LOW, Reversibility.REVERSIBLE_BY_INVERSE

    if kind == "unraid_docker":
        # Restart a Docker container — a service restart is HIGH; always needs
        # a human tap for an agent/autonomous actor.
        return Risk.HIGH, Reversibility.REVERSIBLE_BY_INVERSE

    if kind == "obsidian_task":
        # Check off a vault task — low blast radius, reversible by unchecking.
        return Risk.LOW, Reversibility.REVERSIBLE

    # Unknown kind — we have no policy for it.
    return Risk.UNCLASSIFIABLE, Reversibility.UNKNOWN


def decide(
    actor: Actor,
    risk: Risk,
    reversibility: Reversibility,
    confirmed: bool,
    kind: str | None = None,
) -> Decision:
    """Gate decision: may this actor run this action now?

    Returns one of {allowed, needs_confirm, forbidden} — the GATE outcome, never
    a dispatch outcome.

    `kind` is optional (default None keeps the positional decide() callers/tests
    working). When `kind == "hermes_relay"` a NON-user actor is FORBIDDEN outright:
    free-text relay to the live Hermes bot is quarantined to humans only (Tier
    1.4). `confirmed` is NOT an escape hatch for it — an agent must use the
    structured `hermes_action` allowlist instead.
    """
    # A direct human action is always allowed (preserves the chat UX); it is still
    # classified and logged so the audit trail is complete.
    if actor == Actor.USER:
        return Decision.ALLOWED

    # Free-text Hermes relay is forbidden for agent/autonomous, confirmed or not.
    if kind == "hermes_relay":
        return Decision.FORBIDDEN

    # agent / autonomous — evaluate irreversibility FIRST: an irreversible action
    # is the highest-stakes case and must be confirmed regardless of risk band.
    if reversibility == Reversibility.IRREVERSIBLE:
        return Decision.ALLOWED if confirmed else Decision.FORBIDDEN

    if risk in (Risk.HIGH, Risk.UNCLASSIFIABLE):
        return Decision.ALLOWED if confirmed else Decision.NEEDS_CONFIRM

    # LOW / MEDIUM, reversible enough — allowed (agent MEDIUM is permitted).
    return Decision.ALLOWED


# ---------------------------------------------------------------------------
# Dispatchers — the ONLY place an action actually fires
# ---------------------------------------------------------------------------

async def _dispatch_ha_service(target: str, payload: dict) -> dict:
    from backend.integrations import homeassistant

    if "service_data" in payload and payload["service_data"] is not None:
        service_data = payload["service_data"]
    else:
        service_data = {"entity_id": target}
    result = await homeassistant.call_service(payload["domain"], payload["service"], service_data)
    return result


async def _dispatch_hermes_relay(target: str, payload: dict) -> dict:
    from backend.integrations import hermes

    # NOTE: relay() returns a plain str and swallows its own errors INTO that
    # string ("Hermes is not reachable right now: ..."). So from the broker's
    # point of view a relay always "succeeds" — distinguishing a real Hermes-side
    # failure from a normal response is the Tier 1.4 relay-quarantine follow-up.
    r = await hermes.relay(payload["message"])
    return {"response": r}


async def _dispatch_hermes_action(target: str, payload: dict) -> dict:
    """Build a phrase from the structured allowlist verb, then relay it.

    `build_command` validates the verb + args and raises ValueError on anything
    invalid/unknown — that propagates up to the dispatch try/except in
    `execute_action`, which records the action FAILED (never re-raises).
    """
    from backend.integrations import hermes
    from backend.safety import hermes_actions

    command = hermes_actions.build_command(payload["verb"], payload.get("args") or {})
    # Use the structured relay (Tier 1.4 follow-up, now unblocked by the Hermes #2
    # response contract): it returns {"ok", "response", "intent"} so a Hermes-side
    # action failure (e.g. Proxmox 500 → "error: ...") is no longer swallowed into
    # a success string. Raise on ok=False so execute_action records this FAILED.
    # Forward the broker idempotency key (if any) so a retry racing our own dedup
    # can't double-execute on Hermes (Hermes-side #7).
    result = await hermes.relay_action(command, idempotency_key=payload.get("idempotency_key"))
    if not result.get("ok", True):
        raise RuntimeError(f"Hermes action failed: {result.get('response')}")
    return {"command": command, "response": result.get("response"), "intent": result.get("intent")}


async def _dispatch_channels_record(target: str, payload: dict) -> dict:
    """Trigger a Channels DVR recording for the given program_id.

    Calls channels_dvr.trigger_recording directly from this PC — NOT via Hermes.
    """
    from backend.integrations import channels_dvr

    r = await channels_dvr.trigger_recording(payload["program_id"])
    # trigger_recording returns a dict; surface it directly.
    return r if isinstance(r, dict) else {"result": r}


async def _dispatch_unraid_docker(target: str, payload: dict) -> dict:
    """Restart a Docker container on Unraid.

    Calls unraid.restart_docker directly from this PC — NOT via Hermes.
    """
    from backend.integrations import unraid

    ok = await unraid.restart_docker(payload["container_id"])
    return {"success": bool(ok)}


async def _dispatch_obsidian_task(target: str, payload: dict) -> dict:
    """Check off a task in an Obsidian vault note.

    Calls obsidian.complete_task directly from this PC — NOT via Hermes.
    """
    from backend.integrations import obsidian

    await obsidian.complete_task(payload["note_path"], payload["task_text"])
    return {"ok": True}


_DISPATCHERS = {
    "ha_service": _dispatch_ha_service,
    "hermes_relay": _dispatch_hermes_relay,
    "hermes_action": _dispatch_hermes_action,
    "channels_record": _dispatch_channels_record,
    "unraid_docker": _dispatch_unraid_docker,
    "obsidian_task": _dispatch_obsidian_task,
}


# ---------------------------------------------------------------------------
# Durable DB helpers — SYNCHRONOUS, invoked ONLY via asyncio.to_thread. They
# open/close their own Session inside the worker thread and return plain
# dicts/scalars so no ORM object or Session crosses an `await` (Windows
# ProactorEventLoop safety, see CLAUDE.md).
# ---------------------------------------------------------------------------

def _insert_action_log(
    actor: str,
    kind: str,
    target: str,
    payload: dict,
    risk: str,
    reversibility: str,
    decision: str,
    idempotency_key: str | None,
) -> int:
    """Insert the BEFORE/intent ActionLog row and return its id."""
    from sqlmodel import Session

    from backend.database import ActionLog, engine

    with Session(engine) as session:
        row = ActionLog(
            actor=actor,
            kind=kind,
            target=target,
            payload_json=json.dumps(payload),
            risk=risk,
            reversibility=reversibility,
            decision=decision,
            idempotency_key=idempotency_key,
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        return row.id


def _update_action_log(log_id: int, decision: str, result_json: str | None) -> None:
    """Update-only: stamp the final decision + result on an existing row."""
    from sqlmodel import Session

    from backend.database import ActionLog, engine

    with Session(engine) as session:
        row = session.get(ActionLog, log_id)
        if row is None:  # pragma: no cover - defensive
            return
        row.decision = decision
        row.result_json = result_json
        row.updated_at = datetime.utcnow()
        session.add(row)
        session.commit()


def _find_completed_action(key: str) -> dict | None:
    """Most-recent terminal (executed/failed/forbidden) ActionLog row for a key.

    Returns a plain dict (or None). A `needs_confirm` row is NOT terminal and is
    ignored here — the action has not completed yet.
    """
    from sqlmodel import Session, select

    from backend.database import ActionLog, engine

    with Session(engine) as session:
        row = session.exec(
            select(ActionLog)
            .where(ActionLog.idempotency_key == key)
            .where(ActionLog.decision.in_(_TERMINAL_DECISIONS))
            .order_by(ActionLog.created_at.desc())
        ).first()
        if row is None:
            return None
        return {
            "id": row.id,
            "decision": row.decision,
            "risk": row.risk,
            "reversibility": row.reversibility,
            "result_json": row.result_json,
        }


def _get_action_log(log_id: int) -> dict | None:
    """Fetch a single ActionLog row by id as a plain dict (or None).

    Returned fields: id, actor, kind, target, payload (dict, json-parsed),
    decision, risk, reversibility, created_at (datetime), idempotency_key.
    Sync only — call via asyncio.to_thread.
    """
    from sqlmodel import Session

    from backend.database import ActionLog, engine

    with Session(engine) as session:
        row = session.get(ActionLog, log_id)
        if row is None:
            return None
        try:
            payload = json.loads(row.payload_json)
        except (TypeError, ValueError):
            payload = {}
        return {
            "id": row.id,
            "actor": row.actor,
            "kind": row.kind,
            "target": row.target,
            "payload": payload,
            "decision": row.decision,
            "risk": row.risk,
            "reversibility": row.reversibility,
            "created_at": row.created_at,
            "idempotency_key": row.idempotency_key,
        }


# ---------------------------------------------------------------------------
# The chokepoint
# ---------------------------------------------------------------------------

def _coerce_actor(actor) -> Actor:
    """Normalize an actor to the Actor enum. An UNKNOWN actor string degrades to
    AUTONOMOUS (the most restrictive role) — never silently to USER."""
    if isinstance(actor, Actor):
        return actor
    try:
        return Actor(str(actor))
    except ValueError:
        return Actor.AUTONOMOUS


async def _publish_action(
    actor, kind: str, target: str, decision, risk, reversibility, log_id
) -> None:
    """Best-effort broadcast of a terminal broker outcome to /ws/logs clients.

    Never raises — all errors are swallowed by events.publish itself.
    """
    from backend import events
    await events.publish("action", {
        "actor": getattr(actor, "value", str(actor)),
        "kind": kind,
        "target": target,
        "decision": getattr(decision, "value", str(decision)),
        "risk": getattr(risk, "value", str(risk)),
        "reversibility": getattr(reversibility, "value", str(reversibility)),
        "log_id": log_id,
    })


async def execute_action(
    actor,
    kind: str,
    target: str,
    payload: dict,
    idempotency_key: str | None = None,
    *,
    confirmed: bool = False,
) -> ActionResult:
    """Run a side-effecting action through the policy gate + audit log.

    Returns an ActionResult whose `decision` is the FINAL state:
      * forbidden / needs_confirm — the gate refused/deferred; nothing dispatched.
      * executed / failed — the gate allowed it and dispatch was attempted.
    Never re-raises a dispatch error; failures are caught, logged, and recorded.
    """
    actor = _coerce_actor(actor)
    payload = payload or {}

    # Validate payload is JSON-serializable BEFORE any DB write — a bad payload
    # is a programming error, surface it loudly rather than half-logging it.
    try:
        json.dumps(payload)
    except (TypeError, ValueError) as e:
        raise ValueError(f"payload is not JSON-serializable: {e}") from e

    # Idempotency replay: if this key already completed, return that outcome and
    # do NOT dispatch again.
    if idempotency_key:
        existing = await asyncio.to_thread(_find_completed_action, idempotency_key)
        if existing is not None:
            result_obj = None
            if existing["result_json"]:
                try:
                    result_obj = json.loads(existing["result_json"])
                except (TypeError, ValueError):
                    result_obj = None
            return ActionResult(
                decision=Decision(existing["decision"]),
                risk=Risk(existing["risk"]),
                reversibility=Reversibility(existing["reversibility"]),
                log_id=existing["id"],
                result=result_obj if isinstance(result_obj, dict) else None,
                error=(result_obj.get("error") if isinstance(result_obj, dict) else None),
                replayed=True,
            )

    # Global kill switch: when autonomy is disabled, agent/autonomous side effects
    # are forbidden outright (a USER action is unaffected — preserves chat UX).
    # Checked AFTER the idempotency replay (a completed action still replays its
    # recorded result) but BEFORE classify/decide/dispatch.
    if actor in (Actor.AGENT, Actor.AUTONOMOUS):
        from backend.safety import governor
        state = await asyncio.to_thread(governor.get_system_state)
        if not state["autonomy_enabled"]:
            risk, reversibility = classify(kind, payload)
            log_id = await asyncio.to_thread(
                _insert_action_log,
                actor.value,
                kind,
                target,
                payload,
                risk.value,
                reversibility.value,
                Decision.FORBIDDEN.value,
                idempotency_key,
            )
            await asyncio.to_thread(
                _update_action_log,
                log_id,
                Decision.FORBIDDEN.value,
                json.dumps({"reason": "autonomy_disabled"}),
            )
            await _publish_action(actor, kind, target, Decision.FORBIDDEN, risk, reversibility, log_id)
            return ActionResult(
                decision=Decision.FORBIDDEN,
                risk=risk,
                reversibility=reversibility,
                log_id=log_id,
                error="autonomy_disabled",
            )

    risk, reversibility = classify(kind, payload)
    decision = decide(actor, risk, reversibility, confirmed, kind=kind)

    # BEFORE write — the intent/gate record exists even if the process dies now.
    log_id = await asyncio.to_thread(
        _insert_action_log,
        actor.value,
        kind,
        target,
        payload,
        risk.value,
        reversibility.value,
        decision.value,
        idempotency_key,
    )

    # Gate said no / not-yet — record stands as-is, nothing dispatched.
    if decision in (Decision.FORBIDDEN, Decision.NEEDS_CONFIRM):
        await _publish_action(actor, kind, target, decision, risk, reversibility, log_id)
        # Phone alert ONLY on needs_confirm (something awaits human tap) — NOT on forbidden.
        if decision == Decision.NEEDS_CONFIRM:
            from backend import events
            await events.notify_phone(
                f"NEXUS needs your approval: {kind} -> {target} (risk {risk.value}). "
                f"Open the Safety page to confirm or reject.",
                kind="needs_confirm",
            )
        return ActionResult(
            decision=decision,
            risk=risk,
            reversibility=reversibility,
            log_id=log_id,
        )

    # decision == ALLOWED — throttle/circuit-breaker gate for agent/autonomous actors.
    # USER actions are never throttled (the user is always allowed — preserves chat UX).
    if actor in (Actor.AGENT, Actor.AUTONOMOUS):
        from backend.safety import throttle
        from backend.config import get_settings
        _s = get_settings()
        _ok, _reason = throttle.allow(
            kind,
            max_per_window=_s.verb_throttle_max,
            window_s=_s.verb_throttle_window_s,
        )
        if not _ok:
            await asyncio.to_thread(
                _update_action_log, log_id, Decision.FORBIDDEN.value, json.dumps({"reason": _reason})
            )
            await _publish_action(actor, kind, target, Decision.FORBIDDEN, risk, reversibility, log_id)
            from backend import events
            await events.notify_phone(
                f"NEXUS blocked '{kind}' ({_reason}).",
                kind="throttled",
            )
            return ActionResult(
                decision=Decision.FORBIDDEN,
                risk=risk,
                reversibility=reversibility,
                log_id=log_id,
                error=_reason,
            )
        throttle.record_attempt(kind)

    # dispatch.
    dispatcher = _DISPATCHERS.get(kind)
    if dispatcher is None:
        error = f"no dispatcher for kind '{kind}'"
        await asyncio.to_thread(
            _update_action_log, log_id, Decision.FAILED.value, json.dumps({"error": error})
        )
        await _publish_action(actor, kind, target, Decision.FAILED, risk, reversibility, log_id)
        # Record outcome for agent/autonomous circuit breaker (no dispatcher = failure).
        if actor in (Actor.AGENT, Actor.AUTONOMOUS):
            from backend.safety import throttle as _throttle
            from backend.config import get_settings as _gs
            _cfg = _gs()
            _tripped = _throttle.record_result(
                kind, False,
                failure_threshold=_cfg.breaker_failure_threshold,
                window_s=_cfg.verb_throttle_window_s,
                cooldown_s=_cfg.breaker_cooldown_s,
            )
            if _tripped:
                from backend import events as _events
                await _events.notify_phone(
                    f"NEXUS circuit breaker TRIPPED for '{kind}' after repeated failures"
                    f" — auto-paused {_cfg.breaker_cooldown_s}s.",
                    kind="circuit_breaker",
                )
        return ActionResult(
            decision=Decision.FAILED,
            risk=risk,
            reversibility=reversibility,
            log_id=log_id,
            error=error,
        )

    # Forward the broker idempotency key to the hermes_action dispatcher (so it can
    # set the Hermes Idempotency-Key header). Non-mutating: only a local copy for
    # this dispatch carries the extra field; the caller's payload is untouched.
    dispatch_payload = payload
    if kind == "hermes_action" and idempotency_key:
        dispatch_payload = {**payload, "idempotency_key": idempotency_key}

    try:
        result = await dispatcher(target, dispatch_payload)
    except Exception as e:  # never re-raise — record the failure and return it
        logger.warning(f"Action dispatch failed kind={kind} target={target}: {e}")
        await asyncio.to_thread(
            _update_action_log, log_id, Decision.FAILED.value, json.dumps({"error": str(e)})
        )
        await _publish_action(actor, kind, target, Decision.FAILED, risk, reversibility, log_id)
        # Record outcome for agent/autonomous circuit breaker.
        if actor in (Actor.AGENT, Actor.AUTONOMOUS):
            from backend.safety import throttle as _throttle
            from backend.config import get_settings as _gs
            _cfg = _gs()
            _tripped = _throttle.record_result(
                kind, False,
                failure_threshold=_cfg.breaker_failure_threshold,
                window_s=_cfg.verb_throttle_window_s,
                cooldown_s=_cfg.breaker_cooldown_s,
            )
            if _tripped:
                from backend import events as _events
                await _events.notify_phone(
                    f"NEXUS circuit breaker TRIPPED for '{kind}' after repeated failures"
                    f" — auto-paused {_cfg.breaker_cooldown_s}s.",
                    kind="circuit_breaker",
                )
        return ActionResult(
            decision=Decision.FAILED,
            risk=risk,
            reversibility=reversibility,
            log_id=log_id,
            error=str(e),
        )

    await asyncio.to_thread(
        _update_action_log, log_id, Decision.EXECUTED.value, json.dumps(result)
    )
    await _publish_action(actor, kind, target, Decision.EXECUTED, risk, reversibility, log_id)
    # Record outcome for agent/autonomous circuit breaker (success resets failure streak).
    if actor in (Actor.AGENT, Actor.AUTONOMOUS):
        from backend.safety import throttle as _throttle
        from backend.config import get_settings as _gs
        _cfg = _gs()
        _throttle.record_result(
            kind, True,
            failure_threshold=_cfg.breaker_failure_threshold,
            window_s=_cfg.verb_throttle_window_s,
            cooldown_s=_cfg.breaker_cooldown_s,
        )
    return ActionResult(
        decision=Decision.EXECUTED,
        risk=risk,
        reversibility=reversibility,
        log_id=log_id,
        result=result if isinstance(result, dict) else {"result": result},
    )


async def confirm_action(
    log_id: int,
    *,
    ttl_seconds: int | None = None,
) -> tuple[str, "ActionResult | None"]:
    """Confirm-and-dispatch a needs_confirm action.

    Returns (status, ActionResult|None) where status is one of:
      not_found        — no ActionLog row with this id
      not_confirmable  — row exists but decision is not needs_confirm (double-confirm prevention)
      expired          — confirmation window exceeded ttl_seconds
      forbidden        — kill switch is ON for an agent/autonomous actor
      executed         — dispatch succeeded
      failed           — dispatch failed (dispatcher error or no dispatcher for kind)

    Re-checks the kill switch and TTL at confirm time. Updates the SAME ActionLog
    row in place — no second row is inserted. Never re-raises a dispatch error.
    """
    # Step 1: fetch the row
    row = await asyncio.to_thread(_get_action_log, log_id)
    if row is None:
        return ("not_found", None)

    # Step 2: must be awaiting confirmation (also blocks double-dispatch of same row)
    if row["decision"] != Decision.NEEDS_CONFIRM.value:
        return ("not_confirmable", None)

    # Step 3: parse risk/reversibility defensively
    try:
        risk = Risk(row["risk"])
    except ValueError:
        risk = Risk.UNCLASSIFIABLE
    try:
        reversibility = Reversibility(row["reversibility"])
    except ValueError:
        reversibility = Reversibility.UNKNOWN

    # Step 4: TTL check — if the confirmation window has elapsed, record FORBIDDEN
    if ttl_seconds is not None:
        age_seconds = (datetime.utcnow() - row["created_at"]).total_seconds()
        if age_seconds > ttl_seconds:
            await asyncio.to_thread(
                _update_action_log,
                log_id,
                Decision.FORBIDDEN.value,
                json.dumps({"reason": "expired"}),
            )
            await _publish_action(
                _coerce_actor(row["actor"]), row["kind"], row["target"],
                Decision.FORBIDDEN, risk, reversibility, log_id,
            )
            return ("expired", None)

    # Step 5: kill switch re-check for non-user actors
    actor = _coerce_actor(row["actor"])
    if actor in (Actor.AGENT, Actor.AUTONOMOUS):
        from backend.safety import governor
        state = await asyncio.to_thread(governor.get_system_state)
        if not state["autonomy_enabled"]:
            await asyncio.to_thread(
                _update_action_log,
                log_id,
                Decision.FORBIDDEN.value,
                json.dumps({"reason": "autonomy_disabled"}),
            )
            await _publish_action(
                actor, row["kind"], row["target"],
                Decision.FORBIDDEN, risk, reversibility, log_id,
            )
            return (
                "forbidden",
                ActionResult(
                    decision=Decision.FORBIDDEN,
                    risk=risk,
                    reversibility=reversibility,
                    log_id=log_id,
                    error="autonomy_disabled",
                ),
            )

    # Step 6: look up dispatcher
    dispatcher = _DISPATCHERS.get(row["kind"])
    if dispatcher is None:
        error = f"no dispatcher for kind '{row['kind']}'"
        await asyncio.to_thread(
            _update_action_log, log_id, Decision.FAILED.value, json.dumps({"error": error})
        )
        await _publish_action(
            actor, row["kind"], row["target"],
            Decision.FAILED, risk, reversibility, log_id,
        )
        return (
            "failed",
            ActionResult(
                decision=Decision.FAILED,
                risk=risk,
                reversibility=reversibility,
                log_id=log_id,
                error=error,
            ),
        )

    # Step 7: dispatch
    try:
        result = await dispatcher(row["target"], row["payload"])
    except Exception as e:
        logger.warning(f"confirm_action dispatch failed kind={row['kind']} id={log_id}: {e}")
        await asyncio.to_thread(
            _update_action_log, log_id, Decision.FAILED.value, json.dumps({"error": str(e)})
        )
        await _publish_action(
            actor, row["kind"], row["target"],
            Decision.FAILED, risk, reversibility, log_id,
        )
        return (
            "failed",
            ActionResult(
                decision=Decision.FAILED,
                risk=risk,
                reversibility=reversibility,
                log_id=log_id,
                error=str(e),
            ),
        )

    await asyncio.to_thread(
        _update_action_log, log_id, Decision.EXECUTED.value, json.dumps(result)
    )
    await _publish_action(
        actor, row["kind"], row["target"],
        Decision.EXECUTED, risk, reversibility, log_id,
    )
    return (
        "executed",
        ActionResult(
            decision=Decision.EXECUTED,
            risk=risk,
            reversibility=reversibility,
            log_id=log_id,
            result=result if isinstance(result, dict) else {"result": result},
        ),
    )
