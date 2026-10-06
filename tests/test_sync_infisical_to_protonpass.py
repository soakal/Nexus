import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_SCRIPT_PATH = Path(__file__).resolve().parent.parent / "tools" / "sync_infisical_to_protonpass.py"
_spec = importlib.util.spec_from_file_location("sync_infisical_to_protonpass", _SCRIPT_PATH)
sync = importlib.util.module_from_spec(_spec)
sys.modules["sync_infisical_to_protonpass"] = sync

# The script chdir()s to WORKDIR (/var/lib/nexus) at import time so the real
# infisical_client can find .env by relative path. That's fine standalone,
# but importing it here would leak the cwd change into every other test that
# runs afterward in this session (e.g. tests that read "backend/main.py" by
# relative path) -- restore it right after exec_module.
_cwd_before_import = os.getcwd()
_spec.loader.exec_module(sync)
os.chdir(_cwd_before_import)


def _completed(returncode, stderr=""):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout="", stderr=stderr)


def test_ensure_session_noop_when_already_logged_in(monkeypatch):
    monkeypatch.setattr(sync.subprocess, "run", MagicMock(return_value=_completed(0)))
    monkeypatch.setattr(sync.ic, "get_secret", MagicMock(side_effect=AssertionError("should not need the PAT")))
    sync.ensure_session({})  # no exception == pass


def test_ensure_session_logs_in_with_pat_when_session_missing(monkeypatch):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs.get("env")))
        if cmd == ["pass-cli", "login"]:
            assert kwargs["env"].get("PROTON_PASS_PERSONAL_ACCESS_TOKEN") == "pst_fake::key"
            return _completed(0)
        return _completed(1) if len(calls) == 1 else _completed(0)  # first info: no session, second info: logged in

    monkeypatch.setattr(sync.subprocess, "run", MagicMock(side_effect=fake_run))
    monkeypatch.setattr(sync.ic, "warm_up", MagicMock())
    monkeypatch.setattr(sync.ic, "get_secret", MagicMock(return_value="pst_fake::key"))

    sync.ensure_session({})

    login_calls = [c for c in calls if c[0] == ["pass-cli", "login"]]
    assert len(login_calls) == 1
    # the token must never appear in argv, only in env
    assert all("pst_fake" not in str(c[0]) for c in calls)


def test_ensure_session_raises_when_pat_missing_from_infisical(monkeypatch):
    monkeypatch.setattr(sync.subprocess, "run", MagicMock(return_value=_completed(1)))
    monkeypatch.setattr(sync.ic, "warm_up", MagicMock())
    monkeypatch.setattr(sync.ic, "get_secret", MagicMock(return_value=None))

    with pytest.raises(RuntimeError, match="PROTON_PASS_BREAKGLASS_PAT"):
        sync.ensure_session({})


def test_ensure_session_failure_surfaces_pass_cli_reason_without_token(monkeypatch):
    def fake_run(cmd, **kwargs):
        if cmd == ["pass-cli", "login"]:
            return _completed(1, stderr="error: login failed for pst_fake::key\nConnection timed out")
        return _completed(1, stderr="No session found")

    monkeypatch.setattr(sync.subprocess, "run", MagicMock(side_effect=fake_run))
    monkeypatch.setattr(sync.ic, "warm_up", MagicMock())
    monkeypatch.setattr(sync.ic, "get_secret", MagicMock(return_value="pst_fake::key"))

    with pytest.raises(RuntimeError) as exc:
        sync.ensure_session({})
    msg = str(exc.value)
    assert "Connection timed out" in msg and "No session found" in msg
    assert "pst_fake" not in msg


def test_redact_strips_token_even_if_echoed_mid_line():
    assert "pst_" not in sync._redact("bad token pst_abc::xyz here", "pst_other::val")
