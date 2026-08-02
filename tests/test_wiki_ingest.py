"""Tests for the reference-doc verbatim import path in wiki_ingest.

When a file in Brain/raw/ trips either skip condition (too_large or
reference_doc), it must be imported VERBATIM to the wiki — full text preserved,
embedded base64 PNGs extracted to disk, original moved to processed/ — with NO
Haiku call and NO summarization.
"""

import ast
import base64
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.agents import wiki_ingest


# A tiny valid 1x1 PNG, base64-encoded.
_PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)
_PNG_B64 = base64.b64encode(_PNG_BYTES).decode()


@pytest.fixture
def vault(tmp_path, monkeypatch):
    """Point get_settings at a tmp vault and make haiku explode if ever called."""
    (tmp_path / "Brain" / "raw").mkdir(parents=True)
    (tmp_path / "Brain" / "wiki").mkdir(parents=True)

    fake_settings = SimpleNamespace(obsidian_vault_path=str(tmp_path))
    monkeypatch.setattr(
        "backend.config.get_settings", lambda: fake_settings
    )

    async def _boom(*a, **k):
        raise AssertionError("haiku must NOT be called for reference-doc imports")

    monkeypatch.setattr("backend.agents.router.haiku", AsyncMock(side_effect=_boom))
    return tmp_path


def _raw(vault: Path, name: str, content: str) -> Path:
    p = vault / "Brain" / "raw" / name
    p.write_text(content, encoding="utf-8")
    return p


@pytest.mark.asyncio
async def test_large_file_with_images_imported_verbatim(vault):
    # >50KB body so it trips the too_large branch.
    body = "# Patio Build Guide\n\n" + ("Lorem ipsum filler line.\n" * 3000)
    assert len(body.encode()) > wiki_ingest.MAX_RAW_FILE_BYTES
    content = (
        body
        + f"\n\n![Foundation Diagram](data:image/png;base64,{_PNG_B64})\n"
        + "\nMore text after the image.\n"
    )
    src = _raw(vault, "patio-build-guide.md", content)

    result = await wiki_ingest.ingest_file(str(src))

    assert result["reference_doc_imported"] is True
    assert result["images"] == 1

    wiki = vault / "Brain" / "wiki"
    page = wiki / "patio-build-guide.md"
    assert page.exists()
    written = page.read_text(encoding="utf-8")

    # Full text preserved (not summarized).
    assert "Patio Build Guide" in written
    assert "More text after the image." in written
    assert written.count("Lorem ipsum filler line.") == 3000

    # base64 blob stripped, Obsidian embed substituted.
    assert "data:image/png;base64" not in written
    assert "![[foundation_diagram.png]]" in written

    # Image written to disk with exact bytes.
    img = wiki / "foundation_diagram.png"
    assert img.exists()
    assert img.read_bytes() == _PNG_BYTES

    # Original moved to processed/.
    assert not src.exists()
    assert (wiki / "processed" / "patio-build-guide.md").exists()

    # Ledgered as seen.
    ledger = json.loads(
        (vault / "Brain" / ".wiki_ingest_state.json").read_text(encoding="utf-8")
    )
    assert "patio-build-guide.md" in ledger


@pytest.mark.asyncio
async def test_reference_doc_no_images_verbatim(vault):
    # >=8 headers, under the size cap → reference_doc branch, no base64.
    headers = "".join(f"## Section {i}\nbody {i}\n\n" for i in range(10))
    content = "# Manual\n\n" + headers
    assert len(content.encode()) < wiki_ingest.MAX_RAW_FILE_BYTES
    src = _raw(vault, "the-manual.md", content)

    result = await wiki_ingest.ingest_file(str(src))

    assert result["reference_doc_imported"] is True
    assert result["images"] == 0

    page = vault / "Brain" / "wiki" / "the-manual.md"
    assert page.exists()
    # Verbatim — exact text preserved.
    assert page.read_text(encoding="utf-8") == content.strip()

    assert not src.exists()
    assert (vault / "Brain" / "wiki" / "processed" / "the-manual.md").exists()


