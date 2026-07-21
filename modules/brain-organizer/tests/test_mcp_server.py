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
