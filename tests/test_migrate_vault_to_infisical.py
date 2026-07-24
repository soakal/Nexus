import importlib.util
import sys
from pathlib import Path

import pytest

_SCRIPT_PATH = Path(__file__).resolve().parent.parent / "tools" / "migrate_vault_to_infisical.py"
_spec = importlib.util.spec_from_file_location("migrate_vault_to_infisical", _SCRIPT_PATH)
mig = importlib.util.module_from_spec(_spec)
sys.modules["migrate_vault_to_infisical"] = mig
_spec.loader.exec_module(mig)

SENTINEL = "sentinel-v4lu3-abc123"


class _FakePath:
    def __init__(self, exists=True):
        self._exists = exists

    def exists(self):
        return self._exists


@pytest.fixture(autouse=True)
def _vault_files_exist(monkeypatch):
    monkeypatch.setattr(mig.vault, "VAULT_PATH", _FakePath(True))
    monkeypatch.setattr(mig.vault, "KEY_PATH", _FakePath(True))


def _no_sentinel_leak(capsys):
    out = capsys.readouterr()
    combined = out.out + out.err
    assert SENTINEL not in combined, f"a secret value leaked into output: {combined!r}"
    return combined


def test_no_args_exits_2():
    with pytest.raises(SystemExit) as exc:
        mig.main([])
    assert exc.value.code == 2


def test_mutually_exclusive_flags_exit_2():
    with pytest.raises(SystemExit) as exc:
        mig.main(["--dry-run", "--verify"])
    assert exc.value.code == 2


def test_preflight_vault_missing_exits_2(monkeypatch, capsys):
    monkeypatch.setattr(mig.vault, "VAULT_PATH", _FakePath(False))
    monkeypatch.setattr(mig.infisical_client, "warm_up", lambda: True)
    with pytest.raises(SystemExit) as exc:
        mig.preflight()
    assert exc.value.code == 2


def test_preflight_empty_vault_exits_2(monkeypatch):
    monkeypatch.setattr(mig.vault, "list_keys", lambda: [])
    monkeypatch.setattr(mig.infisical_client, "warm_up", lambda: True)
    with pytest.raises(SystemExit) as exc:
        mig.preflight()
    assert exc.value.code == 2


def test_preflight_canonical_collision_exits_2(monkeypatch):
    monkeypatch.setattr(mig.vault, "list_keys", lambda: ["cred:Hermes:host", "cred:hermes:host"])
    monkeypatch.setattr(mig.infisical_client, "warm_up", lambda: True)
    with pytest.raises(SystemExit) as exc:
        mig.preflight()
    assert exc.value.code == 2


def test_preflight_infisical_unreachable_exits_2(monkeypatch):
    monkeypatch.setattr(mig.vault, "list_keys", lambda: ["KEY_A"])
    monkeypatch.setattr(mig.infisical_client, "warm_up", lambda: False)
    with pytest.raises(SystemExit) as exc:
        mig.preflight()
    assert exc.value.code == 2


def test_dry_run_create_vs_update_split(monkeypatch, capsys):
    keys = ["ANTHROPIC_API_KEY", "cred:hermes:host"]
    monkeypatch.setattr(mig.infisical_client, "list_keys", lambda: ["cred:hermes:host"])
    fake_values = {"ANTHROPIC_API_KEY": SENTINEL, "cred:hermes:host": SENTINEL}
    monkeypatch.setattr(mig.vault, "get_secret", lambda k: fake_values[k])
    set_calls = []
    monkeypatch.setattr(mig.infisical_client, "set_secret", lambda k, v: set_calls.append((k, v)))

    rc = mig.run_dry_run(keys)
    out = _no_sentinel_leak(capsys)

    assert rc == 0
    assert set_calls == []  # dry-run must never write
    assert "PLAN ANTHROPIC_API_KEY -> /:ANTHROPIC_API_KEY [create]" in out
    assert "PLAN cred:hermes:host -> /creds/hermes:HOST [update]" in out
    assert "SUMMARY total=2 create=1 update=1 unreadable=0" in out


