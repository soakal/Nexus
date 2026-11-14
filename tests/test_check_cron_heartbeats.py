"""Tests for tools/check_cron_heartbeats.py.

Loaded via importlib (tools/ has no __init__.py, matching
tests/test_relay_vault_signals.py's own pattern for the sibling script).
Most tests here stub `_post_flag`/`_open_flags`/`_resolve_flag` out so
`_check_one`'s recover-vs-post logic can be tested without a real socket --
but `test_resolve_flag_sends_correct_request_shape` below deliberately calls
the REAL `_resolve_flag` and monkeypatches only `urllib.request.urlopen`, so
the actual request shape is pinned too, not just assumed by every caller's
fake.
"""

import importlib.util
import json
import pathlib
import urllib.error
import urllib.request

_SCRIPT_PATH = (
    pathlib.Path(__file__).resolve().parents[1]
    / "tools"
    / "check_cron_heartbeats.py"
)

_spec = importlib.util.spec_from_file_location("check_cron_heartbeats", _SCRIPT_PATH)
hb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hb)


import pytest


_REAL_HC_PING = hb._hc_ping  # kept for the one test that exercises it (with subprocess.run stubbed)


@pytest.fixture(autouse=True)
def _no_real_healthchecks_ping(monkeypatch):
    """Tests must never ping the real healthchecks.io check (main() pings on a
    fully successful run). Tests that care about the ping re-patch _hc_ping."""
    monkeypatch.setattr(hb, "_hc_ping", lambda: True)


def _patch_state(monkeypatch, tmp_path, jobs=None):
    monkeypatch.setattr(hb, "STATE_DIR", tmp_path)
    monkeypatch.setattr(hb, "EXPECTED_INTERVAL_HOURS", jobs or {"job_a": 24})


def _patch_key(monkeypatch, key="test-key"):
    monkeypatch.setenv("NEXUS_API_KEY", key)


def _write_heartbeat(tmp_path, job, exit_code=0, finished_at=None):
    from datetime import datetime, timezone
    finished_at = finished_at or datetime.now(timezone.utc).isoformat()
    (tmp_path / f"{job}.json").write_text(
        json.dumps({"exit_code": exit_code, "finished_at": finished_at}),
        encoding="utf-8",
    )


class _FakeUrlopenResponse:
    def __init__(self, status):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


def test_healthy_job_clears_a_stale_failed_flag(monkeypatch, tmp_path):
    """The literal flag-431 regression: a job that's now healthy but has an
    open failed:<job> flag from a prior bad run must get that flag
    auto-resolved, with zero new posts."""
    _patch_state(monkeypatch, tmp_path)
    _patch_key(monkeypatch)
    _write_heartbeat(tmp_path, "job_a", exit_code=0)

    posts = []
    resolves = []
    monkeypatch.setattr(hb, "_post_flag", lambda *a: posts.append(a) or True)
    monkeypatch.setattr(hb, "_open_flags", lambda *a: {"failed:job_a": 431})
    monkeypatch.setattr(hb, "_resolve_flag", lambda base_url, key, flag_id, check: resolves.append((flag_id, check)) or True)

    hb.main()

    assert posts == []
    assert resolves == [(431, "failed:job_a")]


def test_failed_job_posts_and_clears_only_the_other_kind(monkeypatch, tmp_path):
    """A job that's `failed` with an open `overdue` flag AND an open `failed`
    flag: the still-true `failed` kind must be posted (bumped) and NOT
    resolved, while the no-longer-true `overdue` kind must be resolved."""
    _patch_state(monkeypatch, tmp_path)
    _patch_key(monkeypatch)
    _write_heartbeat(tmp_path, "job_a", exit_code=1)

    posts = []
    resolves = []
    monkeypatch.setattr(hb, "_post_flag", lambda *a: posts.append(a[2]) or True)
    monkeypatch.setattr(
        hb, "_open_flags", lambda *a: {"overdue:job_a": 1, "failed:job_a": 2}
    )
    monkeypatch.setattr(hb, "_resolve_flag", lambda base_url, key, flag_id, check: resolves.append((flag_id, check)) or True)

    hb.main()

    assert posts == ["failed:job_a"]
    assert resolves == [(1, "overdue:job_a")]


def test_healthy_job_no_open_flags_is_a_silent_noop(monkeypatch, tmp_path):
    _patch_state(monkeypatch, tmp_path)
    _patch_key(monkeypatch)
    _write_heartbeat(tmp_path, "job_a", exit_code=0)

    posts = []
    resolves = []
    monkeypatch.setattr(hb, "_post_flag", lambda *a: posts.append(a) or True)
    monkeypatch.setattr(hb, "_open_flags", lambda *a: {})
    monkeypatch.setattr(hb, "_resolve_flag", lambda *a: resolves.append(a) or True)

    hb.main()  # must not raise

    assert posts == []
    assert resolves == []


