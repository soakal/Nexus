"""Integration tests for mcp_server.py using Flask test client."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from mcp_server import create_app

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def seed_wiki(vault: Path, topic: str, content: str) -> None:
    wiki_dir = vault / "wiki"
    wiki_dir.mkdir(parents=True, exist_ok=True)
    (wiki_dir / f"{topic}.md").write_text(content, encoding="utf-8")


def seed_daily(vault: Path, date: str, content: str) -> None:
    daily_dir = vault / "wiki" / "daily"
    daily_dir.mkdir(parents=True, exist_ok=True)
    (daily_dir / f"{date}.md").write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# GET /health
# ---------------------------------------------------------------------------

def test_health_endpoint_returns_200(wiki_app) -> None:
    resp = wiki_app.get("/health")
    assert resp.status_code == 200
    data = json.loads(resp.data)
    assert data["status"] == "ok"


# ---------------------------------------------------------------------------
# GET /wiki
# ---------------------------------------------------------------------------

def test_wiki_list_endpoint_empty(wiki_app) -> None:
    resp = wiki_app.get("/wiki")
    assert resp.status_code == 200
    data = json.loads(resp.data)
    assert data["topics"] == []


def test_wiki_list_endpoint_returns_topics(
    wiki_app, tmp_vault: Path, tmp_config: dict[str, Any]
) -> None:
    seed_wiki(tmp_vault, "NEXUS", "# NEXUS")
    seed_wiki(tmp_vault, "Hermes", "# Hermes")
    resp = wiki_app.get("/wiki")
    assert resp.status_code == 200
    data = json.loads(resp.data)
    assert set(data["topics"]) == {"NEXUS", "Hermes"}


def test_wiki_list_endpoint_stays_non_recursive(wiki_app, tmp_vault: Path) -> None:
    """GET /wiki (the flat topic list) must NOT show wiki/daily/ or
    wiki/processed/ pages -- that's the entire point of moving daily notes
    out of wiki root in the first place. Only search/read/link-resolution
    should be subfolder-aware, never the topic list itself."""
    seed_wiki(tmp_vault, "NEXUS", "# NEXUS")
    seed_daily(tmp_vault, "2026-07-25", "# 2026-07-25")
    (tmp_vault / "wiki" / "processed").mkdir(parents=True, exist_ok=True)
    (tmp_vault / "wiki" / "processed" / "2026-06-08.md").write_text("# old", encoding="utf-8")

    resp = wiki_app.get("/wiki")
    data = json.loads(resp.data)

    assert data["topics"] == ["NEXUS"]


# ---------------------------------------------------------------------------
# GET /wiki/<topic>
# ---------------------------------------------------------------------------

def test_wiki_read_endpoint(wiki_app, tmp_vault: Path) -> None:
    seed_wiki(tmp_vault, "NEXUS", "# NEXUS\n\nContent here.")
    resp = wiki_app.get("/wiki/NEXUS")
    assert resp.status_code == 200
    data = json.loads(resp.data)
    assert data["topic"] == "NEXUS"
    assert "Content here." in data["content"]


def test_wiki_read_endpoint_returns_404_for_missing(wiki_app) -> None:
    resp = wiki_app.get("/wiki/DoesNotExist")
    assert resp.status_code == 404


def test_wiki_read_endpoint_path_traversal_blocked(wiki_app) -> None:
    resp = wiki_app.get("/wiki/..%2F..%2Fetc%2Fpasswd")
    assert resp.status_code in (400, 404)


def test_wiki_read_uses_same_sanitizer_as_writer(wiki_app, tmp_vault: Path) -> None:
    # Writer would create "Home-Assistant.md" from topic "Home Assistant"
    seed_wiki(tmp_vault, "Home-Assistant", "# Home Assistant\n\nSmart home.")
    # Reader should resolve "Home Assistant" → "Home-Assistant.md"
    resp = wiki_app.get("/wiki/Home%20Assistant")
    assert resp.status_code == 200
    data = json.loads(resp.data)
    assert data["topic"] == "Home-Assistant"
    assert "Smart home." in data["content"]


def test_wiki_read_falls_back_to_daily_folder(wiki_app, tmp_vault: Path) -> None:
    """A date page lives in wiki/daily/ (brain_organizer.py's
    _daily_note_route), not wiki root -- /wiki/<topic> must still resolve it
    instead of 404ing."""
    seed_daily(tmp_vault, "2026-07-25", "# 2026-07-25\n\nDaily content.")
    resp = wiki_app.get("/wiki/2026-07-25")
    assert resp.status_code == 200
    data = json.loads(resp.data)
    assert "Daily content." in data["content"]


def test_wiki_read_prefers_root_over_daily_on_collision(wiki_app, tmp_vault: Path) -> None:
    seed_wiki(tmp_vault, "2026-07-25", "root version")
    seed_daily(tmp_vault, "2026-07-25", "daily version")
    resp = wiki_app.get("/wiki/2026-07-25")
    data = json.loads(resp.data)
    assert "root version" in data["content"]


# ---------------------------------------------------------------------------
# GET /wiki/search
# ---------------------------------------------------------------------------

def test_wiki_search_endpoint(wiki_app, tmp_vault: Path) -> None:
    seed_wiki(tmp_vault, "NEXUS", "# NEXUS\n\nThis is about NEXUS personal AI.")
    seed_wiki(tmp_vault, "Hermes", "# Hermes\n\nTelegram bot for home automation.")
    resp = wiki_app.get("/wiki/search?q=telegram")
    assert resp.status_code == 200
    data = json.loads(resp.data)
    topics = [r["topic"] for r in data["results"]]
    assert "Hermes" in topics
    assert "NEXUS" not in topics


def test_wiki_search_requires_query(wiki_app) -> None:
    resp = wiki_app.get("/wiki/search")
    assert resp.status_code == 400


def test_wiki_search_returns_matching_lines(wiki_app, tmp_vault: Path) -> None:
    seed_wiki(tmp_vault, "NEXUS", "# NEXUS\n\nPersonal AI OS.\nPowered by Claude.")
    resp = wiki_app.get("/wiki/search?q=claude")
    data = json.loads(resp.data)
    assert len(data["results"]) == 1
    assert any("Claude" in line for line in data["results"][0]["matches"])


def test_wiki_search_excludes_processed_folder(wiki_app, tmp_vault: Path) -> None:
    """wiki/processed/ (archived pre-distillation source notes) must stay out
    of search results -- a blanket rglob swept them in, roughly doubling
    result counts with stale duplicates and creating a search/read
    inconsistency (search finds a topic that GET /wiki/<topic> then 404s
    on, since only daily_folder gets a read fallback). Found live 2026-07-25."""
    (tmp_vault / "wiki" / "processed").mkdir(parents=True, exist_ok=True)
    (tmp_vault / "wiki" / "processed" / "2026-06-08.md").write_text(
        "# 2026-06-08\n\nparity check notes", encoding="utf-8"
    )

    resp = wiki_app.get("/wiki/search?q=parity")
    data = json.loads(resp.data)

    assert data["results"] == []


def test_wiki_search_finds_daily_folder_pages(wiki_app, tmp_vault: Path) -> None:
    """A page in wiki/daily/ must still be full-text searchable -- moving
    daily notes out of wiki root must not blind /wiki/search to them."""
    seed_daily(tmp_vault, "2026-07-25", "# 2026-07-25\n\nUnraid parity check needed.")
    resp = wiki_app.get("/wiki/search?q=parity")
    data = json.loads(resp.data)
    assert len(data["results"]) == 1
    assert data["results"][0]["topic"] == "2026-07-25"


# ---------------------------------------------------------------------------
# build_link_index / _normalize_wikilinks — wiki/daily/ resolution
# ---------------------------------------------------------------------------

def test_build_stem_index_includes_daily_folder_pages(tmp_vault: Path) -> None:
    """A [[2026-07-25]] link must resolve even though the target page moved
    into wiki/daily/ -- without this it would get flagged in a spurious
    '## Broken Links' footer on every note that references a daily page."""
    from brain_organizer import build_link_index

    seed_daily(tmp_vault, "2026-07-25", "# 2026-07-25\n\ncontent")
    wiki_dir = tmp_vault / "wiki"
    index = build_link_index([wiki_dir.parent, wiki_dir, wiki_dir / "daily"])

    assert "2026-07-25" in index.values()


def test_build_stem_index_excludes_processed_folder(tmp_vault: Path) -> None:
    from brain_organizer import build_link_index

    (tmp_vault / "wiki" / "processed").mkdir(parents=True, exist_ok=True)
    (tmp_vault / "wiki" / "processed" / "Old-Note.md").write_text("# Old", encoding="utf-8")

    wiki_dir = tmp_vault / "wiki"
    index = build_link_index([wiki_dir.parent, wiki_dir, wiki_dir / "daily"])

    assert "Old-Note" not in index.values()


def test_normalize_wikilinks_resolves_daily_folder_target(tmp_vault: Path) -> None:
    from brain_organizer import build_link_index
    from mcp_server import _normalize_wikilinks

    seed_daily(tmp_vault, "2026-07-25", "# 2026-07-25\n\ncontent")
    wiki_dir = tmp_vault / "wiki"
    index = build_link_index([wiki_dir.parent, wiki_dir, wiki_dir / "daily"])

    result, broken = _normalize_wikilinks("See [[2026-07-25]] for details.", index)

    assert "Broken Links" not in result
    assert "[[2026-07-25]]" in result
    assert broken == []


def test_normalize_wikilinks_shared_index_resolves_case_insensitive_target(tmp_vault: Path) -> None:
    """Proves build_link_index (brain_organizer) and _normalize_wikilinks
    (mcp_server) are genuinely wired together, not just independently
    working: a target that only matches via canonical_link_key's
    case/spacing normalization (not a literal string match) must still
    resolve to the on-disk stem."""
    from brain_organizer import build_link_index
    from mcp_server import _normalize_wikilinks

    seed_wiki(tmp_vault, "Home-Assistant", "# Home Assistant")
    wiki_dir = tmp_vault / "wiki"
    index = build_link_index([wiki_dir.parent, wiki_dir, wiki_dir / "daily"])

    result, broken = _normalize_wikilinks("See [[home assistant]] for setup.", index)

    assert "Broken Links" not in result
    assert "[[Home-Assistant]]" in result
    assert broken == []


def test_normalize_wikilinks_substitutes_stem_for_bare_trailing_heading_sigil(
    tmp_vault: Path,
) -> None:
    """§3.3(a) bare-trailing-heading-sigil regression pin (NOT criterion 16 --
    see test_raw_post_calls_build_link_index_with_exact_folder_scope below
    and test_build_wiki_catalog_excludes_daily_and_processed_subfolders in
    test_organizer.py for that): WIKILINK_PAT allows an empty heading
    group (unlike the old mcp_server _WIKILINK_RE, whose anchor group
    required >=1 char after '#' and so never matched a bare trailing '#' at
    all). A resolvable target with a bare trailing '#' must now get its stem
    substituted, with the bare sigil preserved."""
    from brain_organizer import build_link_index
    from mcp_server import _normalize_wikilinks

    seed_wiki(tmp_vault, "Home-Assistant", "# Home Assistant")
    wiki_dir = tmp_vault / "wiki"
    index = build_link_index([wiki_dir.parent, wiki_dir, wiki_dir / "daily"])

    result, broken = _normalize_wikilinks("See [[home-assistant#]] for setup.", index)

    assert result == "See [[Home-Assistant#]] for setup."
    assert broken == []


def test_normalize_wikilinks_leaves_pure_anchor_untouched() -> None:
    """Net-identical-behavior regression pin: [[#heading]] (empty target)
    must still be left completely unchanged. WIKILINK_PAT's target group
    requires >=1 char so it never matches this at all (the old mcp_server
    regex did match, with an empty captured target, and hit the same
    early-return branch explicitly -- same net output, different code
    path)."""
    from mcp_server import _normalize_wikilinks

    text = "See [[#heading]] for context."
    result, broken = _normalize_wikilinks(text, {})

    assert result == text
    assert "Broken Links" not in result
    assert broken == []


def test_mcp_server_no_longer_defines_its_own_wikilink_helpers() -> None:
    """mcp_server.py must not re-define its own _WIKILINK_RE / _canonical_key
    / _build_stem_index -- the whole point of this step is a single shared
    implementation in brain_organizer.py that both modules import."""
    import mcp_server

    assert not hasattr(mcp_server, "_WIKILINK_RE")
    assert not hasattr(mcp_server, "_canonical_key")
    assert not hasattr(mcp_server, "_build_stem_index")


# ---------------------------------------------------------------------------
# POST /raw
# ---------------------------------------------------------------------------

def test_raw_post_creates_file(wiki_app, tmp_vault: Path) -> None:
    resp = wiki_app.post(
        "/raw",
        json={"content": "A new note from remote", "filename": "test-note.md"},
        content_type="application/json",
    )
    assert resp.status_code == 201
    data = json.loads(resp.data)
    assert data["status"] == "ok"
    created = tmp_vault / "raw" / data["file"]
    assert created.exists()
    assert created.read_text(encoding="utf-8") == "A new note from remote"


def test_raw_post_reports_broken_links_without_footer_injection(
    wiki_app, tmp_vault: Path
) -> None:
    """Criterion 17 (docs/brain-organizer-robustness-spec.md §3.3(c)): an
    unresolvable [[wikilink]] must NOT get a '## Broken Links' footer
    injected into the saved note content, and the POST /raw JSON response
    must instead surface the unresolved target via a top-level
    'broken_links' list -- this is the actual regression case (a real
    broken link), not the no-broken-link resolved-case tweaks already
    covered by the 4 updated _normalize_wikilinks unit tests."""
    resp = wiki_app.post(
        "/raw",
        json={
            "content": "See [[NoSuchTopic]] for details.",
            "filename": "broken-link-note.md",
        },
        content_type="application/json",
    )
    assert resp.status_code == 201
    data = json.loads(resp.data)
    assert data["broken_links"] == ["NoSuchTopic"]

    created = tmp_vault / "raw" / data["file"]
    saved_content = created.read_text(encoding="utf-8")
    assert "Broken Links" not in saved_content
    assert saved_content == "See [[NoSuchTopic]] for details."


def test_raw_post_calls_build_link_index_with_exact_folder_scope(
    wiki_app, tmp_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Criterion 16 (docs/brain-organizer-robustness-spec.md:191) / §3.3(b):
    the POST /raw handler's build_link_index call must scan exactly 3
    folders, in this order -- [vault_root, wiki/, wiki/daily] -- and never a
    4th (e.g. wiki/processed/, which build_link_index's whole non-recursive
    design exists to exclude, see its docstring in brain_organizer.py).
    Spies on the actual call site in mcp_server.py's post_raw route, not
    just build_link_index in isolation, so a widened call-site literal is
    caught even though build_link_index itself behaves correctly for
    whatever folders it's handed."""
    import mcp_server

    calls: list[list[Path]] = []
    real_build_link_index = mcp_server.build_link_index

    def spy(folders: list[Path]) -> dict[str, str]:
        calls.append(list(folders))
        return real_build_link_index(folders)

    monkeypatch.setattr(mcp_server, "build_link_index", spy)

    resp = wiki_app.post(
        "/raw",
        json={"content": "hi", "filename": "scope-check.md"},
        content_type="application/json",
    )

    assert resp.status_code == 201
    assert len(calls) == 1
    wf = tmp_vault / "wiki"
    assert calls[0] == [wf.parent, wf, wf / "daily"]


def test_raw_post_requires_content(wiki_app) -> None:
    resp = wiki_app.post("/raw", json={"filename": "oops.md"}, content_type="application/json")
    assert resp.status_code == 400


def test_raw_post_no_json_body(wiki_app) -> None:
    resp = wiki_app.post("/raw", data="plain text", content_type="text/plain")
    assert resp.status_code == 400


def test_raw_post_sanitizes_filename(wiki_app, tmp_vault: Path) -> None:
    resp = wiki_app.post(
        "/raw",
        json={"content": "hi", "filename": "../../../etc/passwd"},
        content_type="application/json",
    )
    assert resp.status_code == 201
    data = json.loads(resp.data)
    created = tmp_vault / "raw" / data["file"]
    assert created.parent == tmp_vault / "raw"


def test_raw_post_generates_filename_if_missing(wiki_app, tmp_vault: Path) -> None:
    resp = wiki_app.post(
        "/raw",
        json={"content": "note with no name"},
        content_type="application/json",
    )
    assert resp.status_code == 201
    data = json.loads(resp.data)
    assert data["file"].endswith(".md")


def test_raw_post_rejects_dotfile_filename(wiki_app, tmp_vault: Path) -> None:
    resp = wiki_app.post(
        "/raw",
        json={"content": "content", "filename": "..."},
        content_type="application/json",
    )
    # Should succeed but use a timestamp fallback name, not the dotfile
    assert resp.status_code == 201
    data = json.loads(resp.data)
    created = tmp_vault / "raw" / data["file"]
    assert not data["file"].startswith(".")


# ---------------------------------------------------------------------------
# Bearer token auth on POST /raw
# ---------------------------------------------------------------------------

def test_raw_post_requires_token_when_configured(tmp_config: dict[str, Any], tmp_vault: Path) -> None:
    tmp_config["mcp_write_token"] = "secret123"
    app = create_app(config=tmp_config)
    app.config["TESTING"] = True
    client = app.test_client()

    # No token → 401
    resp = client.post(
        "/raw",
        json={"content": "hi"},
        content_type="application/json",
        environ_overrides={"REMOTE_ADDR": "203.0.113.9"},
    )
    assert resp.status_code == 401

    # Wrong token → 401
    resp = client.post(
        "/raw",
        json={"content": "hi"},
        content_type="application/json",
        headers={"Authorization": "Bearer wrong"},
        environ_overrides={"REMOTE_ADDR": "203.0.113.9"},
    )
    assert resp.status_code == 401

    # Correct token → 201
    resp = client.post(
        "/raw",
        json={"content": "hi"},
        content_type="application/json",
        headers={"Authorization": "Bearer secret123"},
        environ_overrides={"REMOTE_ADDR": "203.0.113.9"},
    )
    assert resp.status_code == 201


def test_raw_post_no_auth_when_token_empty(tmp_config: dict[str, Any]) -> None:
    tmp_config["mcp_write_token"] = ""
    app = create_app(config=tmp_config)
    app.config["TESTING"] = True
    client = app.test_client()

    resp = client.post("/raw", json={"content": "hi"}, content_type="application/json")
    assert resp.status_code == 201


def test_raw_post_loopback_exempt_when_token_configured(tmp_config: dict[str, Any]) -> None:
    """Loopback callers never need a token, even when one IS configured (decision 1)."""
    tmp_config["mcp_write_token"] = "secret123"
    app = create_app(config=tmp_config)
    app.config["TESTING"] = True
    client = app.test_client()

    # Default test client REMOTE_ADDR is 127.0.0.1 (loopback) — no Authorization header sent.
    resp = client.post("/raw", json={"content": "hi"}, content_type="application/json")
    assert resp.status_code == 201


def test_raw_post_remote_rejected_when_no_token_configured(tmp_config: dict[str, Any]) -> None:
    """Secure-by-default: an unset write token rejects remote writes outright (decision 2)."""
    tmp_config["mcp_write_token"] = ""
    app = create_app(config=tmp_config)
    app.config["TESTING"] = True
    client = app.test_client()

    resp = client.post(
        "/raw",
        json={"content": "hi"},
        content_type="application/json",
        environ_overrides={"REMOTE_ADDR": "192.0.2.10"},
    )
    assert resp.status_code == 403


def test_raw_post_empty_remote_addr_denied(tmp_config: dict[str, Any]) -> None:
    """An empty-string REMOTE_ADDR is not one of the loopback addresses, so it is
    treated as remote (deny-safe) rather than exempted like 127.0.0.1/::1."""
    tmp_config["mcp_write_token"] = "secret123"
    app = create_app(config=tmp_config)
    app.config["TESTING"] = True
    client = app.test_client()

    resp = client.post(
        "/raw",
        json={"content": "hi"},
        content_type="application/json",
        environ_overrides={"REMOTE_ADDR": ""},
    )
    assert resp.status_code == 401


def test_raw_post_malformed_authorization_header_rejected(tmp_config: dict[str, Any]) -> None:
    """Malformed Authorization headers (wrong scheme, or bare token missing the
    'Bearer ' prefix) from a remote caller are rejected, not silently accepted."""
    tmp_config["mcp_write_token"] = "secret123"
    app = create_app(config=tmp_config)
    app.config["TESTING"] = True
    client = app.test_client()

    # Wrong scheme entirely
    resp = client.post(
        "/raw",
        json={"content": "hi"},
        content_type="application/json",
        headers={"Authorization": "Basic xyz"},
        environ_overrides={"REMOTE_ADDR": "192.0.2.10"},
    )
    assert resp.status_code == 401

    # Bare token, missing the required "Bearer " prefix
    resp = client.post(
        "/raw",
        json={"content": "hi"},
        content_type="application/json",
        headers={"Authorization": "secret123"},
        environ_overrides={"REMOTE_ADDR": "192.0.2.10"},
    )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Bearer token auth on GET /wiki, GET /wiki/search, GET /wiki/<topic>
# (same loopback-exempt gate as POST /raw -- these read raw personal vault
# content and were previously reachable with zero authentication).
# ---------------------------------------------------------------------------

def test_wiki_list_requires_token_when_configured(tmp_config: dict[str, Any]) -> None:
    tmp_config["mcp_write_token"] = "secret123"
    app = create_app(config=tmp_config)
    app.config["TESTING"] = True
    client = app.test_client()

    # No token → 401
    resp = client.get("/wiki", environ_overrides={"REMOTE_ADDR": "203.0.113.9"})
    assert resp.status_code == 401

    # Wrong token → 401
    resp = client.get(
        "/wiki",
        headers={"Authorization": "Bearer wrong"},
        environ_overrides={"REMOTE_ADDR": "203.0.113.9"},
    )
    assert resp.status_code == 401

    # Correct token → 200
    resp = client.get(
        "/wiki",
        headers={"Authorization": "Bearer secret123"},
        environ_overrides={"REMOTE_ADDR": "203.0.113.9"},
    )
    assert resp.status_code == 200


def test_wiki_list_loopback_exempt_when_token_configured(tmp_config: dict[str, Any]) -> None:
    """Loopback callers never need a token, even when one IS configured."""
    tmp_config["mcp_write_token"] = "secret123"
    app = create_app(config=tmp_config)
    app.config["TESTING"] = True
    client = app.test_client()

    # Default test client REMOTE_ADDR is 127.0.0.1 (loopback) — no Authorization header sent.
    resp = client.get("/wiki")
    assert resp.status_code == 200


def test_wiki_list_remote_rejected_when_no_token_configured(tmp_config: dict[str, Any]) -> None:
    """Secure-by-default: an unset token rejects remote reads outright."""
    tmp_config["mcp_write_token"] = ""
    app = create_app(config=tmp_config)
    app.config["TESTING"] = True
    client = app.test_client()

    resp = client.get("/wiki", environ_overrides={"REMOTE_ADDR": "192.0.2.10"})
    assert resp.status_code == 403


def test_wiki_search_requires_token_when_configured(
    tmp_config: dict[str, Any], tmp_vault: Path
) -> None:
    seed_wiki(tmp_vault, "NEXUS", "# NEXUS\n\nsome findable content")
    tmp_config["mcp_write_token"] = "secret123"
    app = create_app(config=tmp_config)
    app.config["TESTING"] = True
    client = app.test_client()

    # No token → 401
    resp = client.get("/wiki/search?q=findable", environ_overrides={"REMOTE_ADDR": "203.0.113.9"})
    assert resp.status_code == 401

    # Wrong token → 401
    resp = client.get(
        "/wiki/search?q=findable",
        headers={"Authorization": "Bearer wrong"},
        environ_overrides={"REMOTE_ADDR": "203.0.113.9"},
    )
    assert resp.status_code == 401

    # Correct token → 200
    resp = client.get(
        "/wiki/search?q=findable",
        headers={"Authorization": "Bearer secret123"},
        environ_overrides={"REMOTE_ADDR": "203.0.113.9"},
    )
    assert resp.status_code == 200


def test_wiki_search_loopback_exempt_when_token_configured(
    tmp_config: dict[str, Any], tmp_vault: Path
) -> None:
    seed_wiki(tmp_vault, "NEXUS", "# NEXUS\n\nsome findable content")
    tmp_config["mcp_write_token"] = "secret123"
    app = create_app(config=tmp_config)
    app.config["TESTING"] = True
    client = app.test_client()

    resp = client.get("/wiki/search?q=findable")  # default REMOTE_ADDR is loopback
    assert resp.status_code == 200


def test_wiki_search_remote_rejected_when_no_token_configured(tmp_config: dict[str, Any]) -> None:
    tmp_config["mcp_write_token"] = ""
    app = create_app(config=tmp_config)
    app.config["TESTING"] = True
    client = app.test_client()

    resp = client.get("/wiki/search?q=anything", environ_overrides={"REMOTE_ADDR": "192.0.2.10"})
    assert resp.status_code == 403


def test_wiki_read_requires_token_when_configured(
    tmp_config: dict[str, Any], tmp_vault: Path
) -> None:
    seed_wiki(tmp_vault, "NEXUS", "# NEXUS\n\nSecret personal content.")
    tmp_config["mcp_write_token"] = "secret123"
    app = create_app(config=tmp_config)
    app.config["TESTING"] = True
    client = app.test_client()

    # No token → 401, content never leaked in the response body
    resp = client.get("/wiki/NEXUS", environ_overrides={"REMOTE_ADDR": "203.0.113.9"})
    assert resp.status_code == 401
    assert b"Secret personal content" not in resp.data

    # Wrong token → 401
    resp = client.get(
        "/wiki/NEXUS",
        headers={"Authorization": "Bearer wrong"},
        environ_overrides={"REMOTE_ADDR": "203.0.113.9"},
    )
    assert resp.status_code == 401

    # Correct token → 200
    resp = client.get(
        "/wiki/NEXUS",
        headers={"Authorization": "Bearer secret123"},
        environ_overrides={"REMOTE_ADDR": "203.0.113.9"},
    )
    assert resp.status_code == 200
    data = json.loads(resp.data)
    assert "Secret personal content." in data["content"]


def test_wiki_read_loopback_exempt_when_token_configured(
    tmp_config: dict[str, Any], tmp_vault: Path
) -> None:
    seed_wiki(tmp_vault, "NEXUS", "# NEXUS\n\ncontent")
    tmp_config["mcp_write_token"] = "secret123"
    app = create_app(config=tmp_config)
    app.config["TESTING"] = True
    client = app.test_client()

    resp = client.get("/wiki/NEXUS")  # default REMOTE_ADDR is loopback
    assert resp.status_code == 200


def test_wiki_read_remote_rejected_when_no_token_configured(
    tmp_config: dict[str, Any], tmp_vault: Path
) -> None:
    seed_wiki(tmp_vault, "NEXUS", "# NEXUS\n\ncontent")
    tmp_config["mcp_write_token"] = ""
    app = create_app(config=tmp_config)
    app.config["TESTING"] = True
    client = app.test_client()

    resp = client.get("/wiki/NEXUS", environ_overrides={"REMOTE_ADDR": "192.0.2.10"})
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Server binding configuration
# ---------------------------------------------------------------------------

def test_server_binds_to_all_interfaces(tmp_config: dict[str, Any]) -> None:
    assert tmp_config["mcp_host"] == "0.0.0.0", (
        "MCP server must bind to 0.0.0.0 so Tailscale and LAN devices can reach it"
    )


def test_create_app_accepts_custom_config(tmp_config: dict[str, Any]) -> None:
    tmp_config["mcp_port"] = 9999
    app = create_app(config=tmp_config)
    assert app is not None


def test_create_app_raises_on_config_missing_required_key(tmp_config: dict[str, Any]) -> None:
    """spec #2 criterion 26/mcp_server tail: create_app() must call
    validate_config() before serving -- a config missing a required key
    (e.g. vault_path) must raise instead of silently constructing an app
    that will fail later, mid-request, at some unrelated line."""
    del tmp_config["vault_path"]
    with pytest.raises(ValueError, match="vault_path"):
        create_app(config=tmp_config)
