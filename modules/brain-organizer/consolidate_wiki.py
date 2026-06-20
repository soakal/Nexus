"""
consolidate_wiki.py — One-time/occasional dedup tool for the Brain Organizer wiki.

Finds near-duplicate wiki pages (similar titles, e.g. "Financial Forecast" /
"Financial Forecasting"), proposes a merge plan, and optionally applies it.

Usage:
    python consolidate_wiki.py                  # dry run — writes plan, prints summary
    python consolidate_wiki.py --apply          # prompts for CONFIRM, then merges
    python consolidate_wiki.py --config <path>  # use a different config.json

The --apply path MOVES absorbed pages to _meta/merged-backups/ — no files are
ever hard-deleted.
"""
from __future__ import annotations

import argparse
import difflib
import json
import logging
import os
import re
import shutil
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import anthropic

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ---------------------------------------------------------------------------
# Paths & defaults
# ---------------------------------------------------------------------------

CONFIG_PATH = Path(__file__).parent / "config.json"

# Clustering thresholds (lower than the runtime guard — a human reviews before apply)
_CLUSTER_SEQ_THRESHOLD = 0.75
_CLUSTER_JACCARD_THRESHOLD = 0.65

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("consolidate_wiki")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_config(config_path: Path | None = None) -> dict[str, Any]:
    path = config_path or CONFIG_PATH
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


# ---------------------------------------------------------------------------
# Title normalisation (mirrors brain_organizer._normalize_title)
# ---------------------------------------------------------------------------

def _normalize_title(s: str) -> str:
    """Lower-case, strip punctuation, collapse whitespace, trim common suffixes."""
    s = s.lower().strip()
    s = re.sub(r"[^\w\s]", "", s)
    s = re.sub(r"\s+", " ", s).strip()

    # Strip common suffixes word-by-word (longest-first so "tion" beats "ion" beats "s").
    _SUFFIXES = ("tion", "ment", "ing", "ion", "ed", "es", "s")
    words: list[str] = []
    for word in s.split():
        for suf in _SUFFIXES:
            if word.endswith(suf) and len(word) - len(suf) >= 3:
                word = word[: -len(suf)]
                break
        words.append(word)
    return " ".join(words)


# ---------------------------------------------------------------------------
# Catalog building
# ---------------------------------------------------------------------------

def _extract_page_entry(f: Path) -> dict[str, Any]:
    """Parse a single wiki page file into a catalog entry dict."""
    try:
        text = f.read_text(encoding="utf-8")
    except Exception:
        return {
            "title": f.stem,
            "filename": f.name,
            "path_str": str(f),
            "headers": "",
            "summary": "",
            "chars": 0,
        }

    lines = text.splitlines()
    chars = len(text)

    # Title: first line starting with exactly "# "
    title = f.stem
    h1_idx = 0
    for i, line in enumerate(lines):
        if line.startswith("# ") and not line.startswith("## "):
            title = line[2:].strip()
            h1_idx = i
            break

    # Headers: lines starting with "## "
    raw_headers: list[str] = []
    for line in lines:
        if line.startswith("## "):
            raw_headers.append(line[3:].strip())
            if len(raw_headers) >= 10:
                break
    headers_joined = " | ".join(raw_headers)

    # Summary: first real prose after H1, skip metadata / rules / headers
    _META_RE = re.compile(r"^\*\*[\w\s]+:\*\*|^>\s*\*\*[\w\s]+:\*\*")
    _RULE_RE = re.compile(r"^[-*_]{3,}$")
    summary_parts: list[str] = []
    for line in lines[h1_idx + 1:]:
        stripped = line.strip()
        if not stripped:
            if summary_parts:
                break
            continue
        if stripped.startswith("#"):
            continue
        if _RULE_RE.match(stripped):
            continue
        if _META_RE.match(stripped):
            continue
        summary_parts.append(stripped)

    summary_raw = " ".join(summary_parts)[:300]

    return {
        "title": title,
        "filename": f.name,
        "path_str": str(f),
        "headers": headers_joined,
        "summary": summary_raw,
        "chars": chars,
    }