def test_open_flags_get_failure_still_lets_checks_run_and_post(monkeypatch, tmp_path):
    """If GET /api/safety/flags itself fails, the per-job checks must still
    run (and still post on a real problem), just clearing nothing this run.
    Since the K3 dead-man ping, the run then exits 1 and sends no
    healthchecks ping (an API failure is not a successful run)."""
    _patch_state(monkeypatch, tmp_path)
    _patch_key(monkeypatch)
    _write_heartbeat(tmp_path, "job_a", exit_code=1)

    posts = []
    monkeypatch.setattr(hb, "_post_flag", lambda *a: posts.append(a[2]) or True)

    # Exercise the REAL _open_flags with a failing urlopen, rather than a fake.
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=30: (_ for _ in ()).throw(OSError("network down")))
    resolves = []
    monkeypatch.setattr(hb, "_resolve_flag", lambda *a: resolves.append(a) or True)

    pings = []
    monkeypatch.setattr(hb, "_hc_ping", lambda: pings.append(1) or True)
    try:
        hb.main()
        code = None
    except SystemExit as e:
        code = e.code

    assert posts == ["failed:job_a"]
    assert resolves == []
    assert code == 1 and pings == []


def test_resolve_flag_returns_true_on_409_conflict(monkeypatch):
    """A 409 means a human already resolved it between our GET and this
    POST -- the flag is closed either way, so this counts as success, not
    an error."""
    def raise_conflict(req, timeout=30):
        raise urllib.error.HTTPError(req.full_url, 409, "Conflict", hdrs=None, fp=None)

    monkeypatch.setattr(urllib.request, "urlopen", raise_conflict)
    assert hb._resolve_flag("http://x", "k", 1, "failed:job_a") is True


def test_resolve_flag_sends_correct_request_shape(monkeypatch):
    """Exercises the REAL `_resolve_flag` -- pins the exact URL/method/
    headers/body shape, matching test_relay_vault_signals.py's
    test_post_flag_sends_correct_request_shape discipline."""
    captured = {}

    def fake_urlopen(req, timeout=30):
        captured["req"] = req
        return _FakeUrlopenResponse(200)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    ok = hb._resolve_flag("http://example.test:8000", "sekret", 431, "failed:vault_signals_routine")

    assert ok is True
    req = captured["req"]
    assert req.full_url == "http://example.test:8000/api/safety/flags/431/resolve"
    assert req.get_method() == "POST"
    assert req.get_header("Authorization") == "Bearer sekret"
    assert req.get_header("Content-type") == "application/json"
    body = json.loads(req.data)
    assert body["status"] == "resolved"
    assert "note" in body


# ---- dead-man ping (Kithos K3 plan-v6-small section 2.4) -------------------------------

def _run_main(monkeypatch, tmp_path, *, open_flags=None, post_ok=True, resolve_ok=True, exit_code=0):
    """Drive main() with every API call stubbed; returns (pings, exit_code_or_None)."""
    _patch_state(monkeypatch, tmp_path)
    _patch_key(monkeypatch)
    _write_heartbeat(tmp_path, "job_a", exit_code=exit_code)
    pings = []
    monkeypatch.setattr(hb, "_open_flags", lambda *a: open_flags)
    monkeypatch.setattr(hb, "_post_flag", lambda *a: post_ok)
    monkeypatch.setattr(hb, "_resolve_flag", lambda *a: resolve_ok)
    monkeypatch.setattr(hb, "_hc_ping", lambda: pings.append(1) or True)
    try:
        hb.main()
        return pings, None
    except SystemExit as e:
        return pings, e.code


def test_successful_run_pings_healthchecks(monkeypatch, tmp_path):
    pings, code = _run_main(monkeypatch, tmp_path, open_flags={})
    assert pings == [1] and code is None


def test_open_flags_get_failure_suppresses_ping_and_exits_1(monkeypatch, tmp_path):
    pings, code = _run_main(monkeypatch, tmp_path, open_flags=None)
    assert pings == [] and code == 1


def test_flag_post_failure_suppresses_ping_and_exits_1(monkeypatch, tmp_path):
    # a failed job needs a POST; make it fail
    pings, code = _run_main(monkeypatch, tmp_path, open_flags={}, post_ok=False, exit_code=1)
    assert pings == [] and code == 1


def test_resolve_failure_suppresses_ping_and_exits_1(monkeypatch, tmp_path):
    # healthy job with a stale open flag needs a resolve; make it fail
    pings, code = _run_main(monkeypatch, tmp_path, open_flags={"failed:job_a": 7}, resolve_ok=False)
    assert pings == [] and code == 1


def test_open_flags_returns_none_on_api_failure(monkeypatch):
    def boom(req, timeout=30):
        raise OSError("down")
    monkeypatch.setattr(urllib.request, "urlopen", boom)
    assert hb._open_flags("http://example.test:8000", "k") is None


def test_hc_ping_never_puts_the_url_on_argv_and_never_raises(monkeypatch):
    seen = {}

    class R:
        returncode = 0

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        seen["env"] = kw.get("env", {})
        return R()

    import subprocess
    monkeypatch.setattr(subprocess, "run", fake_run)
    assert _REAL_HC_PING() is True
    flat = " ".join(seen["cmd"])
    assert "hc-ping.com" not in flat and "HC_DEVBOX_CRON_CHECKER_URL" in flat and "-K -" in flat
    assert "DBUS_SESSION_BUS_ADDRESS" in seen["env"]

    def raising_run(cmd, **kw):
        raise FileNotFoundError("secret")
    monkeypatch.setattr(subprocess, "run", raising_run)
    assert _REAL_HC_PING() is False
