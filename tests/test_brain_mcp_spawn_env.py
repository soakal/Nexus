import os
import subprocess

import pytest

import backend.main as main
from backend.main import _brain_mcp_spawn_env


def test_spawn_env_none_when_token_missing():
    assert _brain_mcp_spawn_env(None) is None


def test_spawn_env_none_when_token_blank():
    assert _brain_mcp_spawn_env("") is None


def test_spawn_env_carries_token_and_inherits_parent_env(monkeypatch):
    monkeypatch.setenv("NEXUS_TEST_PARENT_VAR", "parent-value")
    env = _brain_mcp_spawn_env("abc123")
    assert env is not None
    assert env["MCP_WRITE_TOKEN"] == "abc123"
    # Full copy of the parent env, not a minimal dict (a minimal dict would
    # strip PATH/HOME/other env-provided config the child would otherwise
    # inherit from the parent process).
    assert env["NEXUS_TEST_PARENT_VAR"] == "parent-value"
    assert env.keys() >= os.environ.keys()


# ---------------------------------------------------------------------------
# Brain MCP token-drift self-heal (backend/scheduler.py::_brain_mcp_token_check)
# ---------------------------------------------------------------------------


class FakeProc:
    """Minimal stand-in for subprocess.Popen, enough for _brain_mcp_reconcile/
    _brain_mcp_stop/_brain_mcp_shutdown to drive."""

    def __init__(self, pid=1234, stubborn=False):
        self.pid = pid
        self.returncode = None
        self._stubborn = stubborn
        self._terminated = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self._terminated = True
        if not self._stubborn:
            self.returncode = -15

    def kill(self):
        self.returncode = -9

    def wait(self, timeout=None):
        if self.returncode is None:
            raise subprocess.TimeoutExpired(cmd="fake", timeout=timeout)
        return self.returncode


@pytest.fixture(autouse=True)
def _reset_bo_state(monkeypatch):
    """Every test gets a fresh, isolated _bo_state — the real module-level
    dict must never leak between tests (or into a real running process)."""
    monkeypatch.setattr(main, "_bo_state", {"proc": None, "token": None, "stopped": False})


def _set_token(monkeypatch, value):
    monkeypatch.setattr(main, "_read_brain_mcp_token", lambda: value)


def _spy_spawn(monkeypatch):
    """Replace _brain_mcp_spawn with a spy that installs a FakeProc and
    records every call's token, without touching real subprocess.Popen."""
    calls = []

    def fake_spawn(token):
        calls.append(token)
        main._bo_state["proc"] = FakeProc()
        main._bo_state["token"] = token

    monkeypatch.setattr(main, "_brain_mcp_spawn", fake_spawn)
    return calls


def test_reconcile_not_managed_when_never_spawned(monkeypatch):
    _set_token(monkeypatch, "whatever")
    spawn_calls = _spy_spawn(monkeypatch)
    assert main._brain_mcp_reconcile() == "not_managed"
    assert spawn_calls == []


def test_reconcile_noop_when_token_unchanged(monkeypatch):
    proc = FakeProc()
    main._bo_state["proc"] = proc
    main._bo_state["token"] = "same"
    _set_token(monkeypatch, "same")
    spawn_calls = _spy_spawn(monkeypatch)
    assert main._brain_mcp_reconcile() == "ok"
    assert spawn_calls == []
    assert proc.returncode is None  # never terminated


def test_reconcile_respawns_on_token_change(monkeypatch):
    old_proc = FakeProc()
    main._bo_state["proc"] = old_proc
    main._bo_state["token"] = "old"
    _set_token(monkeypatch, "new")
    spawn_calls = _spy_spawn(monkeypatch)
    assert main._brain_mcp_reconcile() == "respawned"
    assert old_proc.returncode == -15  # terminated
    assert spawn_calls == ["new"]
    assert main._bo_state["token"] == "new"


def test_reconcile_respawns_tokenless_server_when_token_appears(monkeypatch):
    old_proc = FakeProc()
    main._bo_state["proc"] = old_proc
    main._bo_state["token"] = None
    _set_token(monkeypatch, "real")
    spawn_calls = _spy_spawn(monkeypatch)
    assert main._brain_mcp_reconcile() == "respawned"
    assert spawn_calls == ["real"]


def test_reconcile_does_not_downgrade_to_empty_token(monkeypatch):
    proc = FakeProc()
    main._bo_state["proc"] = proc
    main._bo_state["token"] = "real"
    _set_token(monkeypatch, None)
    spawn_calls = _spy_spawn(monkeypatch)
    assert main._brain_mcp_reconcile() == "skipped_empty"
    assert spawn_calls == []
    assert proc.returncode is None  # untouched
    assert main._bo_state["token"] == "real"


def test_reconcile_restarts_dead_process(monkeypatch):
    dead = FakeProc()
    dead.returncode = 1
    main._bo_state["proc"] = dead
    main._bo_state["token"] = "old"
    _set_token(monkeypatch, "old")  # unchanged token, still restarts since dead
    spawn_calls = _spy_spawn(monkeypatch)
    assert main._brain_mcp_reconcile() == "restarted_dead"
    assert spawn_calls == ["old"]


def test_reconcile_restarts_dead_process_even_tokenless(monkeypatch):
    dead = FakeProc()
    dead.returncode = 1
    main._bo_state["proc"] = dead
    main._bo_state["token"] = None
    _set_token(monkeypatch, None)
    spawn_calls = _spy_spawn(monkeypatch)
    assert main._brain_mcp_reconcile() == "restarted_dead"
    assert spawn_calls == [None]


