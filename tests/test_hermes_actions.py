"""Item 1 — pure-function ACs for the Hermes structured allowlist."""

import json

import pytest

from backend.safety import hermes_actions as ha
from backend.safety.broker import Reversibility, Risk

# Expected (risk, reversibility) per the spec table.
_TABLE = {
    "proxmox_status": (Risk.LOW, Reversibility.REVERSIBLE),
    "unraid_status": (Risk.LOW, Reversibility.REVERSIBLE),
    "ha_status": (Risk.LOW, Reversibility.REVERSIBLE),
    "garage_status": (Risk.LOW, Reversibility.REVERSIBLE),
    "unifi_status": (Risk.LOW, Reversibility.REVERSIBLE),
    "adguard_status": (Risk.LOW, Reversibility.REVERSIBLE),
    "channels_status": (Risk.LOW, Reversibility.REVERSIBLE),
    "jellyfin_status": (Risk.LOW, Reversibility.REVERSIBLE),
    "daily_digest": (Risk.LOW, Reversibility.REVERSIBLE),
    "adguard_control": (Risk.MEDIUM, Reversibility.REVERSIBLE_BY_INVERSE),
    "restart_service": (Risk.HIGH, Reversibility.REVERSIBLE_BY_INVERSE),
    "vm_action": (Risk.HIGH, Reversibility.REVERSIBLE_BY_INVERSE),
    "service_logs": (Risk.LOW, Reversibility.REVERSIBLE),
    "reload_integration": (Risk.MEDIUM, Reversibility.REVERSIBLE),
    "wol": (Risk.HIGH, Reversibility.REVERSIBLE_BY_INVERSE),
}


def test_build_service_logs():
    assert ha.build_command("service_logs", {"name": "jellyfin"}) == "logs for jellyfin"


def test_build_wol():
    assert ha.build_command("wol", {"host": "nas01"}) == "wake nas01"


def test_wol_injection_rejected():
    # wol is HIGH (agent needs-confirm) AND its arg is injection-defended.
    import pytest as _pytest
    with _pytest.raises(ValueError):
        ha.build_command("wol", {"host": "nas01; rm -rf /"})
    with _pytest.raises(ValueError):
        ha.build_command("service_logs", {})  # missing required arg


def test_allowlist_has_exactly_the_table_verbs():
    assert set(ha.ALLOWLIST.keys()) == set(_TABLE.keys())


@pytest.mark.parametrize("verb,expected", list(_TABLE.items()))
def test_classify_verb(verb, expected):
    assert ha.classify_verb(verb) == expected


def test_classify_verb_unknown():
    assert ha.classify_verb("bogus") == (Risk.UNCLASSIFIABLE, Reversibility.UNKNOWN)
    assert ha.classify_verb("") == (Risk.UNCLASSIFIABLE, Reversibility.UNKNOWN)
    assert ha.classify_verb(None) == (Risk.UNCLASSIFIABLE, Reversibility.UNKNOWN)


@pytest.mark.parametrize("verb", list(_TABLE.keys()))
def test_is_allowed_true(verb):
    assert ha.is_allowed(verb) is True


def test_is_allowed_case_insensitive():
    assert ha.is_allowed("PROXMOX_STATUS") is True
    assert ha.is_allowed("Vm_Action") is True


def test_is_allowed_false():
    assert ha.is_allowed("bogus") is False
    assert ha.is_allowed("") is False
    assert ha.is_allowed(None) is False


# --- build_command happy paths (spec ACs) ---

def test_build_no_arg_verb():
    assert ha.build_command("proxmox_status", {}) == "check proxmox"
    assert ha.build_command("unraid_status", {}) == "check unraid"
    assert ha.build_command("ha_status", {}) == "check ha"
    assert ha.build_command("garage_status", {}) == "is the garage open"
    assert ha.build_command("unifi_status", {}) == "check unifi"
    assert ha.build_command("adguard_status", {}) == "adguard"
    assert ha.build_command("channels_status", {}) == "channels dvr"
    assert ha.build_command("jellyfin_status", {}) == "what's playing"
    assert ha.build_command("daily_digest", {}) == "daily report"


def test_build_no_arg_verb_ignores_extras():
    assert ha.build_command("proxmox_status", {"foo": "bar"}) == "check proxmox"
    assert ha.build_command("proxmox_status") == "check proxmox"


