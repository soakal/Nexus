"""Unit tests for wikilink_census.py (spec #1 §0.1 census / §6.5 crit 26-28)."""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import brain_organizer as bo

sys.path.insert(0, str(Path(__file__).parent.parent))
import wikilink_census as wc  # noqa: E402 -- must follow the sys.path insert above


def write_wiki(vault: Path, name: str, content: str) -> Path:
    f = vault / "wiki" / name
    f.write_text(content, encoding="utf-8")
    return f


def _snapshot_dir(root: Path) -> dict[str, tuple[int, str]]:
    """(mtime_ns, sha256) for every file under root, recursive."""
    snap: dict[str, tuple[int, str]] = {}
    for f in root.rglob("*"):
        if f.is_file():
            snap[str(f.relative_to(root))] = (f.stat().st_mtime_ns, bo.compute_sha256(f))
    return snap


# ---------------------------------------------------------------------------
# _classify_links / _build_indexes
# ---------------------------------------------------------------------------

def test_classify_links_resolved_stem_match() -> None:
    catalog = [{"filename": "Bug-Fixes.md", "title": "Bug Fixes"}]
    indexes = wc._build_indexes(catalog)
    counts = wc._classify_links("See [[Bug-Fixes]] for details.", *indexes)
    assert counts == {"resolved": 1, "cat_a": 0, "cat_c": 0, "cat_d": 0}


def test_classify_links_category_a_exact_title_match() -> None:
    """A link naming a real page's TITLE (not its filename stem) is category A --
    defect A, the title-vs-filename namespace bug."""
    catalog = [{"filename": "Bug-Fixes.md", "title": "Bug Fixes"}]
    indexes = wc._build_indexes(catalog)
    counts = wc._classify_links("See [[Bug Fixes]] for details.", *indexes)
    assert counts == {"resolved": 0, "cat_a": 1, "cat_c": 0, "cat_d": 0}


def test_classify_links_category_c_daily_note_shaped_target() -> None:
    catalog = [{"filename": "NEXUS.md", "title": "NEXUS"}]
    indexes = wc._build_indexes(catalog)
    counts = wc._classify_links("See [[2026-07-24]] for that day's log.", *indexes)
    assert counts == {"resolved": 0, "cat_a": 0, "cat_c": 1, "cat_d": 0}


def test_classify_links_category_d_catch_all() -> None:
    """A broken link that isn't a known title and doesn't look like a daily
    note (e.g. a legacy hyphenated slug with no matching file) is category D."""
    catalog = [{"filename": "NEXUS.md", "title": "NEXUS"}]
    indexes = wc._build_indexes(catalog)
    counts = wc._classify_links("See [[Pricing-Calculator]] for details.", *indexes)
    assert counts == {"resolved": 0, "cat_a": 0, "cat_c": 0, "cat_d": 1}


def test_classify_links_case_insensitive_stem_and_title_still_resolve() -> None:
    catalog = [{"filename": "Bug-Fixes.md", "title": "Bug Fixes"}]
    indexes = wc._build_indexes(catalog)
    counts = wc._classify_links("[[bug-fixes]] and [[bug fixes]]", *indexes)
    assert counts["resolved"] == 1
    assert counts["cat_a"] == 1


def test_classify_links_embed_and_headings_ignored_by_grammar() -> None:
    """Embeds (![[...]]) are never counted -- WIKILINK_PAT's own negative
    lookbehind for '!' (reused, not re-derived)."""
    catalog: list[dict[str, Any]] = []
    indexes = wc._build_indexes(catalog)
    counts = wc._classify_links("![[Some-Image.png]]", *indexes)
    assert counts == {"resolved": 0, "cat_a": 0, "cat_c": 0, "cat_d": 0}


# ---------------------------------------------------------------------------
# census_vault -- read-only contract + basic shape
# ---------------------------------------------------------------------------

