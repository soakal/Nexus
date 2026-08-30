"""Tests for tools/relay_vault_signals.py.

Loaded via importlib (tools/ has no __init__.py, matching
tests/test_cleanup_calibration_contamination.py's own pattern for the
sibling script). Every record_flag call is monkeypatched out -- these tests
never touch a real DB.
"""

import asyncio
import importlib.util
import json
import pathlib

import pytest

_SCRIPT_PATH = (
    pathlib.Path(__file__).resolve().parents[1]
    / "tools"
    / "relay_vault_signals.py"
)

_spec = importlib.util.spec_from_file_location("relay_vault_signals", _SCRIPT_PATH)
relay = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(relay)


def _write_digest(tmp_path, name: str, content: str) -> pathlib.Path:
    p = tmp_path / name
    p.write_text(content, encoding="utf-8")
    return p


def _patch_dirs(monkeypatch, tmp_path):
    monkeypatch.setattr(relay, "DIGEST_DIR", tmp_path)
    monkeypatch.setattr(relay, "STATE_FILE", tmp_path / ".relay_state.json")


def _patch_outcomes(monkeypatch, record_flag):
    import backend.agents.outcomes as real_outcomes

    monkeypatch.setattr(real_outcomes, "record_flag", record_flag)


def test_one_record_flag_call_per_finding(monkeypatch, tmp_path):
    _patch_dirs(monkeypatch, tmp_path)
    calls = []

    async def fake_record_flag(source, check, summary, *, detail=None, severity="medium", action_log_id=None):
        calls.append({"source": source, "check": check, "summary": summary, "severity": severity})
        return 1

    _patch_outcomes(monkeypatch, fake_record_flag)

    _write_digest(
        tmp_path,
        "2026-01-01.md",
        "## First finding\nBody text one.\n\n## Second finding\nBody text two.\n\n## Third finding\nBody text three.\n",
    )

    rc = asyncio.run(relay.main())
    assert rc == 0
    assert len(calls) == 3
    for c in calls:
        assert c["source"] == "vault_signals"
        assert c["severity"] == "medium"


def test_slug_is_stable_across_digests(monkeypatch, tmp_path):
    _patch_dirs(monkeypatch, tmp_path)
    calls = []

    async def fake_record_flag(source, check, summary, *, detail=None, severity="medium", action_log_id=None):
        calls.append(check)
        return 1

    _patch_outcomes(monkeypatch, fake_record_flag)

    same_bullet = "- The garage sensor note has an unresolved TODO\n"
    _write_digest(tmp_path, "2026-01-01.md", same_bullet)
    _write_digest(tmp_path, "2026-01-02.md", same_bullet)

    asyncio.run(relay.main())

    assert len(calls) == 2
    assert calls[0] == calls[1]


def test_bullets_under_a_section_are_separate_findings(monkeypatch, tmp_path):
    """A `## ` section with multiple bullets must yield one finding PER
    bullet, not one finding for the whole section -- and the same bullet
    text appearing unchanged in two separately-parsed dated digests must
    slug identically both times."""
    _patch_dirs(monkeypatch, tmp_path)
    calls = []

    async def fake_record_flag(source, check, summary, *, detail=None, severity="medium", action_log_id=None):
        calls.append({"check": check, "summary": summary})
        return 1

    _patch_outcomes(monkeypatch, fake_record_flag)

    digest = (
        "## Work\n"
        "- Stale open item in General-Motors.md: the remote-access request...\n"
        "- Something else entirely\n"
        "## Business\n"
        "- Unresolved follow-up in Business.md: confirm with Jon whether Shantry Bills...\n"
    )
    _write_digest(tmp_path, "2026-01-01.md", digest)
    _write_digest(tmp_path, "2026-01-02.md", digest)

    asyncio.run(relay.main())

    assert len(calls) == 6  # 3 findings per digest x 2 digests
    day1_checks = [c["check"] for c in calls[:3]]
    day2_checks = [c["check"] for c in calls[3:]]
    assert day1_checks == day2_checks
    assert any("Stale open item" in c["summary"] for c in calls[:3])
    assert any("Something else entirely" in c["summary"] for c in calls[:3])
    assert any("Unresolved follow-up" in c["summary"] for c in calls[:3])


