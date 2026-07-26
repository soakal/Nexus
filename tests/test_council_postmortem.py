"""Tests for backend/agents/council_postmortem.py (Feature 2 — Council-Loop
Post-Mortem). All tests build a REAL temp git repo (git init, real commits)
and patch council_postmortem._effective_config directly — no real
Council-loop repo, no real ProcessForge, no network."""
import json
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from backend.agents import council_postmortem as cpm


def _git(repo: str, *args: str) -> str:
    result = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _init_repo(path: Path) -> str:
    repo = str(path)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    return repo


def _commit(repo: str, files: dict[str, str], message: str) -> str:
    for name, content in files.items():
        p = Path(repo) / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        _git(repo, "add", name)
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _fake_config(target: str, commit_prefix: str = "council:") -> dict:
    return {"target_repo": target, "commit_prefix": commit_prefix, "test_commands": []}


def _history_line(cycle: int, commit: str | None, notes: str = "") -> dict:
    return {"cycle": cycle, "ts": "2026-07-26T00:00:00Z", "step": "step", "verdict": "accept",
            "commit": commit, "notes": notes}


# ---------------------------------------------------------------------------
# _derive_range
# ---------------------------------------------------------------------------

def test_derive_range_happy(tmp_path):
    repo = _init_repo(tmp_path)
    c1 = _commit(repo, {"a.py": "x = 1\n"}, "council: cycle 1: a")
    c2 = _commit(repo, {"b.py": "y = 2\n"}, "council: cycle 2: b")
    history = [_history_line(1, c1[:7]), _history_line(2, None), _history_line(3, c2[:7])]
    result = cpm._derive_range(history, repo)
    assert result == (c1, c2)


def test_derive_range_no_commits(tmp_path):
    repo = _init_repo(tmp_path)
    history = [_history_line(1, None), _history_line(2, None)]
    assert cpm._derive_range(history, repo) is None


def test_derive_range_first_commit_in_repo(tmp_path):
    repo = _init_repo(tmp_path)
    c1 = _commit(repo, {"a.py": "x = 1\n"}, "council: cycle 1: a")
    history = [_history_line(1, c1[:7])]
    first, last = cpm._derive_range(history, repo)
    assert first == last == c1
    rng = cpm._range_expr(repo, first, last)
    assert rng.startswith(cpm._EMPTY_TREE_HASH)


# ---------------------------------------------------------------------------
# _count_council_commits (foreign commits excluded, not scanned)
# ---------------------------------------------------------------------------

def test_foreign_commits_excluded(tmp_path):
    repo = _init_repo(tmp_path)
    c0 = _commit(repo, {"README.md": "x\n"}, "initial")
    _commit(repo, {"a.py": "x = 1\n"}, "council: cycle 1: a")
    _commit(repo, {"human.py": "z = 3\n"}, "a human's manual commit")
    c3 = _commit(repo, {"b.py": "y = 2\n"}, "council: cycle 2: b")
    rng = f"{c0}..{c3}"
    council, foreign = cpm._count_council_commits(repo, rng, "council:")
    assert council == 2
    assert foreign == 1


# ---------------------------------------------------------------------------
# _check_scope_drift
# ---------------------------------------------------------------------------

def test_scope_drift_detected(tmp_path):
    repo = _init_repo(tmp_path)
    c0 = _commit(repo, {"README.md": "x\n"}, "initial")
    c1 = _commit(repo, {"allowed.py": "x = 1\n", "sneaky.py": "y = 2\n"}, "council: cycle 1")
    findings = cpm._check_scope_drift(repo, f"{c0}..{c1}", ["allowed.py"])
    assert any("sneaky.py" in f["detail"] for f in findings)
    assert not any("allowed.py" in f["detail"] for f in findings)


def test_scope_drift_skipped_when_goal_names_no_files():
    # explicit=False path is enforced in run_postmortem, not _check_scope_drift
    # itself — verified via the async extraction helper returning ([], False)
    # for a goal with no file tokens.
    import asyncio
    paths, explicit = asyncio.run(cpm._extract_allowed_paths("", "claude-haiku-4-5-20251001"))
    assert paths == [] and explicit is False


# ---------------------------------------------------------------------------
# _check_placeholders
# ---------------------------------------------------------------------------

