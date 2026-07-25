"""
migrate_daily_pages.py — One-time migration of existing date-stamped wiki
pages (YYYY-MM-DD.md) from wiki root into wiki/daily/.

brain_organizer.py's _daily_note_route now routes NEW daily notes into
wiki/daily/ (kept out of build_wiki_catalog's non-recursive scan, so they
stop cluttering the topic list and the router's prompt context). This script
migrates the pages that already existed before that change.

Scope is a STRICT non-recursive match against wiki_folder root only:
^\\d{4}-\\d{2}-\\d{2}[a-z]?\\.md$ — this deliberately excludes:
  - wiki/processed/*.md (a different folder, untouched)
  - any hand-written note in Brain/ itself (daily-notes plugin default,
    outside wiki_folder entirely)
  - legacy pre-guard pages like "Daily-Operations-Log-2026-07-02.md" or
    "Morning-Briefing-2026-06-28.md" (date is not the whole stem) -- these
    are cross-linked from other pages by their exact title and are left for
    manual handling, not touched by this script

Usage:
    python migrate_daily_pages.py                  # dry run — prints the plan only
    python migrate_daily_pages.py --apply           # prompts for CONFIRM, then moves
    python migrate_daily_pages.py --config <path>    # use a different config.json

The --apply path COPIES each page to _meta/merged-backups/ (same convention
consolidate_wiki.py already established) BEFORE moving it — no files are
ever hard-deleted, and originals are recoverable from the backup copy even
though the move itself already preserves content (belt and suspenders for
irreplaceable personal data).
"""
from __future__ import annotations

import argparse
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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("migrate_daily_pages")

CONFIG_PATH = Path(__file__).parent / "config.json"

# Strict: the ENTIRE stem must be a bare date (optional trailing letter for a
# same-day collision suffix, matching _DAILY_NOTE_STEM_PAT's own shape) --
# NOT brain_organizer._is_daily_note's broader match, which also catches
# "daily"/"briefing"-named files. Those never land at wiki root as
# YYYY-MM-DD.md in the first place (only the Daily-Log.md fallback does),
# so this narrower pattern is the correct, safer scope for a migration
# script touching real existing files.
_DATE_STEM_PAT = re.compile(r"^\d{4}-\d{2}-\d{2}[a-z]?$", re.IGNORECASE)


def load_config(config_path: Path | None = None) -> dict[str, Any]:
    path = config_path or CONFIG_PATH
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def find_daily_pages(wiki_folder: Path) -> list[Path]:
    """Non-recursive: wiki_folder root only, strict date-stem match."""
    return sorted(
        f for f in wiki_folder.glob("*.md") if _DATE_STEM_PAT.match(f.stem)
    )


def print_plan(pages: list[Path], daily_folder: Path) -> None:
    print(f"\nFound {len(pages)} date-stamped page(s) at wiki root to migrate:\n")
    for p in pages:
        print(f"  {p.name}  ->  {daily_folder.name}/{p.name}")
    print()


def _repair_registry(meta_folder: Path, moved: dict[str, Path]) -> None:
    """Point any topics-registry.json entry whose value was one of the OLD
    (wiki-root) paths at its new (daily-folder) path instead. Without this,
    every migrated page leaves a dangling registry entry pointing at a file
    that no longer exists there (found live 2026-07-25 -- update_topics_registry
    used to reconstruct paths from wiki_folder alone, see brain_organizer.py)."""
    registry_path = meta_folder / "topics-registry.json"
    if not registry_path.exists():
        return
    try:
        with open(registry_path, encoding="utf-8") as fh:
            registry: dict[str, str] = json.load(fh)
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Could not read topics-registry.json to repair it: %s", exc)
        return

    repaired = 0
    for topic, path_str in list(registry.items()):
        for name, new_path in moved.items():
            if path_str.endswith(f"wiki\\{name}") or path_str.endswith(f"wiki/{name}"):
                registry[topic] = str(new_path)
                repaired += 1
                break

    if repaired:
        tmp = registry_path.parent / f".topics-registry_{uuid.uuid4().hex}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(registry, fh, indent=2, sort_keys=True)
            os.replace(tmp, registry_path)
            logger.info("Repaired %d topics-registry.json entr(ies) to point at the new path", repaired)
        finally:
            if tmp.exists():
                tmp.unlink(missing_ok=True)


def apply_migration(pages: list[Path], daily_folder: Path, meta_folder: Path) -> None:
    backups_dir = meta_folder / "merged-backups"
    backups_dir.mkdir(parents=True, exist_ok=True)
    daily_folder.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now(UTC).strftime("%Y-%m-%d_%H-%M-%S")
    moved: dict[str, Path] = {}
    for p in pages:
        try:
            dest = daily_folder / p.name
            if dest.exists():
                logger.warning("Skipping %s: %s already exists, needs manual review", p.name, dest)
                continue

            backup_dest = backups_dir / f"{timestamp}_{p.name}"
            shutil.copy2(p, backup_dest)

            shutil.move(str(p), str(dest))
            logger.info("Moved %s -> %s (backed up to %s)", p.name, dest, backup_dest.name)
            moved[p.name] = dest
        except Exception as exc:
            logger.error("Failed to migrate %s: %s — left in place", p.name, exc)
            continue

    _repair_registry(meta_folder, moved)

    # Force a clean catalog rebuild on the next organizer run -- the moved
    # pages are correctly excluded from a fresh build (non-recursive scan),
    # this just avoids carrying stale cached entries for them in the interim.
    catalog_cache = meta_folder / "wiki-catalog.json"
    if catalog_cache.exists():
        catalog_cache.unlink()
        logger.info("Removed stale wiki-catalog.json cache — next run rebuilds fresh")

    print(f"\nMigrated {len(moved)} of {len(pages)} page(s) to {daily_folder}/")
    print(f"Backups of all moved pages are in {backups_dir}/")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Migrate existing date-stamped wiki pages into wiki/daily/."
    )
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually perform the migration. Default is dry-run.",
    )
    args = parser.parse_args()

    try:
        config = load_config(args.config)
    except Exception as exc:
        print(f"ERROR: could not load config: {exc}", file=sys.stderr)
        return 1

    vault_path = Path(config["vault_path"])
    wiki_folder = vault_path / config["wiki_folder"]
    daily_folder = vault_path / config.get("daily_folder", "wiki/daily")
    meta_folder = vault_path / config["meta_folder"]

    if not wiki_folder.exists():
        print(f"ERROR: wiki folder not found: {wiki_folder}", file=sys.stderr)
        return 1

    pages = find_daily_pages(wiki_folder)
    if not pages:
        print("Nothing to migrate — no date-stamped pages found at wiki root.")
        return 0

    print_plan(pages, daily_folder)

    if not args.apply:
        print("(Dry run — pass --apply to migrate. Files are copied to merged-backups/ before being moved, never deleted.)")
        return 0

    print(f"WARNING: This will move {len(pages)} page(s) from {wiki_folder} into {daily_folder}.")
    print("Each page is backed up to _meta/merged-backups/ before being moved.")
    print()
    resp = input("Type CONFIRM to proceed (or anything else to abort): ")
    if resp.strip() != "CONFIRM":
        print("Aborted.")
        return 0

    apply_migration(pages, daily_folder, meta_folder)
    return 0


if __name__ == "__main__":
    sys.exit(main())