@pytest.mark.asyncio
async def test_duplicate_alt_text_dedupes_filenames(vault):
    # Large file with two images sharing the same alt text → deduped names.
    body = "# Guide\n\n" + ("filler line here\n" * 3000)
    assert len(body.encode()) > wiki_ingest.MAX_RAW_FILE_BYTES
    content = (
        body
        + f"\n![diagram](data:image/png;base64,{_PNG_B64})\n"
        + f"\n![diagram](data:image/png;base64,{_PNG_B64})\n"
    )
    src = _raw(vault, "dup-guide.md", content)

    result = await wiki_ingest.ingest_file(str(src))
    assert result["images"] == 2

    wiki = vault / "Brain" / "wiki"
    written = (wiki / "dup-guide.md").read_text(encoding="utf-8")

    # First keeps bare slug; second gets _1 suffix.
    assert "![[diagram.png]]" in written
    assert "![[diagram_1.png]]" in written
    assert (wiki / "diagram.png").exists()
    assert (wiki / "diagram_1.png").exists()


@pytest.mark.asyncio
async def test_daily_note_bypasses_verbatim_import(vault, monkeypatch):
    """Regression: a bare-date briefing (>=8 headers) must NOT become a
    standalone wiki/{date}.md page — it goes through extract+classify like
    any other session note, landing in the matched topic page instead."""
    headers = "".join(f"## Section {i}\nbody {i}\n\n" for i in range(10))
    content = "# Morning Briefing — 2026-07-01\n\n" + headers
    src = _raw(vault, "2026-07-01.md", content)

    (vault / "Brain" / "wiki" / "AdGuard.md").write_text(
        "# AdGuard\n\n## Existing\nprior content\n", encoding="utf-8"
    )

    extract_json = json.dumps([{"topic_hint": "adguard", "bullet": "AdGuard blocked 500 queries."}])
    monkeypatch.setattr(
        "backend.agents.router.haiku",
        AsyncMock(side_effect=[extract_json, json.dumps(["AdGuard"])]),
    )

    result = await wiki_ingest.ingest_file(str(src))

    assert result["items"] == 1
    assert result["wikis_touched"] == ["AdGuard"]

    wiki = vault / "Brain" / "wiki"
    # No verbatim date-named page was created.
    assert not (wiki / "2026-07-01.md").exists()
    # The extracted bullet landed in the existing topic page instead.
    adguard = (wiki / "AdGuard.md").read_text(encoding="utf-8")
    assert "AdGuard blocked 500 queries." in adguard
    assert "prior content" in adguard  # never rewrites existing content

    assert (wiki / "processed" / "2026-07-01.md").exists()


def test_is_features_digest():
    assert wiki_ingest._is_features_digest("claude-features-digest-2026-07-12")
    assert wiki_ingest._is_features_digest(
        "claude-features-digest-2026-07-12_20260712T043640Z"
    )
    assert not wiki_ingest._is_features_digest("claude-desktop-mcp-notes")
    assert not wiki_ingest._is_features_digest("2026-07-01")
    assert not wiki_ingest._is_features_digest("nexus-session-2026-06-25b")


