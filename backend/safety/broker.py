"""Policy-gated action broker — the single chokepoint for side-effecting writes.

Every action that changes the state of an external system (turning a Home
Assistant device on/off, restarting a Docker container on Unraid, ...)
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
import html
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

# Transient marker `confirm_action` writes to `ActionLog.decision` the instant it
# WINS the atomic claim below (see `_claim_action_log_for_confirm`), before TTL/
# kill-switch/dispatch run. It is never a `Decision` enum member and is never
# returned to a caller as a terminal `ActionResult.decision` — it exists purely
# so the claim's `UPDATE ... WHERE decision = 'needs_confirm'` has somewhere to
# move the row OUT of `needs_confirm` atomically, guaranteeing that a second,
# concurrently-racing `confirm_action(log_id)` call (a double-tapped Telegram
# button, a client retry, two independent callers) sees zero rows affected and
# is refused, instead of also passing the guard and also dispatching.
_DISPATCHING = "dispatching"


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
        # Fail closed: treat it the same as the explicit HIGH domains above
        # (HIGH/UNKNOWN) rather than MEDIUM, which decide() auto-allows for
        # agent/autonomous actors. An unlisted domain (script.*, automation.*,
        # media_player, etc.) must land on NEEDS_CONFIRM for a non-user actor,
        # not silently execute.
        return Risk.HIGH, Reversibility.UNKNOWN

    if kind == "channels_record":
        # Trigger a DVR recording — low blast radius, deletable via the inverse.
        return Risk.LOW, Reversibility.REVERSIBLE_BY_INVERSE

    if kind == "unraid_docker":
        # Restart a Docker container — a service restart is HIGH; always needs
        # a human tap for an agent/autonomous actor.
        return Risk.HIGH, Reversibility.REVERSIBLE_BY_INVERSE

    if kind == "unraid_docker_prune":
        # Native SSH command. Deletes dangling Docker images only (never
        # containers/volumes/networks — see unraid.py's prune_docker_images
        # docstring), but it's still a live SSH command against production
        # infra, so it's HIGH and always needs a human tap for an agent/
        # autonomous actor. The inverse (re-pull an image) is available, so
        # not IRREVERSIBLE.
        return Risk.HIGH, Reversibility.REVERSIBLE_BY_INVERSE

    if kind == "vm_power":
        # Starting/stopping/rebooting a VM or LXC is HIGH, always needs a
        # human tap for an agent/autonomous actor.
        return Risk.HIGH, Reversibility.REVERSIBLE_BY_INVERSE

    if kind in ("unifi_block", "unifi_unblock"):
        # HIGH so an agent always needs a human tap. The inverse is clean
        # (block <-> unblock), but the blast radius includes Brian's own
        # phone/NEXUS host/tailnet path if the wrong MAC is targeted — a
        # self-lockout risk an agent must never trigger unsupervised.
        return Risk.HIGH, Reversibility.REVERSIBLE_BY_INVERSE

    if kind == "obsidian_task":
        # Check off a vault task — low blast radius, reversible by unchecking.
        return Risk.LOW, Reversibility.REVERSIBLE

    if kind == "protonmail_send":
        # Sending an email cannot be unsent — IRREVERSIBLE by construction. decide()
        # therefore hard-FORBIDs an unconfirmed agent/autonomous actor (never a
        # needs_confirm phone tap for this kind) and always ALLOWs a user actor.
        return Risk.HIGH, Reversibility.IRREVERSIBLE

    if kind == "protonmail_archive":
        # Moves an email between two folders inside the same mailbox — nothing
        # leaves the account, nothing external changes, nothing live is disrupted.
        # Cleanly reversible via move_emails back to INBOX. Same band as
        # channels_record, not unraid_docker (that's HIGH despite being reversible
        # because it disrupts a live running service; archiving disrupts nothing).
        return Risk.LOW, Reversibility.REVERSIBLE_BY_INVERSE

    if kind == "protonmail_delete":
        # Verified 2026-07-23: the MCP hard-remove tool permanently expunges (a
        # test email never appeared in the real Trash folder), so the dispatcher
        # now moves mail to Trash instead (protonmail.trash_email, move_emails)
        # — same band and reasoning as protonmail_archive: moves mail between
        # folders in the same account, disrupts nothing live, reversible via
        # move_emails back to INBOX. Kind name kept as "protonmail_delete" (not
        # renamed) so the endpoint/frontend/audit-log history stay continuous.
        return Risk.LOW, Reversibility.REVERSIBLE_BY_INVERSE

    if kind == "send_notification":
        # Send a Telegram message to the OWNER only — trivial blast radius (a
        # message to yourself). LOW + REVERSIBLE so an agent may send; per-verb
        # throttle (verb_throttle_max/window) caps the spam surface, and the
        # kill switch forbids it when autonomy is off.
        return Risk.LOW, Reversibility.REVERSIBLE

    if kind == "policy_promote":
        # Feature 3 — Confirm-Policy Learner proposing to auto-allow a kind.
        # HIGH so an autonomous proposer always lands on NEEDS_CONFIRM, never
        # auto-allow itself (the mechanism that grants more autonomy is gated
        # by the exact confirm mechanism it's trying to reduce).
        # REVERSIBLE_BY_INVERSE: a later demotion (or the DELETE endpoint)
        # undoes the promotion, so decide()'s irreversibility branch doesn't
        # hard-forbid it outright.
        return Risk.HIGH, Reversibility.REVERSIBLE_BY_INVERSE

    # Unknown kind — we have no policy for it.
    return Risk.UNCLASSIFIABLE, Reversibility.UNKNOWN


# Kinds that may NEVER be auto-allowed by policy, whatever the persisted
# override state says. Hardcoded, not configurable — this is the floor.
_NEVER_PROMOTABLE = frozenset({
    "policy_promote",  # the promotion mechanism must never promote itself
})


def decide(
    actor: Actor,
    risk: Risk,
    reversibility: Reversibility,
    confirmed: bool,
    kind: str | None = None,
    *,
    policy: dict | None = None,
) -> Decision:
    """Gate decision: may this actor run this action now?

    Returns one of {allowed, needs_confirm, forbidden} — the GATE outcome, never
    a dispatch outcome.

    `kind` is optional (default None keeps the positional decide() callers/tests
    working).

    `policy` (Feature 3 — Confirm-Policy Learner) is an optional
    `{"auto_allow": set[str], "forbid": set[str]}` dict of kinds a human has
    explicitly promoted/demoted via the weekly policy review. `policy=None`
    (the default) reproduces pre-Feature-3 behavior byte for byte — every
    existing positional-arg call/test keeps working unchanged. Evaluation
    order below IS the safety property: forbid beats auto_allow (checked
    first, so a kind in both lists is FORBIDDEN, fail-safe on contradictory
    state); auto_allow can never reach an IRREVERSIBLE action (checked after
    irreversibility, not before) or an UNCLASSIFIABLE risk (an unknown kind
    has no real policy behind it, so promoting it would sign a blank cheque);
    and `_NEVER_PROMOTABLE` cannot be overridden by any policy/config value.
    """
    # A direct human action is always allowed (preserves the chat UX); it is still
    # classified and logged so the audit trail is complete.
    if actor == Actor.USER:
        return Decision.ALLOWED

    forbid = (policy or {}).get("forbid") or set()
    auto_allow = (policy or {}).get("auto_allow") or set()

    # A demotion always beats a promotion, checked BEFORE the irreversibility/
    # risk logic below — fail-safe if a kind somehow ends up in both lists.
    if kind in forbid:
        return Decision.FORBIDDEN

    # agent / autonomous — evaluate irreversibility FIRST: an irreversible action
    # is the highest-stakes case and must be confirmed regardless of risk band.
    # Auto-allow CANNOT reach here — an irreversible kind stays unpromotable
    # by construction, not by policy (e.g. protonmail_send).
    if reversibility == Reversibility.IRREVERSIBLE:
        return Decision.ALLOWED if confirmed else Decision.FORBIDDEN

    if kind in auto_allow and kind not in _NEVER_PROMOTABLE and risk != Risk.UNCLASSIFIABLE:
        return Decision.ALLOWED

    if risk in (Risk.HIGH, Risk.UNCLASSIFIABLE):
        return Decision.ALLOWED if confirmed else Decision.NEEDS_CONFIRM

    # LOW / MEDIUM, reversible enough — allowed (agent MEDIUM is permitted).
    return Decision.ALLOWED


# ---------------------------------------------------------------------------
# Dispatchers — the ONLY place an action actually fires
# ---------------------------------------------------------------------------

async def _dispatch_ha_service(target: str, payload: dict) -> dict:
    from backend.integrations import homeassistant

    # If service_data is absent, None, or empty (e.g. chat.py's non-parameterised
    # branch passes {}), fall back to targeting the entity. Non-empty dicts (e.g.
    # reload_config_entry with {"entry_id": "cloud"}) already have what they need.
    raw = payload.get("service_data")
    service_data = raw if raw else {"entity_id": target}
    result = await homeassistant.call_service(payload["domain"], payload["service"], service_data)
    return result


async def _dispatch_channels_record(target: str, payload: dict) -> dict:
    """Trigger a Channels DVR recording for the given program_id.

    Calls channels_dvr.trigger_recording directly from this PC.
    """
    from backend.integrations import channels_dvr

    r = await channels_dvr.trigger_recording(payload["program_id"])
    # trigger_recording returns a dict; surface it directly.
    return r if isinstance(r, dict) else {"result": r}


async def _dispatch_unraid_docker(target: str, payload: dict) -> dict:
    """Restart a Docker container on Unraid.

    Calls unraid.restart_docker directly from this PC.
    restart_docker already returns a rich dict ({"success": ...}, optionally
    with "stopped": True on a stop-succeeded/start-failed half-restart) —
    surfaced as-is, not re-wrapped, so that state isn't lost.
    """
    from backend.integrations import unraid

    return await unraid.restart_docker(payload["container_id"])


async def _dispatch_unraid_docker_prune(target: str, payload: dict) -> dict:
    """Prune dangling Docker images on Unraid over native SSH."""
    from backend.integrations import unraid

    return await unraid.prune_docker_images()


async def _dispatch_vm_power(target: str, payload: dict) -> dict:
    """Start/reboot/gracefully-shut-down a Proxmox VM or LXC.

    Calls proxmox.set_vm_power directly from this PC. Validates action here
    (not just inside set_vm_power) so an invalid action is a clean
    ValueError -> recorded FAILED, never an HTTP call.
    """
    from backend.integrations import proxmox

    action = payload.get("action")
    if action not in ("start", "stop", "reboot"):
        raise ValueError(f"vm_power: invalid action {action!r} (expected start/stop/reboot)")

    return await proxmox.set_vm_power(payload["vmid"], action)


async def _dispatch_unifi_block(target: str, payload: dict) -> dict:
    """Block a client from the UniFi network by MAC address."""
    from backend.integrations import unifi

    return await unifi.block_client(payload["mac"])


async def _dispatch_unifi_unblock(target: str, payload: dict) -> dict:
    """Unblock a client on the UniFi network by MAC address."""
    from backend.integrations import unifi

    return await unifi.unblock_client(payload["mac"])


async def _dispatch_obsidian_task(target: str, payload: dict) -> dict:
    """Check off a task in an Obsidian vault note."""
    from backend.integrations import obsidian

    await obsidian.complete_task(payload["note_path"], payload["task_text"])
    return {"ok": True}


async def _dispatch_send_notification(target: str, payload: dict) -> dict:
    """Send a phone (Telegram) notification to the owner.

    Wraps events.notify_phone. A delivery failure (e.g. Telegram down or a 401)
    returns delivered=False; we RAISE so execute_action records the action FAILED
    — this gives the verifier an honest success/failure signal to ground against
    instead of silently "succeeding" on an undelivered message.
    """
    from backend import events

    delivered = await events.notify_phone(payload["content"], kind="agent_message")
    if not delivered:
        raise RuntimeError("notification not delivered (Telegram unreachable or auth failed)")
    return {"delivered": True}


async def _dispatch_protonmail_send(target: str, payload: dict) -> dict:
    """Send a Proton Mail email via the MCP client integration.

    protonmail.send_email raises IntegrationError on a tool-reported failure,
    which propagates to execute_action's dispatch try/except -> recorded FAILED
    (never re-raised), matching every other dispatcher.
    """
    from backend.integrations import protonmail

    result = await protonmail.send_email(
        recipients=payload["recipients"],
        subject=payload["subject"],
        body=payload["body"],
        cc=payload.get("cc"),
        bcc=payload.get("bcc"),
        in_reply_to=payload.get("in_reply_to"),
        references=payload.get("references"),
        html=payload.get("html", False),
    )
    return result


async def _dispatch_protonmail_archive(target: str, payload: dict) -> dict:
    from backend.integrations import protonmail

    return await protonmail.archive_email(payload["email_id"], mailbox=payload.get("mailbox"))


async def _dispatch_protonmail_delete(target: str, payload: dict) -> dict:
    from backend.integrations import protonmail

    return await protonmail.trash_email(payload["email_id"], mailbox=payload.get("mailbox"))


async def _dispatch_policy_promote(target: str, payload: dict) -> dict:
    """Feature 3 — apply a human-confirmed policy promotion. `target` is the
    kind being promoted (so the audit trail reads "policy_promote -> kind",
    and it's what the confirm alert Brian actually taps is built from —
    broker.py's _publish_action call). `payload["kind"]` must describe the
    SAME single kind, or this must refuse to dispatch.

    Verification caught a real gap here: nothing previously enforced that
    `payload["kind"]` matched `target` at all. Since `add_auto_allow_kind`
    splits on comma, a payload of "obsidian_task,unraid_docker" promoted
    THREE kinds while the alert only ever named one — the human taps confirm
    on a description of the action that isn't what actually happens. Not
    reachable in Phase 1 (no caller of policy_promote exists yet — the
    learner that would propose one is Phase 2, not built), but this is the
    exact guard that must exist before Phase 2 ever calls execute_action
    with this kind.
    """
    from backend.safety import governor

    kind = payload.get("kind")
    if not isinstance(kind, str) or "," in kind or kind != target or kind in _NEVER_PROMOTABLE:
        raise ValueError(
            f"policy_promote refused: payload kind {kind!r} must be a single "
            f"kind matching target {target!r}, and not in _NEVER_PROMOTABLE"
        )
    await asyncio.to_thread(governor.add_auto_allow_kind, kind)
    return {"ok": True, "promoted_kind": kind}


_DISPATCHERS = {
    "ha_service": _dispatch_ha_service,
    "channels_record": _dispatch_channels_record,
    "unraid_docker": _dispatch_unraid_docker,
    "unraid_docker_prune": _dispatch_unraid_docker_prune,
    "vm_power": _dispatch_vm_power,
    "unifi_block": _dispatch_unifi_block,
    "unifi_unblock": _dispatch_unifi_unblock,
    "obsidian_task": _dispatch_obsidian_task,
    "send_notification": _dispatch_send_notification,
    "protonmail_send": _dispatch_protonmail_send,
    "protonmail_archive": _dispatch_protonmail_archive,
    "protonmail_delete": _dispatch_protonmail_delete,
    "policy_promote": _dispatch_policy_promote,
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


def _update_action_log_judge(
    log_id: int,
    judge_verdict: str,
    judge_reason: str,
    decision: str | None = None,
) -> None:
    """Update-only: stamp the judge's verdict/reason on an existing row.

    Mirrors `_update_action_log`, but for the judge-gate path: `decision` is
    optional so a shadow-mode call (or an enforce-mode approve) can record the
    verdict without touching the row's decision — it keeps whatever the gate
    already wrote. An enforce-mode veto passes `decision="needs_confirm"` to
    flip the already-inserted ALLOWED row in place, mirroring the throttle
    ALLOWED->FORBIDDEN update above.
    """
    from sqlmodel import Session

    from backend.database import ActionLog, engine

    with Session(engine) as session:
        row = session.get(ActionLog, log_id)
        if row is None:  # pragma: no cover - defensive
            return
        row.judge_verdict = judge_verdict
        row.judge_reason = judge_reason
        if decision is not None:
            row.decision = decision
        row.updated_at = datetime.utcnow()
        session.add(row)
        session.commit()


def _stamp_confirmed_at(log_id: int) -> None:
    """UPDATE-only: stamp confirmed_at=utcnow on an existing row (Feature 3 —
    Confirm-Policy Learner). Sync — to_thread only.

    Called from confirm_action() immediately after the needs_confirm guard
    and BEFORE the TTL/kill-switch/dispatch checks, so the timestamp is the
    moment the human tapped confirm, uncontaminated by dispatch latency
    (median ~1.9s, but 19-30s for protonmail_delete) — confirmed_at minus
    created_at is meant to be a clean human-reaction-time signal the learner
    can threshold on. A row that stamps this and then still hits expired or
    forbidden is meaningfully different from one that was never tapped at all
    ("he did tap, too late") — deliberately not cleared in that case.
    """
    from sqlmodel import Session

    from backend.database import ActionLog, engine

    with Session(engine) as session:
        row = session.get(ActionLog, log_id)
        if row is None:  # pragma: no cover - defensive
            return
        row.confirmed_at = datetime.utcnow()
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


def _claim_action_log_for_confirm(log_id: int) -> dict | None:
    """Atomically claim a `needs_confirm` row for `confirm_action` (F10 fix).

    A single conditional `UPDATE ... WHERE id = ? AND decision = 'needs_confirm'`
    is the ONLY guard against a double-confirm race: SQLite serializes writers
    against a given row, so of any number of concurrent callers racing this
    statement for the same `log_id`, at most one can ever see its row-count come
    back as 1 (this UPDATE moves the row to `_DISPATCHING`, so every other
    concurrent caller's identical `WHERE decision = 'needs_confirm'` clause no
    longer matches it). This closes the window the OLD code left open: a plain
    `SELECT`-then-later-`UPDATE` guard checked at the very top of
    `confirm_action`, with the actual terminal-state UPDATE not landing until
    AFTER `dispatch` completes — during that whole span (stamp confirmed_at,
    TTL check, kill-switch re-check via `governor.get_system_state`, the
    dispatch `await` itself) a second concurrent `confirm_action(log_id)` call
    could also read `decision == needs_confirm` and also dispatch.

    Returns the freshly-claimed row (same shape as `_get_action_log`, with
    `decision` reflecting the just-written `_DISPATCHING` value) on a win, or
    `None` on a loss (another caller already claimed it, or it was never
    `needs_confirm` to begin with) — the caller falls back to `_get_action_log`
    to tell `not_found` apart from `not_confirmable` in that case. Sync only —
    call via `asyncio.to_thread`.
    """
    from sqlalchemy import text
    from sqlmodel import Session

    from backend.database import ActionLog, engine

    with Session(engine) as session:
        result = session.execute(
            text(
                "UPDATE actionlog SET decision = :claimed, updated_at = :now "
                "WHERE id = :id AND decision = :needs_confirm"
            ),
            {
                "claimed": _DISPATCHING,
                "now": datetime.utcnow(),
                "id": log_id,
                "needs_confirm": Decision.NEEDS_CONFIRM.value,
            },
        )
        session.commit()
        if result.rowcount != 1:
            return None

        row = session.get(ActionLog, log_id)
        if row is None:  # pragma: no cover - defensive, can't happen right after a win
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
    actor, kind: str, target: str, decision, risk, reversibility, log_id,
    judge_verdict: str | None = None, judge_reason: str | None = None,
) -> None:
    """Best-effort broadcast of a terminal broker outcome to /ws/logs clients.

    Never raises — all errors are swallowed by events.publish itself.

    `judge_verdict`/`judge_reason` are included so the real-time Live Activity
    feed can render the action-judge second opinion the same way the polled
    Recent Actions / Pending Confirmations views do. They are None at call sites
    reached before the judge runs (kill switch, gate refusal, throttle) — the
    frontend renders the judge badge only when a verdict is present.
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
        "judge_verdict": judge_verdict,
        "judge_reason": judge_reason,
    })
    try:
        from backend import activity
        activity.pulse("broker", "action", f"{kind} {getattr(decision, 'value', str(decision))} · {target}")
    except Exception:
        pass


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
    policy: dict | None = None
    if actor in (Actor.AGENT, Actor.AUTONOMOUS):
        from backend.safety import governor
        state = await asyncio.to_thread(governor.get_system_state)
        # Same state fetch as the kill-switch check below — zero extra DB
        # round trips for the policy override layer (Feature 3). USER actors
        # never reach here, so `policy` stays None for them (decide() never
        # touches it before its own actor==USER early return anyway).
        policy = {
            "auto_allow": state.get("policy_auto_allow_kinds", set()),
            "forbid": state.get("policy_forbid_kinds", set()),
        }
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
    decision = decide(actor, risk, reversibility, confirmed, kind=kind, policy=policy)

    # Judge verdict/reason default to None and are only populated if the action
    # judge actually runs (AGENT/AUTONOMOUS, not confirmed, mode != off, not
    # exempt). Pre-declared here so every post-judge _publish_action call site
    # can pass them unconditionally — USER actions and confirmed/exempt/off
    # paths skip the judge block and legitimately broadcast None.
    judge_verdict: str | None = None
    judge_reason: str | None = None

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
                f"NEXUS needs your approval: {kind} -> {html.escape(target)} (risk {risk.value}).",
                kind="needs_confirm",
                buttons=[
                    {"text": "✓ Confirm", "callback_data": f"safety:confirm:{log_id}"},
                    {"text": "✗ Reject", "callback_data": f"safety:reject:{log_id}"},
                ],
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

        # Action-judge gate (Tier 3 second-opinion check) — runs AFTER the
        # throttle/circuit-breaker gate passes and BEFORE the attempt is
        # recorded against the throttle window, so a judge veto never counts
        # against the actor's rate cap. Skipped outright for `confirmed=True`
        # calls (the caller already explicitly confirmed this exact dispatch),
        # exempt kinds, and mode=="off" (fast-path allow). USER actor is
        # already excluded — this whole throttle/judge block only runs for
        # AGENT/AUTONOMOUS actors.
        if (
            not confirmed
            and _s.action_judge_mode != "off"
            and kind not in _s.action_judge_exempt_kinds
        ):
            from backend.safety import judge

            try:
                verdict = await judge.evaluate_action(actor, kind, target, payload, risk, reversibility)
            except Exception as e:  # pragma: no cover - evaluate_action never raises
                # per its own contract; this is a defensive fail-safe only, so a
                # future regression there can never escape execute_action either.
                logger.warning(f"action judge raised unexpectedly kind={kind} target={target}: {e}")
                verdict = {"allow": False, "confidence": 0.0, "reason": f"judge error: {e}", "verdict": "error"}

            judge_verdict = verdict.get("verdict") or "error"
            judge_reason = str(verdict.get("reason") or "")[:300]

            if _s.action_judge_mode == "enforce" and not verdict.get("allow"):
                # Veto (or judge failure/timeout/BudgetExceeded, which
                # evaluate_action fails safe into verdict="error"/allow=False):
                # flip the already-inserted ALLOWED row to NEEDS_CONFIRM in
                # place, mirroring the throttle ALLOWED->FORBIDDEN update just
                # above. Do NOT record a throttle attempt for a vetoed action.
                await asyncio.to_thread(
                    _update_action_log_judge,
                    log_id,
                    judge_verdict,
                    judge_reason,
                    Decision.NEEDS_CONFIRM.value,
                )
                await _publish_action(
                    actor, kind, target, Decision.NEEDS_CONFIRM, risk, reversibility, log_id,
                    judge_verdict, judge_reason,
                )
                from backend import events
                await events.notify_phone(
                    f"NEXUS needs your approval: {kind} -> {html.escape(target)} (risk {risk.value}). "
                    f"Judge: {html.escape(judge_reason)}.",
                    kind="needs_confirm",
                    buttons=[
                        {"text": "✓ Confirm", "callback_data": f"safety:confirm:{log_id}"},
                        {"text": "✗ Reject", "callback_data": f"safety:reject:{log_id}"},
                    ],
                )
                return ActionResult(
                    decision=Decision.NEEDS_CONFIRM,
                    risk=risk,
                    reversibility=reversibility,
                    log_id=log_id,
                )

            # Shadow mode, or enforce-mode approve: record the verdict on the
            # row but never block dispatch — falls through to record_attempt
            # and the normal dispatch path below.
            await asyncio.to_thread(_update_action_log_judge, log_id, judge_verdict, judge_reason)

        throttle.record_attempt(kind)

    # dispatch.
    dispatcher = _DISPATCHERS.get(kind)
    if dispatcher is None:
        error = f"no dispatcher for kind '{kind}'"
        await asyncio.to_thread(
            _update_action_log, log_id, Decision.FAILED.value, json.dumps({"error": error})
        )
        await _publish_action(
            actor, kind, target, Decision.FAILED, risk, reversibility, log_id,
            judge_verdict, judge_reason,
        )
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

    try:
        result = await dispatcher(target, payload)
    except Exception as e:  # never re-raise — record the failure and return it
        logger.warning(f"Action dispatch failed kind={kind} target={target}: {e}")
        await asyncio.to_thread(
            _update_action_log, log_id, Decision.FAILED.value, json.dumps({"error": str(e)})
        )
        await _publish_action(
            actor, kind, target, Decision.FAILED, risk, reversibility, log_id,
            judge_verdict, judge_reason,
        )
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
    await _publish_action(
        actor, kind, target, Decision.EXECUTED, risk, reversibility, log_id,
        judge_verdict, judge_reason,
    )
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
    # Step 1+2: atomically claim the row out of `needs_confirm` (F10 fix). A plain
    # fetch-then-check here (as this used to be two separate steps) would leave a
    # window open from here until the terminal-decision UPDATE at the bottom of
    # this function — during that whole span (stamp confirmed_at, TTL check,
    # kill-switch re-check, the dispatch await itself) a second concurrent
    # confirm_action(log_id) call (a double-tapped Telegram button, a client
    # retry, two independent callers) could also read decision==needs_confirm
    # and also dispatch. `_claim_action_log_for_confirm` closes that window with
    # a single conditional UPDATE: only one concurrent caller can ever win it.
    row = await asyncio.to_thread(_claim_action_log_for_confirm, log_id)
    if row is None:
        # Either no such row at all, or it exists but wasn't (or is no longer)
        # needs_confirm — including a row another concurrent caller just won
        # the claim on. Re-fetch (plain, no claim) purely to tell not_found
        # apart from not_confirmable; this fetch never mutates anything.
        existing = await asyncio.to_thread(_get_action_log, log_id)
        if existing is None:
            return ("not_found", None)
        return ("not_confirmable", None)

    # Step 2b: stamp the human-tap timestamp NOW — before TTL/kill-switch/
    # dispatch below, so it measures reaction time, not dispatch latency.
    # Placed after the needs_confirm guard (a double-confirm attempt never
    # re-stamps) and unconditionally before every possible outcome below,
    # including expired/forbidden — "he tapped, too late" is still real
    # signal, distinct from never tapping at all.
    await asyncio.to_thread(_stamp_confirmed_at, log_id)

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

    # Step 5: kill switch re-check for non-user actors. Deliberately does NOT
    # re-consult the forbid/auto_allow policy lists (Feature 3) — an explicit
    # human tap on a specific pending row outranks a policy change that
    # happened to land while it was in flight. This means a kind demoted to
    # forbid AFTER a needs_confirm row was created can still dispatch on
    # confirm; that's the accepted tradeoff, not an oversight.
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


