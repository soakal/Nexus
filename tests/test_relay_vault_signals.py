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
import types
import urllib.error
import urllib.request

import pytest

_SCRIPT_PATH = (
    pathlib.Path(__file__).resolve().parents[1]
    / "tools"
    / "relay_vault_signals.py"
)

_spec = importlib.util.spec_from_file_location("relay_vault_signals", _SCRIPT_PATH)
relay = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(relay)


class _FakeCP:
    """Minimal stand-in for subprocess.CompletedProcess -- just the three
    attributes _open_and_merge_pending_digest_prs()/_branch_diff_is_single_file()/
    _pr_only_touches() actually read."""

    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


@pytest.fixture(autouse=True)
def _no_pending_digest_prs_by_default(monkeypatch):
    """Every test in this file predates _open_and_merge_pending_digest_prs(),
    which main() now calls first thing -- default every subprocess.run call
    (git/gh) to "no matching branches" so that new step is a harmless no-op
    for all of them, and none of them shell out to real git/gh/network. A
    test that actually wants to exercise the PR-open/merge machinery
    overrides this by calling monkeypatch.setattr(relay, "subprocess", ...)
    itself, which simply wins for the rest of that test."""
    monkeypatch.setattr(relay, "subprocess", types.SimpleNamespace(run=lambda cmd, **kw: _FakeCP(0, "", "")))


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
        "## First finding\n[personal] Body text one.\n\n"
        "## Second finding\n[business] Body text two.\n\n"
        "## Third finding\n[work] Body text three.\n",
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

    _write_digest(tmp_path, "2026-01-01.md", "- [personal] a finding\n")

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

    same_bullet = "- [personal] The garage sensor note has an unresolved TODO\n"
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
        "- [work] Stale open item in General-Motors.md: the remote-access request...\n"
        "- [personal] Something else entirely\n"
        "## Business\n"
        "- [business] Unresolved follow-up in Business.md: confirm with Jon whether Shantry Bills...\n"
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
        "1. [work] General-Motors.md — remote-access request still unresolved (stale).\n"
        "2. [work] MOC-VRSI.md — new pricing section (new).\n"
    )
    digest_day2 = digest_day1 + "3. [work] Another-Doc.md — a third item added later.\n"

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


def test_all_four_tag_categories_prefix_the_check_slug_and_are_stripped_from_summary(
    monkeypatch, tmp_path
):
    """Each of the four required tags -- [personal]/[business]/[work]/
    [homelab] -- must (a) prefix the POSTed `check` slug as `<category>:`
    and (b) be stripped out of the POSTed `summary` text entirely."""
    _patch_dirs(monkeypatch, tmp_path)
    _patch_key(monkeypatch)
    calls = []

    def fake_post_flag(base_url, key, check, summary):
        calls.append({"check": check, "summary": summary})
        return True

    _patch_post_flag(monkeypatch, fake_post_flag)

    digest = (
        "- [personal] Dentist appointment still unscheduled\n"
        "- [business] GM contract renewal date approaching\n"
        "- [work] Quarterly review notes need follow-up\n"
        "- [homelab] Unraid parity check overdue\n"
    )
    _write_digest(tmp_path, "2026-01-01.md", digest)

    rc = relay.main()

    assert rc == 0
    assert len(calls) == 4
    by_category = {c["check"].split(":", 1)[0]: c for c in calls}
    assert set(by_category) == {"personal", "business", "work", "homelab"}
    for category, call in by_category.items():
        assert call["check"].startswith(f"{category}:")
        assert f"[{category}]" not in call["summary"]
    assert "Dentist appointment still unscheduled" in by_category["personal"]["summary"]
    assert "GM contract renewal date approaching" in by_category["business"]["summary"]
    assert "Quarterly review notes need follow-up" in by_category["work"]["summary"]
    assert "Unraid parity check overdue" in by_category["homelab"]["summary"]


def test_backticked_tag_bullet_parses_end_to_end(monkeypatch, tmp_path):
    """VAULT_SIGNALS_INSTRUCTIONS.md's worked bullet examples are plain (no
    backticks), but its prose elsewhere wraps tag names in markdown code
    spans (e.g. "`[work]`"), and the digest is LLM-generated -- a model could
    plausibly emit a bullet like `` `[work]` **Title** -- body ``. That shape
    must still parse as a `work` finding -- not fall through as untagged --
    with the backticks (and tag) gone from `summary`."""
    _patch_dirs(monkeypatch, tmp_path)
    _patch_key(monkeypatch)
    calls = []

    def fake_post_flag(base_url, key, check, summary):
        calls.append({"check": check, "summary": summary})
        return True

    _patch_post_flag(monkeypatch, fake_post_flag)

    digest = "- `[work]` **GM contract renewal date approaching** -- body text here\n"
    _write_digest(tmp_path, "2026-01-01.md", digest)

    rc = relay.main()

    assert rc == 0
    assert len(calls) == 1
    assert calls[0]["check"].startswith("work:")
    assert "`" not in calls[0]["summary"]
    assert "[work]" not in calls[0]["summary"]