@pytest.mark.asyncio
async def test_features_digest_routes_to_own_page_not_claude_md(vault, monkeypatch):
    """Regression: claude-features-digest-* must land on its own running-log
    page, never merged into the generic Claude.md page — the bare "claude"
    filename hint would otherwise prefix-match Claude.md via _match_existing_page."""
    content = "# Claude Features Digest — 2026-07-12\n\n**Some new capability** — details.\n"
    src = _raw(vault, "claude-features-digest-2026-07-12_20260712T043640Z.md", content)

    (vault / "Brain" / "wiki" / "Claude.md").write_text(
        "# Claude\n\n## Existing\nprior content\n", encoding="utf-8"
    )

    extract_json = json.dumps(
        [{"topic_hint": "claude", "bullet": "Some new capability shipped."}]
    )
    # A single-element side_effect: if the classify branch is ever reached
    # (i.e. routing wasn't forced), the second haiku() call raises StopIteration.
    monkeypatch.setattr(
        "backend.agents.router.haiku",
        AsyncMock(side_effect=[extract_json]),
    )

    result = await wiki_ingest.ingest_file(str(src))

    assert result["items"] == 1
    assert result["wikis_touched"] == ["Claude Features Digest"]

    wiki = vault / "Brain" / "wiki"
    digest_page = (wiki / "Claude Features Digest.md").read_text(encoding="utf-8")
    assert "Some new capability shipped." in digest_page
    assert "## 2026-07-12 — from" in digest_page

    # Claude.md is untouched by the digest ingest.
    claude_page = (wiki / "Claude.md").read_text(encoding="utf-8")
    assert claude_page == "# Claude\n\n## Existing\nprior content\n"

    assert (wiki / "processed" / "claude-features-digest-2026-07-12_20260712T043640Z.md").exists()


@pytest.mark.asyncio
async def test_oversized_session_dump_bypasses_verbatim_import(vault, monkeypatch):
    """Regression: a >50KB session dump (## Human/## Assistant headers) must NOT
    be verbatim-imported into wiki/ root as a fake reference doc — it goes
    through extract+classify like any session note."""
    turns = "".join(f"## Human\nquestion {i}\n\n## Assistant\nanswer {i}\n\n" for i in range(2000))
    content = (
        "---\ntitle: \"Session 2026-07-11 abc\"\ndate: 2026-07-11\n"
        "type: conversation\ntags:\n  - session-log\n  - raw\n  - unprocessed\n---\n\n"
        + turns
    )
    assert len(content.encode()) > wiki_ingest.MAX_RAW_FILE_BYTES
    src = _raw(vault, "2026-07-11-session-abc123.md", content)

    (vault / "Brain" / "wiki" / "NEXUS.md").write_text(
        "# NEXUS\n\n## Existing\nprior content\n", encoding="utf-8"
    )

    extract_json = json.dumps([{"topic_hint": "nexus", "bullet": "Fixed the wiki ingest bug."}])
    monkeypatch.setattr(
        "backend.agents.router.haiku",
        AsyncMock(side_effect=[extract_json, json.dumps(["NEXUS"])]),
    )

    result = await wiki_ingest.ingest_file(str(src))

    assert result.get("reference_doc_imported") is None
    assert result["items"] == 1

    wiki = vault / "Brain" / "wiki"
    # No verbatim session-named page was created in wiki/ root.
    assert not (wiki / "2026-07-11-session-abc123.md").exists()
    # The extracted bullet landed in the topic page.
    assert "Fixed the wiki ingest bug." in (wiki / "NEXUS.md").read_text(encoding="utf-8")
    assert (wiki / "processed" / "2026-07-11-session-abc123.md").exists()