def test_census_vault_categorizes_pages(tmp_vault: Path) -> None:
    write_wiki(tmp_vault, "Bug-Fixes.md", "# Bug Fixes\n\nSome content.")
    write_wiki(tmp_vault, "NEXUS.md", "# NEXUS\n\nSee [[Bug Fixes]] and [[Pricing-Calculator]].")
    census = wc.census_vault(tmp_vault)
    assert set(census.pages) == {"Bug-Fixes.md", "NEXUS.md"}
    nexus_page = census.pages["NEXUS.md"]
    assert nexus_page.cat_a == 1  # [[Bug Fixes]]
    assert nexus_page.cat_d == 1  # [[Pricing-Calculator]]
    assert census.totals["cat_a"] == 1
    assert census.totals["cat_d"] == 1


def test_census_vault_reports_daily_and_processed_as_context_only(tmp_vault: Path) -> None:
    write_wiki(tmp_vault, "NEXUS.md", "# NEXUS\n\nHello.")
    daily = tmp_vault / "wiki" / "daily"
    daily.mkdir()
    (daily / "2026-07-24.md").write_text("# 2026-07-24\n\nDaily note.", encoding="utf-8")
    processed = tmp_vault / "wiki" / "processed"
    processed.mkdir()
    (processed / "Old-Page.md").write_text("# Old Page\n\nLegacy.", encoding="utf-8")

    census = wc.census_vault(tmp_vault)

    # Non-recursive glob("*.md") -- daily/processed pages never enter the
    # per-page census dict that crit26/27 evaluation reads.
    assert set(census.pages) == {"NEXUS.md"}
    assert census.daily_count == 1
    assert census.processed_count == 1


def test_census_vault_is_read_only(tmp_vault: Path) -> None:
    write_wiki(tmp_vault, "NEXUS.md", "# NEXUS\n\nSee [[Bug Fixes]].")
    write_wiki(tmp_vault, "Bug-Fixes.md", "# Bug Fixes\n\nHello.")
    before = _snapshot_dir(tmp_vault)

    wc.census_vault(tmp_vault)

    after = _snapshot_dir(tmp_vault)
    assert before == after


# ---------------------------------------------------------------------------
# title_h1_mismatches -- crit28 proxy, incl. regression guard for the
# stem-equals-title false positive caught while building this script
# ---------------------------------------------------------------------------

def test_title_h1_mismatches_flags_real_divergence(tmp_vault: Path) -> None:
    write_wiki(tmp_vault, "Foo.md", "# Foo\n\nBody text.")
    census = wc.census_vault(tmp_vault)
    # Force a catalog title that disagrees with the file's real H1.
    census.catalog_by_filename["Foo.md"]["title"] = "Wrong Title"
    assert census.title_h1_mismatches() == ["Foo.md"]


def test_title_h1_mismatches_no_false_positive_when_title_equals_stem(tmp_vault: Path) -> None:
    """Regression guard: AI.md whose real H1 is literally '# AI' (matching
    its own filename stem) must NOT be flagged just because title == stem --
    that coincidence is a normal, correctly-parsed page, not a defect."""
    write_wiki(tmp_vault, "AI.md", "# AI\n\nArtificial Intelligence content.")
    census = wc.census_vault(tmp_vault)
    assert census.catalog_by_filename["AI.md"]["title"] == "AI"
    assert census.title_h1_mismatches() == []


def test_title_h1_mismatches_no_h1_never_flagged(tmp_vault: Path) -> None:
    write_wiki(tmp_vault, "NoHeading.md", "Just prose, no H1 at all.")
    census = wc.census_vault(tmp_vault)
    assert census.title_h1_mismatches() == []


# ---------------------------------------------------------------------------
# crit26 / crit27 / crit28 evaluation
# ---------------------------------------------------------------------------

def _page(filename: str, title: str = "", **counts: int) -> wc.PageCensus:
    return wc.PageCensus(filename=filename, title=title, **counts)