def test_build_restart_service():
    assert ha.build_command("restart_service", {"name": "jellyfin"}) == "restart jellyfin"


def test_build_vm_action():
    assert ha.build_command("vm_action", {"vm": "200", "action": "reboot"}) == "reboot 200"
    assert ha.build_command("vm_action", {"vm": "200", "action": "stop"}) == "stop 200"


def test_build_adguard_control():
    assert ha.build_command("adguard_control", {"action": "disable"}) == "disable adguard"
    assert ha.build_command("adguard_control", {"action": "enable"}) == "enable adguard"


def test_build_case_insensitive_verb():
    assert ha.build_command("RESTART_SERVICE", {"name": "jellyfin"}) == "restart jellyfin"


def test_build_strips_string_arg():
    assert ha.build_command("restart_service", {"name": "  jellyfin  "}) == "restart jellyfin"


# --- build_command failure paths -> ValueError ---

def test_build_unknown_verb_raises():
    with pytest.raises(ValueError):
        ha.build_command("bogus", {})


def test_build_missing_required_arg_raises():
    with pytest.raises(ValueError):
        ha.build_command("restart_service", {})


def test_build_blank_required_arg_raises():
    with pytest.raises(ValueError):
        ha.build_command("restart_service", {"name": "   "})


def test_build_missing_enum_arg_raises():
    with pytest.raises(ValueError):
        ha.build_command("adguard_control", {})
    with pytest.raises(ValueError):
        ha.build_command("vm_action", {"vm": "200"})


def test_build_bad_enum_value_raises():
    with pytest.raises(ValueError):
        ha.build_command("adguard_control", {"action": "nuke"})
    with pytest.raises(ValueError):
        ha.build_command("vm_action", {"vm": "200", "action": "destroy"})


def test_build_injection_in_vm_arg_raises():
    with pytest.raises(ValueError):
        ha.build_command("vm_action", {"vm": "200; rm -rf /", "action": "stop"})


def test_build_injection_in_name_arg_raises():
    for bad in ("jelly;fin", "a$(whoami)", "x && y", "a|b", "back`tick`"):
        with pytest.raises(ValueError):
            ha.build_command("restart_service", {"name": bad})


# --- validate_args ---

def test_validate_args_ok():
    assert ha.validate_args("proxmox_status", {}) is None
    assert ha.validate_args("restart_service", {"name": "jellyfin"}) is None
    assert ha.validate_args("vm_action", {"vm": "200", "action": "start"}) is None
    assert ha.validate_args("adguard_control", {"action": "enable"}) is None


def test_validate_args_errors_are_strings():
    assert isinstance(ha.validate_args("restart_service", {}), str)
    assert isinstance(ha.validate_args("vm_action", {"vm": "x;y", "action": "stop"}), str)
    assert isinstance(ha.validate_args("adguard_control", {"action": "bad"}), str)
    assert isinstance(ha.validate_args("bogus", {}), str)


def test_validate_args_non_string_arg():
    assert ha.validate_args("restart_service", {"name": 123}) is not None
    assert ha.validate_args("vm_action", {"vm": "200", "action": 5}) is not None


# --- allowed_verbs JSON-safe, no callables ---

def test_allowed_verbs_json_safe_no_callables():
    verbs = ha.allowed_verbs()
    # round-trips through JSON cleanly
    blob = json.dumps(verbs)
    again = json.loads(blob)
    assert again == verbs
    assert len(verbs) == len(_TABLE)
    keys = {"verb", "risk", "reversibility", "required_args", "enum_args"}
    for v in verbs:
        assert set(v.keys()) == keys
        assert not callable(v.get("verb"))
        assert isinstance(v["required_args"], list)
        assert isinstance(v["enum_args"], dict)


def test_allowed_verbs_enum_shape():
    by_verb = {v["verb"]: v for v in ha.allowed_verbs()}
    assert by_verb["vm_action"]["enum_args"] == {"action": ["reboot", "start", "stop"]}
    assert by_verb["adguard_control"]["enum_args"] == {"action": ["disable", "enable"]}
    assert by_verb["restart_service"]["required_args"] == ["name"]
    assert by_verb["proxmox_status"]["enum_args"] == {}
    assert by_verb["proxmox_status"]["required_args"] == []
