"""Tests for spec #3 (docs/brain-organizer-frontmatter-tags-spec.md) §2.1:
_find_frontmatter and _parse_frontmatter_tags. Pure functions, no I/O --
covers acceptance criteria 1-7 (§6.1) plus the §2.1 prose contract
("de-duplicated case-insensitively, first-seen spelling wins, order
preserved") and BOM handling."""

from __future__ import annotations

import brain_organizer as bo


# ---------------------------------------------------------------------------
# _find_frontmatter / _parse_frontmatter_tags -- spec §6.1 criteria 1-7
# ---------------------------------------------------------------------------


def test_find_frontmatter_returns_body_start_and_closing_index() -> None:
    """Sanity check on the (first_body_line_index, closing_delimiter_line_index)
    contract before exercising the tag parser built on top of it."""
    text = "---\ntags:\n  - alpha\n---\n\nBody.\n"

    assert bo._find_frontmatter(text) == (1, 3)


def test_parse_frontmatter_tags_block_list_form() -> None:
    """Criterion 1: YAML block list form (the measured majority, 19/25 live
    pages) parses correctly."""
    text = "---\ntitle: Test\ntags:\n  - ford\n  - beyondtrust\n---\n\nBody.\n"

    assert bo._parse_frontmatter_tags(text) == ["ford", "beyondtrust"]


def test_parse_frontmatter_tags_inline_array_form_quoted_and_unquoted() -> None:
    """Criterion 2: inline array, mixing an unquoted and a quoted entry."""
    text = '---\ntags: [risk-management, audit, "some tag"]\n---\n\nBody.\n'

    assert bo._parse_frontmatter_tags(text) == ["risk-management", "audit", "some tag"]


def test_parse_frontmatter_tags_hash_string_form() -> None:
    """Criterion 3: hash-string form (invalid YAML but present verbatim on
    4 live pages: HIPAA.md, Home-Automation.md, Insurance.md, Lead-Generation.md)."""
    text = "---\ntags: #compliance #legal #hipaa\n---\n\nBody.\n"

    assert bo._parse_frontmatter_tags(text) == ["compliance", "legal", "hipaa"]


def test_parse_frontmatter_tags_mid_file_tags_line_returns_empty() -> None:
    """Criterion 4: a 'tags:' line appearing only mid-file (the real
    NEXUS.md/Obsidian.md/Sales.md shape) must not false-positive -- parsing
    is anchored strictly to position 0, never a document-wide search."""
    text = (
        "# NEXUS\n\nSome prose.\n\n"
        "## Notes\n\n"
        "---\ntags: [should-not-be-parsed]\n---\n\n"
        "More prose.\n"
    )

    assert bo._find_frontmatter(text) is None
    assert bo._parse_frontmatter_tags(text) == []


def test_find_frontmatter_fence_led_document_returns_none() -> None:
    """Criterion 5: a document opening with a code fence (3 live pages wrap
    their whole doc in one, hiding an interior --- block from Obsidian)
    returns None even though a --- block exists inside the fence."""
    text = "```markdown\n---\ntags: [hidden]\n---\n\nBody.\n```\n"

    assert bo._find_frontmatter(text) is None
    assert bo._parse_frontmatter_tags(text) == []


def test_find_frontmatter_no_closing_delimiter_within_limit_returns_none() -> None:
    """Criterion 6: a --- opener with no closing --- within _FM_MAX_LINES
    (100) lines returns None."""
    body_lines = "\n".join(f"line {i}" for i in range(bo._FM_MAX_LINES + 5))
    text = f"---\ntags: [orphaned]\n{body_lines}\n"

    assert bo._find_frontmatter(text) is None
    assert bo._parse_frontmatter_tags(text) == []


def test_parse_frontmatter_tags_bom_prefixed_matches_bom_less() -> None:
    """Criterion 7: BOM-prefixed frontmatter parses identically to the same
    bytes without the BOM."""
    plain = "---\ntags:\n  - alpha\n  - beta\n---\n\nBody.\n"
    bommed = "﻿" + plain

    assert bo._parse_frontmatter_tags(bommed) == bo._parse_frontmatter_tags(plain) == ["alpha", "beta"]


# ---------------------------------------------------------------------------
# §2.1 prose contract beyond the numbered criteria
# ---------------------------------------------------------------------------


def test_parse_frontmatter_tags_case_insensitive_dedup_first_seen_wins() -> None:
    """§2.1 docstring contract: tags are de-duplicated case-insensitively,
    the first-seen spelling wins, and order is preserved."""
    text = "---\ntags:\n  - Ford\n  - beyondtrust\n  - FORD\n  - BeyondTrust\n---\n\nBody.\n"

    assert bo._parse_frontmatter_tags(text) == ["Ford", "beyondtrust"]


def test_parse_frontmatter_tags_unbracketed_scalar_after_tags_key_skipped() -> None:
    """Not one of the three documented forms (block list / hash-string /
    inline array) and not covered by an explicit §6.1 criterion -- spec §2.1
    is silent on this shape. An unbracketed, non-'#'-prefixed scalar after
    'tags:' hits the current implementation's 'else: i += 1' branch and
    contributes no tags; this pins that real, observable behavior rather
    than a documented requirement. Also confirms a sibling frontmatter key
    is left untouched by the miss."""
    text = "---\ntags: solo-tag\ncategory: Reference\n---\n\nBody.\n"

    assert bo._parse_frontmatter_tags(text) == []
