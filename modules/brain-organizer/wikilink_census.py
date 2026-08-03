"""
wikilink_census.py -- read-only broken-wikilink census for spec #1
(`docs/brain-organizer-wikilink-router-fixes-spec.md`) §0.1, evaluating §6.5
criteria 26-28 against real vault directories.

READ-ONLY CONTRACT (see the council step's RISK #1): this script only ever
opens files under the given vault directories for READING. The one file it
writes is the repo-local evidence markdown (default
`modules/brain-organizer/docs/wikilink-census-evidence.md`), plus its own
stdout. `brain_organizer.build_wiki_catalog` normally writes an incremental
cache into `<vault>/_meta/wiki-catalog.json` -- this script redirects that
cache to a throwaway `tempfile.TemporaryDirectory()` on every call (see
`_catalog_for`) specifically so neither vault directory is ever opened for
writing, at the cost of never reusing brain_organizer's real on-disk cache.

Categories (spec §0.1's three-row table, letters as used in the council
step and cross-referenced against crit 26/27/28):

  * Category A -- link target is the exact `title` of a real catalog page,
    but not a real filename stem. This is defect A (the title-vs-filename
    namespace bug this pipeline is meant to fix). crit26.
  * Category D -- broken link that isn't A or C: typically an already-
    hyphenated slug with no matching file (the one-time `processed/` rename
    migration, explicitly OUT OF SCOPE per spec §7). crit27.
  * Category C -- link target looks like a `wiki/daily/` date-stem page,
    per the same `_is_daily_note` recognizer `_daily_note_route` uses --
    outside the catalog's non-recursive glob, a known narrow gap (§7).
    (Not to be confused with spec §0's unlettered "defect C", the BOM/H1
    mis-titling bug -- that one is measured separately, by crit28 below.)

Link resolution reuses `brain_organizer.WIKILINK_PAT` (the module's own
wikilink grammar) and mirrors the exact by_stem/by_title index-construction
order `_defuse_unknown_wikilinks` uses (filename-stem match first, then
catalog-title match, both case-sensitive then case-insensitive) -- see
`_build_indexes` below. It does NOT reuse `_defuse_unknown_wikilinks`
itself: that function REWRITES text (including a fuzzy `find_similar_page`
step meant for repair, not classification) and this script must never
write to a vault file, so only its resolution *order* is mirrored, not its
rewriting body.

crit28 (NEXUS.md title=="NEXUS", 0 title/H1 mismatches) is checked by
finding each page's real first H1 line the same way `_extract_page_entry`
does -- skip past real frontmatter via `brain_organizer._find_frontmatter`
(reused directly, not re-derived), then the first line starting with "# "
and not "## " -- and comparing its text to the catalog's parsed title. A
page with no H1 at all is never flagged (a stem-fallback title on a
title-less page is correct behavior, not a defect). This reuses the same
frontmatter helper the production parser uses instead of re-deriving
frontmatter detection.

Usage:
    venv/Scripts/python.exe modules/brain-organizer/wikilink_census.py \\
        --before <vault-dir> [--after <vault-dir>] [--out <evidence.md>]

If --after is omitted, the census runs in single-dir mode against --before
alone (e.g. the cycle-39 supervised-run temp copy no longer being on disk)
-- crit26/27 degrade to "reported, non-regressing" (nothing committed yet
to regress against) and crit26 additionally requires the current
category-A count to be exactly 0.
"""
from __future__ import annotations

import argparse
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))
import brain_organizer as bo  # noqa: E402 -- must follow the sys.path insert above

MODULE_ROOT = Path(__file__).parent
DEFAULT_EVIDENCE_PATH = MODULE_ROOT / "docs" / "wikilink-census-evidence.md"

# Spec §0.1's own baseline (269 pages, measured 2026-08-02) -- reported for
# context only. Per RISK #4, these are NEVER used as pass/fail thresholds;
# a shrunk/grown vault since spec-writing time means raw counts naturally
# diverge from these, and the evidence doc says so explicitly.
_SPEC_TIME_BASELINE = {"pages": 269, "cat_a": 112, "cat_d": 888, "cat_c": 14}


# ---------------------------------------------------------------------------
# Catalog construction (read-only wrapper around build_wiki_catalog)
# ---------------------------------------------------------------------------

