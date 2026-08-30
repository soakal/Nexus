"""Tests for tools/relay_vault_signals.py.

Loaded via importlib (tools/ has no __init__.py, matching
tests/test_cleanup_calibration_contamination.py's own pattern for the
sibling script). Most tests here monkeypatch `_post_flag` itself out (so
`main()`/`_relay_file`'s own logic can be tested without a real socket) --
but `test_post_flag_*` below deliberately call the REAL `_post_flag` and
monkeypatch only `urllib.request.urlopen`, so the actual request shape
(URL/method/headers/body) and status-code contract are pinned too, not just
assumed by every caller's fake.
"""

import importlib.util
import json
import pathlib
import urllib.error
import urllib.request

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


def _patch_key(monkeypatch, key="test-key"):
    monkeypatch.setenv("NEXUS_API_KEY", key)


def _patch_post_flag(monkeypatch, fn):
    monkeypatch.setattr(relay, "_post_flag", fn)


def test_one_post_flag_call_per_finding(monkeypatch, tmp_path):
    _patch_dirs(monkeypatch, tmp_path)
    _patch_key(monkeypatch)
    # Isolate from any real NEXUS_BASE_URL set in the ambient shell env, so
    # the base_url-fallback assertion below actually exercises the unset
    # case rather than whatever happens to be in the caller's environment.
    monkeypatch.delenv("NEXUS_BASE_URL", raising=False)
    calls = []

    def fake_post_flag(base_url, key, check, summary):
        calls.append({"base_url": base_url, "key": key, "check": check, "summary": summary})
        return True

    _patch_post_flag(monkeypatch, fake_post_flag)

    _write_digest(
        tmp_path,
        "2026-01-01.md",
        "## First finding\nBody text one.\n\n## Second finding\nBody text two.\n\n## Third finding\nBody text three.\n",
    )

    rc = relay.main()
    assert rc == 0
    assert len(calls) == 3
    for c in calls:
        assert c["key"] == "test-key"
        # NEXUS_BASE_URL unset -> _relay_file must pass the script's own
        # default constant through to _post_flag, not some other value.
        assert c["base_url"] == relay._DEFAULT_BASE_URL


def test_relay_file_passes_env_base_url_to_post_flag(monkeypatch, tmp_path):
    """When NEXUS_BASE_URL is set, _relay_file must resolve it and pass that
    exact value through to _post_flag (not the default constant)."""
    _patch_dirs(monkeypatch, tmp_path)
    _patch_key(monkeypatch)
    monkeypatch.setenv("NEXUS_BASE_URL", "https://custom.example.test")
    calls = []

    def fake_post_flag(base_url, key, check, summary):
        calls.append(base_url)
        return True

    _patch_post_flag(monkeypatch, fake_post_flag)

    _write_digest(tmp_path, "2026-01-01.md", "- a finding\n")

    rc = relay.main()

    assert rc == 0
    assert calls == ["https://custom.example.test"]


def test_slug_is_stable_across_digests(monkeypatch, tmp_path):
    _patch_dirs(monkeypatch, tmp_path)
    _patch_key(monkeypatch)
    calls = []

    def fake_post_flag(base_url, key, check, summary):
        calls.append(check)
        return True

    _patch_post_flag(monkeypatch, fake_post_flag)

    same_bullet = "- The garage sensor note has an unresolved TODO\n"
    _write_digest(tmp_path, "2026-01-01.md", same_bullet)
    _write_digest(tmp_path, "2026-01-02.md", same_bullet)

    relay.main()

    assert len(calls) == 2
    assert calls[0] == calls[1]