def test_untagged_bullet_is_skipped_but_file_still_marked_relayed(monkeypatch, tmp_path, capsys):
    """A bullet with no recognized [category] tag must be skipped (never
    posted) and logged -- but must NOT stop the file's other, tagged
    findings from posting, and must NOT leave the file unmarked in
    .relay_state.json (an untagged bullet is not a POST failure)."""
    _patch_dirs(monkeypatch, tmp_path)
    _patch_key(monkeypatch)
    calls = []

    def fake_post_flag(base_url, key, check, summary):
        calls.append(check)
        return True

    _patch_post_flag(monkeypatch, fake_post_flag)

    digest = "- [personal] a properly tagged finding\n- an untagged finding, no bracket tag\n"
    _write_digest(tmp_path, "2026-01-01.md", digest)

    rc = relay.main()

    assert rc == 0
    assert len(calls) == 1  # only the tagged bullet was posted
    assert calls[0].startswith("personal:")
    state = json.loads((tmp_path / ".relay_state.json").read_text(encoding="utf-8"))
    assert "2026-01-01.md" in state  # untagged bullet didn't block marking the file relayed
    out = capsys.readouterr().out
    assert "skipping untagged finding" in out


def test_tag_prefix_and_strip_also_apply_to_bulletless_section_prose(monkeypatch, tmp_path):
    """`_extract_findings`' flush() branch (a `## ` section with no bullets
    at all -- its own prose body is the finding) parses/strips the tag via
    a separate code path from the bullet branches above. The other
    tag-category/strip tests only exercise bulleted findings; this pins the
    same contract -- `check` prefixed `<category>:`, tag gone from
    `summary`, section title still present -- for the bulletless shape too,
    since nothing else in this file asserts on content for that branch."""
    _patch_dirs(monkeypatch, tmp_path)
    _patch_key(monkeypatch)
    calls = []

    def fake_post_flag(base_url, key, check, summary):
        calls.append({"check": check, "summary": summary})
        return True

    _patch_post_flag(monkeypatch, fake_post_flag)

    digest = "## Homelab\n[homelab] Unraid parity check overdue.\n"
    _write_digest(tmp_path, "2026-01-01.md", digest)

    rc = relay.main()

    assert rc == 0
    assert len(calls) == 1
    assert calls[0]["check"].startswith("homelab:")
    assert "[homelab]" not in calls[0]["summary"]
    assert "Unraid parity check overdue" in calls[0]["summary"]


def test_tag_anchored_to_bullet_content_not_loose_search_of_section_title(monkeypatch, tmp_path):
    """A section title that itself contains bracketed text resembling a tag
    (e.g. "[work] related codebase notes") must NEVER be mistaken for the
    finding's own tag -- the tag must be parsed from the bullet's own
    content only, anchored to its start, not found via a loose search of
    the section-title-prefixed display string."""
    _patch_dirs(monkeypatch, tmp_path)
    _patch_key(monkeypatch)
    calls = []

    def fake_post_flag(base_url, key, check, summary):
        calls.append({"check": check, "summary": summary})
        return True

    _patch_post_flag(monkeypatch, fake_post_flag)

    digest = "## [work] related codebase notes\n- [personal] Some personal note here\n"
    _write_digest(tmp_path, "2026-01-01.md", digest)

    rc = relay.main()

    assert rc == 0
    assert len(calls) == 1
    assert calls[0]["check"].startswith("personal:")


