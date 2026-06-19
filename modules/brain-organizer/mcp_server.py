"""
Brain Organizer MCP HTTP Server.

Exposes the wiki vault for reading and accepts new raw notes via POST.
Binds to 0.0.0.0 so it is reachable over Tailscale from any device.

POST /raw requires an Authorization: Bearer <token> header when MCP_WRITE_TOKEN
env var (or config mcp_write_token) is set. Leave blank to run without auth
(acceptable on a trusted Tailscale tailnet).

Usage:
    python mcp_server.py
    python mcp_server.py --config /path/to/config.json
    python mcp_server.py --host 0.0.0.0 --port 8765
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from brain_organizer import sanitize_topic_name
from flask import Flask, jsonify, request

CONFIG_PATH = Path(__file__).parent / "config.json"


def load_config(config_path: Path | None = None) -> dict[str, Any]:
    path = config_path or CONFIG_PATH
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _setup_logging(config: dict[str, Any]) -> None:
    logs_folder = Path(config["logs_folder"])
    logs_folder.mkdir(parents=True, exist_ok=True)
    log_file = logs_folder / "mcp.log"
    fmt = "%(asctime)s [%(levelname)s] %(message)s"
    handlers: list[logging.Handler] = [
        logging.FileHandler(log_file, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ]
    logging.basicConfig(level=logging.INFO, format=fmt, handlers=handlers, force=True)


def _sanitize_filename(name: str) -> str:
    """Allow only safe characters in a raw note filename, stripping path components."""
    safe = re.sub(r"[^\w\s\-.]", "", Path(name).name).strip()
    # Reject names that reduce to empty or only dots after stripping
    if not safe or not safe.strip("."):
        return ""
    return safe


# ---------------------------------------------------------------------------
# App factory (enables clean unit testing via Flask test client)
# ---------------------------------------------------------------------------

def create_app(
    config: dict[str, Any] | None = None,
    config_path: Path | None = None,
) -> Flask:
    if config is None:
        config = load_config(config_path)

    _setup_logging(config)
    logger = logging.getLogger("mcp_server")
    app = Flask(__name__)

    def _wiki_folder() -> Path:
        return Path(config["vault_path"]) / config["wiki_folder"]

    def _raw_folder() -> Path:
        return Path(config["vault_path"]) / config["raw_folder"]

    def _write_token() -> str:
        """Optional shared secret for POST /raw. Empty string = no auth required."""
        return os.environ.get("MCP_WRITE_TOKEN") or config.get("mcp_write_token", "")

    @app.before_request
    def _log_request() -> None:
        logger.info("%s %s", request.method, request.path)

    # ------------------------------------------------------------------
    # GET /health
    # ------------------------------------------------------------------
    @app.route("/health")
    def health() -> Any:
        return jsonify({"status": "ok"})

    # ------------------------------------------------------------------
    # GET /wiki  — list all topics
    # ------------------------------------------------------------------
    @app.route("/wiki")
    def list_wiki() -> Any:
        wf = _wiki_folder()
        if not wf.exists():
            return jsonify({"topics": []})
        topics = [f.stem for f in sorted(wf.glob("*.md")) if f.is_file()]
        return jsonify({"topics": topics})

    # ------------------------------------------------------------------
    # GET /wiki/search?q=query  — full-text search across all wiki files
    # ------------------------------------------------------------------
    @app.route("/wiki/search")
    def search_wiki() -> Any:
        q = request.args.get("q", "").lower().strip()
        if not q:
            return jsonify({"error": "query parameter 'q' is required"}), 400

        wf = _wiki_folder()
        if not wf.exists():
            return jsonify({"results": []})

        results = []
        for md_file in sorted(wf.glob("*.md")):
            try:
                content = md_file.read_text(encoding="utf-8")
            except OSError:
                continue
            if q in content.lower():
                matching_lines = [ln for ln in content.splitlines() if q in ln.lower()]
                results.append({"topic": md_file.stem, "matches": matching_lines[:5]})

        return jsonify({"results": results})

    # ------------------------------------------------------------------
    # GET /wiki/<topic>  — read a specific wiki file
    # Uses the same sanitizer as brain_organizer so topic names always resolve.
    # ------------------------------------------------------------------
    @app.route("/wiki/<topic>")
    def read_wiki(topic: str) -> Any:
        safe = sanitize_topic_name(topic)
        if not safe or safe == "Uncategorized" and topic.strip() != "Uncategorized":
            return jsonify({"error": "Invalid topic name"}), 400

        wiki_file = _wiki_folder() / f"{safe}.md"
        if not wiki_file.exists():
            return jsonify({"error": f"Topic '{safe}' not found"}), 404

        try:
            content = wiki_file.read_text(encoding="utf-8")
        except OSError as exc:
            logger.error("Failed to read wiki file %s: %s", wiki_file, exc)
            return jsonify({"error": "Failed to read wiki file"}), 500

        return jsonify({"topic": safe, "content": content})

    # ------------------------------------------------------------------
    # POST /raw  — drop a new note into raw/ for next processing run
    # Optionally protected by a bearer token (MCP_WRITE_TOKEN env var).
    # ------------------------------------------------------------------
    @app.route("/raw", methods=["POST"])
    def post_raw() -> Any:
        token = _write_token()
        if token:
            auth_header = request.headers.get("Authorization", "")
            if auth_header != f"Bearer {token}":
                return jsonify({"error": "Unauthorized"}), 401

        data = request.get_json(silent=True)
        if not data or "content" not in data:
            return jsonify({"error": "JSON body with 'content' field required"}), 400

        content = str(data["content"])
        raw_name = data.get("filename") or f"remote-note-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.md"

        safe_name = _sanitize_filename(raw_name)
        if not safe_name:
            safe_name = f"note-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.md"
        if not safe_name.endswith((".md", ".txt")):
            safe_name += ".md"

        rf = _raw_folder()
        rf.mkdir(parents=True, exist_ok=True)
        target = rf / safe_name

        if target.exists():
            stem = Path(safe_name).stem
            suffix = Path(safe_name).suffix
            ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            target = rf / f"{stem}_{ts}{suffix}"

        try:
            target.write_text(content, encoding="utf-8")
        except OSError as exc:
            logger.error("Failed to write raw file %s: %s", target, exc)
            return jsonify({"error": "Failed to write file"}), 500

        logger.info("Raw file created: %s", target.name)
        return jsonify({"status": "ok", "file": target.name}), 201

    return app


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Brain Organizer MCP HTTP server")
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    args = parser.parse_args()

    config = load_config(args.config)
    host = args.host or config.get("mcp_host", "0.0.0.0")  # nosec B104 — intentional for Tailscale access
    port = args.port or config.get("mcp_port", 8765)

    app = create_app(config)
    logger = logging.getLogger("mcp_server")
    logger.info("Starting Brain Organizer MCP server on %s:%s", host, port)
    app.run(host=host, port=port, use_reloader=False)


if __name__ == "__main__":
    main()
