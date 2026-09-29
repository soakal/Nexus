from unittest.mock import AsyncMock, patch

import pytest

from backend import scheduler as scheduler_module
from backend.scheduler import _secret_drift_check, _secret_drift_scan
from backend.secrets import infisical_client, manager, vault


# ---------------------------------------------------------------------------
# _secret_drift_scan (sync, pure comparison logic)
# ---------------------------------------------------------------------------


def test_scan_skips_when_backend_not_infisical(monkeypatch):
    monkeypatch.setattr(manager, "_active_backend_name", lambda: "vault")
    called = []
    monkeypatch.setattr(infisical_client, "warm_up", lambda: called.append(1) or True)
    result = _secret_drift_scan()
    assert result == {"skipped": "active secrets backend is not infisical"}
    assert called == []


def test_scan_skips_whole_run_when_infisical_unreachable(monkeypatch):
    monkeypatch.setattr(manager, "_active_backend_name", lambda: "infisical")
    monkeypatch.setattr(infisical_client, "warm_up", lambda: False)
    get_calls = []
    monkeypatch.setattr(infisical_client, "get_secret", lambda k: get_calls.append(k))
    monkeypatch.setattr(vault, "list_keys", lambda: ["A", "B"])
    result = _secret_drift_scan()
    assert result == {"skipped": "infisical unreachable"}
    assert get_calls == []


def test_scan_matching_values_not_mismatched(monkeypatch):
    monkeypatch.setattr(manager, "_active_backend_name", lambda: "infisical")
    monkeypatch.setattr(infisical_client, "warm_up", lambda: True)
    monkeypatch.setattr(vault, "list_keys", lambda: ["A"])
    monkeypatch.setattr(vault, "get_secret", lambda k: "same-value")
    monkeypatch.setattr(infisical_client, "get_secret", lambda k: "same-value")
    result = _secret_drift_scan()
    assert result["compared"] == 1
    assert result["mismatched"] == {}
    assert result["vault_only"] == []
    assert result["errors"] == []


def test_scan_mismatch_reports_hash_prefixes_never_values(monkeypatch):
    monkeypatch.setattr(manager, "_active_backend_name", lambda: "infisical")
    monkeypatch.setattr(infisical_client, "warm_up", lambda: True)
    monkeypatch.setattr(vault, "list_keys", lambda: ["A"])
    monkeypatch.setattr(vault, "get_secret", lambda k: "vault-secret-value-xyz")
    monkeypatch.setattr(infisical_client, "get_secret", lambda k: "infisical-secret-value-abc")
    result = _secret_drift_scan()
    assert "A" in result["mismatched"]
    vh, ih = result["mismatched"]["A"]
    assert len(vh) == 12 and all(c in "0123456789abcdef" for c in vh)
    assert len(ih) == 12 and all(c in "0123456789abcdef" for c in ih)
    dump = repr(result)
    assert "vault-secret-value-xyz" not in dump
    assert "infisical-secret-value-abc" not in dump


def test_scan_keyerror_is_vault_only(monkeypatch):
    monkeypatch.setattr(manager, "_active_backend_name", lambda: "infisical")
    monkeypatch.setattr(infisical_client, "warm_up", lambda: True)
    monkeypatch.setattr(vault, "list_keys", lambda: ["A"])
    monkeypatch.setattr(vault, "get_secret", lambda k: "some-value")

    def raise_keyerror(k):
        raise KeyError(k)
    monkeypatch.setattr(infisical_client, "get_secret", raise_keyerror)
    result = _secret_drift_scan()
    assert result["vault_only"] == ["A"]
    assert result["mismatched"] == {}


def test_scan_blank_vault_value_ignored(monkeypatch):
    monkeypatch.setattr(manager, "_active_backend_name", lambda: "infisical")
    monkeypatch.setattr(infisical_client, "warm_up", lambda: True)
    monkeypatch.setattr(vault, "list_keys", lambda: ["A", "B"])
    monkeypatch.setattr(vault, "get_secret", lambda k: "" if k == "A" else "   ")
    called = []
    monkeypatch.setattr(infisical_client, "get_secret", lambda k: called.append(k))
    result = _secret_drift_scan()
    assert called == []
    assert result["compared"] == 0
    assert result["vault_only"] == []