def test_numbered_list_items_are_separate_findings(monkeypatch, tmp_path):
    """A `## ` section with a numbered list (`1. `/`2. `) must yield one
    finding PER item, same as `-`/`*` bullets -- and adding a third numbered
    item on a later digest must not change the first two items' slugs."""
    _patch_dirs(monkeypatch, tmp_path)
    calls = []

    async def fake_record_flag(source, check, summary, *, detail=None, severity="medium", action_log_id=None):
        calls.append({"check": check, "summary": summary})
        return 1

    _patch_outcomes(monkeypatch, fake_record_flag)

    digest_day1 = (
        "## Work\n"
        "1. General-Motors.md — remote-access request still unresolved (stale).\n"
        "2. MOC-VRSI.md — new pricing section (new).\n"
    )
    digest_day2 = digest_day1 + "3. Another-Doc.md — a third item added later.\n"

    _write_digest(tmp_path, "2026-01-01.md", digest_day1)
    _write_digest(tmp_path, "2026-01-02.md", digest_day2)

    asyncio.run(relay.main())

    day1_checks = [c["check"] for c in calls[:2]]
    day2_checks = [c["check"] for c in calls[2:5]]
    assert len(calls) == 5  # 2 findings day1 + 3 findings day2
    assert any("General-Motors.md" in c["summary"] for c in calls[:2])
    assert any("MOC-VRSI.md" in c["summary"] for c in calls[:2])
    # the first two items' slugs are unchanged by the third item appearing.
    assert day1_checks == day2_checks[:2]


def test_summary_truncated_to_300_chars(monkeypatch, tmp_path):
    _patch_dirs(monkeypatch, tmp_path)
    calls = []

    async def fake_record_flag(source, check, summary, *, detail=None, severity="medium", action_log_id=None):
        calls.append(summary)
        return 1

    _patch_outcomes(monkeypatch, fake_record_flag)

    long_text = "x" * 500
    _write_digest(tmp_path, "2026-01-01.md", f"- {long_text}\n")

    asyncio.run(relay.main())

    assert len(calls) == 1
    assert len(calls[0]) <= 300


def test_per_file_finding_cap_holds(monkeypatch, tmp_path):
    _patch_dirs(monkeypatch, tmp_path)
    calls = []

    async def fake_record_flag(source, check, summary, *, detail=None, severity="medium", action_log_id=None):
        calls.append(check)
        return 1

    _patch_outcomes(monkeypatch, fake_record_flag)

    bullets = "\n".join(f"- distinct finding number {i}" for i in range(30))
    _write_digest(tmp_path, "2026-01-01.md", bullets + "\n")

    asyncio.run(relay.main())

    assert relay.MAX_FINDINGS_PER_FILE == 20
    assert len(calls) == relay.MAX_FINDINGS_PER_FILE


def test_already_relayed_file_is_skipped(monkeypatch, tmp_path):
    _patch_dirs(monkeypatch, tmp_path)
    calls = []

    async def fake_record_flag(source, check, summary, *, detail=None, severity="medium", action_log_id=None):
        calls.append(check)
        return 1

    _patch_outcomes(monkeypatch, fake_record_flag)

    _write_digest(tmp_path, "2026-01-01.md", "- something that should be skipped\n")
    (tmp_path / ".relay_state.json").write_text(json.dumps(["2026-01-01.md"]), encoding="utf-8")

    rc = asyncio.run(relay.main())
    assert rc == 0
    assert calls == []


def test_record_flag_raising_does_not_propagate(monkeypatch, tmp_path):
    _patch_dirs(monkeypatch, tmp_path)

    async def raising_record_flag(source, check, summary, *, detail=None, severity="medium", action_log_id=None):
        raise RuntimeError("boom")

    _patch_outcomes(monkeypatch, raising_record_flag)

    _write_digest(tmp_path, "2026-01-01.md", "- a finding whose relay call will explode\n")

    rc = asyncio.run(relay.main())  # must not raise
    assert rc == 0
    # File is still marked relayed -- a poisoned record_flag call is caught
    # per-finding, not treated as a whole-file failure.
    state = json.loads((tmp_path / ".relay_state.json").read_text(encoding="utf-8"))
    assert "2026-01-01.md" in state


