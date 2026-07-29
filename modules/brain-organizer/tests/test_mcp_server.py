"""Integration tests for mcp_server.py using Flask test client."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

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
# _build_stem_index / _normalize_wikilinks — wiki/daily/ resolution
# ---------------------------------------------------------------------------

def test_build_stem_index_includes_daily_folder_pages(tmp_vault: Path) -> None:
    """A [[2026-07-25]] link must resolve even though the target page moved
    into wiki/daily/ -- without this it would get flagged in a spurious
    '## Broken Links' footer on every note that references a daily page."""
    from mcp_server import _build_stem_index

    seed_daily(tmp_vault, "2026-07-25", "# 2026-07-25\n\ncontent")
    index = _build_stem_index(tmp_vault / "wiki", tmp_vault / "wiki" / "daily")

    assert "2026-07-25" in index.values()


def test_build_stem_index_excludes_processed_folder(tmp_vault: Path) -> None:
    from mcp_server import _build_stem_index

    (tmp_vault / "wiki" / "processed").mkdir(parents=True, exist_ok=True)
    (tmp_vault / "wiki" / "processed" / "Old-Note.md").write_text("# Old", encoding="utf-8")

    index = _build_stem_index(tmp_vault / "wiki", tmp_vault / "wiki" / "daily")

    assert "Old-Note" not in index.values()


def test_normalize_wikilinks_resolves_daily_folder_target(tmp_vault: Path) -> None:
    from mcp_server import _build_stem_index, _normalize_wikilinks

    seed_daily(tmp_vault, "2026-07-25", "# 2026-07-25\n\ncontent")
    index = _build_stem_index(tmp_vault / "wiki", tmp_vault / "wiki" / "daily")

    result = _normalize_wikilinks("See [[2026-07-25]] for details.", index)

    assert "Broken Links" not in result
    assert "[[2026-07-25]]" in result


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