def test_scan_blank_infisical_value_is_vault_only(monkeypatch):
    monkeypatch.setattr(manager, "_active_backend_name", lambda: "infisical")
    monkeypatch.setattr(infisical_client, "warm_up", lambda: True)
    monkeypatch.setattr(vault, "list_keys", lambda: ["A"])
    monkeypatch.setattr(vault, "get_secret", lambda k: "real-value")
    monkeypatch.setattr(infisical_client, "get_secret", lambda k: "")
    result = _secret_drift_scan()
    assert result["vault_only"] == ["A"]
    assert result["mismatched"] == {}


def test_scan_runtimeerror_mid_scan_aborts_run(monkeypatch):
    monkeypatch.setattr(manager, "_active_backend_name", lambda: "infisical")
    monkeypatch.setattr(infisical_client, "warm_up", lambda: True)
    monkeypatch.setattr(vault, "list_keys", lambda: ["A", "B"])
    monkeypatch.setattr(vault, "get_secret", lambda k: "some-value")

    def flaky(k):
        if k == "A":
            return "some-value"
        raise RuntimeError("infisical down mid-scan")
    monkeypatch.setattr(infisical_client, "get_secret", flaky)
    result = _secret_drift_scan()
    assert result == {"skipped": "infisical unreachable mid-scan"}


def test_scan_vault_read_error_recorded_and_scan_continues(monkeypatch):
    monkeypatch.setattr(manager, "_active_backend_name", lambda: "infisical")
    monkeypatch.setattr(infisical_client, "warm_up", lambda: True)
    monkeypatch.setattr(vault, "list_keys", lambda: ["A", "B"])

    def flaky_vault(k):
        if k == "A":
            raise RuntimeError("vault decrypt failed")
        return "same-value"
    monkeypatch.setattr(vault, "get_secret", flaky_vault)
    monkeypatch.setattr(infisical_client, "get_secret", lambda k: "same-value")
    result = _secret_drift_scan()
    assert result["errors"] == ["A"]
    assert result["compared"] == 1
    assert result["mismatched"] == {}


# ---------------------------------------------------------------------------
# _secret_drift_check (async job)
# ---------------------------------------------------------------------------


async def test_job_skipped_run_records_and_clears_nothing(monkeypatch):
    monkeypatch.setattr(scheduler_module, "_secret_drift_scan", lambda: {"skipped": "infisical unreachable"})
    with patch("backend.agents.outcomes.record_flag_ex", new_callable=AsyncMock) as m_record, \
         patch("backend.agents.outcomes.clear_flag", new_callable=AsyncMock) as m_clear, \
         patch("backend.agents.outcomes.open_flags", new_callable=AsyncMock) as m_open, \
         patch("backend.events.notify_phone", new_callable=AsyncMock) as m_notify:
        await _secret_drift_check()
    m_record.assert_not_awaited()
    m_clear.assert_not_awaited()
    m_open.assert_not_awaited()
    m_notify.assert_not_awaited()


async def test_job_mismatch_records_high_flag_per_key_and_pages_once(monkeypatch):
    scan_result = {
        "mismatched": {"KEY_A": ("aaaaaaaaaaaa", "bbbbbbbbbbbb"), "KEY_B": ("cccccccccccc", "dddddddddddd")},
        "vault_only": [], "errors": [], "compared": 2,
    }
    monkeypatch.setattr(scheduler_module, "_secret_drift_scan", lambda: scan_result)
    with patch("backend.agents.outcomes.record_flag_ex", new_callable=AsyncMock,
               return_value={"id": 1, "surface": True, "reason": None}) as m_record, \
         patch("backend.agents.outcomes.clear_flag", new_callable=AsyncMock) as m_clear, \
         patch("backend.agents.outcomes.open_flags", new_callable=AsyncMock, return_value=[]) as m_open, \
         patch("backend.events.notify_phone", new_callable=AsyncMock) as m_notify:
        await _secret_drift_check()
    assert m_record.await_count == 2
    calls = [c.args[:2] for c in m_record.await_args_list]
    assert ("secret_drift", "KEY_A") in calls
    assert ("secret_drift", "KEY_B") in calls
    for c in m_record.await_args_list:
        assert c.kwargs.get("severity") == "high"
    m_notify.assert_awaited_once()
    assert m_notify.await_args.kwargs.get("kind") == "secret_drift"
    m_clear.assert_not_awaited()
    m_open.assert_awaited_once()