def test_bullets_under_a_section_are_separate_findings(monkeypatch, tmp_path):
    """A `## ` section with multiple bullets must yield one finding PER
    bullet, not one finding for the whole section -- and the same bullet
    text appearing unchanged in two separately-parsed dated digests must
    slug identically both times."""
    _patch_dirs(monkeypatch, tmp_path)
    _patch_key(monkeypatch)
    calls = []

    def fake_post_flag(base_url, key, check, summary):
        calls.append({"check": check, "summary": summary})
        return True

    _patch_post_flag(monkeypatch, fake_post_flag)

    digest = (
        "## Work\n"
        "- Stale open item in General-Motors.md: the remote-access request...\n"
        "- Something else entirely\n"
        "## Business\n"
        "- Unresolved follow-up in Business.md: confirm with Jon whether Shantry Bills...\n"
    )
    _write_digest(tmp_path, "2026-01-01.md", digest)
    _write_digest(tmp_path, "2026-01-02.md", digest)

    relay.main()

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
    _patch_key(monkeypatch)
    calls = []

    def fake_post_flag(base_url, key, check, summary):
        calls.append({"check": check, "summary": summary})
        return True

    _patch_post_flag(monkeypatch, fake_post_flag)

    digest_day1 = (
        "## Work\n"
        "1. General-Motors.md — remote-access request still unresolved (stale).\n"
        "2. MOC-VRSI.md — new pricing section (new).\n"
    )
    digest_day2 = digest_day1 + "3. Another-Doc.md — a third item added later.\n"

    _write_digest(tmp_path, "2026-01-01.md", digest_day1)
    _write_digest(tmp_path, "2026-01-02.md", digest_day2)

    relay.main()

    day1_checks = [c["check"] for c in calls[:2]]
    day2_checks = [c["check"] for c in calls[2:5]]
    assert len(calls) == 5  # 2 findings day1 + 3 findings day2
    assert any("General-Motors.md" in c["summary"] for c in calls[:2])
    assert any("MOC-VRSI.md" in c["summary"] for c in calls[:2])
    # the first two items' slugs are unchanged by the third item appearing.
    assert day1_checks == day2_checks[:2]


def test_summary_truncated_to_300_chars(monkeypatch, tmp_path):
    _patch_dirs(monkeypatch, tmp_path)
    _patch_key(monkeypatch)
    calls = []

    def fake_post_flag(base_url, key, check, summary):
        calls.append(summary)
        return True

    _patch_post_flag(monkeypatch, fake_post_flag)

    long_text = "x" * 500
    _write_digest(tmp_path, "2026-01-01.md", f"- {long_text}\n")

    relay.main()

    assert len(calls) == 1
    assert len(calls[0]) <= 300


def test_per_file_finding_cap_holds(monkeypatch, tmp_path):
    _patch_dirs(monkeypatch, tmp_path)
    _patch_key(monkeypatch)
    calls = []

    def fake_post_flag(base_url, key, check, summary):
        calls.append(check)
        return True

    _patch_post_flag(monkeypatch, fake_post_flag)

    bullets = "\n".join(f"- distinct finding number {i}" for i in range(30))
    _write_digest(tmp_path, "2026-01-01.md", bullets + "\n")

    relay.main()

    assert relay.MAX_FINDINGS_PER_FILE == 20
    assert len(calls) == relay.MAX_FINDINGS_PER_FILE


def test_already_relayed_file_is_skipped(monkeypatch, tmp_path):
    _patch_dirs(monkeypatch, tmp_path)
    _patch_key(monkeypatch)
    calls = []

    def fake_post_flag(base_url, key, check, summary):
        calls.append(check)
        return True

    _patch_post_flag(monkeypatch, fake_post_flag)

    _write_digest(tmp_path, "2026-01-01.md", "- something that should be skipped\n")
    (tmp_path / ".relay_state.json").write_text(json.dumps(["2026-01-01.md"]), encoding="utf-8")

    rc = relay.main()
    assert rc == 0
    assert calls == []


