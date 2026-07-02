"""Structured Hermes verb allowlist — the quarantine for free-text relay.

The Hermes integration's `relay()` posts raw natural language straight at a live
PRODUCTION homelab bot that can restart LXCs, open the garage, stop VMs, etc.
Letting an agent or an autonomous task hand-write that free text is a command
injection waiting to happen. Tier 1.4 quarantines it:

  * AGENT / AUTONOMOUS actors may ONLY emit a Hermes command via this allowlist
    (kind="hermes_action") — a fixed set of verbs, each with validated/whitelisted
    args, each building its phrase through a pure function. They can NEVER send
    free text (kind="hermes_relay" is FORBIDDEN for non-user actors in the broker).
  * A human USER chat can still fall back to free-text relay (kind="hermes_relay")
    because a direct human action is always allowed — that path is unchanged.

Everything in this module is PURE: no I/O, no await, no DB. The broker is the
only place a built command is actually dispatched to Hermes.

Import note: this module imports `Risk`/`Reversibility` from the broker at module
top. To avoid a circular import the broker imports THIS module LAZILY
(function-local) inside `classify` and the dispatcher — never at its module top.
"""

from collections.abc import Callable
from dataclasses import dataclass

from backend.safety.broker import Reversibility, Risk

# Characters permitted in free string args (vm name, service name). Anything
# outside this set is rejected — this is the injection defense that stops a
# second shell/command ("200; rm -rf /") from being smuggled into the phrase.
_SAFE_ARG_CHARS = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789 ._-")


@dataclass(frozen=True)
class HermesVerb:
    verb: str
    risk: Risk
    reversibility: Reversibility
    required_args: tuple[str, ...]
    enum_args: dict[str, frozenset[str]]
    build: Callable[[dict], str]


def _no_arg(phrase: str) -> Callable[[dict], str]:
    """Build a pure args->phrase function for a no-argument verb (extras ignored)."""
    return lambda args: phrase


# ---------------------------------------------------------------------------
# The allowlist — keyed by verb. Each `build` is a pure args -> phrase fn.
# ---------------------------------------------------------------------------

ALLOWLIST: dict[str, HermesVerb] = {
    "proxmox_status": HermesVerb(
        verb="proxmox_status", risk=Risk.LOW, reversibility=Reversibility.REVERSIBLE,
        required_args=(), enum_args={}, build=_no_arg("check proxmox"),
    ),
    "unraid_status": HermesVerb(
        verb="unraid_status", risk=Risk.LOW, reversibility=Reversibility.REVERSIBLE,
        required_args=(), enum_args={}, build=_no_arg("check unraid"),
    ),
    "ha_status": HermesVerb(
        verb="ha_status", risk=Risk.LOW, reversibility=Reversibility.REVERSIBLE,
        required_args=(), enum_args={}, build=_no_arg("check ha"),
    ),
    "garage_status": HermesVerb(
        verb="garage_status", risk=Risk.LOW, reversibility=Reversibility.REVERSIBLE,
        required_args=(), enum_args={}, build=_no_arg("is the garage open"),
    ),
    "unifi_status": HermesVerb(
        verb="unifi_status", risk=Risk.LOW, reversibility=Reversibility.REVERSIBLE,
        required_args=(), enum_args={}, build=_no_arg("check unifi"),
    ),
    "adguard_status": HermesVerb(
        verb="adguard_status", risk=Risk.LOW, reversibility=Reversibility.REVERSIBLE,
        required_args=(), enum_args={}, build=_no_arg("adguard"),
    ),
    "channels_status": HermesVerb(
        verb="channels_status", risk=Risk.LOW, reversibility=Reversibility.REVERSIBLE,
        required_args=(), enum_args={}, build=_no_arg("channels dvr"),
    ),
    "jellyfin_status": HermesVerb(
        verb="jellyfin_status", risk=Risk.LOW, reversibility=Reversibility.REVERSIBLE,
        required_args=(), enum_args={}, build=_no_arg("what's playing"),
    ),
    "daily_digest": HermesVerb(
        verb="daily_digest", risk=Risk.LOW, reversibility=Reversibility.REVERSIBLE,
        required_args=(), enum_args={}, build=_no_arg("daily report"),
    ),
    "adguard_control": HermesVerb(
        verb="adguard_control", risk=Risk.MEDIUM, reversibility=Reversibility.REVERSIBLE_BY_INVERSE,
        required_args=(), enum_args={"action": frozenset({"enable", "disable"})},
        build=lambda args: f"{args['action']} adguard",
    ),
    "restart_service": HermesVerb(
        verb="restart_service", risk=Risk.HIGH, reversibility=Reversibility.REVERSIBLE_BY_INVERSE,
        required_args=("name",), enum_args={},
        build=lambda args: f"restart {args['name'].strip()}",
    ),
    "vm_action": HermesVerb(
        verb="vm_action", risk=Risk.HIGH, reversibility=Reversibility.REVERSIBLE_BY_INVERSE,
        required_args=("vm",), enum_args={"action": frozenset({"start", "stop", "reboot"})},
        build=lambda args: f"{args['action']} {args['vm'].strip()}",
    ),
    # Read-only diagnostic: fetch a service's recent logs. Zero blast radius.
    "service_logs": HermesVerb(
        verb="service_logs", risk=Risk.LOW, reversibility=Reversibility.REVERSIBLE,
        required_args=("name",), enum_args={},
        build=lambda args: f"logs for {args['name'].strip()}",
    ),
    # Reload a Home Assistant config entry by integration domain (e.g. "unifi").
    # MEDIUM: temporary integration disruption, auto-recovers on reload completion.
    "reload_integration": HermesVerb(
        verb="reload_integration", risk=Risk.MEDIUM, reversibility=Reversibility.REVERSIBLE,
        required_args=("integration",), enum_args={},
        build=lambda args: f"reload integration {args['integration'].strip()}",
    ),
    # Wake-on-LAN a machine. HIGH so an agent/autonomous actor ALWAYS needs a human
    # tap (it powers on hardware); reversible by shutting the machine back down.
    "wol": HermesVerb(
        verb="wol", risk=Risk.HIGH, reversibility=Reversibility.REVERSIBLE_BY_INVERSE,
        required_args=("host",), enum_args={},
        build=lambda args: f"wake {args['host'].strip()}",
    ),
    # --- Tier C batch 2 verbs (council mandated HIGH/needs_confirm for ALL) ---
    # Resync the Proxmox apt package index. Intrinsically low risk (an index
    # refresh only — the PVE REST API CANNOT apply/upgrade packages, so this was
    # descoped from apply-updates), but the council mandated HIGH/needs_confirm
    # for every Tier C verb. REVERSIBLE: re-runnable and non-mutating.
    "pve_refresh_updates": HermesVerb(
        verb="pve_refresh_updates", risk=Risk.HIGH, reversibility=Reversibility.REVERSIBLE,
        required_args=(), enum_args={}, build=_no_arg("refresh updates"),
    ),
    # Prune dangling docker images on Unraid (images only — never containers/
    # volumes/networks). REVERSIBLE_BY_INVERSE: a removed image is recovered by a
    # re-pull. HIGH per the Tier C mandate.
    "docker_prune": HermesVerb(
        verb="docker_prune", risk=Risk.HIGH, reversibility=Reversibility.REVERSIBLE_BY_INVERSE,
        required_args=(), enum_args={}, build=_no_arg("docker prune"),
    ),
    # Block / unblock a Unifi client by hostname or hyphenated MAC. The arg
    # charset bans colons, so a MAC must travel hyphenated (aa-bb-cc-dd-ee-ff) or
    # as a hostname; Hermes normalizes it. REVERSIBLE_BY_INVERSE (block<->unblock).
    "unifi_block_client": HermesVerb(
        verb="unifi_block_client", risk=Risk.HIGH, reversibility=Reversibility.REVERSIBLE_BY_INVERSE,
        required_args=("client",), enum_args={},
        build=lambda args: f"block {args['client'].strip()}",
    ),
    "unifi_unblock_client": HermesVerb(
        verb="unifi_unblock_client", risk=Risk.HIGH, reversibility=Reversibility.REVERSIBLE_BY_INVERSE,
        required_args=("client",), enum_args={},
        build=lambda args: f"unblock {args['client'].strip()}",
    ),
}


