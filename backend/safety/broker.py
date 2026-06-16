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
        # is HIGH by construction. This is what makes the Tier 1.4
        # relay-quarantine follow-up visible in the audit log.
        return Risk.HIGH, Reversibility.UNKNOWN

    # Unknown kind — we have no policy for it.
    return Risk.UNCLASSIFIABLE, Reversibility.UNKNOWN


def decide(actor: Actor, risk: Risk, reversibility: Reversibility, confirmed: bool) -> Decision:
    """Gate decision: may this actor run this action now?

    Returns one of {allowed, needs_confirm, forbidden} — the GATE outcome, never
    a dispatch outcome.
    """
    # A direct human action is always allowed (preserves the chat UX); it is still
    # classified and logged so the audit trail is complete.
    if actor == Actor.USER:
        return Decision.ALLOWED

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

    result = await homeassistant.call_service(
        payload["domain"], payload["service"], {"entity_id": target}
    )
    return result


async def _dispatch_hermes_relay(target: str, payload: dict) -> dict:
    from backend.integrations import hermes

    # NOTE: relay() returns a plain str and swallows its own errors INTO that
    # string ("Hermes is not reachable right now: ..."). So from the broker's
    # point of view a relay always "succeeds" — distinguishing a real Hermes-side
    # failure from a normal response is the Tier 1.4 relay-quarantine follow-up.
    r = await hermes.relay(payload["message"])
    return {"response": r}


_DISPATCHERS = {
    "ha_service": _dispatch_ha_service,
    "hermes_relay": _dispatch_hermes_relay,
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
            return ActionResult(
                decision=Decision.FORBIDDEN,
                risk=risk,
                reversibility=reversibility,
                log_id=log_id,
                error="autonomy_disabled",
            )

    risk, reversibility = classify(kind, payload)
    decision = decide(actor, risk, reversibility, confirmed)

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
        return ActionResult(
            decision=decision,
            risk=risk,
            reversibility=reversibility,
            log_id=log_id,
        )

    # decision == ALLOWED — dispatch.
    dispatcher = _DISPATCHERS.get(kind)
    if dispatcher is None:
        error = f"no dispatcher for kind '{kind}'"
        await asyncio.to_thread(
            _update_action_log, log_id, Decision.FAILED.value, json.dumps({"error": error})
        )
        return ActionResult(
            decision=Decision.FAILED,
            risk=risk,
            reversibility=reversibility,
            log_id=log_id,
            error=error,
        )

    try:
        result = await dispatcher(target, payload)
    except Exception as e:  # never re-raise — record the failure and return it
        logger.warning(f"Action dispatch failed kind={kind} target={target}: {e}")
        await asyncio.to_thread(
            _update_action_log, log_id, Decision.FAILED.value, json.dumps({"error": str(e)})
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
    return ActionResult(
        decision=Decision.EXECUTED,
        risk=risk,
        reversibility=reversibility,
        log_id=log_id,
        result=result if isinstance(result, dict) else {"result": result},
    )