def test_summary_truncated_to_300_chars(monkeypatch, tmp_path):
    _patch_dirs(monkeypatch, tmp_path)
    _patch_key(monkeypatch)
    calls = []

    def fake_post_flag(base_url, key, check, summary):
        calls.append(summary)
        return True

    _patch_post_flag(monkeypatch, fake_post_flag)

    long_text = "x" * 500
    _write_digest(tmp_path, "2026-01-01.md", f"- [personal] {long_text}\n")

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

    bullets = "\n".join(f"- [personal] distinct finding number {i}" for i in range(30))
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

    _write_digest(tmp_path, "2026-01-01.md", "- [personal] a finding whose relay call will explode\n")

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
        "## First finding\n[personal] first body.\n\n"
        "## Second finding\n[business] second body.\n\n"
        "## Third finding\n[work] third body.\n",
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

    _write_digest(tmp_path, "2026-01-01.md", "- [personal] a finding whose POST fails\n")

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

    _write_digest(tmp_path, "2026-01-01.md", "- [personal] a finding behind a corrupt state file\n")
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
    digest = f"- [personal] {shared_prefix} AAAA\n- [personal] {shared_prefix} BBBB\n"
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
    _write_digest(tmp_path, "2026-01-02.md", "- [personal] a finding in a good file\n")

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


def test_prless_branch_is_opened_as_a_pr_and_merged(monkeypatch):
    """No open PR exists yet for a pushed digest/vault-* branch whose diff is
    exactly its own dated digest file -- must gh pr create it, then merge it
    through the same gating an already-open PR would go through."""
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[:4] == ["git", "ls-remote", "--heads", "origin"]:
            return _FakeCP(0, "abc123\trefs/heads/digest/vault-2026-01-01\n")
        if cmd[:3] == ["gh", "pr", "list"]:
            return _FakeCP(0, "[]")
        if cmd[:3] == ["git", "fetch", "origin"]:
            return _FakeCP(0, "")
        if cmd[:3] == ["git", "diff", "--name-only"]:
            return _FakeCP(0, "digests/vault-signals/2026-01-01.md\n")
        if cmd[:3] == ["gh", "pr", "create"]:
            return _FakeCP(0, "https://github.com/soakal/Nexus/pull/42\n")
        if cmd[:3] == ["gh", "pr", "view"] and cmd[3] == "digest/vault-2026-01-01":
            return _FakeCP(0, json.dumps({
                "number": 42,
                "headRefName": "digest/vault-2026-01-01",
                "baseRefName": "main",
                "isDraft": False,
                "isCrossRepository": False,
                "author": {"login": "soakal"},
                "headRepositoryOwner": {"login": "soakal"},
            }))
        if cmd[:3] == ["gh", "pr", "view"] and cmd[3] == "42":
            return _FakeCP(0, json.dumps({"files": [{"path": "digests/vault-signals/2026-01-01.md"}]}))
        if cmd[:3] == ["gh", "pr", "merge"]:
            return _FakeCP(0, "")
        if cmd[:2] == ["git", "pull"]:
            return _FakeCP(0, "")
        raise AssertionError(f"unexpected subprocess call: {cmd}")

    monkeypatch.setattr(relay, "subprocess", types.SimpleNamespace(run=fake_run))

    merged = relay._open_and_merge_pending_digest_prs()

    assert merged == ["digest/vault-2026-01-01"]
    assert sum(1 for c in calls if c[:3] == ["gh", "pr", "create"]) == 1
    assert sum(1 for c in calls if c[:3] == ["gh", "pr", "merge"]) == 1
    assert sum(1 for c in calls if c[:2] == ["git", "pull"]) == 1


@pytest.mark.parametrize(
    "diff_stdout",
    [
        "digests/vault-signals/2026-01-01.md\nsome_other_file.py\n",  # >1 file
        "digests/other-dir/2026-01-01.md\n",  # single file, wrong path
    ],
)
def test_branch_with_extra_or_wrong_diff_never_gets_a_pr_created(monkeypatch, diff_stdout):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[:4] == ["git", "ls-remote", "--heads", "origin"]:
            return _FakeCP(0, "abc123\trefs/heads/digest/vault-2026-01-01\n")
        if cmd[:3] == ["gh", "pr", "list"]:
            return _FakeCP(0, "[]")
        if cmd[:3] == ["git", "fetch", "origin"]:
            return _FakeCP(0, "")
        if cmd[:3] == ["git", "diff", "--name-only"]:
            return _FakeCP(0, diff_stdout)
        return _FakeCP(1, "", f"unexpected call in this test: {cmd}")

    monkeypatch.setattr(relay, "subprocess", types.SimpleNamespace(run=fake_run))

    merged = relay._open_and_merge_pending_digest_prs()

    assert merged == []
    assert not any(c[:3] == ["gh", "pr", "create"] for c in calls)
    assert not any(c[:3] == ["gh", "pr", "merge"] for c in calls)