def test_dry_run_unreadable_key_continues_and_flags(monkeypatch, capsys):
    keys = ["GOOD_KEY", "BAD_KEY"]
    monkeypatch.setattr(mig.infisical_client, "list_keys", lambda: [])

    def fake_get(k):
        if k == "BAD_KEY":
            raise RuntimeError(".vault.key not found")
        return SENTINEL

    monkeypatch.setattr(mig.vault, "get_secret", fake_get)
    rc = mig.run_dry_run(keys)
    out = _no_sentinel_leak(capsys)
    assert rc == 1
    assert "UNREADABLE BAD_KEY (RuntimeError)" in out
    assert "SUMMARY total=2 create=1 update=0 unreadable=1" in out


def test_execute_happy_path_preserves_original_casing(monkeypatch, capsys):
    keys = ["cred:Unraid:host", "ANTHROPIC_API_KEY"]
    monkeypatch.setattr(mig.infisical_client, "list_keys", lambda: [])
    monkeypatch.setattr(mig.vault, "get_secret", lambda k: SENTINEL)
    set_calls = []
    monkeypatch.setattr(mig.infisical_client, "set_secret", lambda k, v: set_calls.append((k, v)))

    rc = mig.run_execute(keys)
    out = _no_sentinel_leak(capsys)

    assert rc == 0
    assert ("cred:Unraid:host", SENTINEL) in set_calls  # original casing preserved
    assert ("ANTHROPIC_API_KEY", SENTINEL) in set_calls
    assert "SUMMARY total=2 created=2 updated=0 failed=0" in out


def test_execute_one_failure_does_not_abort_remaining_keys(monkeypatch, capsys):
    keys = ["KEY_A", "KEY_B", "KEY_C"]
    monkeypatch.setattr(mig.infisical_client, "list_keys", lambda: [])
    monkeypatch.setattr(mig.vault, "get_secret", lambda k: SENTINEL)

    written = []

    def fake_set(k, v):
        if k == "KEY_B":
            raise ValueError("boom")
        written.append(k)

    monkeypatch.setattr(mig.infisical_client, "set_secret", fake_set)
    rc = mig.run_execute(keys)
    out = _no_sentinel_leak(capsys)

    assert rc == 1
    assert written == ["KEY_A", "KEY_C"]
    assert "FAIL KEY_B (ValueError)" in out
    assert "SUMMARY total=3 created=2 updated=0 failed=1" in out


def test_verify_canonicalizes_mixed_case_cred_keys(monkeypatch, capsys):
    keys = ["cred:Unraid:host"]
    monkeypatch.setattr(mig.vault, "get_secret", lambda k: SENTINEL)

    def fake_infisical_get(k):
        assert k == "cred:unraid:host"  # canonicalized lookup
        return SENTINEL

    monkeypatch.setattr(mig.infisical_client, "get_secret", fake_infisical_get)
    monkeypatch.setattr(mig.infisical_client, "list_keys", lambda: ["cred:unraid:host"])

    rc = mig.run_verify(keys)
    out = _no_sentinel_leak(capsys)
    assert rc == 0
    assert "OK cred:Unraid:host" in out
    assert "SUMMARY total=1 ok=1 mismatch=0 missing=0 error=0 extra=0" in out


def test_verify_mismatch_reports_no_values(monkeypatch, capsys):
    keys = ["SOME_KEY"]
    monkeypatch.setattr(mig.vault, "get_secret", lambda k: "vault-value-xyz")
    monkeypatch.setattr(mig.infisical_client, "get_secret", lambda k: "different-value-abc")
    monkeypatch.setattr(mig.infisical_client, "list_keys", lambda: ["SOME_KEY"])

    rc = mig.run_verify(keys)
    out = capsys.readouterr()
    combined = out.out + out.err
    assert "vault-value-xyz" not in combined
    assert "different-value-abc" not in combined
    assert rc == 1
    assert "MISMATCH SOME_KEY" in combined


def test_verify_extra_keys_do_not_fail_the_run(monkeypatch, capsys):
    keys = ["KNOWN_KEY"]
    monkeypatch.setattr(mig.vault, "get_secret", lambda k: SENTINEL)
    monkeypatch.setattr(mig.infisical_client, "get_secret", lambda k: SENTINEL)
    monkeypatch.setattr(mig.infisical_client, "list_keys", lambda: ["KNOWN_KEY", "LEFTOVER_KEY"])

    rc = mig.run_verify(keys)
    out = _no_sentinel_leak(capsys)
    assert rc == 0
    assert "EXTRA LEFTOVER_KEY" in out
    assert "extra=1" in out