def test_post_flag_raising_does_not_propagate_but_leaves_file_unrelayed(monkeypatch, tmp_path):
    """A poisoned _post_flag call is caught per-finding and must not crash
    main() -- but unlike the old never-raising record_flag, a genuinely
    failed POST must NOT mark the file relayed, or that finding is lost
    forever."""
    _patch_dirs(monkeypatch, tmp_path)
    _patch_key(monkeypatch)

    def raising_post_flag(base_url, key, check, summary):
        raise RuntimeError("boom")

    _patch_post_flag(monkeypatch, raising_post_flag)

    _write_digest(tmp_path, "2026-01-01.md", "- a finding whose relay call will explode\n")

    rc = relay.main()  # must not raise
    assert rc == 1
    state = json.loads((tmp_path / ".relay_state.json").read_text(encoding="utf-8"))
    assert "2026-01-01.md" not in state


def test_partial_failure_within_a_file_still_attempts_every_finding_and_stays_unrelayed(
    monkeypatch, tmp_path
):
    """The documented all-or-nothing contract for one file's .relay_state.json
    marking: if some findings POST fine but even one fails, _relay_file must
    still have attempted EVERY finding (no early break on first failure) and
    the file must be left unmarked so a retry doesn't lose the failed one."""
    _patch_dirs(monkeypatch, tmp_path)
    _patch_key(monkeypatch)
    calls = []

    def flaky_post_flag(base_url, key, check, summary):
        calls.append(check)
        return "second" not in summary  # exactly one of three findings fails

    _patch_post_flag(monkeypatch, flaky_post_flag)

    _write_digest(
        tmp_path,
        "2026-01-01.md",
        "## First finding\nfirst body.\n\n## Second finding\nsecond body.\n\n## Third finding\nthird body.\n",
    )

    rc = relay.main()

    assert rc == 1
    # every finding was still attempted, despite the middle one failing
    assert len(calls) == 3
    state = json.loads((tmp_path / ".relay_state.json").read_text(encoding="utf-8"))
    assert "2026-01-01.md" not in state


def test_post_flag_returning_false_leaves_file_unrelayed(monkeypatch, tmp_path):
    _patch_dirs(monkeypatch, tmp_path)
    _patch_key(monkeypatch)

    def failing_post_flag(base_url, key, check, summary):
        return False

    _patch_post_flag(monkeypatch, failing_post_flag)

    _write_digest(tmp_path, "2026-01-01.md", "- a finding whose POST fails\n")

    rc = relay.main()
    assert rc == 1
    state = json.loads((tmp_path / ".relay_state.json").read_text(encoding="utf-8"))
    assert "2026-01-01.md" not in state


def test_no_api_key_skips_cleanly_and_marks_nothing_relayed(monkeypatch, tmp_path):
    """No NEXUS_API_KEY (env or ~/.config/nexus/api_key) must skip the whole
    run, log it, return rc 0, and never call _post_flag or mark any file
    relayed."""
    _patch_dirs(monkeypatch, tmp_path)
    monkeypatch.delenv("NEXUS_API_KEY", raising=False)
    monkeypatch.setattr(relay.Path, "home", lambda: tmp_path / "no-such-home")
    calls = []

    def fake_post_flag(base_url, key, check, summary):
        calls.append(check)
        return True

    _patch_post_flag(monkeypatch, fake_post_flag)

    _write_digest(tmp_path, "2026-01-01.md", "- a finding that should never be posted\n")

    rc = relay.main()

    assert rc == 0
    assert calls == []
    assert not (tmp_path / ".relay_state.json").exists()


