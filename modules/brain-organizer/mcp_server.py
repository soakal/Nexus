"""
Brain Organizer MCP HTTP Server.

Exposes the wiki vault for reading and accepts new raw notes via POST.
Binds to 0.0.0.0 so it is reachable over Tailscale from any device.

Every route except GET /health is loopback-exempt: callers from 127.0.0.1/::1
never need a token. Non-loopback (LAN/Tailscale) callers require an
Authorization: Bearer <token> header matching MCP_WRITE_TOKEN env var (or
config mcp_write_token); if no token is configured, remote callers are
rejected outright (403). This covers POST /raw (writes) and GET /wiki,
GET /wiki/search, GET /wiki/<topic> (reads of personal vault content) alike.

Usage:
    python mcp_server.py
    python mcp_server.py --config /path/to/config.json
    python mcp_server.py --host 0.0.0.0 --port 8765
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import re
import secrets
import sys
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from brain_organizer import (
    WIKILINK_PAT,
    build_link_index,
    canonical_link_key,
    sanitize_topic_name,
    validate_config,
)
from flask import Flask, jsonify, request

CONFIG_PATH = Path(__file__).parent / "config.json"

_LOOPBACK_ADDRS = ("127.0.0.1", "::1")


# One user, one process: serialize /raw so same-request_id retries can't race.
_RAW_LOCK = threading.Lock()
_CATEGORIES = ("work", "personal")
_REQUEST_ID_PAT = re.compile(r"[A-Za-z0-9_-]{1,80}")


def _err(msg: str, code: int) -> tuple[Any, int]:
    return jsonify({"status": "error", "error": msg}), code


def _is_loopback(remote_addr: str | None) -> bool:
    return remote_addr in _LOOPBACK_ADDRS


def load_config(config_path: Path | None = None) -> dict[str, Any]:
    path = config_path or CONFIG_PATH
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _setup_logging(config: dict[str, Any]) -> None:
    logs_folder = Path(config["logs_folder"])
    logs_folder.mkdir(parents=True, exist_ok=True)
    log_file = logs_folder / "mcp.log"
    fmt = "%(asctime)s [%(levelname)s] %(message)s"
    # RotatingFileHandler: mcp.log grew unbounded (7MB/37 days, ~98% /health
    # noise) with a plain FileHandler — cap at 5MB x 3 backups.
    handlers: list[logging.Handler] = [
        RotatingFileHandler(log_file, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"),
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


def _write_tmp(folder: Path, text: str) -> Path:
    tmp = folder / f".tmp-{secrets.token_hex(8)}"
    with open(tmp, "x", encoding="utf-8") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    return tmp


def _publish(folder: Path, name: str, text: str, exact: bool = False) -> Path:
    """Publish text as folder/name without ever exposing a partial file.

    Writes a dot-prefixed .tmp-<rand> (no .md/.txt, so the organizer scan skips
    it), fsyncs, then exclusive-links it into place (counter suffix on
    collision; exact=True raises FileExistsError instead) and drops the tmp.
    """
    folder.mkdir(parents=True, exist_ok=True)
    tmp = _write_tmp(folder, text)
    try:
        stem, suffix = Path(name).stem, Path(name).suffix
        for n in range(1000):
            final = folder / (name if n == 0 else f"{stem}_{n}{suffix}")
            try:
                os.link(tmp, final)
                return final
            except FileExistsError:
                if exact:
                    raise
        raise FileExistsError(name)
    finally:
        tmp.unlink(missing_ok=True)


def _free_name(folder: Path, name: str) -> str:
    stem, suffix = Path(name).stem, Path(name).suffix
    for n in range(1000):
        cand = name if n == 0 else f"{stem}_{n}{suffix}"
        if not (folder / cand).exists():
            return cand
    raise FileExistsError(name)


def _overlaps(a: Path, b: Path) -> bool:
    return a == b or a in b.parents or b in a.parents


def _normalize_wikilinks(content: str, index: dict[str, str]) -> tuple[str, list[str]]:
    """Rewrite [[wikilinks]] to canonical file stems; flag unresolved ones.

    Resolvable targets are rewritten to the actual stem (heading + alias
    preserved). Unresolved targets are left in place and returned as a list
    of broken targets (original spelling preserved) -- callers are
    responsible for surfacing them (e.g. a log warning / API response field)
    rather than injecting diagnostic text into note content. Pure same-file
    anchors ([[#heading]]) and embeds (![[...]]) are skipped -- WIKILINK_PAT's
    target group requires at least one non-#/|/] character, so it never
    matches [[#heading]] at all (left untouched by .sub() as unmatched text).

    Thin HTTP-side wrapper over brain_organizer's WIKILINK_PAT / index --
    that pattern's heading/alias groups are sigil-free (no leading "#"/"|"),
    unlike the regex this replaced, so the sigils are re-added on emit here.
    """
    broken: list[str] = []
    seen_broken: set[str] = set()

    def _sub(m: "re.Match[str]") -> str:
        target = m.group(1) or ""
        heading = m.group(2)
        alias = m.group(3)

        if not target.strip():  # pure same-file anchor [[#heading]] — leave alone
            return m.group(0)

        key = canonical_link_key(target)
        canonical = index.get(key)
        if canonical is None:
            if key and key not in seen_broken:
                seen_broken.add(key)
                broken.append(target.strip())
            return m.group(0)
        heading_part = f"#{heading}" if heading is not None else ""
        alias_part = f"|{alias}" if alias is not None else ""
        return f"[[{canonical}{heading_part}{alias_part}]]"

    new_content = WIKILINK_PAT.sub(_sub, content)

    if broken:
        # WIKILINK_PAT's target group only excludes ]/|/#, so a target may
        # contain \r/\n -- strip those before logging to prevent forged
        # fake log lines (log injection) from attacker-controlled note content.
        safe_broken = [t.replace("\r", "").replace("\n", " ") for t in broken]
        logging.getLogger(__name__).warning(
            "Unresolved wikilink targets: %s", ", ".join(safe_broken)
        )

    return new_content, broken


# ---------------------------------------------------------------------------
# App factory (enables clean unit testing via Flask test client)
# ---------------------------------------------------------------------------

def create_app(
    config: dict[str, Any] | None = None,
    config_path: Path | None = None,
) -> Flask:
    if config is None:
        config = load_config(config_path)
    config = validate_config(config)

    _setup_logging(config)
    logger = logging.getLogger("mcp_server")
    app = Flask(__name__)

    # Archive roots must resolve inside the vault and stay disjoint from each
    # other and from raw/wiki (symlinks and ".." resolved before comparing).
    vault_r = Path(config["vault_path"]).resolve()
    roots = {}
    for key in ("captures_folder", "views_folder", "raw_folder", "wiki_folder"):
        rel = Path(config[key])
        if rel.is_absolute() or ".." in rel.parts:
            raise ValueError(f"{key} must be a relative path inside the vault")
        roots[key] = (vault_r / rel).resolve()
    for key in ("captures_folder", "views_folder"):
        if vault_r not in roots[key].parents:
            raise ValueError(f"{key} must resolve inside the vault")
        for other in ("raw_folder", "wiki_folder") + (("captures_folder",) if key == "views_folder" else ()):
            if _overlaps(roots[key], roots[other]):
                raise ValueError(f"{key} overlaps {other}")

    def _wiki_folder() -> Path:
        return Path(config["vault_path"]) / config["wiki_folder"]

    def _daily_folder() -> Path:
        return Path(config["vault_path"]) / config.get("daily_folder", "wiki/daily")

    def _raw_folder() -> Path:
        return Path(config["vault_path"]) / config["raw_folder"]

    def _auth_token() -> str:
        """Optional shared secret gating every non-loopback route except
        /health (POST /raw and all GET /wiki* reads). Empty string = no
        auth required for loopback; remote callers are always rejected
        when empty (see _check_auth)."""
        return os.environ.get("MCP_WRITE_TOKEN") or config.get("mcp_write_token", "")

    def _check_auth() -> tuple[Any, int] | None:
        """Loopback-exempt bearer-token gate shared by POST /raw and every
        GET /wiki* read route. Callers from 127.0.0.1/::1 never need a
        token. Non-loopback (LAN/Tailscale) callers require an
        Authorization: Bearer <token> header matching the configured
        token; if no token is configured, remote callers are rejected
        outright.

        Returns None when the caller may proceed, or a (response, status)
        tuple the route should return immediately when the caller must be
        rejected.
        """
        if _is_loopback(request.remote_addr):
            return None
        token = _auth_token()
        if not token:
            return _err("Remote access disabled (no token configured)", 403)
        auth_header = request.headers.get("Authorization", "")
        if not hmac.compare_digest(auth_header, f"Bearer {token}"):
            return _err("Unauthorized", 401)
        return None

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
    # Loopback-exempt bearer-token gated (same as POST /raw) -- this lists
    # every personal wiki topic and would otherwise be readable by any
    # caller that can reach the socket.
    # ------------------------------------------------------------------
    @app.route("/wiki")
    def list_wiki() -> Any:
        auth_error = _check_auth()
        if auth_error is not None:
            return auth_error
        wf = _wiki_folder()
        if not wf.exists():
            return jsonify({"topics": []})
        topics = [f.stem for f in sorted(wf.glob("*.md")) if f.is_file()]
        return jsonify({"topics": topics})

    # ------------------------------------------------------------------
    # GET /wiki/search?q=query  — full-text search across all wiki files
    # Loopback-exempt bearer-token gated (same as POST /raw) -- search
    # results include raw matching lines from personal notes.
    # ------------------------------------------------------------------
    @app.route("/wiki/search")
    def search_wiki() -> Any:
        auth_error = _check_auth()
        if auth_error is not None:
            return auth_error
        q = request.args.get("q", "").lower().strip()
        if not q:
            return _err("query parameter 'q' is required", 400)

        wf = _wiki_folder()
        if not wf.exists():
            return jsonify({"results": []})

        # wiki root + wiki/daily only -- NOT a blanket rglob. wiki_folder also
        # contains wiki/processed/ (263 archived pre-distillation notes on the
        # real vault), which the rest of the codebase treats as out-of-scope;
        # sweeping it in roughly doubled search results with stale duplicates
        # and could return a topic that GET /wiki/<topic> then 404s on (found
        # live 2026-07-25). See build_link_index's docstring for the same call.
        md_files = list(wf.glob("*.md"))
        df = _daily_folder()
        if df.exists():
            md_files.extend(df.glob("*.md"))

        results = []
        for md_file in sorted(md_files):
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
    # Loopback-exempt bearer-token gated (same as POST /raw) -- this is the
    # full raw markdown content of a personal note.
    # ------------------------------------------------------------------
    @app.route("/wiki/<topic>")
    def read_wiki(topic: str) -> Any:
        auth_error = _check_auth()
        if auth_error is not None:
            return auth_error
        safe = sanitize_topic_name(topic)
        if (not safe) or (safe == "Uncategorized" and topic.strip() != "Uncategorized"):
            return _err("Invalid topic name", 400)

        wiki_file = _wiki_folder() / f"{safe}.md"
        if not wiki_file.exists():
            # Daily notes live in a subfolder (brain_organizer.py's
            # _daily_note_route) -- fall back there before 404ing.
            daily_file = _daily_folder() / f"{safe}.md"
            if daily_file.exists():
                wiki_file = daily_file
            else:
                return _err(f"Topic '{safe}' not found", 404)

        try:
            content = wiki_file.read_text(encoding="utf-8")
        except OSError as exc:
            logger.error("Failed to read wiki file %s: %s", wiki_file, exc)
            return _err("Failed to read wiki file", 500)

        return jsonify({"topic": safe, "content": content})

    # ------------------------------------------------------------------
    # POST /raw  — drop a new note into raw/ for next processing run
    # Optionally protected by a bearer token (MCP_WRITE_TOKEN env var).
    # ------------------------------------------------------------------
    @app.route("/raw", methods=["POST"])
    def post_raw() -> Any:
        auth_error = _check_auth()
        if auth_error is not None:
            return auth_error
        with _RAW_LOCK:
            return _ingest()

    def _ingest() -> Any:
        data = request.get_json(silent=True)
        if not data or "content" not in data:
            return _err("JSON body with 'content' field required", 400)

        content = str(data["content"])
        if not content.strip():
            # Empty Quill captures were saved as 0-byte notes the organizer never processes.
            return _err("content is empty", 400)

        category = data.get("category")
        if category is not None and category not in _CATEGORIES:
            return _err("category must be 'work' or 'personal'", 400)
        rid = data.get("request_id")
        if rid is not None and not (isinstance(rid, str) and _REQUEST_ID_PAT.fullmatch(rid)):
            return _err("request_id must match [A-Za-z0-9_-]{1,80}", 400)
        # Archive rule: explicit category wins; no category + a `source` (a real
        # capture client) -> unsorted; otherwise (NEXUS itself, the digest relay)
        # keep the exact legacy raw-only path.
        archive = category or ("unsorted" if data.get("source") else None)
        fingerprint = hashlib.sha256(f"{archive or ''}\n{content}".encode()).hexdigest()

        broken_links: list[str] = []
        try:
            wf = _wiki_folder()
            link_index = build_link_index([wf.parent, wf, _daily_folder()])
            content, broken_links = _normalize_wikilinks(content, link_index)
        except Exception as exc:  # never block a save on normalization
            logger.warning("Wikilink normalization skipped: %s", exc)

        raw_name = data.get("filename") or f"remote-note-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.md"

        safe_name = _sanitize_filename(raw_name)
        if not safe_name:
            safe_name = f"note-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.md"
        if not safe_name.endswith((".md", ".txt")):
            safe_name += ".md"

        rf = _raw_folder()
        try:
            if archive is None:
                # Legacy path: byte-identical raw-only write.
                target = _publish(rf, safe_name, content)
                logger.info("Raw file created: %s", target.name)
                return jsonify({"status": "ok", "file": target.name, "broken_links": broken_links}), 201

            cap_dir = Path(config["vault_path"]) / config["captures_folder"] / archive
            cap_rel = f"{config['captures_folder']}/{archive}"
            # Names are picked up front and stored in the marker so a retry
            # re-does only the missing writes instead of minting duplicates.
            st = {"fingerprint": fingerprint, "state": "pending",
                  "file": _free_name(cap_dir, Path(safe_name).stem + ".md"),
                  "raw": _free_name(rf, safe_name)}
            marker_dir = Path(config["vault_path"]) / config["meta_folder"] / "capture_ids"
            marker = marker_dir / f"{rid}.json" if rid else None

            def _save_marker() -> None:
                tmp = _write_tmp(marker_dir, json.dumps(st))
                try:
                    os.replace(tmp, marker)
                finally:
                    tmp.unlink(missing_ok=True)

            if rid:
                try:  # reserve: exclusive create, so an existing marker is a replay
                    _publish(marker_dir, marker.name, json.dumps(st), exact=True)
                except FileExistsError:
                    try:
                        st = json.loads(marker.read_text(encoding="utf-8"))
                    except ValueError:
                        logger.error("Corrupt capture marker: %s", marker)
                        raise
                    if st["fingerprint"] != fingerprint:
                        return _err("request_id reused with different payload", 409)

            def _body(deduped: bool) -> Any:
                return jsonify({"status": "ok", "file": st["raw"], "category": archive,
                                "capture_path": f"{cap_rel}/{st['file']}", "deduped": deduped,
                                "broken_links": broken_links})

            if st["state"] == "committed":
                return _body(True), 200
            def _clean(v: Any) -> str:
                return re.sub(r"[\r\n]+", " ", str(v or ""))[:80]

            fm = {"category": archive, "source": _clean(data.get("source")),
                  "captured_at": _clean(data.get("captured_at")),
                  "request_id": rid, "fingerprint": fingerprint}
            doc = "---\n" + "".join(f"{k}: {json.dumps(v)}\n" for k, v in fm.items()) + "---\n\n" + content

            def _mine(path: Path) -> bool:
                try:
                    return f"fingerprint: {json.dumps(fingerprint)}\n" in path.read_text(encoding="utf-8")[:2000]
                except OSError:
                    return False

            # A pre-chosen name may have been taken by someone else since the
            # reservation: only a file carrying our fingerprint counts as ours.
            renamed = False
            for key, folder in (("file", cap_dir), ("raw", rf)):
                if (folder / st[key]).exists() and not _mine(folder / st[key]):
                    st[key] = _free_name(folder, st[key])
                    renamed = True
            if renamed and rid:
                _save_marker()
            if not (cap_dir / st["file"]).exists():
                st["file"] = _publish(cap_dir, st["file"], doc).name
            # ponytail: crash after this raw write but before the commit means a
            # retry rewrites raw (organizer may fold it twice: one duplicate wiki mention).
            if not (rf / st["raw"]).exists():
                st["raw"] = _publish(rf, st["raw"], doc).name
            if rid:
                st["state"] = "committed"
                _save_marker()
        except FileExistsError:
            return _err("Too many notes with this filename", 409)
        except (OSError, ValueError, KeyError) as exc:
            logger.error("Failed to write raw file %s: %s", safe_name, exc)
            return _err("Failed to write file", 500)

        logger.info("Raw file created: %s", st["raw"])
        return _body(False), 201

    # ------------------------------------------------------------------
    # PUT /views/<category>/<name>  — NEXUS-rendered task views
    # Body {content, expected_sha}: sha256 of the file's current bytes ("" if
    # absent). Mismatch -> 409 so a checkbox tick made in Obsidian since the
    # caller last read the file is never overwritten.
    # ------------------------------------------------------------------
    @app.route("/views/<category>/<name>", methods=["PUT"])
    def put_view(category: str, name: str) -> Any:
        auth_error = _check_auth()
        if auth_error is not None:
            return auth_error
        if category not in _CATEGORIES or name not in ("today", "master"):
            return _err("Not found", 404)
        data = request.get_json(silent=True)
        if not isinstance(data, dict) or not isinstance(data.get("content"), str) \
                or not isinstance(data.get("expected_sha"), str):
            return _err("JSON body with 'content' and 'expected_sha' strings required", 400)
        folder = Path(config["vault_path"]) / config["views_folder"] / category
        target = folder / f"{name}.md"
        with _RAW_LOCK:
            try:
                for p in (target, folder, folder.parent):
                    if p.is_symlink():
                        return _err("Refusing symlinked view path", 400)
                folder.mkdir(parents=True, exist_ok=True)
                current = hashlib.sha256(target.read_bytes()).hexdigest() if target.exists() else ""
                if current != data["expected_sha"]:
                    return _err("View changed since read", 409)
                tmp = _write_tmp(folder, data["content"])
                try:
                    os.replace(tmp, target)
                finally:
                    tmp.unlink(missing_ok=True)
            except OSError as exc:
                logger.error("Failed to write view %s/%s: %s", category, name, exc)
                return _err("Failed to write file", 500)
        return jsonify({"status": "ok"})

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

    logger = logging.getLogger("mcp_server")

    # Singleton guard: NEXUS Popen-spawns this on every startup, and a hard-killed
    # NEXUS orphans the child. Werkzeug's SO_REUSEADDR lets orphans stack on the
    # same port silently — dozens of instances were found bound to 8765 at once.
    try:
        import urllib.request
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as resp:
            if resp.status == 200:
                logger.info("MCP server already healthy on port %s — exiting.", port)
                return
    except Exception:
        pass  # nothing responding — proceed to start

    app = create_app(config)
    logger.info("Starting Brain Organizer MCP server on %s:%s", host, port)
    app.run(host=host, port=port, use_reloader=False)


if __name__ == "__main__":
    main()