async def reject_action(log_id: int) -> tuple[str, None]:
    """Close a needs_confirm action without dispatching it.

    Returns (status, None) where status is one of:
      not_found        — no ActionLog row with this id
      not_confirmable  — row exists but decision is not needs_confirm
      rejected          — closed, decision now forbidden

    No TTL check (rejecting an already-expired action is still a valid close)
    and no kill-switch check (rejection never dispatches, so it's always safe).
    Updates the SAME ActionLog row in place — no second row is inserted.
    """
    row = await asyncio.to_thread(_get_action_log, log_id)
    if row is None:
        return ("not_found", None)

    if row["decision"] != Decision.NEEDS_CONFIRM.value:
        return ("not_confirmable", None)

    try:
        risk = Risk(row["risk"])
    except ValueError:
        risk = Risk.UNCLASSIFIABLE
    try:
        reversibility = Reversibility(row["reversibility"])
    except ValueError:
        reversibility = Reversibility.UNKNOWN

    await asyncio.to_thread(
        _update_action_log, log_id, Decision.FORBIDDEN.value, json.dumps({"reason": "rejected_by_user"})
    )
    await _publish_action(
        _coerce_actor(row["actor"]), row["kind"], row["target"],
        Decision.FORBIDDEN, risk, reversibility, log_id,
    )
    return ("rejected", None)
