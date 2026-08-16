"""Tests for backend/agents/council_postmortem.py (Feature 2 — Council-Loop
Post-Mortem). Since the payload-based repoint (2026-08-16), this module does
no git/filesystem I/O at all — every check operates on plain data structures
supplied in the payload dict (gathered client-side by Council-loop/scripts/
postmortem_payload.py). No temp git repos needed anymore."""
from unittest.mock import AsyncMock, patch

import pytest

from backend.agents import council_postmortem as cpm


def _history_line(cycle: int, commit: str | None, notes: str = "") -> dict:
    return {"cycle": cycle, "ts": "2026-07-26T00:00:00Z", "step": "step", "verdict": "accept",
            "commit": commit, "notes": notes}


def _payload(**overrides) -> dict:
    base = {
        "target_repo_name": "ProcessForge",
        "commit_prefix": "council:",
        "goal": "",
        "history": [_history_line(1, "abc1234")],
        "transcripts": "",
        "range": "base..abc1234",
        "last": "abc1234",
        "log": "abc1234\x00council: cycle 1: a",
        "files_changed": [],
        "ls_tree_last": [],
        "py_changed_count": 0,
        "py_files": {},
    }
    base.update(overrides)
    return base


def _settings(**overrides):
    from backend.config import Settings
    defaults = dict(council_postmortem_enabled=True)
    defaults.update(overrides)
    return Settings(**defaults)


# ---------------------------------------------------------------------------
# _count_council_commits (foreign commits excluded, not scanned)
# ---------------------------------------------------------------------------

def test_foreign_commits_excluded():
    log = "\n".join([
        "c0\x00initial",
        "c1\x00council: cycle 1: a",
        "c2\x00a human's manual commit",
        "c3\x00council: cycle 2: b",
    ])
    council, foreign = cpm._count_council_commits(log, "council:")
    assert council == 2
    assert foreign == 2  # c0 (initial) + c2 (manual) are both foreign


# ---------------------------------------------------------------------------
# _check_scope_drift
# ---------------------------------------------------------------------------

def test_scope_drift_detected():
    findings = cpm._check_scope_drift(["allowed.py", "sneaky.py"], ["allowed.py"])
    assert any("sneaky.py" in f["detail"] for f in findings)
    assert not any("allowed.py" in f["detail"] for f in findings)


def test_scope_drift_normalizes_backslash_and_dot_slash():
    findings_backslash = cpm._check_scope_drift(["src/foo.py"], ["src\\foo.py"])
    findings_dotslash = cpm._check_scope_drift(["src/foo.py"], ["./src/foo.py"])
    assert findings_backslash == []
    assert findings_dotslash == []


@pytest.mark.asyncio
async def test_scope_drift_skipped_when_goal_names_no_files():
    # explicit=False path is enforced in run_postmortem, not _check_scope_drift
    # itself — verified via the async extraction helper returning ([], False)
    # for a goal with no file tokens.
    paths, explicit = await cpm._extract_allowed_paths("", "claude-haiku-4-5-20251001")
    assert paths == [] and explicit is False


# ---------------------------------------------------------------------------
# _check_placeholders
# ---------------------------------------------------------------------------

def test_placeholder_pass_body_detected():
    py_files = {"a.py": {"source": "def f():\n    return 1\n\ndef g():\n    pass\n", "diff_u0": ""}}
    findings = cpm._check_placeholders(py_files, py_changed_count=1)
    assert any("g()" in f["detail"] and "pass" in f["detail"] for f in findings)


def test_placeholder_docstring_only_body_detected():
    py_files = {"a.py": {"source": 'x = 1\n\ndef g():\n    """does nothing yet"""\n', "diff_u0": ""}}
    findings = cpm._check_placeholders(py_files, py_changed_count=1)
    assert any("g()" in f["detail"] for f in findings)


def test_placeholder_ignores_unchanged_files():
    # Only changed files are ever present in py_files — an unchanged file
    # never gets here at all, so a placeholder in it can't be flagged.
    py_files = {"fresh.py": {"source": "x = 2\n", "diff_u0": ""}}
    findings = cpm._check_placeholders(py_files, py_changed_count=1)
    assert not any("stale.py" in f["detail"] for f in findings)


def test_todo_in_added_lines_only():
    diff = "+y = 2  # TODO: fix this\n-old line\n context line\n"
    py_files = {"a.py": {"source": "x = 1\ny = 2  # TODO: fix this\n", "diff_u0": diff}}
    findings = cpm._check_placeholders(py_files, py_changed_count=1)
    cruft = [f for f in findings if "cruft marker" in f["detail"]]
    assert len(cruft) == 1
    assert "TODO" in cruft[0]["detail"]


def test_placeholder_scan_skipped_over_max_files():
    py_files = {f"f{i}.py": {"source": "x = 1\n", "diff_u0": ""} for i in range(5)}
    findings = cpm._check_placeholders(py_files, py_changed_count=5, max_files=3)
    assert len(findings) == 1
    assert "exceeds council_postmortem_max_files" in findings[0]["detail"]


def test_placeholder_deleted_file_skipped():
    # source=None signals the file was deleted in this range (client couldn't
    # `git show` it) — must not crash, must not flag it.
    py_files = {"gone.py": {"source": None, "diff_u0": ""}}
    findings = cpm._check_placeholders(py_files, py_changed_count=1)
    assert findings == []


# ---------------------------------------------------------------------------
# _check_test_claims
# ---------------------------------------------------------------------------

def test_test_claim_missing_path_detected():
    history = [_history_line(1, "abc1234", notes="Ran tests/test_roi.py successfully")]
    findings = cpm._check_test_claims(["a.py"], "abc1234", history, "")
    assert any("tests/test_roi.py" in f["detail"] for f in findings)