def test_branch_with_existing_open_pr_skips_create_and_only_merges(monkeypatch):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[:4] == ["git", "ls-remote", "--heads", "origin"]:
            return _FakeCP(0, "abc123\trefs/heads/digest/vault-2026-01-02\n")
        if cmd[:3] == ["gh", "pr", "list"]:
            return _FakeCP(0, json.dumps([{
                "number": 7,
                "headRefName": "digest/vault-2026-01-02",
                "baseRefName": "main",
                "isDraft": False,
                "isCrossRepository": False,
                "author": {"login": "soakal"},
                "headRepositoryOwner": {"login": "soakal"},
            }]))
        if cmd[:3] == ["gh", "pr", "view"] and cmd[3] == "7":
            return _FakeCP(0, json.dumps({"files": [{"path": "digests/vault-signals/2026-01-02.md"}]}))
        if cmd[:3] == ["gh", "pr", "merge"]:
            return _FakeCP(0, "")
        if cmd[:2] == ["git", "pull"]:
            return _FakeCP(0, "")
        raise AssertionError(f"unexpected subprocess call: {cmd}")

    monkeypatch.setattr(relay, "subprocess", types.SimpleNamespace(run=fake_run))

    merged = relay._open_and_merge_pending_digest_prs()

    assert merged == ["digest/vault-2026-01-02"]
    assert not any(c[:3] == ["gh", "pr", "create"] for c in calls)
    assert sum(1 for c in calls if c[:3] == ["gh", "pr", "merge"]) == 1


@pytest.mark.parametrize(
    "pr_files",
    [
        [{"path": "digests/vault-signals/2026-01-02.md"}, {"path": "some_other_file.py"}],  # >1 file
        [{"path": "digests/other-dir/2026-01-02.md"}],  # single file, wrong path
    ],
)
def test_existing_open_pr_with_extra_or_wrong_diff_is_never_merged(monkeypatch, pr_files):
    """Same _pr_only_touches gate as test_branch_with_extra_or_wrong_diff_never_gets_a_pr_created
    above, but for an ALREADY-open PR (the post-PR `gh pr view <number> --json
    files` re-check right before merge, not the pre-PR `git diff` check).
    Pins that a PR whose live diff no longer matches the branch's own dated
    digest file is skipped, not merged -- deleting the `if not
    _pr_only_touches(...): continue` gate at relay_vault_signals.py:304 must
    make this fail."""
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[:4] == ["git", "ls-remote", "--heads", "origin"]:
            return _FakeCP(0, "abc123\trefs/heads/digest/vault-2026-01-02\n")
        if cmd[:3] == ["gh", "pr", "list"]:
            return _FakeCP(0, json.dumps([{
                "number": 7,
                "headRefName": "digest/vault-2026-01-02",
                "baseRefName": "main",
                "isDraft": False,
                "isCrossRepository": False,
                "author": {"login": "soakal"},
                "headRepositoryOwner": {"login": "soakal"},
            }]))
        if cmd[:3] == ["gh", "pr", "view"] and cmd[3] == "7":
            return _FakeCP(0, json.dumps({"files": pr_files}))
        return _FakeCP(1, "", f"unexpected call in this test: {cmd}")

    monkeypatch.setattr(relay, "subprocess", types.SimpleNamespace(run=fake_run))

    merged = relay._open_and_merge_pending_digest_prs()

    assert merged == []
    assert not any(c[:3] == ["gh", "pr", "merge"] for c in calls)


def test_ls_remote_nonzero_returncode_is_a_skip_not_a_crash(monkeypatch):
    monkeypatch.setattr(
        relay, "subprocess", types.SimpleNamespace(run=lambda cmd, **kw: _FakeCP(1, "", "network error"))
    )
    assert relay._open_and_merge_pending_digest_prs() == []


def test_gh_pr_list_nonzero_returncode_is_a_skip_not_a_crash(monkeypatch):
    def fake_run(cmd, **kwargs):
        if cmd[:4] == ["git", "ls-remote", "--heads", "origin"]:
            return _FakeCP(0, "abc123\trefs/heads/digest/vault-2026-01-01\n")
        if cmd[:3] == ["gh", "pr", "list"]:
            return _FakeCP(1, "", "gh: not authenticated")
        raise AssertionError(f"unexpected subprocess call: {cmd}")

    monkeypatch.setattr(relay, "subprocess", types.SimpleNamespace(run=fake_run))
    assert relay._open_and_merge_pending_digest_prs() == []