def _catalog_for(wiki_folder: Path) -> list[dict[str, Any]]:
    """Build a wiki catalog for *wiki_folder* without writing anything into
    the real vault. Reuses `build_wiki_catalog` (the production catalog
    builder -- same `_extract_page_entry` per-page parser, same non-
    recursive `glob("*.md")` scan) but redirects its cache file to a
    throwaway temp directory each call so the vault's own `_meta/` is never
    opened for writing.
    """
    with tempfile.TemporaryDirectory(prefix="wikilink_census_meta_") as tmp:
        return bo.build_wiki_catalog(wiki_folder, Path(tmp))


def _build_indexes(
    catalog: list[dict[str, Any]],
) -> tuple[set[str], dict[str, str], dict[str, str], dict[str, str]]:
    """by_stem (set), by_stem_ci, by_title, by_title_ci -- mirrors the
    index-construction order in `_defuse_unknown_wikilinks` (filename stem
    is the canonical target space; title is first-wins per catalog sort
    order, matching that function's own comment at brain_organizer.py:770).
    """
    by_stem: set[str] = set()
    by_title: dict[str, str] = {}
    for p in catalog:
        filename = p.get("filename")
        title = p.get("title")
        if not filename:
            continue
        stem = Path(filename).stem
        by_stem.add(stem)
        if title:
            by_title.setdefault(title, stem)
    by_stem_ci = {s.lower(): s for s in by_stem}
    by_title_ci = {t.lower(): s for t, s in by_title.items()}
    return by_stem, by_stem_ci, by_title, by_title_ci


# ---------------------------------------------------------------------------
# Per-page / per-vault census
# ---------------------------------------------------------------------------

@dataclass
class PageCensus:
    filename: str
    title: str
    resolved: int = 0
    cat_a: int = 0
    cat_c: int = 0
    cat_d: int = 0

    @property
    def total_links(self) -> int:
        return self.resolved + self.cat_a + self.cat_c + self.cat_d


@dataclass
class VaultCensus:
    vault_dir: Path
    wiki_dir: Path
    pages: dict[str, PageCensus] = field(default_factory=dict)
    catalog_by_filename: dict[str, dict[str, Any]] = field(default_factory=dict)
    daily_count: int = 0       # context only -- never folded into crit26/27/28
    processed_count: int = 0   # context only -- never folded into crit26/27/28

    @property
    def totals(self) -> dict[str, int]:
        out = {"total_links": 0, "resolved": 0, "cat_a": 0, "cat_c": 0, "cat_d": 0}
        for p in self.pages.values():
            out["total_links"] += p.total_links
            out["resolved"] += p.resolved
            out["cat_a"] += p.cat_a
            out["cat_c"] += p.cat_c
            out["cat_d"] += p.cat_d
        return out

    def title_h1_mismatches(self) -> list[str]:
        """Read-only crit28 check: filenames whose production-parsed
        catalog title (`_extract_page_entry`'s `title` field) does not
        match the file's own real first H1 line, found the same way
        `_extract_page_entry` finds it -- skip real frontmatter via
        `brain_organizer._find_frontmatter` (reused directly), then the
        first line starting with "# " and not "## ". A page with no H1 at
        all is never flagged -- see the module docstring's crit28
        section."""
        flagged: list[str] = []
        for filename, entry in self.catalog_by_filename.items():
            f = self.wiki_dir / filename
            try:
                text = f.read_text(encoding="utf-8-sig")
            except OSError:
                continue
            fm = bo._find_frontmatter(text)
            start = fm[1] + 1 if fm is not None else 0
            lines = text.splitlines()
            real_h1 = None
            for line in lines[start:]:
                if line.startswith("# ") and not line.startswith("## "):
                    real_h1 = line[2:].strip()
                    break
            if real_h1 is not None and real_h1 != entry.get("title"):
                flagged.append(filename)
        return sorted(flagged)


def _classify_links(
    text: str,
    by_stem: set[str],
    by_stem_ci: dict[str, str],
    by_title: dict[str, str],
    by_title_ci: dict[str, str],
) -> dict[str, int]:
    counts = {"resolved": 0, "cat_a": 0, "cat_c": 0, "cat_d": 0}
    for m in bo.WIKILINK_PAT.finditer(text):
        target = m.group(1).strip()
        if target in by_stem or target.lower() in by_stem_ci:
            counts["resolved"] += 1
            continue
        if target in by_title or target.lower() in by_title_ci:
            counts["cat_a"] += 1
            continue
        if bo._is_daily_note(target):
            counts["cat_c"] += 1
            continue
        counts["cat_d"] += 1
    return counts