def test_test_claim_existing_path_not_flagged():
    history = [_history_line(1, "abc1234", notes="Ran tests/test_real.py successfully")]
    findings = cpm._check_test_claims(["tests/test_real.py"], "abc1234", history, "")
    assert findings == []


def test_test_claims_ignores_url_with_allowlisted_extension():
    history = [_history_line(
        1, "abc1234", notes="Ref https://example.com/path/file.py in the docs.",
    )]
    findings = cpm._check_test_claims([], "abc1234", history, "")
    assert findings == []


def test_test_claims_ignores_url_and_non_extension_tokens():
    history = [_history_line(
        1, "abc1234",
        notes="See https://github.com/soakal/Nexus for context, and/or.So it goes",
    )]
    findings = cpm._check_test_claims([], "abc1234", history, "")
    assert findings == []


def test_looks_like_url_detects_real_urls():
    text = "See https://github.com/soakal/Nexus for context"
    m = cpm._PATH_TOKEN_RE.search(text)
    assert m is not None
    assert cpm._looks_like_url(text, m.start()) is True


def test_looks_like_url_false_for_real_path():
    text = "Ran tests/test_foo.py successfully"
    m = cpm._PATH_TOKEN_RE.search(text)
    assert m is not None
    assert cpm._looks_like_url(text, m.start()) is False


def test_powershell_extension_with_digit_matches():
    text = "Ran scripts/deploy.ps1 successfully"
    m = cpm._PATH_TOKEN_RE.search(text)
    assert m is not None
    assert m.group(0) == "scripts/deploy.ps1"


# ---------------------------------------------------------------------------
# run_postmortem — full integration against payload dicts
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_no_findings_does_not_notify():
    notify_mock = AsyncMock(return_value=True)
    payload = _payload(files_changed=["a.py"])

    with patch("backend.config.get_settings", return_value=_settings()), \
         patch("backend.events.notify_phone", notify_mock):
        result = await cpm.run_postmortem(payload)

    assert result["ok"] is True
    assert result["findings"] == []
    notify_mock.assert_not_called()


@pytest.mark.asyncio
async def test_findings_notify_kind():
    notify_mock = AsyncMock(return_value=True)
    payload = _payload(
        files_changed=["a.py"],
        py_changed_count=1,
        py_files={"a.py": {"source": "def f():\n    pass\n", "diff_u0": ""}},
    )

    with patch("backend.config.get_settings", return_value=_settings()), \
         patch("backend.events.notify_phone", notify_mock):
        result = await cpm.run_postmortem(payload)

    assert result["findings"] != []
    notify_mock.assert_awaited_once()
    assert notify_mock.await_args.kwargs["kind"] == "council_postmortem"
    assert "buttons" not in notify_mock.await_args.kwargs


@pytest.mark.asyncio
async def test_no_payload_skips():
    with patch("backend.config.get_settings", return_value=_settings()):
        result = await cpm.run_postmortem(None)
    assert result["ok"] is False
    assert "no payload" in result["skipped"]


@pytest.mark.asyncio
async def test_range_error_notifies_instead_of_silent_failure():
    notify_mock = AsyncMock(return_value=True)
    payload = _payload(range_error="commit deadbee no longer resolves in target repo")

    with patch("backend.config.get_settings", return_value=_settings()), \
         patch("backend.events.notify_phone", notify_mock):
        result = await cpm.run_postmortem(payload)

    assert result["ok"] is False
    assert result["findings"] and result["findings"][0]["check"] == "range_resolution"
    notify_mock.assert_awaited_once()
    assert notify_mock.await_args.kwargs["kind"] == "council_postmortem"


@pytest.mark.asyncio
async def test_no_commits_in_session_is_clean_skip():
    payload = _payload(range=None)
    del payload["last"]
    with patch("backend.config.get_settings", return_value=_settings()):
        result = await cpm.run_postmortem(payload)
    assert result["ok"] is True
    assert result["skipped"] == "no commits in session — nothing to check"


@pytest.mark.asyncio
async def test_never_raises_on_malformed_payload():
    notify_mock = AsyncMock(return_value=True)
    # Missing "last" with a non-None range trips a KeyError inside the try —
    # must degrade to ok=False + a best-effort notify, never propagate.
    payload = _payload()
    del payload["last"]

    with patch("backend.config.get_settings", return_value=_settings()), \
         patch("backend.events.notify_phone", notify_mock):
        result = await cpm.run_postmortem(payload)
    assert result["ok"] is False
    notify_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_disabled_flag_short_circuits():
    with patch("backend.config.get_settings", return_value=_settings(council_postmortem_enabled=False)):
        result = await cpm.run_postmortem(_payload())
    assert result["ok"] is False


@pytest.mark.asyncio
async def test_llm_call_is_labeled():
    run_model_mock = AsyncMock(return_value='{"explicit": false, "paths": []}')
    with patch("backend.agents.router.run_model", run_model_mock):
        await cpm._extract_allowed_paths("touch ONLY a.py", "claude-haiku-4-5-20251001")

    run_model_mock.assert_awaited_once()
    assert run_model_mock.await_args.kwargs.get("label") == "council_postmortem"


def test_no_subprocess_or_git_in_module():
    """Regression guard: this module reads only the supplied payload dict —
    no subprocess/git calls of any kind (that's now Council-loop/scripts/
    postmortem_payload.py's job, on whichever machine runs Council-loop)."""
    import inspect
    source = inspect.getsource(cpm)
    assert "subprocess" not in source
    assert "create_subprocess" not in source
    assert "shell=True" not in source