def test_subprocess_raising_is_a_skip_and_main_still_returns_normally(monkeypatch, tmp_path):
    """A raising subprocess call (e.g. git/gh missing entirely) must not
    propagate out of _open_and_merge_pending_digest_prs() -- and main() as a
    whole must still complete normally (no digest dir here, so rc 0)."""
    _patch_dirs(monkeypatch, tmp_path)

    def raising_run(cmd, **kwargs):
        raise FileNotFoundError("git: command not found")

    monkeypatch.setattr(relay, "subprocess", types.SimpleNamespace(run=raising_run))

    assert relay._open_and_merge_pending_digest_prs() == []

    rc = relay.main()  # must not raise
    assert rc == 0


def test_fork_pr_with_same_branch_name_does_not_shadow_genuine_branch(monkeypatch):
    """Security auto-fix regression: a same-named PR opened from a stranger's
    fork must not be treated as "already open" for the genuine origin branch.
    Pre-fix, `open_by_branch` keyed purely on headRefName -- so a fork PR
    (isCrossRepository=True, foreign owner/author) sharing the branch name
    would be picked up as `pr`, skip the create step entirely, then get
    correctly rejected by the isCrossRepository check below -- silently
    leaving the real digest branch permanently PR-less every run. Post-fix,
    the fork PR is filtered out of `open_by_branch` up front, so a genuine
    first-party PR still gets created and merged."""
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[:4] == ["git", "ls-remote", "--heads", "origin"]:
            return _FakeCP(0, "abc123\trefs/heads/digest/vault-2026-01-05\n")
        if cmd[:3] == ["gh", "pr", "list"]:
            return _FakeCP(0, json.dumps([{
                "number": 99,
                "headRefName": "digest/vault-2026-01-05",
                "baseRefName": "main",
                "isDraft": False,
                "isCrossRepository": True,
                "author": {"login": "some-forker"},
                "headRepositoryOwner": {"login": "some-forker"},
            }]))
        if cmd[:3] == ["git", "fetch", "origin"]:
            return _FakeCP(0, "")
        if cmd[:3] == ["git", "diff", "--name-only"]:
            return _FakeCP(0, "digests/vault-signals/2026-01-05.md\n")
        if cmd[:3] == ["gh", "pr", "create"]:
            return _FakeCP(0, "https://github.com/soakal/Nexus/pull/42\n")
        if cmd[:3] == ["gh", "pr", "view"] and cmd[3] == "digest/vault-2026-01-05":
            return _FakeCP(0, json.dumps({
                "number": 42,
                "headRefName": "digest/vault-2026-01-05",
                "baseRefName": "main",
                "isDraft": False,
                "isCrossRepository": False,
                "author": {"login": "soakal"},
                "headRepositoryOwner": {"login": "soakal"},
            }))
        if cmd[:3] == ["gh", "pr", "view"] and cmd[3] == "42":
            return _FakeCP(0, json.dumps({"files": [{"path": "digests/vault-signals/2026-01-05.md"}]}))
        if cmd[:3] == ["gh", "pr", "merge"]:
            return _FakeCP(0, "")
        if cmd[:2] == ["git", "pull"]:
            return _FakeCP(0, "")
        raise AssertionError(f"unexpected subprocess call: {cmd}")

    monkeypatch.setattr(relay, "subprocess", types.SimpleNamespace(run=fake_run))

    merged = relay._open_and_merge_pending_digest_prs()

    # The fork PR (#99) never blocked detection: a genuine first-party PR
    # (#42) was created and merged for the real origin branch.
    assert merged == ["digest/vault-2026-01-05"]
    assert sum(1 for c in calls if c[:3] == ["gh", "pr", "create"]) == 1
    merge_calls = [c for c in calls if c[:3] == ["gh", "pr", "merge"]]
    assert merge_calls == [["gh", "pr", "merge", "42", "--merge", "--delete-branch"]]