async def test_job_page_text_contains_no_raw_values(monkeypatch):
    monkeypatch.setattr(manager, "_active_backend_name", lambda: "infisical")
    monkeypatch.setattr(infisical_client, "warm_up", lambda: True)
    monkeypatch.setattr(vault, "list_keys", lambda: ["SECRET_KEY"])
    monkeypatch.setattr(vault, "get_secret", lambda k: "vault-raw-value-111")
    monkeypatch.setattr(infisical_client, "get_secret", lambda k: "infisical-raw-value-222")

    with patch("backend.agents.outcomes.record_flag_ex", new_callable=AsyncMock,
               return_value={"id": 1, "surface": True, "reason": None}) as m_record, \
         patch("backend.agents.outcomes.clear_flag", new_callable=AsyncMock), \
         patch("backend.agents.outcomes.open_flags", new_callable=AsyncMock, return_value=[]), \
         patch("backend.events.notify_phone", new_callable=AsyncMock) as m_notify:
        await _secret_drift_check()

    notify_text = m_notify.await_args.args[0]
    assert "vault-raw-value-111" not in notify_text
    assert "infisical-raw-value-222" not in notify_text
    for c in m_record.await_args_list:
        assert "vault-raw-value-111" not in str(c)
        assert "infisical-raw-value-222" not in str(c)


async def test_job_no_page_when_flag_not_surfaced(monkeypatch):
    scan_result = {
        "mismatched": {"KEY_A": ("aaaaaaaaaaaa", "bbbbbbbbbbbb")},
        "vault_only": [], "errors": [], "compared": 1,
    }
    monkeypatch.setattr(scheduler_module, "_secret_drift_scan", lambda: scan_result)
    with patch("backend.agents.outcomes.record_flag_ex", new_callable=AsyncMock,
               return_value={"id": None, "surface": False, "reason": "deferred"}), \
         patch("backend.agents.outcomes.clear_flag", new_callable=AsyncMock), \
         patch("backend.agents.outcomes.open_flags", new_callable=AsyncMock, return_value=[]), \
         patch("backend.events.notify_phone", new_callable=AsyncMock) as m_notify:
        await _secret_drift_check()
    m_notify.assert_not_awaited()


async def test_job_clears_only_stale_secret_drift_flags(monkeypatch):
    scan_result = {
        "mismatched": {"B": ("111111111111", "222222222222")},
        "vault_only": [], "errors": [], "compared": 2,
    }
    monkeypatch.setattr(scheduler_module, "_secret_drift_scan", lambda: scan_result)
    open_rows = [
        {"source": "secret_drift", "check": "A"},  # no longer mismatched -> clear
        {"source": "secret_drift", "check": "B"},  # still mismatched -> keep
        {"source": "homelab_watch", "check": "x"},  # different source -> untouched
    ]
    with patch("backend.agents.outcomes.record_flag_ex", new_callable=AsyncMock,
               return_value={"id": 1, "surface": True, "reason": None}), \
         patch("backend.agents.outcomes.clear_flag", new_callable=AsyncMock) as m_clear, \
         patch("backend.agents.outcomes.open_flags", new_callable=AsyncMock, return_value=open_rows), \
         patch("backend.events.notify_phone", new_callable=AsyncMock):
        await _secret_drift_check()
    m_clear.assert_awaited_once_with("secret_drift", "A")


async def test_job_reraises_on_error(monkeypatch):
    def boom():
        raise RuntimeError("scan blew up")
    monkeypatch.setattr(scheduler_module, "_secret_drift_scan", boom)
    with pytest.raises(RuntimeError):
        await _secret_drift_check()


def test_scheduler_registers_secret_drift_check_daily_0905(monkeypatch):
    import backend.config as config_mod
    monkeypatch.setenv("UNRAID_BACKUP_PATH", "\\\\test-host\\test-share")
    monkeypatch.setattr(config_mod, "_settings_instance", None)
    with patch.object(scheduler_module.scheduler, "add_job") as mock_add:
        scheduler_module.setup_scheduler("07:00", "America/Detroit")
    matches = [c for c in mock_add.call_args_list if c.kwargs.get("id") == "secret_drift_check"]
    assert len(matches) == 1
    trigger = matches[0].args[1]
    fields = {f.name: str(f) for f in trigger.fields}
    assert fields.get("hour") == "9"
    assert fields.get("minute") == "5"