def test_corrupted_relay_state_does_not_crash_main(monkeypatch, tmp_path):
    """Security auto-fix regression: a malformed .relay_state.json must
    degrade _load_relayed() to an empty set, not raise out of main()."""
    _patch_dirs(monkeypatch, tmp_path)
    _patch_key(monkeypatch)
    calls = []

    def fake_post_flag(base_url, key, check, summary):
        calls.append(check)
        return True

    _patch_post_flag(monkeypatch, fake_post_flag)

    _write_digest(tmp_path, "2026-01-01.md", "- a finding behind a corrupt state file\n")
    (tmp_path / ".relay_state.json").write_text("{not valid json", encoding="utf-8")

    rc = relay.main()  # must not raise

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
    _patch_key(monkeypatch)
    calls = []

    def fake_post_flag(base_url, key, check, summary):
        calls.append(check)
        return True

    _patch_post_flag(monkeypatch, fake_post_flag)

    shared_prefix = "x" * 80
    digest = f"- {shared_prefix} AAAA\n- {shared_prefix} BBBB\n"
    _write_digest(tmp_path, "2026-01-01.md", digest)

    rc = relay.main()

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
    run -- only a failure that prevents relaying a file at all (or a failed
    POST within it) should surface as a nonzero exit."""
    _patch_dirs(monkeypatch, tmp_path)
    _patch_key(monkeypatch)
    calls = []

    def fake_post_flag(base_url, key, check, summary):
        calls.append(check)
        return True

    _patch_post_flag(monkeypatch, fake_post_flag)

    # Invalid UTF-8 bytes make path.read_text(encoding="utf-8") raise inside
    # _relay_file -- a real, not simulated, whole-file failure.
    (tmp_path / "2026-01-01.md").write_bytes(b"\xff\xfe not valid utf-8")
    _write_digest(tmp_path, "2026-01-02.md", "- a finding in a good file\n")

    rc = relay.main()

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
    rc = relay.main()
    assert rc == 0


class _FakeUrlopenResponse:
    """Minimal context-manager stand-in for the object
    `urllib.request.urlopen()` returns -- just enough for `_post_flag`'s
    `with urlopen(...) as resp: resp.status` usage."""

    def __init__(self, status):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


def test_post_flag_sends_correct_request_shape(monkeypatch):
    """Exercises the REAL `_post_flag` (not monkeypatched out) -- pins the
    exact URL/method/headers/body a real relay POST carries. This is the gap
    mutation testing proved: every other test here fakes `_post_flag` itself,
    so a wrong source/severity/URL/missing auth header would ship silently."""
    captured = {}

    def fake_urlopen(req, timeout=30):
        captured["req"] = req
        return _FakeUrlopenResponse(200)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    ok = relay._post_flag("http://example.test:8000", "sekret", "some-check-abc123", "the summary text")

    assert ok is True
    req = captured["req"]
    assert req.full_url == "http://example.test:8000/api/safety/flags"
    assert req.get_method() == "POST"
    assert req.get_header("Authorization") == "Bearer sekret"
    # Request.add_header() stores keys via key.capitalize(), and get_header()
    # looks up its argument VERBATIM (no capitalize on the read side) -- so
    # "Content-Type" must be read back as "Content-type" to actually match.
    assert req.get_header("Content-type") == "application/json"
    assert json.loads(req.data) == {
        "source": "vault_signals",
        "check": "some-check-abc123",
        "summary": "the summary text",
        "severity": "medium",
    }


def test_post_flag_returns_true_on_2xx(monkeypatch):
    monkeypatch.setattr(
        urllib.request, "urlopen", lambda req, timeout=30: _FakeUrlopenResponse(200)
    )
    assert relay._post_flag("http://x", "k", "c", "s") is True


def test_post_flag_returns_false_on_http_error_and_does_not_raise(monkeypatch):
    def raise_http_error(req, timeout=30):
        raise urllib.error.HTTPError(req.full_url, 400, "Bad Request", hdrs=None, fp=None)

    monkeypatch.setattr(urllib.request, "urlopen", raise_http_error)
    assert relay._post_flag("http://x", "k", "c", "s") is False


def test_gitignore_contains_relay_state_entry():
    """The real STATE_FILE this script writes (digests/vault-signals/.relay_state.json)
    must be gitignored, matching the sibling tools/relay_claude_digest.py convention --
    it's local-only bookkeeping, never synced/committed."""
    repo_root = pathlib.Path(__file__).resolve().parents[1]
    gitignore = repo_root / ".gitignore"
    assert gitignore.exists(), ".gitignore not found at repo root"
    lines = [line.strip() for line in gitignore.read_text(encoding="utf-8").splitlines()]
    expected = relay.STATE_FILE.relative_to(repo_root).as_posix()
    assert expected in lines, f"{expected!r} not found in .gitignore lines: {lines}"