def test_placeholder_pass_body_detected(tmp_path):
    repo = _init_repo(tmp_path)
    c0 = _commit(repo, {"a.py": "def f():\n    return 1\n"}, "initial")
    c1 = _commit(repo, {"a.py": "def f():\n    return 1\n\ndef g():\n    pass\n"}, "council: cycle 1")
    findings = cpm._check_placeholders(repo, f"{c0}..{c1}")
    assert any("g()" in f["detail"] and "pass" in f["detail"] for f in findings)


def test_placeholder_docstring_only_body_detected(tmp_path):
    repo = _init_repo(tmp_path)
    c0 = _commit(repo, {"a.py": "x = 1\n"}, "initial")
    c1 = _commit(repo, {"a.py": 'x = 1\n\ndef g():\n    """does nothing yet"""\n'}, "council: cycle 1")
    findings = cpm._check_placeholders(repo, f"{c0}..{c1}")
    assert any("g()" in f["detail"] for f in findings)


def test_placeholder_ignores_unchanged_files(tmp_path):
    repo = _init_repo(tmp_path)
    c0 = _commit(repo, {"stale.py": "def old():\n    pass\n", "fresh.py": "x = 1\n"}, "initial")
    c1 = _commit(repo, {"fresh.py": "x = 2\n"}, "council: cycle 1")
    findings = cpm._check_placeholders(repo, f"{c0}..{c1}")
    assert not any("stale.py" in f["detail"] for f in findings)


def test_todo_in_added_lines_only(tmp_path):
    repo = _init_repo(tmp_path)
    c0 = _commit(repo, {"a.py": "x = 1  # context line, not TODO here\n"}, "initial")
    c1 = _commit(repo, {"a.py": "x = 1  # context line, not TODO here\ny = 2  # TODO: fix this\n"}, "council: cycle 1")
    findings = cpm._check_placeholders(repo, f"{c0}..{c1}")
    assert any("TODO" in f["detail"] for f in findings)
    # The context line's absence of "TODO" (it never had one) confirms only
    # the added line triggered — sanity check there's exactly one cruft finding.
    cruft = [f for f in findings if "cruft marker" in f["detail"]]
    assert len(cruft) == 1


# ---------------------------------------------------------------------------
# _check_test_claims
# ---------------------------------------------------------------------------

def test_test_claim_missing_path_detected(tmp_path):
    repo = _init_repo(tmp_path)
    c1 = _commit(repo, {"a.py": "x = 1\n"}, "council: cycle 1")
    history = [_history_line(1, c1[:7], notes="Ran tests/test_roi.py successfully")]
    findings = cpm._check_test_claims(repo, c1, history, "")
    assert any("tests/test_roi.py" in f["detail"] for f in findings)


def test_test_claim_existing_path_not_flagged(tmp_path):
    repo = _init_repo(tmp_path)
    c1 = _commit(repo, {"tests/test_real.py": "def test_x(): pass\n"}, "council: cycle 1")
    history = [_history_line(1, c1[:7], notes="Ran tests/test_real.py successfully")]
    findings = cpm._check_test_claims(repo, c1, history, "")
    assert findings == []


# ---------------------------------------------------------------------------
# run_postmortem — full integration against a real temp repo + fake config
# ---------------------------------------------------------------------------

def _settings(**overrides):
    from backend.config import Settings
    defaults = dict(council_postmortem_enabled=True, council_loop_path=str(Path.cwd()))
    defaults.update(overrides)
    return Settings(**defaults)


@pytest.mark.asyncio
async def test_no_findings_does_not_notify(tmp_path):
    repo = _init_repo(tmp_path)
    c1 = _commit(repo, {"a.py": "x = 1\n"}, "council: cycle 1")
    notify_mock = AsyncMock(return_value=True)

    with patch("backend.agents.council_postmortem._effective_config", return_value=_fake_config(repo)), \
         patch("backend.agents.council_postmortem._read_history",
               return_value=[_history_line(1, c1[:7])]), \
         patch("backend.agents.council_postmortem._read_goal", return_value=""), \
         patch("backend.agents.council_postmortem._read_transcripts", return_value=""), \
         patch("backend.config.get_settings", return_value=_settings()), \
         patch("backend.events.notify_phone", notify_mock):
        result = await cpm.run_postmortem()

    assert result["ok"] is True
    assert result["findings"] == []
    notify_mock.assert_not_called()