def test_record_flag_returning_none_does_not_propagate(monkeypatch, tmp_path):
    _patch_dirs(monkeypatch, tmp_path)

    async def none_record_flag(source, check, summary, *, detail=None, severity="medium", action_log_id=None):
        return None

    _patch_outcomes(monkeypatch, none_record_flag)

    _write_digest(tmp_path, "2026-01-01.md", "- a suppressed finding\n")

    rc = asyncio.run(relay.main())
    assert rc == 0


def test_corrupted_relay_state_does_not_crash_main(monkeypatch, tmp_path):
    """Security auto-fix regression: a malformed .relay_state.json must
    degrade _load_relayed() to an empty set, not raise out of main()."""
    _patch_dirs(monkeypatch, tmp_path)
    calls = []

    async def fake_record_flag(source, check, summary, *, detail=None, severity="medium", action_log_id=None):
        calls.append(check)
        return 1

    _patch_outcomes(monkeypatch, fake_record_flag)

    _write_digest(tmp_path, "2026-01-01.md", "- a finding behind a corrupt state file\n")
    (tmp_path / ".relay_state.json").write_text("{not valid json", encoding="utf-8")

    rc = asyncio.run(relay.main())  # must not raise

    assert rc == 0
    # corrupt state read as empty -> the file is treated as unrelayed and processed
    assert len(calls) == 1


def test_slugify_hash_suffix_prevents_prefix_collision(monkeypatch, tmp_path):
    """Two distinct findings that share the same first 48 chars (post-
    slugify) must NOT resolve to the same slug -- pre-fix, `_slugify`
    truncated to slug[:48] with no disambiguator, so these two would have
    collided and the second finding would have been silently treated as a
    dup of the first (same OutcomeFlag row) instead of relayed as its own
    flag."""
    _patch_dirs(monkeypatch, tmp_path)
    calls = []

    async def fake_record_flag(source, check, summary, *, detail=None, severity="medium", action_log_id=None):
        calls.append(check)
        return 1

    _patch_outcomes(monkeypatch, fake_record_flag)

    shared_prefix = "x" * 80
    digest = f"- {shared_prefix} AAAA\n- {shared_prefix} BBBB\n"
    _write_digest(tmp_path, "2026-01-01.md", digest)

    rc = asyncio.run(relay.main())

    assert rc == 0
    assert len(calls) == 2
    # pre-fix bug: both slugs' first 48 chars (the whole slug, sans hash)
    # were identical -- confirm that shared prefix really is identical...
    assert calls[0][:48] == calls[1][:48]
    # ...but the full slugs (with the sha1 suffix) must differ.
    assert calls[0] != calls[1]


def test_main_returns_1_when_a_file_fails_to_relay(monkeypatch, tmp_path):
    """A whole-file failure (e.g. undecodable content) must make main()
    return exit code 1, while still relaying every OTHER file in the same
    run -- per-finding record_flag failures (covered above) stay rc==0;
    only a failure that prevents relaying a file at all should surface as
    a nonzero exit."""
    _patch_dirs(monkeypatch, tmp_path)
    calls = []

    async def fake_record_flag(source, check, summary, *, detail=None, severity="medium", action_log_id=None):
        calls.append(check)
        return 1

    _patch_outcomes(monkeypatch, fake_record_flag)

    # Invalid UTF-8 bytes make path.read_text(encoding="utf-8") raise inside
    # _relay_file -- a real, not simulated, whole-file failure.
    (tmp_path / "2026-01-01.md").write_bytes(b"\xff\xfe not valid utf-8")
    _write_digest(tmp_path, "2026-01-02.md", "- a finding in a good file\n")

    rc = asyncio.run(relay.main())

    assert rc == 1
    # the good file still got relayed despite the bad one failing
    assert len(calls) == 1
    state = json.loads((tmp_path / ".relay_state.json").read_text(encoding="utf-8"))
    assert "2026-01-02.md" in state
    assert "2026-01-01.md" not in state


def test_no_dir_is_a_clean_noop(monkeypatch, tmp_path):
    missing = tmp_path / "does-not-exist"
    monkeypatch.setattr(relay, "DIGEST_DIR", missing)
    monkeypatch.setattr(relay, "STATE_FILE", missing / ".relay_state.json")
    rc = asyncio.run(relay.main())
    assert rc == 0