def census_vault(vault_dir: Path, wiki_subfolder: str = "wiki") -> VaultCensus:
    """Read-only census of *vault_dir*'s top-level wiki pages (non-recursive
    `wiki/*.md` only -- `wiki/daily/` and `wiki/processed/` are out of scope
    per spec §7, reported as context counts only, per RISK #3)."""
    wiki_dir = vault_dir / wiki_subfolder
    catalog = _catalog_for(wiki_dir)
    by_stem, by_stem_ci, by_title, by_title_ci = _build_indexes(catalog)

    result = VaultCensus(
        vault_dir=vault_dir,
        wiki_dir=wiki_dir,
        catalog_by_filename={p["filename"]: p for p in catalog},
    )
    for entry in catalog:
        f = wiki_dir / entry["filename"]
        try:
            text = f.read_text(encoding="utf-8-sig")
        except OSError:
            continue
        counts = _classify_links(text, by_stem, by_stem_ci, by_title, by_title_ci)
        result.pages[entry["filename"]] = PageCensus(
            filename=entry["filename"], title=entry.get("title", ""), **counts
        )

    daily_dir = wiki_dir / "daily"
    processed_dir = wiki_dir / "processed"
    result.daily_count = len(list(daily_dir.glob("*.md"))) if daily_dir.exists() else 0
    result.processed_count = len(list(processed_dir.glob("*.md"))) if processed_dir.exists() else 0
    return result


# ---------------------------------------------------------------------------
# Criteria 26-28
# ---------------------------------------------------------------------------

def _check_crit26(before: VaultCensus | None, after: VaultCensus) -> tuple[bool, str]:
    cat_a_after = after.totals["cat_a"]
    if before is None:
        ok = cat_a_after == 0
        return ok, (
            f"single-dir mode (no committed prior baseline to compare against -- this "
            f"run establishes one): category-A (exact-title broken link) count = "
            f"{cat_a_after} across {len(after.pages)} page(s). "
            f"{'PASS: currently 0.' if ok else 'FAIL: non-zero, no prior baseline.'}"
        )
    cat_a_before = before.totals["cat_a"]
    increased_pages = [
        name for name, p in after.pages.items()
        if name in before.pages and p.cat_a > before.pages[name].cat_a
    ]
    non_increasing = cat_a_after <= cat_a_before and not increased_pages
    ok = cat_a_after == 0 and non_increasing
    return ok, (
        f"category-A before={cat_a_before} after={cat_a_after} "
        f"(non-increasing overall={cat_a_after <= cat_a_before}); "
        f"page(s) where category-A increased: {increased_pages}; "
        f"currently-zero requirement met: {cat_a_after == 0}"
    )


def _check_crit27(before: VaultCensus | None, after: VaultCensus) -> tuple[bool, str]:
    if before is None:
        cat_d_after = after.totals["cat_d"]
        return True, (
            f"single-dir mode (no committed prior baseline to compare against): "
            f"category-D (legacy processed/-rename, out-of-scope per spec §7) count = "
            f"{cat_d_after} across {len(after.pages)} page(s), reported as this run's "
            f"baseline -- non-regressing is vacuously true with nothing to compare against."
        )
    regressed = []
    for name, p in after.pages.items():
        before_p = before.pages.get(name)
        before_cat_d = before_p.cat_d if before_p is not None else 0
        if p.cat_d != before_cat_d:
            regressed.append((name, before_cat_d, p.cat_d))
    ok = not regressed
    return ok, (
        f"category-D before(total)={before.totals['cat_d']} after(total)={after.totals['cat_d']}; "
        f"page(s) with a non-zero per-page category-D delta: {regressed[:10]}"
        + (f" (+{len(regressed) - 10} more)" if len(regressed) > 10 else "")
    )