@pytest.mark.asyncio
async def test_findings_notify_kind(tmp_path):
    repo = _init_repo(tmp_path)
    c0 = _commit(repo, {"README.md": "x\n"}, "initial")
    c1 = _commit(repo, {"a.py": "def f():\n    pass\n"}, "council: cycle 1")
    notify_mock = AsyncMock(return_value=True)

    with patch("backend.agents.council_postmortem._effective_config", return_value=_fake_config(repo)), \
         patch("backend.agents.council_postmortem._read_history",
               return_value=[_history_line(1, c0[:7]), _history_line(2, c1[:7])]), \
         patch("backend.agents.council_postmortem._read_goal", return_value=""), \
         patch("backend.agents.council_postmortem._read_transcripts", return_value=""), \
         patch("backend.config.get_settings", return_value=_settings()), \
         patch("backend.events.notify_phone", notify_mock):
        result = await cpm.run_postmortem()

    assert result["findings"] != []
    notify_mock.assert_awaited_once()
    assert notify_mock.await_args.kwargs["kind"] == "council_postmortem"
    assert "buttons" not in notify_mock.await_args.kwargs


@pytest.mark.asyncio
async def test_never_raises_missing_council_path():
    with patch("backend.config.get_settings", return_value=_settings(council_loop_path="Z:/does/not/exist")):
        result = await cpm.run_postmortem()
    assert result["ok"] is False


@pytest.mark.asyncio
async def test_never_raises_bad_effective_config(tmp_path):
    notify_mock = AsyncMock(return_value=True)
    with patch("backend.agents.council_postmortem._effective_config", side_effect=RuntimeError("boom")), \
         patch("backend.config.get_settings", return_value=_settings()), \
         patch("backend.events.notify_phone", notify_mock):
        result = await cpm.run_postmortem()
    assert result["ok"] is False
    # The outer catch-all also notifies now (not just the _derive_range case)
    # — an unexpected failure this early (target repo not even known yet)
    # still must not be silent.
    notify_mock.assert_awaited_once()
    assert notify_mock.await_args.kwargs["kind"] == "council_postmortem"


@pytest.mark.asyncio
async def test_never_raises_malformed_history(tmp_path):
    repo = _init_repo(tmp_path)
    _commit(repo, {"a.py": "x = 1\n"}, "council: cycle 1")
    notify_mock = AsyncMock(return_value=True)

    with patch("backend.agents.council_postmortem._effective_config", return_value=_fake_config(repo)), \
         patch("backend.agents.council_postmortem._read_history", side_effect=RuntimeError("bad jsonl")), \
         patch("backend.config.get_settings", return_value=_settings()), \
         patch("backend.events.notify_phone", notify_mock):
        result = await cpm.run_postmortem()
    assert result["ok"] is False
    notify_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_disabled_flag_short_circuits():
    with patch("backend.config.get_settings", return_value=_settings(council_postmortem_enabled=False)), \
         patch("backend.agents.council_postmortem._effective_config") as mock_cfg:
        result = await cpm.run_postmortem()
    assert result["ok"] is False
    mock_cfg.assert_not_called()


@pytest.mark.asyncio
async def test_llm_call_is_labeled(tmp_path):
    repo = _init_repo(tmp_path)
    _commit(repo, {"a.py": "x = 1\n"}, "council: cycle 1")

    run_model_mock = AsyncMock(return_value='{"explicit": false, "paths": []}')
    with patch("backend.agents.router.run_model", run_model_mock):
        await cpm._extract_allowed_paths("touch ONLY a.py", "claude-haiku-4-5-20251001")

    run_model_mock.assert_awaited_once()
    assert run_model_mock.await_args.kwargs.get("label") == "council_postmortem"


def test_no_create_subprocess_in_module():
    """Regression guard: this module must never use asyncio.create_subprocess_*
    or shell=True — every subprocess call goes through the sync subprocess.run
    (invoked via asyncio.to_thread by callers), matching CLAUDE.md's forced-
    SelectorEventLoop invariant (NEXUS spawns no in-loop subprocesses)."""
    import inspect
    source = inspect.getsource(cpm)
    assert "create_subprocess" not in source
    assert "shell=True" not in source


