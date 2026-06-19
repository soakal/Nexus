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
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import anthropic
import httpx
from anthropic.types import TextBlock

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

CONFIG_PATH = Path(__file__).parent / "config.json"

# Anthropic errors that warrant a retry before falling back to OpenRouter
_RETRYABLE_ERRORS = (
    anthropic.APITimeoutError,
    anthropic.RateLimitError,
)


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

    # Windows consoles default to cp1252 which can't encode emoji in log output.
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

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

def _make_temp_path(directory: Path, prefix: str, suffix: str) -> Path:
    """Return a unique temp path without the deprecated tempfile.mktemp."""
    return directory / f"{prefix}{uuid.uuid4().hex}{suffix}"


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
    tmp = _make_temp_path(path.parent, ".processed_", ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(processed, fh, indent=2)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Topics registry (_meta/topics-registry.json)
# ---------------------------------------------------------------------------

def update_topics_registry(
    config: dict[str, Any],
    topics: list[str],
    wiki_folder: Path,
) -> None:
    """Upsert topic → wiki file path in _meta/topics-registry.json (atomic write)."""
    meta_folder = Path(config["vault_path"]) / config["meta_folder"]
    meta_folder.mkdir(parents=True, exist_ok=True)
    registry_path = meta_folder / "topics-registry.json"

    registry: dict[str, str] = {}
    if registry_path.exists():
        try:
            with open(registry_path, encoding="utf-8") as fh:
                registry = json.load(fh)
        except (json.JSONDecodeError, OSError):
            pass

    for topic in topics:
        safe_name = sanitize_topic_name(topic)
        registry[topic] = str(wiki_folder / f"{safe_name}.md")

    tmp = _make_temp_path(meta_folder, ".topics-registry_", ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(registry, fh, indent=2, sort_keys=True)
        os.replace(tmp, registry_path)
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

    backup_folder = Path(config["vault_path"]) / config["backup_folder"]
    results: list[tuple[Path, str]] = []
    for ext in (".md", ".txt"):
        for f in sorted(raw_folder.rglob(f"*{ext}")):
            if not f.is_file():
                continue
            # Skip anything already inside the backups subfolder
            try:
                f.relative_to(backup_folder)
                continue
            except ValueError:
                pass
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
# Core API call — Anthropic with retry + OpenRouter fallback
# ---------------------------------------------------------------------------

def _call_api(
    model: str,
    messages: list[dict[str, Any]],
    max_tokens: int,
    config: dict[str, Any],
    client: anthropic.Anthropic,
    *,
    max_retries: int = 3,
) -> tuple[str, str]:
    """
    Call Anthropic with exponential-backoff retry (3 attempts), then fall back
    to OpenRouter on persistent failure.

    Returns (text, stop_reason). Raises RuntimeError if both providers fail.
    """
    logger = logging.getLogger("brain_organizer")
    last_exc: Exception | None = None

    for attempt in range(1, max_retries + 1):
        try:
            msg = client.messages.create(
                model=model,
                max_tokens=max_tokens,
                messages=messages,  # type: ignore[arg-type]
            )
            block = next((b for b in msg.content if isinstance(b, TextBlock)), None)
            if block is None:
                raise ValueError("Anthropic response contained no text block")
            return block.text.strip(), (msg.stop_reason or "")
        except _RETRYABLE_ERRORS as exc:
            last_exc = exc
            wait = 2 ** attempt
            logger.warning(
                "Anthropic %s on attempt %d/%d — retrying in %ds",
                type(exc).__name__, attempt, max_retries, wait,
            )
            if attempt < max_retries:
                time.sleep(wait)
        except anthropic.APIStatusError as exc:
            if exc.status_code >= 500:
                last_exc = exc
                wait = 2 ** attempt
                logger.warning(
                    "Anthropic HTTP %d on attempt %d/%d — retrying in %ds",
                    exc.status_code, attempt, max_retries, wait,
                )
                if attempt < max_retries:
                    time.sleep(wait)
            elif exc.status_code == 400 and "usage limits" in str(exc).lower():
                # Hard usage cap — no point retrying Anthropic; go straight to OpenRouter
                last_exc = exc
                logger.warning("Anthropic usage limit reached — skipping retries, falling back to OpenRouter")
                break
            else:
                raise

    # All Anthropic retries exhausted — fall back to OpenRouter
    logger.warning("Anthropic API exhausted after %d retries — falling back to OpenRouter", max_retries)
    or_key = os.environ.get("OPENROUTER_API_KEY", "")
    if not or_key:
        raise RuntimeError(
            f"Anthropic API failed ({last_exc}) and OPENROUTER_API_KEY is not set — no fallback available"
        ) from last_exc

    try:
        # OpenRouter uses the OpenAI-compatible chat/completions endpoint.
        # Prefix with "anthropic/" and strip trailing date suffixes (-YYYYMMDD)
        # that OpenRouter doesn't recognise (e.g. claude-haiku-4-5-20251001).
        or_model = "anthropic/" + re.sub(r"-\d{8}$", "", model)
        resp = httpx.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {or_key}",
                "Content-Type": "application/json",
            },
            json={"model": or_model, "max_tokens": max_tokens, "messages": messages},
            timeout=60.0,
        )
        resp.raise_for_status()
        data = resp.json()
        text = (data["choices"][0]["message"]["content"] or "").strip()
        finish_reason = data["choices"][0].get("finish_reason", "")
        logger.info("OpenRouter fallback succeeded (finish_reason=%s)", finish_reason)
        return text, finish_reason
    except Exception as or_exc:
        raise RuntimeError(
            f"Both Anthropic ({last_exc}) and OpenRouter ({or_exc}) failed"
        ) from or_exc


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

    text, _ = _call_api(
        config["haiku_model"],
        [{"role": "user", "content": prompt}],
        256,
        config,
        client,
    )
    try:
        # Haiku (and some other models) wrap JSON in markdown code fences — strip them.
        cleaned = text.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[-1]
            cleaned = cleaned.rsplit("```", 1)[0].strip()
        data = json.loads(cleaned)
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

    # 8192 tokens gives room for large wiki merges; still check for truncation.
    max_tokens: int = config.get("sonnet_max_tokens", 8192)
    text, stop_reason = _call_api(
        config["sonnet_model"],
        [{"role": "user", "content": prompt}],
        max_tokens,
        config,
        client,
    )

    if stop_reason == "max_tokens":
        raise ValueError(
            f"Synthesis for topic '{topic}' hit max_tokens ({max_tokens}) — "
            "skipping write to prevent data loss. Increase sonnet_max_tokens in config.json."
        )

    return text


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
    """
    Backup → detect topics → synthesize ALL wikis first → write all atomically → update registry → delete raw.

    Phase 1 (synthesis) must fully succeed before Phase 2 (writes) begins.
    This prevents partial application: if topic 2 of 3 fails synthesis, topic 1's
    wiki is untouched and the raw file stays for a clean retry.
    """
    raw_folder = Path(config["vault_path"]) / config["raw_folder"]
    display_name = file_path.relative_to(raw_folder) if file_path.is_relative_to(raw_folder) else file_path.name
    logger.info("Processing: %s", display_name)

    backup_path = backup_file(config, file_path)
    logger.info("Backed up to: %s", backup_path)

    content = file_path.read_text(encoding="utf-8")
    topics = detect_topics(content, config, client)
    logger.info("Topics detected: %s", topics)

    wiki_folder = Path(config["vault_path"]) / config["wiki_folder"]
    wiki_folder.mkdir(parents=True, exist_ok=True)

    # Phase 1: synthesize ALL topics into memory before touching the filesystem
    topic_results: list[tuple[str, str, str]] = []  # (topic, safe_name, wiki_content)
    for topic in topics:
        safe_name = sanitize_topic_name(topic)
        wiki_file = wiki_folder / f"{safe_name}.md"
        existing = wiki_file.read_text(encoding="utf-8") if wiki_file.exists() else ""
        wiki_content = synthesize_wiki(topic, content, existing, config, client)
        topic_results.append((topic, safe_name, wiki_content))
        logger.info("Synthesis complete for topic: %s", topic)

    # Phase 2: all synthesized — write each wiki atomically
    updated_topics: list[str] = []
    for topic, safe_name, wiki_content in topic_results:
        wiki_file = wiki_folder / f"{safe_name}.md"
        tmp = _make_temp_path(wiki_folder, f".{safe_name}_", ".tmp")
        try:
            tmp.write_text(wiki_content, encoding="utf-8")
            os.replace(tmp, wiki_file)
        finally:
            if tmp.exists():
                tmp.unlink(missing_ok=True)
        logger.info("Wiki written: %s", wiki_file)
        updated_topics.append(topic)

    # Phase 3: update _meta/topics-registry.json
    update_topics_registry(config, updated_topics, wiki_folder)

    # Phase 4: raw file deleted only after all writes confirmed
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

    lock_path = Path(__file__).parent / ".organizer.lock"
    if lock_path.exists():
        try:
            pid = int(lock_path.read_text().strip())
            import psutil  # type: ignore[import-untyped]
            if psutil.pid_exists(pid):
                print(f"Brain Organizer already running (PID {pid}) — skipping.", flush=True)
                sys.exit(0)
        except Exception:
            pass  # stale lock — proceed

    lock_path.write_text(str(os.getpid()))
    try:
        sys.exit(run(args.config))
    finally:
        lock_path.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
