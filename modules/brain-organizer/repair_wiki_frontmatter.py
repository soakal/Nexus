"""repair_wiki_frontmatter.py -- one-time repair of wiki pages damaged by a
whole-page ```markdown wrapper and/or stacked frontmatter blocks (2026-09-28).

    python repair_wiki_frontmatter.py            # dry run: unified diff per page
    python repair_wiki_frontmatter.py --apply    # diff, CONFIRM, backup, rewrite

Scope: wiki/*.md + wiki/daily/*.md, non-recursive (wiki/processed/ is
out of scope, same as the rest of the codebase). Each page is copied to
_meta/merged-backups/<ts>_<name> before being rewritten (same convention as
migrate_daily_pages.py / consolidate_wiki.py).
"""
from __future__ import annotations

import argparse
import difflib
import os
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path

from brain_organizer import _make_temp_path, _normalize_wiki_page, load_config


def main() -> int:
    ap = argparse.ArgumentParser(description="Repair fence-wrapped / stacked-frontmatter wiki pages.")
    ap.add_argument("--config", type=Path, default=None)
    ap.add_argument("--apply", action="store_true", help="Actually rewrite. Default is dry-run.")
    args = ap.parse_args()

    if (Path(__file__).parent / ".organizer.lock").exists():
        print("ERROR: .organizer.lock present -- organizer may be running. Retry later.", file=sys.stderr)
        return 1

    config = load_config(args.config)
    vault = Path(config["vault_path"])
    pages = sorted((vault / config["wiki_folder"]).glob("*.md")) + sorted(
        (vault / config["daily_folder"]).glob("*.md")
    )

    todo: list[tuple[Path, str]] = []
    for p in pages:
        old = p.read_text(encoding="utf-8")
        new = _normalize_wiki_page(old)
        if new.lstrip("﻿").startswith("```"):
            print(f"NEEDS MANUAL REVIEW (ambiguous wrapper, left untouched): {p.name}")
        if new == old:
            continue
        # data-loss guards: only fence/delimiter/duplicate-tag lines may go
        if _normalize_wiki_page(new) != new or len(new) < 0.95 * len(old):
            print(f"REFUSING {p.name}: non-idempotent or shrank >5% -- fix by hand", file=sys.stderr)
            continue
        todo.append((p, new))
        sys.stdout.writelines(
            difflib.unified_diff(old.splitlines(True), new.splitlines(True), f"a/{p.name}", f"b/{p.name}", n=2)
        )
        print()

    print(f"\n{len(todo)} page(s) would change.")
    if not args.apply or not todo:
        print("(Dry run -- pass --apply to rewrite. Originals are copied to _meta/merged-backups/ first.)")
        return 0
    if input("Type CONFIRM to proceed: ").strip() != "CONFIRM":
        print("Aborted.")
        return 0

    backups = vault / config["meta_folder"] / "merged-backups"
    backups.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(UTC).strftime("%Y-%m-%d_%H-%M-%S")
    for p, new in todo:
        shutil.copy2(p, backups / f"{ts}_{p.name}")
        tmp = _make_temp_path(p.parent, f".{p.stem}_", ".tmp")
        try:
            tmp.write_text(new, encoding="utf-8")
            os.replace(tmp, p)
        finally:
            tmp.unlink(missing_ok=True)
        print(f"repaired {p.name}")
    print(f"\nBackups: {backups}/{ts}_*.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
