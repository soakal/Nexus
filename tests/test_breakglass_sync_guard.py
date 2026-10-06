import importlib.util
import json
import pathlib
from datetime import datetime, timezone
from subprocess import CompletedProcess
from unittest.mock import MagicMock

_p = pathlib.Path(__file__).resolve().parents[1] / "tools" / "breakglass_sync_guard.py"
_spec = importlib.util.spec_from_file_location("breakglass_sync_guard", _p)
bg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bg)


def _cp(rc, stderr=""):
    return CompletedProcess(args=[], returncode=rc, stdout="", stderr=stderr)


def _setup(tmp_path, monkeypatch, results):
    hb = tmp_path / "hb.json"
    hb.write_text(json.dumps({"finished_at": datetime.now(timezone.utc).isoformat(), "exit_code": 0}))
    monkeypatch.setattr(bg, "HEARTBEAT", hb)
    monkeypatch.setattr(bg.time, "sleep", MagicMock())
    ensure = MagicMock(side_effect=results)
    monkeypatch.setattr(bg, "_ensure_session", ensure)
    page = MagicMock(return_value=True)
    monkeypatch.setattr(bg, "_page", page)
    return ensure, page


def test_check_retries_once_before_paging(tmp_path, monkeypatch):
    ensure, page = _setup(tmp_path, monkeypatch, [_cp(1, "FAIL: blip"), _cp(0)])
    assert bg.check(dry_run=False) == 0
    assert ensure.call_count == 2
    page.assert_not_called()


def test_check_pages_with_latest_reason_when_retry_also_fails(tmp_path, monkeypatch):
    ensure, page = _setup(tmp_path, monkeypatch, [_cp(1, "FAIL: a"), _cp(1, "FAIL: still")])
    bg.check(dry_run=False)
    assert ensure.call_count == 2
    assert page.call_args[0][0] == "no_session:breakglass_sync"
    assert "still" in page.call_args[0][1]