def _check_crit28(before: VaultCensus | None, after: VaultCensus | None) -> tuple[bool, str]:
    target = None
    if after is not None and "NEXUS.md" in after.catalog_by_filename:
        target = after
    elif before is not None and "NEXUS.md" in before.catalog_by_filename:
        target = before
    if target is None:
        return False, "NEXUS.md not found in either census dir's wiki catalog -- cannot evaluate."

    entry = target.catalog_by_filename["NEXUS.md"]
    nexus_ok = entry.get("title") == "NEXUS"
    mismatches = target.title_h1_mismatches()
    ok = nexus_ok and not mismatches
    return ok, (
        f"[dir={target.vault_dir}] NEXUS.md title={entry.get('title')!r} (expected 'NEXUS'): "
        f"{'OK' if nexus_ok else 'MISMATCH'}; title/H1 mismatches: "
        f"{len(mismatches)} {mismatches[:10]}"
    )


_CRIT_DESCRIPTIONS: dict[str, str] = {
    "26": "A re-run of the §0.1 census shows category A (exact-title broken links) "
          "strictly decreasing on every page the run touched, and not increasing on any page.",
    "27": "Category D stays unchanged per page -- the pipeline must not touch the "
          "out-of-scope migration links.",
    "28": "The rebuilt wiki catalog shows NEXUS.md with title == \"NEXUS\" and no "
          "BOM-defeated title/H1 mismatches.",
}


# ---------------------------------------------------------------------------
# Evidence artifact
# ---------------------------------------------------------------------------

def _touched_pages(before: VaultCensus, after: VaultCensus) -> list[str]:
    touched = []
    for name in after.pages:
        f_after = after.wiki_dir / name
        f_before = before.wiki_dir / name
        if not f_before.exists():
            touched.append(name)
            continue
        try:
            if bo.compute_sha256(f_before) != bo.compute_sha256(f_after):
                touched.append(name)
        except OSError:
            touched.append(name)
    return sorted(touched)