def _normalize(verb) -> str:
    return str(verb or "").strip().lower()


def is_allowed(verb) -> bool:
    """True iff `verb` (case-insensitive) is in the allowlist."""
    return _normalize(verb) in ALLOWLIST


def classify_verb(verb) -> tuple[Risk, Reversibility]:
    """(Risk, Reversibility) for a verb; unknown -> (UNCLASSIFIABLE, UNKNOWN)."""
    spec = ALLOWLIST.get(_normalize(verb))
    if spec is None:
        return Risk.UNCLASSIFIABLE, Reversibility.UNKNOWN
    return spec.risk, spec.reversibility


def validate_args(verb, args: dict | None) -> str | None:
    """Validate args against the verb spec.

    Returns an error string if a required arg is missing/blank, a string arg
    contains illegal chars, or an enum arg is missing/out of its set. Returns
    None when the args are valid for the verb. Unknown verb -> error string.
    """
    spec = ALLOWLIST.get(_normalize(verb))
    if spec is None:
        return f"unknown verb: {verb!r}"
    args = args or {}

    # Required free string args: present, non-blank after strip, safe chars only.
    for name in spec.required_args:
        raw = args.get(name)
        if raw is None:
            return f"missing required arg: {name!r}"
        if not isinstance(raw, str):
            return f"arg {name!r} must be a string"
        val = raw.strip()
        if not val:
            return f"arg {name!r} must not be blank"
        if any(c not in _SAFE_ARG_CHARS for c in val):
            return f"arg {name!r} contains illegal characters"

    # Enum args: required (each enum arg is mandatory) and within its set.
    for name, allowed in spec.enum_args.items():
        raw = args.get(name)
        if raw is None:
            return f"missing required arg: {name!r}"
        if not isinstance(raw, str):
            return f"arg {name!r} must be a string"
        if raw not in allowed:
            return f"arg {name!r} must be one of {sorted(allowed)}"

    return None


def build_command(verb, args: dict | None = None) -> str:
    """Validate then build the Hermes phrase for `verb`.

    Raises ValueError on an unknown verb or invalid args (injection defense).
    A no-arg verb ignores any extra args.
    """
    norm = _normalize(verb)
    spec = ALLOWLIST.get(norm)
    if spec is None:
        raise ValueError(f"unknown verb: {verb!r}")
    err = validate_args(norm, args)
    if err is not None:
        raise ValueError(err)
    return spec.build(args or {})


def allowed_verbs() -> list[dict]:
    """JSON-safe description of every allowed verb (NO callables)."""
    out: list[dict] = []
    for spec in ALLOWLIST.values():
        out.append({
            "verb": spec.verb,
            "risk": spec.risk.value,
            "reversibility": spec.reversibility.value,
            "required_args": list(spec.required_args),
            "enum_args": {k: sorted(v) for k, v in spec.enum_args.items()},
        })
    return out