def test_multiple_pending_branches_are_all_processed_independently(monkeypatch):
    """Two branches pending in the same run -- one with no PR yet, one
    already-PR'd -- must both be handled correctly, with neither the loop
    stopping early after the first branch nor a branch being processed more
    than once."""
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[:4] == ["git", "ls-remote", "--heads", "origin"]:
            return _FakeCP(
                0,
                "aaa111\trefs/heads/digest/vault-2026-02-01\n"
                "bbb222\trefs/heads/digest/vault-2026-02-02\n",
            )
        if cmd[:3] == ["gh", "pr", "list"]:
            return _FakeCP(0, json.dumps([{
                "number": 7,
                "headRefName": "digest/vault-2026-02-02",
                "baseRefName": "main",
                "isDraft": False,
                "isCrossRepository": False,
                "author": {"login": "soakal"},
                "headRepositoryOwner": {"login": "soakal"},
            }]))
        if cmd[:3] == ["git", "fetch", "origin"]:
            return _FakeCP(0, "")
        if cmd[:3] == ["git", "diff", "--name-only"]:
            return _FakeCP(0, "digests/vault-signals/2026-02-01.md\n")
        if cmd[:3] == ["gh", "pr", "create"]:
            return _FakeCP(0, "https://github.com/soakal/Nexus/pull/50\n")
        if cmd[:3] == ["gh", "pr", "view"] and cmd[3] == "digest/vault-2026-02-01":
            return _FakeCP(0, json.dumps({
                "number": 50,
                "headRefName": "digest/vault-2026-02-01",
                "baseRefName": "main",
                "isDraft": False,
                "isCrossRepository": False,
                "author": {"login": "soakal"},
                "headRepositoryOwner": {"login": "soakal"},
            }))
        if cmd[:3] == ["gh", "pr", "view"] and cmd[3] == "50":
            return _FakeCP(0, json.dumps({"files": [{"path": "digests/vault-signals/2026-02-01.md"}]}))
        if cmd[:3] == ["gh", "pr", "view"] and cmd[3] == "7":
            return _FakeCP(0, json.dumps({"files": [{"path": "digests/vault-signals/2026-02-02.md"}]}))
        if cmd[:3] == ["gh", "pr", "merge"]:
            return _FakeCP(0, "")
        if cmd[:2] == ["git", "pull"]:
            return _FakeCP(0, "")
        raise AssertionError(f"unexpected subprocess call: {cmd}")

    monkeypatch.setattr(relay, "subprocess", types.SimpleNamespace(run=fake_run))

    merged = relay._open_and_merge_pending_digest_prs()

    assert merged == ["digest/vault-2026-02-01", "digest/vault-2026-02-02"]
    # exactly one PR created (for the PR-less branch), not zero or two.
    assert sum(1 for c in calls if c[:3] == ["gh", "pr", "create"]) == 1
    merge_numbers = {c[3] for c in calls if c[:3] == ["gh", "pr", "merge"]}
    assert merge_numbers == {"50", "7"}
    # one shared git pull for the whole batch, not one per merged branch.
    assert sum(1 for c in calls if c[:2] == ["git", "pull"]) == 1


def test_git_pull_failure_after_merge_prints_warning(monkeypatch, capsys):
    """Mirrors relay_claude_digest.py::_merge_pending_digest_prs' convention:
    a merge followed by a failed `git pull` must not be silent (main() would
    otherwise find nothing new to relay and return 0, indistinguishable from
    "no digest ran today"). merged() is still returned unchanged -- the pull
    failure is a printed WARNING naming the merged branch count, not an
    error that unwinds the merge."""
    def fake_run(cmd, **kwargs):
        if cmd[:4] == ["git", "ls-remote", "--heads", "origin"]:
            return _FakeCP(0, "abc123\trefs/heads/digest/vault-2026-01-02\n")
        if cmd[:3] == ["gh", "pr", "list"]:
            return _FakeCP(0, json.dumps([{
                "number": 7,
                "headRefName": "digest/vault-2026-01-02",
                "baseRefName": "main",
                "isDraft": False,
                "isCrossRepository": False,
                "author": {"login": "soakal"},
                "headRepositoryOwner": {"login": "soakal"},
            }]))
        if cmd[:3] == ["gh", "pr", "view"] and cmd[3] == "7":
            return _FakeCP(0, json.dumps({"files": [{"path": "digests/vault-signals/2026-01-02.md"}]}))
        if cmd[:3] == ["gh", "pr", "merge"]:
            return _FakeCP(0, "")
        if cmd[:2] == ["git", "pull"]:
            return _FakeCP(1, "", "error: cannot pull -- local changes")
        raise AssertionError(f"unexpected subprocess call: {cmd}")

    monkeypatch.setattr(relay, "subprocess", types.SimpleNamespace(run=fake_run))

    merged = relay._open_and_merge_pending_digest_prs()

    assert merged == ["digest/vault-2026-01-02"]
    out = capsys.readouterr().out
    assert "WARNING" in out
    assert "merged 1 digest PR(s)" in out
    assert "git pull" in out


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
