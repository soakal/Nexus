"""
Brain Organizer — main processing script.

Reads raw intake files from vault/raw/, uses Claude AI to detect topics and
synthesize wiki files, then cleans up and reports via Hermes.

Usage:
    python brain_organizer.py
    python brain_organizer.py --config /path/to/config.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import shutil
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import anthropic
import httpx
from anthropic.types import TextBlock

CONFIG_PATH = Path(__file__).parent / "config.json"


# ---------------------------------------------------------------------------
# Config & logging
# ---------------------------------------------------------------------------

def load_config(config_path: Path | None = None) -> dict[str, Any]:
    path = config_path or CONFIG_PATH
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def setup_logging(config: dict[str, Any]) -> logging.Logger:
    logs_folder = Path(config["logs_folder"])
    logs_folder.mkdir(parents=True, exist_ok=True)
    log_file = logs_folder / "organizer.log"

    fmt = "%(asctime)s [%(levelname)s] %(message)s"
    handlers: list[logging.Handler] = [
        logging.FileHandler(log_file, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ]
    logging.basicConfig(level=logging.INFO, format=fmt, handlers=handlers, force=True)
    return logging.getLogger("brain_organizer")


# ---------------------------------------------------------------------------
# File helpers
# ---------------------------------------------------------------------------

def compute_sha256(file_path: Path) -> str:
    h = hashlib.sha256()
    with open(file_path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def load_processed(config: dict[str, Any]) -> dict[str, Any]:
    path = Path(config["processed_file"])
    if path.exists():
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    return {}


def save_processed(config: dict[str, Any], processed: dict[str, Any]) -> None:
    """Atomic write via temp-file + os.replace so a kill mid-write never corrupts the ledger."""
    path = Path(config["processed_file"])
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mktemp(dir=path.parent, prefix=".processed_", suffix=".tmp"))
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(processed, fh, indent=2)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Vault scanning — respects failure records and max-attempt cap
# ---------------------------------------------------------------------------

def scan_raw_folder(
    config: dict[str, Any],
    processed: dict[str, Any],
) -> list[tuple[Path, str]]:
    raw_folder = Path(config["vault_path"]) / config["raw_folder"]
    raw_folder.mkdir(parents=True, exist_ok=True)
    max_attempts: int = config.get("max_file_attempts", 5)
    logger = logging.getLogger("brain_organizer")

    results: list[tuple[Path, str]] = []
    for ext in (".md", ".txt"):
        for f in sorted(raw_folder.glob(f"*{ext}")):
            if not f.is_file():
                continue
            sha = compute_sha256(f)
            record = processed.get(sha)
            if record is None:
                results.append((f, sha))
            elif record.get("status") == "failed":
                attempts = record.get("attempts", 0)
                if attempts < max_attempts:
                    results.append((f, sha))
                else:
                    logger.warning(
                        "Skipping %s — exceeded max attempts (%d). Move it out of raw/ to re-enable.",
                        f.name, max_attempts,
                    )
            # else: success record — skip
    return results


# ---------------------------------------------------------------------------
# Backup
# ---------------------------------------------------------------------------

def backup_file(config: dict[str, Any], file_path: Path) -> Path:
    backup_folder = Path(config["vault_path"]) / config["backup_folder"]
    backup_folder.mkdir(parents=True, exist_ok=True)
    # Microsecond timestamp makes same-second collisions virtually impossible
    timestamp = datetime.now(UTC).strftime("%Y-%m-%d_%H-%M-%S_%f")
    backup_path = backup_folder / f"{timestamp}_{file_path.name}"
    shutil.copy2(file_path, backup_path)
    return backup_path


# ---------------------------------------------------------------------------
# Topic name → safe filename (shared with mcp_server.py)
# ---------------------------------------------------------------------------

def sanitize_topic_name(topic: str) -> str:
    safe = re.sub(r"[^\w\s\-]", "", topic)
    safe = re.sub(r"\s+", "-", safe.strip())
    return safe or "Uncategorized"


# ---------------------------------------------------------------------------
# Topic detection (Haiku)
# ---------------------------------------------------------------------------

def detect_topics(
    content: str,
    config: dict[str, Any],
    client: anthropic.Anthropic,
) -> list[str]:
    max_chars: int = config.get("max_file_chars", 50000)
    prompt = (
        "You are a topic classifier. Read the following note and return ONLY a JSON object with no other text.\n"
        "Identify 1 to 5 topic tags that best describe the main subjects covered.\n"
        'Topics should be short title-case labels like "NEXUS", "Home Assistant", "Unraid", "Hermes", '
        '"Voice Memos", "Networking", etc.\n'
        'If no clear topic, use "Uncategorized".\n\n'
        'Return format: {"topics": ["Topic1", "Topic2"]}\n\n'
        f"Note content:\n{content[:max_chars]}"
    )

    message = client.messages.create(
        model=config["haiku_model"],
        max_tokens=256,
        messages=[{"role": "user", "content": prompt}],
    )

    # Select the first TextBlock regardless of block ordering
    block = next((b for b in message.content if isinstance(b, TextBlock)), None)
    if block is None:
        return ["Uncategorized"]
    raw = block.text.strip()
    try:
        data = json.loads(raw)
        topics = data.get("topics", [])
        if not isinstance(topics, list) or not topics:
            return ["Uncategorized"]
        return [str(t) for t in topics[:5]]
    except json.JSONDecodeError:
        return ["Uncategorized"]


# ---------------------------------------------------------------------------
# Wiki synthesis (Sonnet)
# ---------------------------------------------------------------------------

def synthesize_wiki(
    topic: str,
    new_content: str,
    existing_content: str,
    config: dict[str, Any],
    client: anthropic.Anthropic,
) -> str:
    max_chars: int = config.get("max_file_chars", 50000)

    if existing_content:
        prompt = (
            "You are a personal knowledge base curator. Intelligently merge new information into an existing Wiki document.\n\n"
            "Rules:\n"
            "- Never lose any existing information\n"
            "- Add new info where it logically fits within existing sections\n"
            "- Create new sections if needed\n"
            "- Remove duplicates\n"
            "- Clean Markdown with ## headers\n"
            "- No commentary about what you changed\n\n"
            f'Existing Wiki for topic "{topic}":\n'
            f"{existing_content[:max_chars]}\n\n"
            "New information to merge:\n"
            f"{new_content[:max_chars]}\n\n"
            "Return the complete updated Wiki document only."
        )
    else:
        prompt = (
            f'You are a personal knowledge base curator. Create a new Wiki document for the topic "{topic}".\n\n'
            "Rules:\n"
            "- Organize into logical ## sections\n"
            "- Clean Markdown formatting\n"
            "- Thorough but concise\n"
            "- No commentary\n\n"
            "Source material:\n"
            f"{new_content[:max_chars]}\n\n"
            "Return the complete Wiki document only."
        )

    message = client.messages.create(
        model=config["sonnet_model"],
        max_tokens=4096,
        messages=[{"role": "user", "content": prompt}],
    )

    block = next((b for b in message.content if isinstance(b, TextBlock)), None)
    if block is None:
        raise ValueError("Anthropic response contained no text block")
    return block.text.strip()


# ---------------------------------------------------------------------------
# Hermes notifications
# ---------------------------------------------------------------------------

def _get_hermes_host(config: dict[str, Any]) -> str:
    host = os.environ.get("HERMES_HOST") or config.get("hermes_host", "")
    return "" if host == "http://HERMES_HOST_HERE" else host


def send_hermes_notification(
    config: dict[str, Any],
    message: str,
    priority: str = "normal",
    *,
    http_client: httpx.Client | None = None,
) -> None:
    host = _get_hermes_host(config)
    if not host:
        logging.getLogger("brain_organizer").debug("Hermes host not configured — skipping notification")
        return

    payload = {"message": message, "priority": priority}
    try:
        if http_client is not None:
            http_client.post(f"{host}/notify", json=payload)
        else:
            with httpx.Client(timeout=10.0) as c:
                c.post(f"{host}/notify", json=payload)
    except Exception as exc:
        logging.getLogger("brain_organizer").warning("Hermes notification failed: %s", exc)


# ---------------------------------------------------------------------------
# Per-file processing
# ---------------------------------------------------------------------------

def process_file(
    file_path: Path,
    config: dict[str, Any],
    client: anthropic.Anthropic,
    logger: logging.Logger,
) -> list[str]:
    """Backup → detect topics → write wiki (atomic) → delete raw.

    Returns updated topic names.
    Raises on backup failure so caller leaves the file in raw/ for retry.
    Wiki files are written atomically (temp → os.replace) so a mid-write kill
    leaves the prior version intact rather than a corrupt partial file.
    """
    logger.info("Processing: %s", file_path.name)

    backup_path = backup_file(config, file_path)
    logger.info("Backed up to: %s", backup_path)

    content = file_path.read_text(encoding="utf-8")

    topics = detect_topics(content, config, client)
    logger.info("Topics detected: %s", topics)

    wiki_folder = Path(config["vault_path"]) / config["wiki_folder"]
    wiki_folder.mkdir(parents=True, exist_ok=True)

    updated_topics: list[str] = []
    for topic in topics:
        safe_name = sanitize_topic_name(topic)
        wiki_file = wiki_folder / f"{safe_name}.md"
        existing = wiki_file.read_text(encoding="utf-8") if wiki_file.exists() else ""
        wiki_content = synthesize_wiki(topic, content, existing, config, client)

        # Atomic write: temp file → os.replace so partial writes never corrupt
        tmp = Path(tempfile.mktemp(dir=wiki_folder, prefix=f".{safe_name}_", suffix=".tmp"))
        try:
            tmp.write_text(wiki_content, encoding="utf-8")
            os.replace(tmp, wiki_file)
        finally:
            if tmp.exists():
                tmp.unlink(missing_ok=True)

        logger.info("Wiki written: %s", wiki_file)
        updated_topics.append(topic)

    file_path.unlink()
    logger.info("Deleted raw file: %s", file_path.name)

    return updated_topics


# ---------------------------------------------------------------------------
# Main runner (injectable for tests)
# ---------------------------------------------------------------------------

def run(
    config_path: Path | None = None,
    *,
    _client: anthropic.Anthropic | None = None,
    _http_client: httpx.Client | None = None,
    _config: dict[str, Any] | None = None,
) -> int:
    """Run the organizer. Returns 0 on full success, 1 if any file failed."""
    config = _config if _config is not None else load_config(config_path)
    logger = setup_logging(config)

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key and _client is None:
        logger.error("ANTHROPIC_API_KEY environment variable not set")
        return 1

    client = _client or anthropic.Anthropic(api_key=api_key)
    processed = load_processed(config)
    files = scan_raw_folder(config, processed)

    if not files:
        logger.info("Nothing to process")
        return 0

    logger.info("Found %d new file(s) to process", len(files))
    start = time.monotonic()
    success_count = 0
    failed_count = 0
    all_topics: set[str] = set()

    for file_path, sha in files:
        try:
            updated = process_file(file_path, config, client, logger)
            processed[sha] = {
                "filename": file_path.name,
                "timestamp": datetime.now(UTC).isoformat(),
                "topics": updated,
            }
            save_processed(config, processed)
            success_count += 1
            all_topics.update(updated)
        except Exception as exc:
            logger.error("Failed to process %s: %s", file_path.name, exc, exc_info=True)
            failed_count += 1

            # Record failure with attempt count so we stop re-billing on persistent failures
            existing = processed.get(sha, {})
            attempts = existing.get("attempts", 0) + 1
            processed[sha] = {
                "filename": file_path.name,
                "status": "failed",
                "attempts": attempts,
                "timestamp": datetime.now(UTC).isoformat(),
                "error": str(exc)[:500],
            }
            save_processed(config, processed)

            send_hermes_notification(
                config,
                f"🧠 Brain Organizer — ⚠️ Error\nFile: {file_path.name}\nError: {exc}",
                priority="high",
                http_client=_http_client,
            )

    duration = time.monotonic() - start

    if success_count > 0:
        topics_str = ", ".join(sorted(all_topics))
        summary = (
            f"🧠 Brain Organizer — Run complete\n"
            f"✅ Files processed: {success_count}\n"
            f"📝 Topics updated: {topics_str}\n"
            f"⏱ Duration: {duration:.1f}s"
        )
        if failed_count:
            summary += f"\n⚠️ Failed: {failed_count}"
        logger.info(summary)
        send_hermes_notification(config, summary, http_client=_http_client)

    return 1 if failed_count else 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Brain Organizer — process raw vault files")
    parser.add_argument("--config", type=Path, default=None, help="Path to config.json")
    args = parser.parse_args()
    sys.exit(run(args.config))


if __name__ == "__main__":
    main()
