"""Tests for the confirm-policy override layer in decide() (Feature 3 Phase 1)."""
from backend.safety.broker import Actor, Decision, Reversibility, Risk, decide


# ---------------------------------------------------------------------------
# policy=None reproduces pre-Feature-3 behavior byte for byte
# ---------------------------------------------------------------------------

def test_decide_unchanged_without_policy():
    # A representative sample across every branch — must match decide()'s
    # documented pre-Feature-3 behavior exactly.
    assert decide(Actor.USER, Risk.HIGH, Reversibility.IRREVERSIBLE, False) == Decision.ALLOWED
    assert decide(Actor.AGENT, Risk.HIGH, Reversibility.UNKNOWN, False) == Decision.NEEDS_CONFIRM
    assert decide(Actor.AGENT, Risk.HIGH, Reversibility.UNKNOWN, True) == Decision.ALLOWED
    assert decide(Actor.AGENT, Risk.LOW, Reversibility.REVERSIBLE, False) == Decision.ALLOWED
    assert decide(Actor.AUTONOMOUS, Risk.HIGH, Reversibility.IRREVERSIBLE, False) == Decision.FORBIDDEN
    assert decide(Actor.AGENT, Risk.UNCLASSIFIABLE, Reversibility.UNKNOWN, False) == Decision.NEEDS_CONFIRM


# ---------------------------------------------------------------------------
# Promotion (auto_allow)
# ---------------------------------------------------------------------------

def test_auto_allow_promotes_high_risk():
    policy = {"auto_allow": {"ha_service"}, "forbid": set()}
    result = decide(Actor.AGENT, Risk.HIGH, Reversibility.UNKNOWN, False, kind="ha_service", policy=policy)
    assert result == Decision.ALLOWED


def test_auto_allow_no_effect_when_kind_not_in_set():
    policy = {"auto_allow": {"some_other_kind"}, "forbid": set()}
    result = decide(Actor.AGENT, Risk.HIGH, Reversibility.UNKNOWN, False, kind="ha_service", policy=policy)
    assert result == Decision.NEEDS_CONFIRM


def test_forbid_beats_auto_allow():
    # A kind in BOTH lists — forbid wins, fail-safe on contradictory state.
    policy = {"auto_allow": {"ha_service"}, "forbid": {"ha_service"}}
    result = decide(Actor.AGENT, Risk.HIGH, Reversibility.UNKNOWN, False, kind="ha_service", policy=policy)
    assert result == Decision.FORBIDDEN


def test_auto_allow_cannot_override_irreversible():
    policy = {"auto_allow": {"protonmail_send"}, "forbid": set()}
    result = decide(Actor.AGENT, Risk.HIGH, Reversibility.IRREVERSIBLE, False, kind="protonmail_send", policy=policy)
    assert result == Decision.FORBIDDEN


def test_auto_allow_respects_confirmed_on_irreversible():
    # Irreversible + confirmed=True is still ALLOWED regardless of policy —
    # confirmed always wins for irreversible actions, policy is irrelevant here.
    policy = {"auto_allow": {"protonmail_send"}, "forbid": set()}
    result = decide(Actor.AGENT, Risk.HIGH, Reversibility.IRREVERSIBLE, True, kind="protonmail_send", policy=policy)
    assert result == Decision.ALLOWED


def test_auto_allow_cannot_override_unclassifiable():
    policy = {"auto_allow": {"totally_unknown_kind"}, "forbid": set()}
    result = decide(Actor.AGENT, Risk.UNCLASSIFIABLE, Reversibility.UNKNOWN, False,
                     kind="totally_unknown_kind", policy=policy)
    assert result == Decision.NEEDS_CONFIRM


def test_policy_promote_never_promotes_itself():
    # _NEVER_PROMOTABLE guard — even if someone hand-edited the CSV to include
    # "policy_promote" itself, decide() must never auto-allow it.
    policy = {"auto_allow": {"policy_promote"}, "forbid": set()}
    result = decide(Actor.AUTONOMOUS, Risk.HIGH, Reversibility.REVERSIBLE_BY_INVERSE, False,
                     kind="policy_promote", policy=policy)
    assert result == Decision.NEEDS_CONFIRM


# ---------------------------------------------------------------------------
# Demotion (forbid)
# ---------------------------------------------------------------------------

def test_forbid_demotes_low_risk_kind():
    policy = {"auto_allow": set(), "forbid": {"send_notification"}}
    result = decide(Actor.AGENT, Risk.LOW, Reversibility.REVERSIBLE, False,
                     kind="send_notification", policy=policy)
    assert result == Decision.FORBIDDEN


def test_forbid_applies_before_irreversibility_check():
    # Order sanity: forbid is checked first regardless of which later branch
    # would otherwise have applied.
    policy = {"auto_allow": set(), "forbid": {"vm_power"}}
    result = decide(Actor.AGENT, Risk.HIGH, Reversibility.UNKNOWN, True, kind="vm_power", policy=policy)
    assert result == Decision.FORBIDDEN


# ---------------------------------------------------------------------------
# USER actor is unaffected by policy entirely
# ---------------------------------------------------------------------------

def test_user_actor_unaffected_by_forbid_list():
    policy = {"auto_allow": set(), "forbid": {"ha_service"}}
    result = decide(Actor.USER, Risk.HIGH, Reversibility.UNKNOWN, False, kind="ha_service", policy=policy)
    assert result == Decision.ALLOWED
