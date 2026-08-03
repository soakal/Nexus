"""Tests for spec #3 (docs/brain-organizer-frontmatter-tags-spec.md):

§2.1 -- _find_frontmatter and _parse_frontmatter_tags. Pure functions, no
I/O -- covers acceptance criteria 1-7 (§6.1) plus the §2.1 prose contract
("de-duplicated case-insensitively, first-seen spelling wins, order
preserved") and BOM handling.

§3.1/§3.2 -- _render_tags_block and _write_frontmatter_tags, the write
layer. Pure functions, no I/O -- covers acceptance criteria 13-20 (§6.3)
plus two regression cases flagged by this cycle's Security review: a
line-break embedded in a tag value forging a premature closing delimiter
(structural injection), and a mixed-line-ending document body being
silently normalized by a whole-document splitlines()/join()."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import brain_organizer as bo
from anthropic.types import TextBlock


def _make_message(text: str, stop_reason: str = "end_turn") -> MagicMock:
    """Build a mock Message with a real TextBlock so isinstance checks pass
    (mirrors test_organizer.py's make_message helper)."""
    msg = MagicMock()
    msg.content = [TextBlock(type="text", text=text)]
    msg.stop_reason = stop_reason
    return msg


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


# ---------------------------------------------------------------------------
# _write_frontmatter_tags / _render_tags_block -- spec §3.1/§3.2, §6.3
# criteria 13-20, plus the two Security-flagged regression cases from this
# cycle (line-break-in-tag structural injection, mixed-line-ending body
# byte-preservation). Fixtures below are built by hand-tracing
# _find_frontmatter's (first_body_line_index, closing_delimiter_line_index)
# contract, never a fresh document-wide regex.
# ---------------------------------------------------------------------------


def test_write_frontmatter_tags_no_existing_frontmatter_prepends_block() -> None:
    """Criterion 13: no existing frontmatter -> a fresh block is prepended,
    the rest of the document is byte-identical."""
    content = "Body line 1.\nBody line 2.\n"

    result = bo._write_frontmatter_tags(content, ["alpha", "beta"])

    assert result == "---\ntags:\n  - alpha\n  - beta\n---\n" + content


def test_write_frontmatter_tags_noop_when_tags_unchanged_and_canonical() -> None:
    """Criterion 14: no-op guarantee -- an equal tag list against an
    already-canonical block returns the input byte for byte (not merely an
    equal-valued copy)."""
    content = "---\ntags:\n  - alpha\n  - beta\n---\n\nBody.\n"

    result = bo._write_frontmatter_tags(content, ["alpha", "beta"])

    assert result is content or result == content


def test_write_frontmatter_tags_preserves_other_frontmatter_keys() -> None:
    """Criterion 15: category:/date: stay byte-identical after a tags-only
    change; only the tags: field's rendered values change."""
    content = (
        "---\ncategory: Reference\ntags:\n  - alpha\ndate: 2026-01-01\n---\n\nBody.\n"
    )

    result = bo._write_frontmatter_tags(content, ["alpha", "gamma"])

    assert result == (
        "---\ncategory: Reference\ntags:\n  - alpha\n  - gamma\ndate: 2026-01-01\n"
        "---\n\nBody.\n"
    )


def test_write_frontmatter_tags_inserts_before_closing_delimiter_when_no_tags_key() -> None:
    """Criterion 16: frontmatter with no tags: key gets one inserted
    immediately before the closing "---"; other keys untouched."""
    content = "---\ncategory: Reference\ndate: 2026-01-01\n---\n\nBody.\n"

    result = bo._write_frontmatter_tags(content, ["alpha"])

    assert result == (
        "---\ncategory: Reference\ndate: 2026-01-01\ntags:\n  - alpha\n---\n\nBody.\n"
    )


def test_write_frontmatter_tags_preserves_crlf_newline_style() -> None:
    """Criterion 17: a CRLF document stays CRLF throughout -- no mixed
    endings introduced by the rewrite."""
    content = "---\r\ntags:\r\n  - alpha\r\n---\r\n\r\nBody.\r\n"

    result = bo._write_frontmatter_tags(content, ["alpha", "beta"])

    assert result == "---\r\ntags:\r\n  - alpha\r\n  - beta\r\n---\r\n\r\nBody.\r\n"
    assert "\r\n" in result and "\n" not in result.replace("\r\n", "")


def test_write_frontmatter_tags_preserves_leading_bom() -> None:
    """Criterion 18: a leading UTF-8 BOM byte stays the first character of
    the output."""
    content = "﻿---\ntags:\n  - alpha\n---\n\nBody.\n"

    result = bo._write_frontmatter_tags(content, ["alpha", "beta"])

    assert result.startswith("﻿")
    assert result == "﻿---\ntags:\n  - alpha\n  - beta\n---\n\nBody.\n"


def test_write_frontmatter_tags_fence_led_document_unchanged_and_warns(caplog) -> None:
    """Criterion 19: a document _find_frontmatter can't safely anchor (here,
    a leading code fence hiding an interior --- block) is returned unchanged
    and logs exactly one WARNING -- never a guess."""
    content = "```markdown\n---\ntags: [hidden]\n---\n\nBody.\n```\n"

    with caplog.at_level("WARNING", logger="brain_organizer"):
        result = bo._write_frontmatter_tags(content, ["alpha"])

    assert result == content
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1


def test_write_frontmatter_tags_hash_string_form_rewritten_to_block_list() -> None:
    """Criterion 20: existing hash-string tags: #a #b #c is rewritten to
    block-list form, values preserved."""
    content = "---\ntags: #compliance #legal #hipaa\n---\n\nBody.\n"

    result = bo._write_frontmatter_tags(content, ["compliance", "legal", "hipaa"])

    assert result == (
        "---\ntags:\n  - compliance\n  - legal\n  - hipaa\n---\n\nBody.\n"
    )


def test_write_frontmatter_tags_sanitizes_line_break_in_tag_value() -> None:
    """Security regression: a tag value containing an embedded newline +
    "---" + a new YAML key must not be able to forge a premature closing
    frontmatter delimiter and inject an arbitrary top-level key. The
    line-break character is collapsed to a single space, keeping the
    malicious payload confined to one harmless tags: list item."""
    content = "---\ntags:\n  - alpha\n---\n\nBody.\n"
    malicious_tag = "evil\n---\nnew_key: injected"

    result = bo._write_frontmatter_tags(content, ["alpha", malicious_tag])

    assert result == (
        "---\ntags:\n  - alpha\n  - evil --- new_key: injected\n---\n\nBody.\n"
    )
    # No forged closing delimiter or injected top-level key ended up as its
    # own line anywhere in the output -- exactly the real opening and
    # closing "---" delimiters exist as standalone lines.
    assert "\nnew_key: injected" not in result
    assert sum(1 for line in result.split("\n") if line == "---") == 2


def test_write_frontmatter_tags_preserves_lone_cr_and_exotic_linebreaks_in_body() -> None:
    """Security regression: rewriting the frontmatter must not run
    splitlines()/join() over the whole document body. A lone "\\r" (and, by
    the same mechanism, VT/FF/FS-RS-GS/NEL/U+2028/U+2029) inside the body --
    after the frontmatter's closing "---" -- must survive untouched rather
    than being silently normalized or deleted."""
    content = (
        "---\ntags:\n  - alpha\n---\n\n"
        "Body line1.\rBody line2 with lone CR.\nNormal LF line.\n"
    )

    result = bo._write_frontmatter_tags(content, ["alpha", "beta"])

    assert result == (
        "---\ntags:\n  - alpha\n  - beta\n---\n\n"
        "Body line1.\rBody line2 with lone CR.\nNormal LF line.\n"
    )


# ---------------------------------------------------------------------------
# _build_tag_vocabulary -- spec §2.4, criterion 12 (§6.2). Pure function, no I/O.
# ---------------------------------------------------------------------------


def test_build_tag_vocabulary_excludes_category_tags_and_orders_by_count_then_alpha() -> None:
    """Criterion 12 (exclusion + ordering halves): any "category/*" tag (case-insensitive) is dropped
    entirely from the vocabulary, and the survivors are ordered by
    descending count, ties broken alphabetically -- not catalog order."""
    catalog: list[dict[str, Any]] = [
        {"tags": ["category/reference", "ford", "beyondtrust"]},
        {"tags": ["ford", "Category/Tools", "audit"]},
        {"tags": ["ford", "audit", "beyondtrust"]},
    ]

    result = bo._build_tag_vocabulary(catalog, limit=10)

    # ford: 3, audit: 2, beyondtrust: 2 -> ford first, then audit before
    # beyondtrust (alphabetical tie-break); no category/* tag present at all.
    assert result == ["ford", "audit", "beyondtrust"]


def test_build_tag_vocabulary_slices_to_limit() -> None:
    """Criterion 12 (honors limit half): the vocabulary is capped to `limit` entries, keeping
    the highest-ranked ones."""
    catalog: list[dict[str, Any]] = [
        {"tags": ["alpha", "beta", "gamma", "delta"]},
        {"tags": ["alpha", "beta"]},
        {"tags": ["alpha"]},
    ]

    result = bo._build_tag_vocabulary(catalog, limit=2)

    assert result == ["alpha", "beta"]


# ---------------------------------------------------------------------------
# suggest_tags -- spec §4, criteria 29-31. Mocks the Anthropic client the
# same way test_organizer.py's detect_topics/route_topics tests do.
# ---------------------------------------------------------------------------


def test_suggest_tags_strips_fence_and_parses_json(tmp_config: dict[str, Any]) -> None:
    """Successful path: a fenced ```json ... ``` response is fence-stripped
    and its "tags" list is returned verbatim. Also pins criterion 29's
    prompt-content half: the prompt sent to the model contains every
    vocabulary entry passed in."""
    client = MagicMock()
    client.messages.create.return_value = _make_message(
        '```json\n{"tags": ["home-automation", "ford"]}\n```'
    )
    vocabulary = ["home-automation", "ford", "audit"]

    result = bo.suggest_tags(
        "Some note content about a Ford truck.",
        existing_tags=[],
        vocabulary=vocabulary,
        config=tmp_config,
        client=client,
    )

    assert result == ["home-automation", "ford"]
    assert client.messages.create.call_args.kwargs["model"] == tmp_config["haiku_model"]
    prompt = client.messages.create.call_args.kwargs["messages"][0]["content"]
    for tag in vocabulary:
        assert tag in prompt


def test_suggest_tags_returns_empty_list_on_recursion_error_from_malformed_json(
    tmp_config: dict[str, Any],
) -> None:
    """Security regression pin: a malformed, deeply-nested-bracket response
    (e.g. thousands of unclosed "[") previously raised an uncaught
    RecursionError out of json.loads(), violating the "never raises, falls
    back to []" contract. suggest_tags must catch RecursionError alongside
    JSONDecodeError and return [] instead."""
    client = MagicMock()
    client.messages.create.return_value = _make_message("[" * 100_000)

    result = bo.suggest_tags(
        "content",
        existing_tags=[],
        vocabulary=["ford"],
        config=tmp_config,
        client=client,
    )

    assert result == []


def test_suggest_tags_returns_empty_list_on_non_json_response(
    tmp_config: dict[str, Any], caplog
) -> None:
    """Criterion 30's json.JSONDecodeError arm: a response that is plain
    non-JSON text fails json.loads() and suggest_tags must catch that,
    return [], and log exactly one WARNING rather than raising."""
    client = MagicMock()
    client.messages.create.return_value = _make_message("not json at all")

    with caplog.at_level("WARNING", logger="brain_organizer"):
        result = bo.suggest_tags(
            "content",
            existing_tags=[],
            vocabulary=["ford"],
            config=tmp_config,
            client=client,
        )

    assert result == []
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