# ---------------------------------------------------------------------------
# Post-verify fixes: F1 (unresolvable SHA notifies instead of dying silently),
# F2 (test-claim regex false positives), F3 (scope-drift path normalization),
# F4 (max_files cap)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_unresolvable_sha_notifies_instead_of_silent_failure(tmp_path):
    repo = _init_repo(tmp_path)
    _commit(repo, {"a.py": "x = 1\n"}, "council: cycle 1")
    # A SHA that never existed in this repo — simulates target_repo diverging
    # (rebase/amend/force-push) from what history.jsonl recorded.
    history = [_history_line(1, "deadbee")]
    notify_mock = AsyncMock(return_value=True)

    with patch("backend.agents.council_postmortem._effective_config", return_value=_fake_config(repo)), \
         patch("backend.agents.council_postmortem._read_history", return_value=history), \
         patch("backend.agents.council_postmortem._read_goal", return_value=""), \
         patch("backend.agents.council_postmortem._read_transcripts", return_value=""), \
         patch("backend.config.get_settings", return_value=_settings()), \
         patch("backend.events.notify_phone", notify_mock):
        result = await cpm.run_postmortem()

    assert result["ok"] is False
    assert result["findings"] and result["findings"][0]["check"] == "range_resolution"
    notify_mock.assert_awaited_once()
    assert notify_mock.await_args.kwargs["kind"] == "council_postmortem"


@pytest.mark.asyncio
async def test_unresolvable_full_40char_sha_notifies():
    # Regression lock for the specific bug verification found: a bare
    # `git rev-parse <nonexistent-40-char-hex>` exits 0 and echoes the input
    # back verbatim instead of failing — only `--verify ...^{commit}` (what
    # _derive_range now uses) actually confirms the object exists.
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        repo = _init_repo(Path(tmp))
        _commit(repo, {"a.py": "x = 1\n"}, "council: cycle 1")
        fake_full_sha = "deadbeef" + "a" * 32
        history = [_history_line(1, fake_full_sha)]
        notify_mock = AsyncMock(return_value=True)

        with patch("backend.agents.council_postmortem._effective_config", return_value=_fake_config(repo)), \
             patch("backend.agents.council_postmortem._read_history", return_value=history), \
             patch("backend.agents.council_postmortem._read_goal", return_value=""), \
             patch("backend.agents.council_postmortem._read_transcripts", return_value=""), \
             patch("backend.config.get_settings", return_value=_settings()), \
             patch("backend.events.notify_phone", notify_mock):
            result = await cpm.run_postmortem()

        assert result["ok"] is False
        notify_mock.assert_awaited_once()


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


def test_test_claims_ignores_url_with_allowlisted_extension(tmp_path):
    repo = _init_repo(tmp_path)
    c1 = _commit(repo, {"a.py": "x = 1\n"}, "council: cycle 1")
    history = [_history_line(
        1, c1[:7],
        notes="Ref https://example.com/path/file.py in the docs.",
    )]
    findings = cpm._check_test_claims(repo, c1, history, "")
    assert findings == []


def test_powershell_extension_with_digit_matches():
    text = "Ran scripts/deploy.ps1 successfully"
    m = cpm._PATH_TOKEN_RE.search(text)
    assert m is not None
    assert m.group(0) == "scripts/deploy.ps1"


def test_scope_drift_normalizes_backslash_and_dot_slash(tmp_path):
    repo = _init_repo(tmp_path)
    c0 = _commit(repo, {"README.md": "x\n"}, "initial")
    c1 = _commit(repo, {"src/foo.py": "x = 1\n"}, "council: cycle 1")
    # Goal text named the file with a backslash and a leading './' — both
    # must still match the real (forward-slash, no prefix) touched file.
    findings_backslash = cpm._check_scope_drift(repo, f"{c0}..{c1}", ["src\\foo.py"])
    findings_dotslash = cpm._check_scope_drift(repo, f"{c0}..{c1}", ["./src/foo.py"])
    assert findings_backslash == []
    assert findings_dotslash == []


def test_test_claims_ignores_url_and_non_extension_tokens(tmp_path):
    repo = _init_repo(tmp_path)
    c1 = _commit(repo, {"a.py": "x = 1\n"}, "council: cycle 1")
    history = [_history_line(
        1, c1[:7],
        notes="See https://github.com/soakal/Nexus for context, and/or.So it goes",
    )]
    findings = cpm._check_test_claims(repo, c1, history, "")
    assert findings == []


@pytest.mark.asyncio
async def test_placeholder_scan_skipped_over_max_files(tmp_path):
    repo = _init_repo(tmp_path)
    c0 = _commit(repo, {"README.md": "x\n"}, "initial")
    files = {f"f{i}.py": "x = 1\n" for i in range(5)}
    c1 = _commit(repo, files, "council: cycle 1")
    findings = cpm._check_placeholders(repo, f"{c0}..{c1}", max_files=3)
    assert len(findings) == 1
    assert "exceeds council_postmortem_max_files" in findings[0]["detail"]