def _vault(tmp_path: Path, name: str, pages: dict[str, wc.PageCensus], catalog: dict[str, dict[str, Any]] | None = None) -> wc.VaultCensus:
    d = tmp_path / name
    d.mkdir()
    return wc.VaultCensus(vault_dir=d, wiki_dir=d / "wiki", pages=pages, catalog_by_filename=catalog or {})


def test_check_crit26_single_dir_pass_when_zero(tmp_path: Path) -> None:
    v = _vault(tmp_path, "sole", {"A.md": _page("A.md", cat_a=0)})
    ok, ev = wc._check_crit26(None, v)
    assert ok is True
    assert "single-dir" in ev


def test_check_crit26_single_dir_fail_when_nonzero(tmp_path: Path) -> None:
    v = _vault(tmp_path, "sole", {"A.md": _page("A.md", cat_a=1)})
    ok, _ev = wc._check_crit26(None, v)
    assert ok is False


def test_check_crit26_two_dir_pass_non_increasing_and_zero(tmp_path: Path) -> None:
    before = _vault(tmp_path, "before", {"A.md": _page("A.md", cat_a=0)})
    after = _vault(tmp_path, "after", {"A.md": _page("A.md", cat_a=0)})
    ok, _ev = wc._check_crit26(before, after)
    assert ok is True


def test_check_crit26_two_dir_fail_when_page_increased(tmp_path: Path) -> None:
    before = _vault(tmp_path, "before", {"A.md": _page("A.md", cat_a=0)})
    after = _vault(tmp_path, "after", {"A.md": _page("A.md", cat_a=1)})
    ok, ev = wc._check_crit26(before, after)
    assert ok is False
    assert "A.md" in ev


def test_check_crit26_two_dir_fail_when_stable_but_nonzero(tmp_path: Path) -> None:
    """A page whose category-A count is unchanged (non-increasing) but still
    nonzero must still FAIL crit26 -- "not increasing" alone is not enough;
    the count must actually reach zero. Without this compound requirement, a
    page that never gets fixed could report PASS forever."""
    before = _vault(tmp_path, "before", {"A.md": _page("A.md", cat_a=1)})
    after = _vault(tmp_path, "after", {"A.md": _page("A.md", cat_a=1)})
    ok, _ev = wc._check_crit26(before, after)
    assert ok is False


def test_check_crit27_two_dir_pass_when_stable(tmp_path: Path) -> None:
    before = _vault(tmp_path, "before", {"A.md": _page("A.md", cat_d=5)})
    after = _vault(tmp_path, "after", {"A.md": _page("A.md", cat_d=5)})
    ok, _ev = wc._check_crit27(before, after)
    assert ok is True


def test_check_crit27_two_dir_fail_when_page_delta_nonzero(tmp_path: Path) -> None:
    before = _vault(tmp_path, "before", {"A.md": _page("A.md", cat_d=5)})
    after = _vault(tmp_path, "after", {"A.md": _page("A.md", cat_d=6)})
    ok, ev = wc._check_crit27(before, after)
    assert ok is False
    assert "A.md" in ev


def test_check_crit27_single_dir_always_reports_pass(tmp_path: Path) -> None:
    v = _vault(tmp_path, "sole", {"A.md": _page("A.md", cat_d=999)})
    ok, ev = wc._check_crit27(None, v)
    assert ok is True
    assert "single-dir" in ev


def test_check_crit28_pass_when_nexus_title_correct_and_no_mismatches(tmp_path: Path) -> None:
    v = _vault(tmp_path, "sole", {}, {"NEXUS.md": {"filename": "NEXUS.md", "title": "NEXUS"}})
    v.wiki_dir.mkdir(parents=True, exist_ok=True)
    (v.wiki_dir / "NEXUS.md").write_text("# NEXUS\n\nHello.", encoding="utf-8")
    ok, ev = wc._check_crit28(None, v)
    assert ok is True
    assert "OK" in ev