def _write_evidence_artifact(
    out_path: Path,
    before: VaultCensus | None,
    after: VaultCensus | None,
    results: list[tuple[str, str, str, str]],
) -> Path:
    from datetime import UTC, datetime

    mode = "two-dir (before/after)" if before is not None and after is not None else "single-dir"
    lines = [
        "# Wikilink census -- evidence",
        "",
        "Generated by `wikilink_census.py` for spec #1 "
        "(`docs/brain-organizer-wikilink-router-fixes-spec.md`) §6.5 criteria 26-28, "
        "implementing the §0.1 broken-link census. Read-only w.r.t. both vault "
        "directories -- see the script's module docstring for the read-only contract.",
        "This file is overwritten on every invocation -- it reflects the most recent run only.",
        "",
        f"- **Run timestamp (UTC):** {datetime.now(UTC).isoformat()}",
        f"- **Mode:** {mode}",
    ]
    if before is not None:
        lines.append(f"- **Before dir:** `{before.vault_dir}` -- {len(before.pages)} page(s) "
                      f"(wiki/daily: {before.daily_count} context-only, wiki/processed: "
                      f"{before.processed_count} context-only)")
    if after is not None:
        lines.append(f"- **After dir:** `{after.vault_dir}` -- {len(after.pages)} page(s) "
                      f"(wiki/daily: {after.daily_count} context-only, wiki/processed: "
                      f"{after.processed_count} context-only)")
    if before is not None and after is not None:
        touched = _touched_pages(before, after)
        lines.append(f"- **Pages touched (content differs before->after):** {len(touched)} {touched[:10]}")
        if len(before.pages) != len(after.pages):
            lines.append(
                f"- **NOTE:** before/after page counts differ ({len(before.pages)} vs "
                f"{len(after.pages)}) -- raw absolute link counts below are not directly "
                f"comparable 1:1 per page-set; per-page deltas above only compare pages "
                f"present in both dirs."
            )

    statuses = [status for _n, _d, status, _e in results]
    overall = "PASS" if all(s == "PASS" for s in statuses) else "FAIL"
    lines.append(f"- **Overall status:** {overall}")
    lines.append("")

    lines.append("## §0.1 census totals (measured, not spec-time absolutes)")
    lines.append("")
    lines.append(
        f"Spec-writing-time baseline for context only (§0.1, 2026-08-02, "
        f"{_SPEC_TIME_BASELINE['pages']} pages): category-A={_SPEC_TIME_BASELINE['cat_a']}, "
        f"category-D={_SPEC_TIME_BASELINE['cat_d']}, category-C={_SPEC_TIME_BASELINE['cat_c']}. "
        f"Per RISK #4, these are NEVER used as pass/fail thresholds below -- the live vault "
        f"has changed size since spec-writing time, so raw counts naturally diverge."
    )
    lines.append("")
    lines.append("| Dir | Pages | Resolved | Category A | Category D | Category C |")
    lines.append("|---|---|---|---|---|---|")
    if before is not None:
        t = before.totals
        lines.append(f"| before (`{before.vault_dir.name}`) | {len(before.pages)} | "
                      f"{t['resolved']} | {t['cat_a']} | {t['cat_d']} | {t['cat_c']} |")
    if after is not None:
        t = after.totals
        lines.append(f"| after (`{after.vault_dir.name}`) | {len(after.pages)} | "
                      f"{t['resolved']} | {t['cat_a']} | {t['cat_d']} | {t['cat_c']} |")
    lines.append("")

    lines.append("## Criteria 26-28")
    lines.append("")
    lines.append("| Crit | Result | Description | Evidence |")
    lines.append("|---|---|---|---|")
    for num, desc, status, ev in results:
        lines.append(
            f"| {num} | {status} | {desc.replace('|', chr(92) + '|')} | "
            f"{ev.replace('|', chr(92) + '|')} |"
        )
    lines.append("")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines), encoding="utf-8")
    return out_path


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    # Windows consoles default to cp1252, which can't encode "§" -- same fix
    # brain_organizer.py's setup_logging() applies for its own console output.
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--before", required=True, type=Path, help="Vault directory (contains wiki/) -- the 'before' snapshot.")
    parser.add_argument("--after", type=Path, default=None, help="Optional vault directory -- the 'after' snapshot. Omit for single-dir mode.")
    parser.add_argument("--wiki-subfolder", default="wiki", help="Wiki subfolder name under each vault dir (default: wiki).")
    parser.add_argument("--out", type=Path, default=None, help=f"Evidence markdown path (default: {DEFAULT_EVIDENCE_PATH}).")
    args = parser.parse_args(argv)

    before_dir: Path = args.before
    if not before_dir.exists():
        print(f"ERROR: --before dir does not exist: {before_dir}", file=sys.stderr)
        return 2

    print(f"Census (read-only): before={before_dir}")
    before = census_vault(before_dir, args.wiki_subfolder)
    if not before.pages:
        print(f"WARNING: 0 pages found under {before.wiki_dir} -- check --before/--wiki-subfolder.", file=sys.stderr)

    after: VaultCensus | None = None
    if args.after is not None:
        after_dir: Path = args.after
        if not after_dir.exists():
            print(f"ERROR: --after dir does not exist: {after_dir}", file=sys.stderr)
            return 2
        print(f"Census (read-only): after={after_dir}")
        after = census_vault(after_dir, args.wiki_subfolder)
        if not after.pages:
            print(f"WARNING: 0 pages found under {after.wiki_dir} -- check --after/--wiki-subfolder.", file=sys.stderr)

    eval_before, eval_after = (before, after) if after is not None else (None, before)

    results: list[tuple[str, str, str, str]] = []
    ok, ev = _check_crit26(eval_before, eval_after)
    results.append(("26", _CRIT_DESCRIPTIONS["26"], "PASS" if ok else "FAIL", ev))
    ok, ev = _check_crit27(eval_before, eval_after)
    results.append(("27", _CRIT_DESCRIPTIONS["27"], "PASS" if ok else "FAIL", ev))
    ok, ev = _check_crit28(before, after)
    results.append(("28", _CRIT_DESCRIPTIONS["28"], "PASS" if ok else "FAIL", ev))

    print()
    print("=" * 100)
    print(f"{'Crit':<5} {'Result':<8} Description")
    print("-" * 100)
    for num, desc, status, ev in results:
        print(f"{num:<5} {status:<8} {desc}")
        print(f"        evidence: {ev}")
    print("=" * 100)

    out_path = args.out or DEFAULT_EVIDENCE_PATH
    written = _write_evidence_artifact(out_path, before, after, results)
    print(f"\nEvidence artifact written: {written}")

    return 0 if all(status == "PASS" for _n, _d, status, _e in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