@pytest.mark.asyncio
async def test_reprocessing_same_session_filename_overwrites_archive(vault, monkeypatch):
    """Regression: a long-running session is re-saved under the SAME filename
    on every /save. Processing an earlier snapshot archives it to processed/;
    processing a later, longer snapshot of the SAME session must overwrite
    that archive entry, not crash with FileExistsError (Windows Path.rename
    raises on an existing destination; os.replace doesn't)."""
    wiki = vault / "Brain" / "wiki"
    (wiki / "processed").mkdir(parents=True, exist_ok=True)
    # Simulate an earlier snapshot already archived under this exact name.
    (wiki / "processed" / "2026-07-11-session-xyz.md").write_text(
        "earlier, shorter snapshot", encoding="utf-8"
    )

    content = "## Human\nquestion\n\n## Assistant\nlonger, final answer\n"
    src = _raw(vault, "2026-07-11-session-xyz.md", content)

    extract_json = json.dumps([{"topic_hint": "nexus", "bullet": "Final answer recorded."}])
    monkeypatch.setattr(
        "backend.agents.router.haiku",
        AsyncMock(side_effect=[extract_json, json.dumps(["NEXUS"])]),
    )

    result = await wiki_ingest.ingest_file(str(src))

    assert "error" not in result
    assert result["items"] == 1
    assert not src.exists()
    archived = (wiki / "processed" / "2026-07-11-session-xyz.md").read_text(encoding="utf-8")
    assert archived == content  # overwritten with the newer snapshot, not left stale
    assert "Final answer recorded." in (wiki / "NEXUS.md").read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_blank_alt_uses_counter_filename(vault):
    body = "# Guide\n\n" + ("filler line here\n" * 3000)
    content = body + f"\n![](data:image/png;base64,{_PNG_B64})\n"
    src = _raw(vault, "blank-alt.md", content)

    result = await wiki_ingest.ingest_file(str(src))
    assert result["images"] == 1

    wiki = vault / "Brain" / "wiki"
    written = (wiki / "blank-alt.md").read_text(encoding="utf-8")
    assert "![[image_1.png]]" in written
    assert (wiki / "image_1.png").exists()


# ---------------------------------------------------------------------------
# _is_daily_note drift guard (robustness spec §8.3, criterion 58)
#
# brain_organizer.py runs in its own venv (modules/brain-organizer/venv);
# wiki_ingest.py runs in NEXUS's. A shared import across venvs isn't
# available, so this hand-copied twin can only be kept honest by comparing
# source text directly. This is exactly the mechanism that would have caught
# F1 at edit time (the "event-hermes-" literal was generalized in one file
# and left stale in the other).
# ---------------------------------------------------------------------------

_ORGANIZER_PATH = (
    Path(__file__).resolve().parent.parent
    / "modules"
    / "brain-organizer"
    / "brain_organizer.py"
)
_INGEST_PATH = Path(__file__).resolve().parent.parent / "backend" / "agents" / "wiki_ingest.py"

_MIRRORED_PATTERN_NAMES = (
    "_DAILY_NOTE_STEM_PAT",
    "_DAILY_NOTE_NAME_PAT",
    "_DATE_IN_STEM_PAT",
    "_EVENT_NOTE_PREFIX_PAT",
)


def _module_assignment_source(source: str, name: str) -> str:
    """Return the normalized source of the top-level `name = ...` assignment."""
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == name for t in node.targets
        ):
            return ast.unparse(node.value)
    raise AssertionError(f"module-level assignment {name!r} not found")


def _function_body_source(source: str, name: str) -> str:
    """Return the normalized source of a top-level function's body, with its
    docstring stripped (the two files' docstrings intentionally cross-
    reference each other by differing file path, so they must NOT be part
    of the equality check -- only the executable logic must match)."""
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                body = body[1:]
            return "\n".join(ast.unparse(stmt) for stmt in body)
    raise AssertionError(f"top-level function {name!r} not found")


def test_daily_note_guard_stays_in_sync_with_brain_organizer_mirror() -> None:
    organizer_src = _ORGANIZER_PATH.read_text(encoding="utf-8")
    ingest_src = _INGEST_PATH.read_text(encoding="utf-8")

    for pat_name in _MIRRORED_PATTERN_NAMES:
        organizer_pat = _module_assignment_source(organizer_src, pat_name)
        ingest_pat = _module_assignment_source(ingest_src, pat_name)
        assert organizer_pat == ingest_pat, (
            f"{pat_name} diverged between brain_organizer.py and wiki_ingest.py -- "
            "the daily-note guard is a hand-copied mirror; both must change together"
        )

    organizer_body = _function_body_source(organizer_src, "_is_daily_note")
    ingest_body = _function_body_source(ingest_src, "_is_daily_note")
    assert organizer_body == ingest_body, (
        "_is_daily_note body diverged between brain_organizer.py and wiki_ingest.py -- "
        "the daily-note guard is a hand-copied mirror; both must change together"
    )