def test_check_crit28_fail_when_nexus_title_wrong(tmp_path: Path) -> None:
    v = _vault(tmp_path, "sole", {}, {"NEXUS.md": {"filename": "NEXUS.md", "title": "NEXUS Dashboard Card"}})
    v.wiki_dir.mkdir(parents=True, exist_ok=True)
    (v.wiki_dir / "NEXUS.md").write_text("# NEXUS Dashboard Card\n\nHello.", encoding="utf-8")
    ok, ev = wc._check_crit28(None, v)
    assert ok is False
    assert "MISMATCH" in ev


def test_check_crit28_fail_when_nexus_missing_from_both_dirs(tmp_path: Path) -> None:
    v = _vault(tmp_path, "sole", {}, {})
    ok, ev = wc._check_crit28(None, v)
    assert ok is False
    assert "not found" in ev


# ---------------------------------------------------------------------------
# End-to-end main() -- exit code, evidence artifact, read-only guarantee
# ---------------------------------------------------------------------------

def test_main_two_dir_all_pass_writes_evidence_and_exits_zero(tmp_path: Path) -> None:
    before_dir = tmp_path / "before_vault"
    after_dir = tmp_path / "after_vault"
    for d in (before_dir, after_dir):
        (d / "wiki").mkdir(parents=True)
        (d / "wiki" / "NEXUS.md").write_text("# NEXUS\n\nHello.", encoding="utf-8")

    before_snapshot = _snapshot_dir(before_dir)
    after_snapshot = _snapshot_dir(after_dir)

    out_path = tmp_path / "evidence.md"
    rc = wc.main([
        "--before", str(before_dir),
        "--after", str(after_dir),
        "--out", str(out_path),
    ])

    assert rc == 0
    assert out_path.exists()
    text = out_path.read_text(encoding="utf-8")
    assert "| 26 | PASS |" in text
    assert "| 27 | PASS |" in text
    assert "| 28 | PASS |" in text

    # Neither vault dir was touched by the run.
    assert _snapshot_dir(before_dir) == before_snapshot
    assert _snapshot_dir(after_dir) == after_snapshot


def test_main_two_dir_regressed_category_a_exits_nonzero(tmp_path: Path) -> None:
    before_dir = tmp_path / "before_vault"
    after_dir = tmp_path / "after_vault"
    (before_dir / "wiki").mkdir(parents=True)
    (before_dir / "wiki" / "Bug-Fixes.md").write_text("# Bug Fixes\n\nHello.", encoding="utf-8")
    (before_dir / "wiki" / "NEXUS.md").write_text("# NEXUS\n\nNo broken links here.", encoding="utf-8")

    (after_dir / "wiki").mkdir(parents=True)
    (after_dir / "wiki" / "Bug-Fixes.md").write_text("# Bug Fixes\n\nHello.", encoding="utf-8")
    # A newly-introduced title-form (category A) broken link -- a regression.
    (after_dir / "wiki" / "NEXUS.md").write_text("# NEXUS\n\nSee [[Bug Fixes]] now.", encoding="utf-8")

    out_path = tmp_path / "evidence.md"
    rc = wc.main([
        "--before", str(before_dir),
        "--after", str(after_dir),
        "--out", str(out_path),
    ])

    assert rc == 1
    text = out_path.read_text(encoding="utf-8")
    assert "| 26 | FAIL |" in text


def test_main_single_dir_mode(tmp_path: Path) -> None:
    vault_dir = tmp_path / "only_vault"
    (vault_dir / "wiki").mkdir(parents=True)
    (vault_dir / "wiki" / "NEXUS.md").write_text("# NEXUS\n\nHello.", encoding="utf-8")

    out_path = tmp_path / "evidence.md"
    rc = wc.main(["--before", str(vault_dir), "--out", str(out_path)])

    assert rc == 0
    text = out_path.read_text(encoding="utf-8")
    assert "single-dir" in text
