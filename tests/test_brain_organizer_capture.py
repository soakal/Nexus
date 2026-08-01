"""Regression tests for backend/scheduler.py::_run_brain_organizer's
subprocess capture (see CLAUDE.md's backend/scheduler.py bullet).

Root cause (see the spec this test file was built from): `subprocess.run(...,
text=True)` with no `encoding=` decodes the child's pipes under the parent's
locale default (cp1252 on Brian's host). brain_organizer.py reconfigures its
own stdout to UTF-8 and emits emoji -- an undecodable byte under cp1252 kills
CPython's `_readerthread` mid-read, and `subprocess.run` returns normally with
`stdout=None` instead of raising (dead thread, no exception surfaces to the
outer `except`). Separately, brain_organizer.py logs EVERYTHING via a
`StreamHandler(sys.stdout)` -- it never writes to stderr -- so the pre-fix
`logger.error(...: {result.stderr[:500]})` line was reading an always-empty
stream and threw away every real diagnostic.

None of these tests touch the network, the real vault, real Brain/raw/, or
the real modules/brain-organizer/venv on disk -- the existence check inside
_run_brain_organizer is bypassed via the `_fake_organizer_paths` fixture so
this suite runs the same way regardless of whether that venv happens to
exist on the machine running pytest.
"""
import asyncio
import logging
import subprocess
import sys

import pytest


@pytest.fixture
def _fake_organizer_paths(monkeypatch):
    """Make _run_brain_organizer believe modules/brain-organizer's venv +
    script exist, without depending on (or touching) the real venv on disk."""
    import pathlib

    real_exists = pathlib.Path.exists

    def fake_exists(self):
        if "brain-organizer" in str(self):
            return True
        return real_exists(self)

    monkeypatch.setattr(pathlib.Path, "exists", fake_exists)


def _completed(returncode, stdout, stderr):
    return subprocess.CompletedProcess(args=["python", "brain_organizer.py"], returncode=returncode, stdout=stdout, stderr=stderr)


# ---------------------------------------------------------------------------
# 1. Encoding regression (headline test) -- the actual subprocess.run call
#    must pass encoding="utf-8", errors="replace".
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_subprocess_run_uses_utf8_replace_encoding(monkeypatch, _fake_organizer_paths):
    from backend import scheduler

    captured = {}

    def fake_run(*args, **kwargs):
        captured["kwargs"] = kwargs
        return _completed(0, "ok", "")

    monkeypatch.setattr(subprocess, "run", fake_run)

    await scheduler._run_brain_organizer()

    assert captured, "subprocess.run was never called"
    assert captured["kwargs"].get("encoding") == "utf-8"
    assert captured["kwargs"].get("errors") == "replace"


# ---------------------------------------------------------------------------
# 2. End-to-end no-crash proof -- a REAL child process writing UTF-8 (incl. an
#    emoji) straight to sys.stdout.buffer, through the exact kwargs the
#    production call uses, must not raise UnicodeDecodeError on any thread.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_real_subprocess_utf8_emoji_no_crash():
    child_code = (
        "import sys\n"
        "sys.stdout.buffer.write('hello \\U0001F600 world'.encode('utf-8'))\n"
        "sys.stdout.buffer.flush()\n"
        "sys.exit(1)\n"
    )
    result = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-c", child_code],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    assert result.returncode == 1
    assert result.stdout is not None
    assert "hello" in result.stdout
    assert "world" in result.stdout


# ---------------------------------------------------------------------------
# 3. Stdout reaches the log -- a nonzero rc whose diagnostic text lives on
#    stdout only (stderr empty, matching brain_organizer.py's real logging
#    setup) must show up in the logged error.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stdout_diagnostic_reaches_error_log(monkeypatch, _fake_organizer_paths, caplog):
    from backend import scheduler

    diagnostic = "403 Forbidden — API key invalid or out of credits"

    def fake_run(*args, **kwargs):
        return _completed(1, f"some preceding log lines\n{diagnostic}\n", "")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with caplog.at_level(logging.ERROR):
        await scheduler._run_brain_organizer()

    assert any(diagnostic in r.message for r in caplog.records), caplog.text


# ---------------------------------------------------------------------------
# 4. None streams don't blow up -- and the diagnostic branch must actually
#    run (not get swallowed into the outer generic "job error" catch, which
#    would mean the inner code crashed on `None[:500]`).
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_none_streams_log_coherently_without_raising(monkeypatch, _fake_organizer_paths, caplog):
    from backend import scheduler

    def fake_run(*args, **kwargs):
        return _completed(1, None, None)

    monkeypatch.setattr(subprocess, "run", fake_run)

    with caplog.at_level(logging.ERROR):
        await scheduler._run_brain_organizer()  # must not raise

    error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(error_records) == 1, caplog.text
    assert "Brain Organizer failed" in error_records[0].message
    assert "rc=1" in error_records[0].message


# ---------------------------------------------------------------------------
# 5. Tail not head -- a marker at the very end of an oversized stdout must be
#    logged; a different marker at the very start must NOT be (it fell off
#    the truncated head in a naive [:N] implementation, or -- pre-fix --
#    wasn't logged at all since only stderr was read).
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_tail_not_head_is_logged(monkeypatch, _fake_organizer_paths, caplog):
    from backend import scheduler

    start_marker = "STARTMARKER_NEVER_LOGGED"
    end_marker = "ENDMARKER_MUST_BE_LOGGED"
    stdout = start_marker + ("x" * 3000) + end_marker

    def fake_run(*args, **kwargs):
        return _completed(1, stdout, "")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with caplog.at_level(logging.ERROR):
        await scheduler._run_brain_organizer()

    combined = "\n".join(r.message for r in caplog.records)
    assert end_marker in combined, caplog.text
    assert start_marker not in combined, caplog.text


# ---------------------------------------------------------------------------
# 6. Success path unchanged -- rc==0 logs exactly the existing INFO line,
#    nothing else, no ERROR record.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_success_path_logs_info_only(monkeypatch, _fake_organizer_paths, caplog):
    from backend import scheduler

    def fake_run(*args, **kwargs):
        return _completed(0, "irrelevant stdout noise", "")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with caplog.at_level(logging.INFO):
        await scheduler._run_brain_organizer()

    assert not any(r.levelno == logging.ERROR for r in caplog.records), caplog.text
    info_records = [r for r in caplog.records if r.levelno == logging.INFO]
    assert len(info_records) == 1
    assert info_records[0].message == "Brain Organizer run complete"