def test_reconcile_respawn_failure_propagates_and_retries_next_tick(monkeypatch):
    old_proc = FakeProc()
    main._bo_state["proc"] = old_proc
    main._bo_state["token"] = "old"
    _set_token(monkeypatch, "new")

    attempts = []

    def failing_then_ok(token):
        attempts.append(token)
        if len(attempts) == 1:
            raise OSError("spawn failed")
        main._bo_state["proc"] = FakeProc()
        main._bo_state["token"] = token

    monkeypatch.setattr(main, "_brain_mcp_spawn", failing_then_ok)

    with pytest.raises(OSError):
        main._brain_mcp_reconcile()
    # Old (now-terminated) proc must still be in the slot so the next tick's
    # poll() sees it as dead and retries via restarted_dead, rather than the
    # slot being cleared and reconcile forgetting a server should be running.
    assert main._bo_state["proc"] is old_proc
    assert old_proc.returncode == -15

    # Next tick: dead proc (poll() != None) -> retries the spawn, succeeds.
    assert main._brain_mcp_reconcile() == "restarted_dead"
    assert attempts == ["new", "new"]


def test_reconcile_stop_escalates_to_kill(monkeypatch):
    stubborn = FakeProc(stubborn=True)
    main._bo_state["proc"] = stubborn
    main._bo_state["token"] = "old"
    _set_token(monkeypatch, "new")
    spawn_calls = _spy_spawn(monkeypatch)
    assert main._brain_mcp_reconcile() == "respawned"
    assert stubborn.returncode == -9  # kill(), not just terminate()
    assert spawn_calls == ["new"]


def test_reconcile_no_respawn_if_kill_fails(monkeypatch):
    class NeverDies(FakeProc):
        def kill(self):
            pass  # returncode stays None forever

    proc = NeverDies(stubborn=True)
    main._bo_state["proc"] = proc
    main._bo_state["token"] = "old"
    _set_token(monkeypatch, "new")
    spawn_calls = _spy_spawn(monkeypatch)
    with pytest.raises(subprocess.TimeoutExpired):
        main._brain_mcp_reconcile()
    assert spawn_calls == []


def test_reconcile_noop_after_shutdown(monkeypatch):
    proc = FakeProc()
    main._bo_state["proc"] = proc
    main._bo_state["token"] = "old"
    main._bo_state["stopped"] = True
    _set_token(monkeypatch, "new")
    spawn_calls = _spy_spawn(monkeypatch)
    assert main._brain_mcp_reconcile() == "stopped"
    assert spawn_calls == []
    assert proc.returncode is None


def test_shutdown_terminates_and_sets_stopped():
    proc = FakeProc()
    main._bo_state["proc"] = proc
    main._brain_mcp_shutdown()
    assert main._bo_state["stopped"] is True
    assert proc.returncode == -15


def test_shutdown_noop_when_never_spawned():
    main._brain_mcp_shutdown()  # must not raise
    assert main._bo_state["stopped"] is True


def test_read_token_returns_none_on_exception(monkeypatch):
    class BoomSettings:
        @property
        def brain_mcp_write_token(self):
            raise RuntimeError("vault down")

    monkeypatch.setattr("backend.config.get_settings", lambda: BoomSettings())
    assert main._read_brain_mcp_token() is None


def test_read_token_returns_none_on_blank(monkeypatch):
    class BlankSettings:
        brain_mcp_write_token = ""

    monkeypatch.setattr("backend.config.get_settings", lambda: BlankSettings())
    assert main._read_brain_mcp_token() is None


def test_start_spawns_with_env_token(monkeypatch, tmp_path):
    srv = tmp_path / "mcp_server.py"
    srv.write_text("# fake")
    monkeypatch.setattr(main, "_BO_DIR", tmp_path)
    monkeypatch.setattr(main.brain_organizer, "venv_python_path", lambda d: tmp_path / "python3")
    (tmp_path / "python3").write_text("#!/bin/sh\n")

    captured = {}

    class FakePopen:
        def __init__(self, cmd, cwd=None, env=None):
            captured["cmd"] = cmd
            captured["cwd"] = cwd
            captured["env"] = env
            self.pid = 999

    monkeypatch.setattr(main.subprocess, "Popen", FakePopen)
    _set_token(monkeypatch, "tok")

    main._brain_mcp_start()

    assert captured["env"]["MCP_WRITE_TOKEN"] == "tok"
    assert main._bo_state["token"] == "tok"
    assert main._bo_state["proc"].pid == 999


def test_start_noop_when_module_not_installed(monkeypatch, tmp_path):
    # _BO_DIR points at a real (empty) tmp dir -- neither the venv python nor
    # mcp_server.py exists there, so _brain_mcp_spawn must no-op cleanly.
    monkeypatch.setattr(main, "_BO_DIR", tmp_path)
    monkeypatch.setattr(main.brain_organizer, "venv_python_path", lambda d: tmp_path / "does-not-exist")

    popen_called = []
    monkeypatch.setattr(main.subprocess, "Popen", lambda *a, **k: popen_called.append(1))
    _set_token(monkeypatch, "tok")

    main._brain_mcp_start()

    assert popen_called == []
    assert main._bo_state["proc"] is None


def test_scheduler_registers_brain_mcp_token_check_job():
    from backend import scheduler as scheduler_module

    scheduler_module.setup_scheduler("07:00", "America/Detroit")
    job = scheduler_module.scheduler.get_job("brain_mcp_token_check")
    assert job is not None
    assert job.trigger.interval.total_seconds() == 300


async def test_token_check_job_reraises_on_error(monkeypatch):
    from backend import scheduler as scheduler_module

    def boom():
        raise RuntimeError("reconcile blew up")

    monkeypatch.setattr(main, "_brain_mcp_reconcile", boom)
    with pytest.raises(RuntimeError):
        await scheduler_module._brain_mcp_token_check()