def build_catalog(wiki_folder: Path) -> list[dict[str, Any]]:
    """Build an in-memory catalog of all .md pages in wiki_folder."""
    pages: list[dict[str, Any]] = []
    for f in sorted(wiki_folder.glob("*.md")):
        try:
            entry = _extract_page_entry(f)
            pages.append(entry)
        except Exception as exc:
            logger.warning("catalog: skipping %s: %s", f.name, exc)
    pages.sort(key=lambda p: p["title"].lower())
    return pages


# ---------------------------------------------------------------------------
# Clustering
# ---------------------------------------------------------------------------

def _seq_ratio(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a, b).ratio()


def _jaccard(a: str, b: str) -> float:
    wa = set(a.split())
    wb = set(b.split())
    if not wa or not wb:
        return 0.0
    union = wa | wb
    if not union:
        return 0.0
    return len(wa & wb) / len(union)


def build_clusters(pages: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """
    Greedy single-linkage clustering on normalized title similarity.
    Returns only clusters with >= 2 members.
    """
    norms = [_normalize_title(p["title"]) for p in pages]
    clustered = [False] * len(pages)
    clusters: list[list[dict[str, Any]]] = []

    for i, page_i in enumerate(pages):
        if clustered[i]:
            continue
        cluster = [page_i]
        clustered[i] = True
        norm_i = norms[i]
        for j in range(i + 1, len(pages)):
            if clustered[j]:
                continue
            norm_j = norms[j]
            seq = _seq_ratio(norm_i, norm_j)
            if seq >= _CLUSTER_SEQ_THRESHOLD:
                cluster.append(pages[j])
                clustered[j] = True
                continue
            # Word-Jaccard check only if both titles have > 1 word
            if " " in norm_i and " " in norm_j:
                jac = _jaccard(norm_i, norm_j)
                if jac >= _CLUSTER_JACCARD_THRESHOLD:
                    cluster.append(pages[j])
                    clustered[j] = True

        if len(cluster) >= 2:
            clusters.append(cluster)

    return clusters


def _choose_canonical(cluster: list[dict[str, Any]]) -> dict[str, Any]:
    """
    Pick canonical = most chars (most content).
    Tie-break: shortest title, then alphabetical.
    """
    return max(
        cluster,
        key=lambda p: (p["chars"], -len(p["title"]), "".join(reversed(p["title"].lower()))),
    )


# ---------------------------------------------------------------------------
# Atomic write helper
# ---------------------------------------------------------------------------

def _make_temp_path(directory: Path, prefix: str, suffix: str) -> Path:
    return directory / f"{prefix}{uuid.uuid4().hex}{suffix}"


def _atomic_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = _make_temp_path(path.parent, ".tmp_", ".json")
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Plan builder
# ---------------------------------------------------------------------------

def build_plan(
    clusters: list[list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """
    Build a list of merge group dicts:
    {
        "canonical": str,
        "canonical_path": str,
        "canonical_chars": int,
        "pages": [{"title", "filename", "path_str", "chars"}, ...],
        "reason": str,
    }
    """
    groups: list[dict[str, Any]] = []
    for cluster in clusters:
        canonical = _choose_canonical(cluster)
        absorbed = [p for p in cluster if p["filename"] != canonical["filename"]]
        titles = [p["title"] for p in cluster]
        groups.append(
            {
                "canonical": canonical["title"],
                "canonical_path": canonical["path_str"],
                "canonical_chars": canonical["chars"],
                "pages": [
                    {
                        "title": p["title"],
                        "filename": p["filename"],
                        "path_str": p["path_str"],
                        "chars": p["chars"],
                    }
                    for p in cluster
                ],
                "absorbed": [
                    {
                        "title": p["title"],
                        "filename": p["filename"],
                        "path_str": p["path_str"],
                        "chars": p["chars"],
                    }
                    for p in absorbed
                ],
                "reason": "similar titles: " + ", ".join(f'"{t}"' for t in titles),
            }
        )
    return groups


# ---------------------------------------------------------------------------
# Dry-run output
# ---------------------------------------------------------------------------

def print_summary(groups: list[dict[str, Any]]) -> None:
    total_pages = sum(len(g["pages"]) for g in groups)
    print(f"\nFound {len(groups)} merge group(s) affecting {total_pages} page(s):\n")
    for g in groups:
        absorbed_titles = [p["title"] for p in g["absorbed"]]
        print(f"  [{g['canonical']}]  <=  {absorbed_titles}")
        print(f"    reason: {g['reason']}")
        print()


# ---------------------------------------------------------------------------
# Merge via Sonnet
# ---------------------------------------------------------------------------

def _call_api(
    model: str,
    messages: list[dict[str, Any]],
    max_tokens: int,
    client: anthropic.Anthropic,
) -> tuple[str, str]:
    """Minimal Anthropic call — no retry/fallback (consolidation is interactive)."""
    import anthropic as _anthropic
    msg = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        messages=messages,  # type: ignore[arg-type]
    )
    from anthropic.types import TextBlock
    block = next((b for b in msg.content if isinstance(b, TextBlock)), None)
    if block is None:
        raise ValueError("Anthropic response contained no text block")
    return block.text.strip(), (msg.stop_reason or "")


def merge_pages(
    canonical_title: str,
    canonical_content: str,
    absorbed_contents: list[tuple[str, str]],  # (title, content)
    config: dict[str, Any],
    client: anthropic.Anthropic,
) -> str:
    """Iteratively merge absorbed pages into the canonical page via Sonnet."""
    max_chars: int = config.get("max_file_chars", 50000)
    max_tokens: int = config.get("sonnet_max_tokens", 16384)
    model: str = config.get("sonnet_model", "claude-sonnet-4-6")

    accumulated = canonical_content
    for absorbed_title, absorbed_content in absorbed_contents:
        prompt = (
            f'You are a personal knowledge base curator merging two wiki pages about the same subject.\n\n'
            f'CANONICAL PAGE TITLE: "{canonical_title}"\n\n'
            "Rules:\n"
            "- Never lose any existing information from either page\n"
            "- Add new info where it logically fits\n"
            "- Remove exact duplicates\n"
            "- Use clean Markdown with ## headers\n"
            "- No commentary about what you changed\n"
            "- Keep the canonical page's H1 title\n\n"
            f"EXISTING (canonical) content:\n{accumulated[:max_chars]}\n\n"
            f'CONTENT FROM ABSORBED PAGE "{absorbed_title}":\n{absorbed_content[:max_chars]}\n\n'
            "Return the complete merged wiki document only."
        )

        text, stop_reason = _call_api(
            model,
            [{"role": "user", "content": prompt}],
            max_tokens,
            client,
        )

        if stop_reason == "max_tokens":
            raise ValueError(
                f"Merge of '{absorbed_title}' into '{canonical_title}' hit max_tokens "
                f"({max_tokens}) — skipping this group to prevent data loss."
            )

        accumulated = text

    return accumulated


# ---------------------------------------------------------------------------
# Apply path
# ---------------------------------------------------------------------------

def apply_groups(
    groups: list[dict[str, Any]],
    meta_folder: Path,
    config: dict[str, Any],
    client: anthropic.Anthropic,
) -> None:
    backups_dir = meta_folder / "merged-backups"
    backups_dir.mkdir(parents=True, exist_ok=True)

    registry_path = meta_folder / "topics-registry.json"
    registry: dict[str, str] = {}
    if registry_path.exists():
        try:
            with open(registry_path, encoding="utf-8") as fh:
                registry = json.load(fh)
        except (json.JSONDecodeError, OSError):
            registry = {}

    merged_count = 0
    moved_count = 0

    for g in groups:
        canonical_title = g["canonical"]
        canonical_path = Path(g["canonical_path"])
        absorbed = g["absorbed"]

        logger.info("Merging group: %s <= %s", canonical_title, [a["title"] for a in absorbed])

        try:
            canonical_content = canonical_path.read_text(encoding="utf-8") if canonical_path.exists() else ""
            absorbed_contents: list[tuple[str, str]] = []
            for a in absorbed:
                a_path = Path(a["path_str"])
                a_content = a_path.read_text(encoding="utf-8") if a_path.exists() else ""
                absorbed_contents.append((a["title"], a_content))

            merged = merge_pages(canonical_title, canonical_content, absorbed_contents, config, client)

            # Atomic write of merged result to canonical path
            tmp = _make_temp_path(canonical_path.parent, f".{canonical_path.stem}_", ".tmp")
            try:
                tmp.write_text(merged, encoding="utf-8")
                os.replace(tmp, canonical_path)
            finally:
                if tmp.exists():
                    try:
                        tmp.unlink(missing_ok=True)
                    except Exception:
                        pass

            logger.info("Wrote merged content to %s", canonical_path.name)
            merged_count += 1

            # Move absorbed pages to backups (never delete)
            timestamp = datetime.now(UTC).strftime("%Y-%m-%d_%H-%M-%S")
            for a in absorbed:
                a_path = Path(a["path_str"])
                if a_path.exists():
                    backup_name = f"{timestamp}_{a_path.name}"
                    backup_dest = backups_dir / backup_name
                    shutil.move(str(a_path), str(backup_dest))
                    logger.info("Moved absorbed page %s -> merged-backups/%s", a_path.name, backup_name)
                    moved_count += 1

                # Update registry: point absorbed title to canonical path, or remove
                if a["title"] in registry:
                    registry[a["title"]] = str(canonical_path)

        except Exception as exc:
            logger.error("Failed to merge group '%s': %s — skipping, originals untouched", canonical_title, exc)
            continue

    # Write updated registry atomically
    if registry:
        _atomic_write_json(registry_path, registry)
        logger.info("Updated topics-registry.json")

    print(f"\nMerged {merged_count} group(s), moved {moved_count} page(s) to merged-backups/")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Find and optionally merge near-duplicate wiki pages."
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Path to config.json (default: same directory as this script)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually perform the merge (moves absorbed pages to merged-backups/). Default is dry-run.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Explicit dry-run flag (default behavior; no-op alongside omitting --apply).",
    )
    args = parser.parse_args()

    # Load config
    try:
        config = load_config(args.config)
    except Exception as exc:
        print(f"ERROR: could not load config: {exc}", file=sys.stderr)
        return 1

    vault_path = Path(config["vault_path"])
    wiki_folder = vault_path / config["wiki_folder"]
    meta_folder = vault_path / config["meta_folder"]

    if not wiki_folder.exists():
        print(f"ERROR: wiki folder not found: {wiki_folder}", file=sys.stderr)
        return 1

    # Build catalog
    print(f"Scanning wiki pages in: {wiki_folder}")
    catalog = build_catalog(wiki_folder)
    print(f"Found {len(catalog)} wiki page(s).")

    # Cluster
    clusters = build_clusters(catalog)
    groups = build_plan(clusters)

    # Write consolidation-plan.json
    plan_path = meta_folder / "consolidation-plan.json"
    plan_data = {
        "generated_at": datetime.now(UTC).isoformat(),
        "clusters": [
            {
                "canonical": g["canonical"],
                "pages": [p["title"] for p in g["pages"]],
                "reason": g["reason"],
            }
            for g in groups
        ],
    }
    try:
        meta_folder.mkdir(parents=True, exist_ok=True)
        _atomic_write_json(plan_path, plan_data)
        print(f"Consolidation plan written to: {plan_path}")
    except Exception as exc:
        logger.warning("Could not write consolidation-plan.json: %s", exc)

    if not groups:
        print("\nNothing to consolidate — no near-duplicate pages found.")
        return 0

    print_summary(groups)

    if not args.apply:
        print("(Dry run — pass --apply to merge. Files will be MOVED to merged-backups/, never deleted.)")
        return 0

    # --- Apply path ---
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        print("ERROR: ANTHROPIC_API_KEY environment variable is not set.", file=sys.stderr)
        return 1

    total_absorbed = sum(len(g["absorbed"]) for g in groups)
    print(f"WARNING: This will merge {len(groups)} group(s), moving {total_absorbed} page(s) to merged-backups/.")
    print("Files are NEVER deleted — only moved. You can restore them manually if needed.")
    print()

    resp = input("Type CONFIRM to proceed (or anything else to abort): ")
    if resp.strip() != "CONFIRM":
        print("Aborted.")
        return 0

    client = anthropic.Anthropic(api_key=api_key)
    apply_groups(groups, meta_folder, config, client)
    return 0


if __name__ == "__main__":
    sys.exit(main())
