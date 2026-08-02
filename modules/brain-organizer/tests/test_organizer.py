"""Unit tests for brain_organizer.py."""
from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import anthropic
import brain_organizer as bo
import pytest
from anthropic.types import TextBlock

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def write_raw(vault: Path, name: str, content: str) -> Path:
    f = vault / "raw" / name
    f.write_text(content, encoding="utf-8")
    return f


def make_message(text: str, stop_reason: str = "end_turn") -> MagicMock:
    """Build a mock Message with a real TextBlock so isinstance checks pass."""
    msg = MagicMock()
    msg.content = [TextBlock(type="text", text=text)]
    msg.stop_reason = stop_reason
    return msg


def make_client(routes_text: str, wiki_text: str, stop_reason: str = "end_turn") -> MagicMock:
    """Build a mock client with a routing response followed by a wiki synthesis response.

    routes_text must already be in the new routes JSON shape:
        '{"routes": [{"title":"...", "match": "new"}]}'
    """
    client = MagicMock()
    client.messages.create.side_effect = [
        make_message(routes_text),
        make_message(wiki_text, stop_reason=stop_reason),
    ]
    return client


# ---------------------------------------------------------------------------
# scan_raw_folder
# ---------------------------------------------------------------------------

def test_scan_raw_folder_finds_new_files(tmp_vault: Path, tmp_config: dict[str, Any]) -> None:
    write_raw(tmp_vault, "note.md", "Hello world")
    write_raw(tmp_vault, "memo.txt", "Some memo")
    results = bo.scan_raw_folder(tmp_config, {})
    assert len(results) == 2
    names = {f.name for f, _ in results}
    assert "note.md" in names
    assert "memo.txt" in names


def test_scan_raw_folder_skips_already_processed(tmp_vault: Path, tmp_config: dict[str, Any]) -> None:
    f = write_raw(tmp_vault, "done.md", "Already processed")
    sha = bo.compute_sha256(f)
    processed = {sha: {"filename": "done.md", "timestamp": "2026-01-01", "topics": []}}
    results = bo.scan_raw_folder(tmp_config, processed)
    assert results == []


def test_scan_raw_folder_ignores_non_md_txt(tmp_vault: Path, tmp_config: dict[str, Any]) -> None:
    (tmp_vault / "raw" / "image.png").write_bytes(b"\x89PNG")
    (tmp_vault / "raw" / "data.json").write_text("{}", encoding="utf-8")
    results = bo.scan_raw_folder(tmp_config, {})
    assert results == []


def test_scan_raw_folder_retries_failed_file_under_max_attempts(
    tmp_vault: Path, tmp_config: dict[str, Any]
) -> None:
    f = write_raw(tmp_vault, "bad.md", "Content")
    sha = bo.compute_sha256(f)
    processed = {sha: {"filename": "bad.md", "status": "failed", "attempts": 2}}
    results = bo.scan_raw_folder(tmp_config, processed)
    assert len(results) == 1


def test_scan_raw_folder_skips_permanently_failed_file(
    tmp_vault: Path, tmp_config: dict[str, Any]
) -> None:
    tmp_config["max_file_attempts"] = 3
    f = write_raw(tmp_vault, "bad.md", "Content")
    sha = bo.compute_sha256(f)
    processed = {sha: {"filename": "bad.md", "status": "failed", "attempts": 3}}
    results = bo.scan_raw_folder(tmp_config, processed)
    assert results == []


def test_scan_raw_folder_finds_files_in_subfolders(tmp_vault: Path, tmp_config: dict[str, Any]) -> None:
    sub = tmp_vault / "raw" / "work"
    sub.mkdir(parents=True, exist_ok=True)
    (sub / "meeting.md").write_text("Meeting notes", encoding="utf-8")
    results = bo.scan_raw_folder(tmp_config, {})
    assert any(p.name == "meeting.md" for p, _ in results)


def test_scan_raw_folder_excludes_backup_subfolder(tmp_vault: Path, tmp_config: dict[str, Any]) -> None:
    backup = tmp_vault / "raw" / "backups"
    backup.mkdir(parents=True, exist_ok=True)
    (backup / "old-backup.md").write_text("Old backup", encoding="utf-8")
    results = bo.scan_raw_folder(tmp_config, {})
    assert results == []


def test_scan_raw_folder_skips_zero_byte_file(
    tmp_vault: Path, tmp_config: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    write_raw(tmp_vault, "empty.md", "")
    with caplog.at_level(logging.WARNING, logger="brain_organizer"):
        results = bo.scan_raw_folder(tmp_config, {})
    assert results == []
    assert "empty.md" in caplog.text


def test_scan_raw_folder_skips_whitespace_only_file(
    tmp_vault: Path, tmp_config: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    write_raw(tmp_vault, "blank.md", "   \n\t\n  ")
    with caplog.at_level(logging.WARNING, logger="brain_organizer"):
        results = bo.scan_raw_folder(tmp_config, {})
    assert results == []
    assert "blank.md" in caplog.text


def test_scan_raw_folder_reprocesses_when_success_record_filename_differs(
    tmp_vault: Path, tmp_config: dict[str, Any]
) -> None:
    f = write_raw(tmp_vault, "renamed.md", "Same content")
    sha = bo.compute_sha256(f)
    processed = {sha: {"filename": "original.md", "timestamp": "2026-01-01", "topics": []}}
    results = bo.scan_raw_folder(tmp_config, processed)
    assert len(results) == 1
    assert results[0][0].name == "renamed.md"


def test_scan_raw_folder_skips_when_success_record_filename_matches(
    tmp_vault: Path, tmp_config: dict[str, Any]
) -> None:
    f = write_raw(tmp_vault, "done.md", "Same content")
    sha = bo.compute_sha256(f)
    processed = {sha: {"filename": "done.md", "timestamp": "2026-01-01", "topics": []}}
    results = bo.scan_raw_folder(tmp_config, processed)
    assert results == []


def test_scan_raw_folder_failed_record_ignores_filename_drift(
    tmp_vault: Path, tmp_config: dict[str, Any]
) -> None:
    tmp_config["max_file_attempts"] = 3
    f = write_raw(tmp_vault, "renamed-bad.md", "Content")
    sha = bo.compute_sha256(f)
    processed = {sha: {"filename": "original-bad.md", "status": "failed", "attempts": 3}}
    results = bo.scan_raw_folder(tmp_config, processed)
    assert results == []


# ---------------------------------------------------------------------------
# backup_file
# ---------------------------------------------------------------------------

def test_backup_before_processing(tmp_vault: Path, tmp_config: dict[str, Any]) -> None:
    f = write_raw(tmp_vault, "note.md", "Backup me")
    backup_path = bo.backup_file(tmp_config, f)
    assert backup_path.exists()
    assert backup_path.read_text(encoding="utf-8") == "Backup me"
    assert backup_path.parent == tmp_vault / "raw" / "backups"
    assert "note.md" in backup_path.name


def test_backup_creates_timestamped_filename(tmp_vault: Path, tmp_config: dict[str, Any]) -> None:
    f = write_raw(tmp_vault, "note.md", "content")
    backup_path = bo.backup_file(tmp_config, f)
    assert backup_path.name.endswith("note.md")
    assert backup_path.name.count("-") >= 4


# ---------------------------------------------------------------------------
# detect_topics
# ---------------------------------------------------------------------------

def test_topic_detection_returns_valid_json(tmp_config: dict[str, Any]) -> None:
    client = MagicMock()
    client.messages.create.return_value = make_message(
        '{"routes": [{"title":"NEXUS", "match": "new"}, {"title":"Home Assistant", "match": "new"}]}'
    )
    topics = bo.detect_topics("Some content about NEXUS and Home Assistant", tmp_config, client)
    assert topics == ["NEXUS", "Home Assistant"]


def test_topic_detection_falls_back_on_bad_json(tmp_config: dict[str, Any]) -> None:
    client = MagicMock()
    client.messages.create.return_value = make_message("not json at all")
    topics = bo.detect_topics("content", tmp_config, client)
    assert topics == ["Uncategorized"]


def test_topic_detection_falls_back_on_empty_list(tmp_config: dict[str, Any]) -> None:
    client = MagicMock()
    client.messages.create.return_value = make_message('{"routes": []}')
    topics = bo.detect_topics("content", tmp_config, client)
    assert topics == ["Uncategorized"]


def test_topic_detection_caps_at_five(tmp_config: dict[str, Any]) -> None:
    client = MagicMock()
    many_routes = [{"title":t, "match": "new"} for t in ["A", "B", "C", "D", "E", "F", "G"]]
    client.messages.create.return_value = make_message(json.dumps({"routes": many_routes}))
    topics = bo.detect_topics("content", tmp_config, client)
    assert len(topics) <= 5


def test_topic_detection_uses_haiku_model(tmp_config: dict[str, Any]) -> None:
    client = MagicMock()
    client.messages.create.return_value = make_message('{"routes": [{"title":"Test", "match": "new"}]}')
    bo.detect_topics("content", tmp_config, client)
    assert client.messages.create.call_args.kwargs["model"] == tmp_config["haiku_model"]


# ---------------------------------------------------------------------------
# _is_daily_note / _daily_note_route
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("stem", [
    "2026-07-08",
    "2026-07-08b",
    "Morning-Briefing-2026-06-28",
    "Daily-Operations-Log-2026-07-02",
    "event-hermes-hermes-daily-digest-20260724T120009Z",  # no-hyphen timestamp; needs the event-hermes- carve-out (regression, spec criterion 2)
    "event-nexus-nexus-daily-digest-20260802T120508Z",  # NEXUS's own emitter; the actual F1 fix (spec criterion 1)
])
def test_is_daily_note_matches_dated_and_briefing_stems(stem: str) -> None:
    assert bo._is_daily_note(stem) is True


@pytest.mark.parametrize("stem", [
    "NEXUS",
    "nexus-session-2026-06-25b-ha-cover-lock-fix",
    "Daily-Driver-Setup",  # "daily" substring, no date, not a hermes event -- must NOT be hijacked
    "Briefing-Template",
    "event-council-loop-run-complete-20260802T070438Z",  # no daily/briefing token (spec criterion 3)
    "event-nexus-goal-completed-20260802T142558Z",  # no daily/briefing token (spec criterion 4)
])
def test_is_daily_note_rejects_non_daily_stems(stem: str) -> None:
    assert bo._is_daily_note(stem) is False


def test_daily_note_route_returns_none_for_non_daily(tmp_path: Path) -> None:
    assert bo._daily_note_route("NEXUS-session-notes", [], tmp_path, tmp_path / "daily") is None


@pytest.mark.parametrize("title", [
    "2026-07-11-session-2f47f674-a17b-4eb0-adb1-56b7ccf4b6aa",  # seen live 2026-07-11
    "bb94406a-faf4-4f0e-833a-47d1a55df36c",                     # bare UUID
    "NEXUS Session Notes",                                       # session token
    "2026-07-08 Homelab Work",                                   # date-prefixed log name
])
def test_looks_like_session_title_rejects_log_shaped(title: str) -> None:
    assert bo._looks_like_session_title(title) is True


@pytest.mark.parametrize("title", [
    "Home-Assistant",
    "Council-Loop-Build-2026-07-01",  # date at END is fine — real page convention
    "Budgeting",
    "Sessions-Overview",              # 'session' as substring, not standalone token...
])
def test_looks_like_session_title_allows_real_topics(title: str) -> None:
    assert bo._looks_like_session_title(title) is False


def test_route_topics_rejects_session_shaped_new_title(
    tmp_config: dict[str, Any],
) -> None:
    """A Haiku 'new' route whose title is session-log-shaped must be dropped;
    with no other routes, content falls back to Uncategorized instead of
    creating a filename-titled page."""
    client = MagicMock()
    client.messages.create.return_value = make_message(
        '{"routes": [{"match": "new", '
        '"title": "2026-07-11-session-2f47f674-a17b-4eb0-adb1-56b7ccf4b6aa"}]}'
    )
    routes = bo.route_topics("some session content", [], tmp_config, client)
    assert len(routes) == 1
    title, path, is_new = routes[0]
    assert title == "Uncategorized"
    assert path.name == "Uncategorized.md"


def test_daily_note_route_creates_canonical_date_page(tmp_path: Path) -> None:
    """Date-stem notes route into daily_folder (a subfolder), NOT wiki_folder
    root -- kept out of build_wiki_catalog's non-recursive scan on purpose."""
    wiki_folder = tmp_path / "wiki"
    daily_folder = wiki_folder / "daily"
    routes = bo._daily_note_route("Daily-Operations-Log-2026-07-08", [], wiki_folder, daily_folder)
    assert routes == [("2026-07-08", daily_folder / "2026-07-08.md", True)]


def test_daily_note_route_reuses_existing_date_page(tmp_path: Path) -> None:
    """is_new is decided by path.exists() directly (not a catalog scan --
    the catalog can't see subfolder pages at all)."""
    wiki_folder = tmp_path / "wiki"
    daily_folder = wiki_folder / "daily"
    daily_folder.mkdir(parents=True)
    existing = daily_folder / "2026-07-08.md"
    existing.write_text("# 2026-07-08\n", encoding="utf-8")

    routes = bo._daily_note_route("Morning-Briefing-2026-07-08", [], wiki_folder, daily_folder)

    assert routes == [("2026-07-08", existing, False)]


def test_daily_note_route_second_note_same_day_merges(tmp_path: Path) -> None:
    """Two daily notes on the same date must resolve to the identical path
    (is_new True then False), not two different files."""
    wiki_folder = tmp_path / "wiki"
    daily_folder = wiki_folder / "daily"

    first = bo._daily_note_route("2026-07-08", [], wiki_folder, daily_folder)
    assert first == [("2026-07-08", daily_folder / "2026-07-08.md", True)]

    first[0][1].parent.mkdir(parents=True, exist_ok=True)
    first[0][1].write_text("content", encoding="utf-8")

    second = bo._daily_note_route("Morning-Briefing-2026-07-08", [], wiki_folder, daily_folder)
    assert second == [("2026-07-08", daily_folder / "2026-07-08.md", False)]


def test_daily_note_route_non_date_stem_falls_back_to_daily_log_at_root(tmp_path: Path) -> None:
    """A daily/briefing-named file with NO date in its stem (e.g. the Hermes
    digest's non-hyphenated timestamp) routes to the shared Daily-Log.md at
    wiki root instead -- a real synthesized topic page, stays in the catalog."""
    wiki_folder = tmp_path / "wiki"
    daily_folder = wiki_folder / "daily"

    routes = bo._daily_note_route(
        "event-hermes-hermes-daily-digest-20260724T120009Z", [], wiki_folder, daily_folder
    )

    assert routes == [("Daily-Log", wiki_folder / "Daily-Log.md", True)]


def test_daily_note_route_reuses_existing_daily_log_catalog_entry(tmp_path: Path) -> None:
    wiki_folder = tmp_path / "wiki"
    daily_folder = wiki_folder / "daily"
    catalog = [{
        "title": "Daily-Log", "filename": "Daily-Log.md",
        "path_str": str(wiki_folder / "Daily-Log.md"), "headers": "", "summary": "",
    }]

    routes = bo._daily_note_route(
        "event-hermes-hermes-daily-digest-20260724T120009Z", catalog, wiki_folder, daily_folder
    )

    assert routes == [("Daily-Log", wiki_folder / "Daily-Log.md", False)]



# ---------------------------------------------------------------------------
# _extract_page_entry (BOM-safe decode) / build_wiki_catalog (parser_version)
# ---------------------------------------------------------------------------

def test_extract_page_entry_strips_bom_from_title(tmp_path: Path) -> None:
    """A BOM-prefixed .md file (utf-8-sig decode) must not leak the BOM
    character into the extracted title."""
    f = tmp_path / "page.md"
    f.write_bytes(b"\xef\xbb\xbf# My Title\n\nSome body text.\n")

    entry = bo._extract_page_entry(f)

    assert entry["title"] == "My Title"
    assert "﻿" not in entry["title"]


def test_extract_page_entry_bom_less_file_parses_unchanged(tmp_path: Path) -> None:
    """The common (no-BOM) case must parse identically to before the
    utf-8-sig change -- title, headers, and summary all as documented."""
    f = tmp_path / "page.md"
    f.write_text(
        "# My Title\n\nSome body text.\n\n## Header One\n", encoding="utf-8"
    )

    entry = bo._extract_page_entry(f)

    assert entry["title"] == "My Title"
    assert entry["headers"] == "Header One"
    assert entry["summary"] == "Some body text."


def test_build_wiki_catalog_missing_parser_version_forces_full_reparse(
    tmp_path: Path,
) -> None:
    """A pre-upgrade cache with no 'parser_version' key must be treated as a
    full cache miss -- every page is re-parsed, not reused verbatim."""
    wiki_folder = tmp_path / "wiki"
    meta_folder = tmp_path / "_meta"
    wiki_folder.mkdir()
    page = wiki_folder / "Alpha.md"
    page.write_text("# Alpha\n\nOriginal body.\n", encoding="utf-8")

    meta_folder.mkdir()
    cache_path = meta_folder / "wiki-catalog.json"
    cache_path.write_text(
        json.dumps(
            {
                "built_at": datetime.now(UTC).isoformat(),
                # deliberately no "parser_version" key
                "pages": [{
                    "title": "Alpha", "filename": "Alpha.md",
                    "path_str": str(page), "headers": "",
                    "summary": "STALE CACHED SUMMARY",
                }],
            }
        ),
        encoding="utf-8",
    )

    pages = bo.build_wiki_catalog(wiki_folder, meta_folder)

    assert len(pages) == 1
    assert pages[0]["summary"] == "Original body."


def test_build_wiki_catalog_mismatched_parser_version_forces_full_reparse(
    tmp_path: Path,
) -> None:
    """A cache stamped with an older/different parser_version must also be
    treated as a full cache miss."""
    wiki_folder = tmp_path / "wiki"
    meta_folder = tmp_path / "_meta"
    wiki_folder.mkdir()
    page = wiki_folder / "Alpha.md"
    page.write_text("# Alpha\n\nOriginal body.\n", encoding="utf-8")

    meta_folder.mkdir()
    cache_path = meta_folder / "wiki-catalog.json"
    cache_path.write_text(
        json.dumps(
            {
                "built_at": datetime.now(UTC).isoformat(),
                "parser_version": bo._CATALOG_PARSER_VERSION - 1,
                "pages": [{
                    "title": "Alpha", "filename": "Alpha.md",
                    "path_str": str(page), "headers": "",
                    "summary": "STALE CACHED SUMMARY",
                }],
            }
        ),
        encoding="utf-8",
    )

    pages = bo.build_wiki_catalog(wiki_folder, meta_folder)

    assert len(pages) == 1
    assert pages[0]["summary"] == "Original body."


def test_build_wiki_catalog_matching_parser_version_hits_fast_path(
    tmp_path: Path,
) -> None:
    """Regression check: a cache with a matching parser_version and
    unchanged mtimes must still reuse the cached entry (no re-parse)."""
    wiki_folder = tmp_path / "wiki"
    meta_folder = tmp_path / "_meta"
    wiki_folder.mkdir()
    page = wiki_folder / "Alpha.md"
    page.write_text("# Alpha\n\nOriginal body.\n", encoding="utf-8")

    meta_folder.mkdir()
    cache_path = meta_folder / "wiki-catalog.json"
    # built_at must be >= the page's mtime for the fast path's mtime check
    # (f.stat().st_mtime <= built_at_ts) to pass.
    future_built_at = datetime.now(UTC) + timedelta(days=1)
    cache_path.write_text(
        json.dumps(
            {
                "built_at": future_built_at.isoformat(),
                "parser_version": bo._CATALOG_PARSER_VERSION,
                "pages": [{
                    "title": "Alpha", "filename": "Alpha.md",
                    "path_str": str(page), "headers": "",
                    "summary": "CACHED SUMMARY SHOULD BE REUSED",
                }],
            }
        ),
        encoding="utf-8",
    )

    pages = bo.build_wiki_catalog(wiki_folder, meta_folder)

    assert len(pages) == 1
    assert pages[0]["summary"] == "CACHED SUMMARY SHOULD BE REUSED"


def test_process_file_skips_llm_route_for_daily_note(
    tmp_vault: Path, tmp_config: dict[str, Any]
) -> None:
    f = write_raw(tmp_vault, "2026-07-08.md", "Morning briefing content")
    client = MagicMock()
    # Only the synthesis call should hit the client — a routing call would be
    # the FIRST side_effect entry, so a single-element queue proves route_topics
    # (and its Haiku call) was never invoked.
    client.messages.create.return_value = make_message("Synthesized wiki content")
    bo.process_file(f, tmp_config, client, logging.getLogger("test"), catalog=[])
    assert client.messages.create.call_count == 1


# ---------------------------------------------------------------------------
# synthesize_wiki
# ---------------------------------------------------------------------------

def test_wiki_merge_preserves_existing_content(tmp_config: dict[str, Any]) -> None:
    client = MagicMock()
    existing = "# NEXUS\n\n## Overview\n\nExisting important content."
    new = "Some new notes."
    client.messages.create.return_value = make_message("# NEXUS\n\n## Overview\n\nMerged.")
    bo.synthesize_wiki("NEXUS", new, existing, tmp_config, client)
    prompt_sent = client.messages.create.call_args.kwargs["messages"][0]["content"]
    assert "Existing important content." in prompt_sent
    assert "Some new notes." in prompt_sent


def test_wiki_create_for_new_topic(tmp_config: dict[str, Any]) -> None:
    client = MagicMock()
    client.messages.create.return_value = make_message("# NewTopic\n\n## Info\n\nContent.")
    result = bo.synthesize_wiki("NewTopic", "raw content", "", tmp_config, client)
    assert result == "# NewTopic\n\n## Info\n\nContent."
    prompt_sent = client.messages.create.call_args.kwargs["messages"][0]["content"]
    assert "Existing Wiki" not in prompt_sent


def test_wiki_synthesis_uses_sonnet_model(tmp_config: dict[str, Any]) -> None:
    client = MagicMock()
    client.messages.create.return_value = make_message("# Topic\n")
    bo.synthesize_wiki("Topic", "content", "", tmp_config, client)
    assert client.messages.create.call_args.kwargs["model"] == tmp_config["sonnet_model"]


def test_wiki_synthesis_raises_on_max_tokens_truncation(tmp_config: dict[str, Any]) -> None:
    """A max_tokens stop_reason must raise rather than silently write truncated content."""
    client = MagicMock()
    client.messages.create.return_value = make_message(
        "# Topic\n\nTruncated...", stop_reason="max_tokens"
    )
    with pytest.raises(ValueError, match="max_tokens"):
        bo.synthesize_wiki("Topic", "content", "", tmp_config, client)


def test_wiki_synthesis_uses_sonnet_max_tokens(tmp_config: dict[str, Any]) -> None:
    tmp_config["sonnet_max_tokens"] = 4096
    client = MagicMock()
    client.messages.create.return_value = make_message("# Topic\n")
    bo.synthesize_wiki("Topic", "content", "", tmp_config, client)
    assert client.messages.create.call_args.kwargs["max_tokens"] == 4096


def test_wiki_create_branch_related_links_use_stem_alias(tmp_config: dict[str, Any]) -> None:
    """Spec #1 crit 9: CREATE-branch related-page wikilinks render as
    [[stem|title]] (Obsidian resolves by filename, not title), omit the alias
    when stem == title, and degrade to the bare [[title]] form when a catalog
    entry has no "filename" key at all -- rather than KeyError.
    """
    catalog = [
        {"title": "Bug Fixes", "filename": "Bug-Fixes.md", "path_str": "x", "headers": "", "summary": ""},
        {"title": "NEXUS", "filename": "NEXUS.md", "path_str": "x", "headers": "", "summary": ""},
        {"title": "Old Notes", "path_str": "x", "headers": "", "summary": ""},  # no filename key
    ]
    client = MagicMock()
    client.messages.create.return_value = make_message("# New Topic\n\nBody.")
    bo.synthesize_wiki("New Topic", "content", "", tmp_config, client, catalog=catalog)
    prompt_sent = client.messages.create.call_args.kwargs["messages"][0]["content"]

    # (a) stem != title -> aliased link, hyphenated stem before the "|"
    assert "[[Bug-Fixes|Bug Fixes]]" in prompt_sent
    assert "[[Bug Fixes]]" not in prompt_sent

    # (b) stem == title -> alias omitted (not [[NEXUS|NEXUS]])
    assert "[[NEXUS]]" in prompt_sent
    assert "[[NEXUS|NEXUS]]" not in prompt_sent

    # (c) missing "filename" key -> degrades to the old bare-title form, no
    # KeyError raised by synthesize_wiki
    assert "[[Old Notes]]" in prompt_sent


# ---------------------------------------------------------------------------
# _defuse_unknown_wikilinks -- prevents Haiku/Sonnet from wikilinking things
# that aren't real vault pages (e.g. Claude Code memory-file names mentioned
# in source material), which was producing permanently broken links.
# ---------------------------------------------------------------------------

def test_defuse_unknown_wikilinks_leaves_real_catalog_page_alone() -> None:
    catalog = [{"title": "NEXUS", "filename": "NEXUS.md", "path_str": "x", "headers": "", "summary": ""}]
    text = "See [[NEXUS]] for details."
    result = bo._defuse_unknown_wikilinks(text, "Other Topic", catalog)
    assert result == text


def test_defuse_unknown_wikilinks_rewrites_exact_title_match_to_stem() -> None:
    """Criterion 1: an exact catalog-title match (resolver step 3) rewrites
    the title-form link into filename space, aliasing back to the original
    title text since the stem and title differ.
    """
    catalog = [{"title": "Bug Fixes", "filename": "Bug-Fixes.md", "path_str": "x", "headers": "", "summary": ""}]
    text = "See [[Bug Fixes]]."
    result = bo._defuse_unknown_wikilinks(text, "Other Topic", catalog)
    assert result == "See [[Bug-Fixes|Bug Fixes]]."


def test_defuse_unknown_wikilinks_rewrites_exact_title_match_preserves_em_dash() -> None:
    """Criterion 2: an exact title containing em-dash/en-dash punctuation
    survives the rewrite unmangled as the alias display text.
    """
    title = "Build Log: CWI AI — Passes 1–13, 13–32"
    catalog = [{"title": title, "filename": "Build-Log.md", "path_str": "x", "headers": "", "summary": ""}]
    text = f"See [[{title}]] for details."
    result = bo._defuse_unknown_wikilinks(text, "Other Topic", catalog)
    assert result == f"See [[Build-Log|{title}]] for details."


def test_defuse_unknown_wikilinks_rewrites_heading_fragment_on_resolved_match() -> None:
    """Criterion 7: a heading fragment on a link that DOES resolve (exact
    title match) is preserved through the rewrite -- distinct from
    test_defuse_unknown_wikilinks_preserves_heading_fragment above, which
    only covers the unknown-target backtick-defuse branch.
    """
    catalog = [{"title": "Bug Fixes", "filename": "Bug-Fixes.md", "path_str": "x", "headers": "", "summary": ""}]
    text = "See [[Bug Fixes#Setup]]."
    result = bo._defuse_unknown_wikilinks(text, "Other Topic", catalog)
    assert result == "See [[Bug-Fixes#Setup|Bug Fixes]]."


def test_defuse_unknown_wikilinks_resolves_case_insensitive_stem_and_title() -> None:
    """Resolver steps 4 and 5: a target matching the filename stem or the
    catalog title only case-insensitively still resolves to the real stem,
    with the original (differently-cased) text preserved as the alias.
    """
    catalog = [{"title": "Bug Fixes", "filename": "Bug-Fixes.md", "path_str": "x", "headers": "", "summary": ""}]
    # step 4: case-insensitive stem match
    result_stem = bo._defuse_unknown_wikilinks("See [[bug-fixes]].", "Other Topic", catalog)
    assert result_stem == "See [[Bug-Fixes|bug-fixes]]."
    # step 5: case-insensitive title match
    result_title = bo._defuse_unknown_wikilinks("See [[BUG FIXES]].", "Other Topic", catalog)
    assert result_title == "See [[Bug-Fixes|BUG FIXES]]."


def test_defuse_unknown_wikilinks_ci_stem_match_independent_of_fuzzy_fallback() -> None:
    """Step 4 (case-insensitive stem match) must do real work on its own --
    not merely happen to be covered by step 6's fuzzy fallback. Chosen so the
    filename stem ("FAQ") and the catalog title ("Frequently Asked
    Questions") are dissimilar enough that find_similar_page's normalized
    ratio (~0.23, well under the 0.82 default threshold) would NOT resolve
    this on its own -- only the direct case-insensitive stem lookup can.
    """
    catalog = [{"title": "Frequently Asked Questions", "filename": "FAQ.md", "path_str": "x", "headers": "", "summary": ""}]
    assert bo.find_similar_page("faq", catalog) is None  # confirms no fuzzy safety net for this pair
    result = bo._defuse_unknown_wikilinks("See [[faq]] for help.", "Other Topic", catalog)
    assert result == "See [[FAQ|faq]] for help."


def test_defuse_unknown_wikilinks_exact_title_match_independent_of_fuzzy_fallback() -> None:
    """Step 3 (exact title match) must do real work on its own. An ordinary
    title always self-matches at fuzzy ratio 1.0, which happens to also
    satisfy step 6 -- so this uses a punctuation-only title ("***"), whose
    _normalize_title output is the empty string. find_similar_page's guard
    clause (`if not norm_title: return None`) means step 6 can NEVER
    resolve this target regardless of catalog content, isolating step 3 as
    the only path capable of rewriting it correctly.
    """
    catalog = [{"title": "***", "filename": "Section-Divider.md", "path_str": "x", "headers": "", "summary": ""}]
    assert bo._normalize_title("***") == ""
    assert bo.find_similar_page("***", catalog) is None  # confirms no fuzzy safety net for this title
    result = bo._defuse_unknown_wikilinks("See [[***]] for details.", "Other Topic", catalog)
    assert result == "See [[Section-Divider|***]] for details."


def test_defuse_unknown_wikilinks_converts_unknown_target_to_backticks() -> None:
    text = "Mentioned in [[project_version_scheme]] during the session."
    result = bo._defuse_unknown_wikilinks(text, "Other Topic", [])
    assert result == "Mentioned in `project_version_scheme` during the session."
    assert "[[" not in result


def test_defuse_unknown_wikilinks_preserves_alias_display_text() -> None:
    text = "See [[project_version_scheme|the versioning note]] for details."
    result = bo._defuse_unknown_wikilinks(text, "Other Topic", [])
    assert result == "See `the versioning note` for details."


def test_defuse_unknown_wikilinks_allows_near_duplicate_via_find_similar_page() -> None:
    catalog = [{"title": "Financial Forecast", "filename": "Financial-Forecast.md", "path_str": "x", "headers": "", "summary": ""}]
    text = "See [[Financial Forecasting]] for numbers."
    result = bo._defuse_unknown_wikilinks(text, "Other Topic", catalog)
    # find_similar_page recognizes the near-duplicate and the resolver
    # rewrites it into filename space (stem), keeping the original text as
    # the display alias -- it is no longer left title-form untouched.
    assert result == "See [[Financial-Forecast|Financial Forecasting]] for numbers."


def test_defuse_unknown_wikilinks_allows_self_reference() -> None:
    text = "This page is about [[My New Topic]] specifically."
    result = bo._defuse_unknown_wikilinks(text, "My New Topic", [])
    assert result == text


def test_defuse_unknown_wikilinks_leaves_embed_untouched() -> None:
    text = "See ![[diagram.png]] above for the layout."
    result = bo._defuse_unknown_wikilinks(text, "Other Topic", [])
    assert result == text
    assert "![[diagram.png]]" in result


def test_defuse_unknown_wikilinks_preserves_heading_fragment() -> None:
    text = "See [[project_version_scheme#Rollout]] for details."
    result = bo._defuse_unknown_wikilinks(text, "Other Topic", [])
    assert result == "See `project_version_scheme#Rollout` for details."


def test_synthesize_wiki_defuses_hallucinated_link_in_create_branch(tmp_config: dict[str, Any]) -> None:
    catalog = [{"title": "NEXUS", "filename": "NEXUS.md", "path_str": "x", "headers": "", "summary": "NEXUS stuff"}]
    client = MagicMock()
    client.messages.create.return_value = make_message(
        "# Topic\n\nSee [[NEXUS]] and also [[project_version_scheme]] for context."
    )
    result = bo.synthesize_wiki("Topic", "content", "", tmp_config, client, catalog=catalog)
    assert "[[NEXUS]]" in result
    assert "[[project_version_scheme]]" not in result
    assert "`project_version_scheme`" in result


def test_defuse_unknown_wikilinks_fuzzy_match_without_filename_degrades_gracefully() -> None:
    """Engineer-flagged gap: find_similar_page can fuzzy-match a catalog entry
    that has no "filename" field at all (title-only entry). Step 6 of the
    resolver must not raise and must not emit broken [[#...]] syntax with an
    empty stem -- it degrades to leaving the original link untouched, same as
    the documented title-only-entry graceful degradation.
    """
    catalog = [{"title": "Financial Forecast", "path_str": "x", "headers": "", "summary": ""}]
    text = "See [[Financial Forecasting]] for numbers."
    result = bo._defuse_unknown_wikilinks(text, "Other Topic", catalog)
    assert result == text  # left unchanged -- no filename to rewrite to
    assert "[[#" not in result
    assert "[[|" not in result


def test_defuse_unknown_wikilinks_malformed_threshold_string_falls_back_to_default() -> None:
    """A non-numeric new_page_similarity_threshold must not raise a TypeError
    mid-synthesis -- it clamps to the 0.82 default and still resolves a
    near-duplicate match exactly as the untouched default would.
    """
    catalog = [{"title": "Financial Forecast", "filename": "Financial-Forecast.md", "path_str": "x", "headers": "", "summary": ""}]
    text = "See [[Financial Forecasting]] for numbers."
    result = bo._defuse_unknown_wikilinks(text, "Other Topic", catalog, threshold="not-a-number")
    assert result == "See [[Financial-Forecast|Financial Forecasting]] for numbers."


def test_defuse_unknown_wikilinks_out_of_range_high_threshold_falls_back_to_default() -> None:
    """A threshold > 1.0 would make find_similar_page's ratio check
    unsatisfiable (ratio never exceeds 1.0), permanently defusing every
    near-duplicate -- the clamp must fall back to 0.82 instead so the
    near-duplicate still resolves.
    """
    catalog = [{"title": "Financial Forecast", "filename": "Financial-Forecast.md", "path_str": "x", "headers": "", "summary": ""}]
    text = "See [[Financial Forecasting]] for numbers."
    result = bo._defuse_unknown_wikilinks(text, "Other Topic", catalog, threshold=5.0)
    assert result == "See [[Financial-Forecast|Financial Forecasting]] for numbers."


def test_defuse_unknown_wikilinks_negative_threshold_falls_back_to_default() -> None:
    """A negative threshold would make find_similar_page's ratio check
    trivially true for anything -- the clamp must fall back to 0.82 so an
    unrelated low-similarity target is still correctly backtick-defused
    rather than silently over-matched.
    """
    catalog = [{"title": "Financial Forecast", "filename": "Financial-Forecast.md", "path_str": "x", "headers": "", "summary": ""}]
    text = "See [[Kubernetes]] for numbers."
    result = bo._defuse_unknown_wikilinks(text, "Other Topic", catalog, threshold=-5.0)
    assert result == "See `Kubernetes` for numbers."


# ---------------------------------------------------------------------------
# _normalize_title -- lowercase/strip-punctuation/stem helper shared by
# find_similar_page and the wikilink resolver above.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw, expected",
    [
        ("Financial Forecasting", "financial forecast"),
        ("Financial Forecast", "financial forecast"),
        ("Startups", "startup"),
        ("Front—End Development", "frontend development"),
    ],
)
def test_normalize_title(raw: str, expected: str) -> None:
    assert bo._normalize_title(raw) == expected


# ---------------------------------------------------------------------------
# find_similar_page -- near-duplicate title finder (spec #2 criteria 35-37)
# ---------------------------------------------------------------------------

def test_find_similar_page_returns_near_duplicate_entry() -> None:
    catalog = [{"title": "Financial Forecast", "filename": "Financial-Forecast.md", "path_str": "x", "headers": "", "summary": ""}]
    assert bo.find_similar_page("Financial Forecasting", catalog) is catalog[0]


def test_find_similar_page_returns_none_for_unrelated_title() -> None:
    catalog = [{"title": "Financial Forecast", "filename": "Financial-Forecast.md", "path_str": "x", "headers": "", "summary": ""}]
    assert bo.find_similar_page("Kubernetes", catalog) is None


def test_find_similar_page_threshold_changes_match_outcome() -> None:
    """Same inputs, different threshold kwarg -- a low threshold matches, a
    high threshold does not, proving `threshold` actually gates the ratio
    check rather than being ignored.
    """
    catalog = [{"title": "Machine Learning Overview", "filename": "ML-Overview.md", "path_str": "x", "headers": "", "summary": ""}]
    assert bo.find_similar_page("Machine Learning Notes", catalog, threshold=0.5) is catalog[0]
    assert bo.find_similar_page("Machine Learning Notes", catalog, threshold=0.95) is None


# ---------------------------------------------------------------------------
# API retry + OpenRouter fallback
# ---------------------------------------------------------------------------

def test_api_retries_on_timeout_then_succeeds(tmp_config: dict[str, Any]) -> None:
    client = MagicMock()
    client.messages.create.side_effect = [
        anthropic.APITimeoutError(request=MagicMock()),
        make_message('{"routes": [{"title":"NEXUS", "match": "new"}]}'),
    ]
    topics = bo.detect_topics("content", tmp_config, client)
    assert topics == ["NEXUS"]
    assert client.messages.create.call_count == 2


def test_api_retries_on_rate_limit_then_succeeds(tmp_config: dict[str, Any]) -> None:
    client = MagicMock()
    client.messages.create.side_effect = [
        anthropic.RateLimitError(message="rate limited", response=MagicMock(), body={}),
        make_message('{"routes": [{"title":"NEXUS", "match": "new"}]}'),
    ]
    with patch("brain_organizer.time.sleep"):
        topics = bo.detect_topics("content", tmp_config, client)
    assert topics == ["NEXUS"]


def test_openrouter_fallback_on_anthropic_failure(
    tmp_config: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """When Anthropic exhausts retries, the code falls back to OpenRouter."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-or-key")

    client = MagicMock()
    client.messages.create.side_effect = anthropic.APITimeoutError(request=MagicMock())

    or_response = {
        "choices": [{"message": {"content": '{"routes": [{"title":"NEXUS", "match": "new"}]}'}, "finish_reason": "stop"}]
    }

    with patch("brain_organizer.time.sleep"), \
         patch("brain_organizer.httpx.post") as mock_post:
        mock_post.return_value = MagicMock(
            status_code=200,
            json=lambda: or_response,
            raise_for_status=lambda: None,
        )
        topics = bo.detect_topics("content", tmp_config, client)

    assert topics == ["NEXUS"]
    mock_post.assert_called_once()
    call_json = mock_post.call_args.kwargs["json"]
    assert "openrouter.ai" in mock_post.call_args.args[0]
    assert call_json["model"].startswith("anthropic/")


def test_openrouter_fallback_fails_without_key(
    tmp_config: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    client = MagicMock()
    client.messages.create.side_effect = anthropic.APITimeoutError(request=MagicMock())

    with patch("brain_organizer.time.sleep"), pytest.raises(RuntimeError, match="OPENROUTER_API_KEY"):
        bo.detect_topics("content", tmp_config, client)


# ---------------------------------------------------------------------------
# Multi-topic atomicity (M4)
# ---------------------------------------------------------------------------

def test_multi_topic_partial_synthesis_failure_leaves_existing_wikis_intact(
    tmp_vault: Path, tmp_config: dict[str, Any]
) -> None:
    """If topic 2 synthesis fails, topic 1's existing wiki must not be overwritten."""
    existing_wiki = tmp_vault / "wiki" / "NEXUS.md"
    existing_wiki.write_text("# NEXUS\n\nOriginal content.", encoding="utf-8")

    raw_file = write_raw(tmp_vault, "note.md", "Content about NEXUS and Hermes")

    client = MagicMock()
    topic_resp = make_message(
        '{"routes": [{"title":"NEXUS", "match": "new"}, {"title":"Hermes", "match": "new"}]}'
    )
    nexus_wiki_resp = make_message("# NEXUS\n\nUpdated content.")
    hermes_fail = RuntimeError("Hermes synthesis failed")
    client.messages.create.side_effect = [topic_resp, nexus_wiki_resp, hermes_fail]

    result = bo.run(_client=client, _config=tmp_config)

    assert result == 1  # failed
    assert raw_file.exists()  # raw file not deleted
    # The existing wiki must be untouched — synthesis failed before any write happened
    assert existing_wiki.read_text(encoding="utf-8") == "# NEXUS\n\nOriginal content."


# ---------------------------------------------------------------------------
# processed.json tracking + file lifecycle
# ---------------------------------------------------------------------------

def test_processed_json_tracking(
    tmp_vault: Path, tmp_config: dict[str, Any], mock_anthropic_client: MagicMock
) -> None:
    write_raw(tmp_vault, "note.md", "NEXUS content")
    bo.run(_client=mock_anthropic_client, _config=tmp_config)
    mock_anthropic_client.messages.create.reset_mock()
    bo.run(_client=mock_anthropic_client, _config=tmp_config)
    mock_anthropic_client.messages.create.assert_not_called()


def test_raw_file_deleted_after_success(tmp_vault: Path, tmp_config: dict[str, Any]) -> None:
    raw_file = write_raw(tmp_vault, "note.md", "NEXUS content")
    client = make_client('{"routes": [{"title":"NEXUS", "match": "new"}]}', "# NEXUS\n\nWiki content.")
    result = bo.run(_client=client, _config=tmp_config)
    assert result == 0
    assert not raw_file.exists()


def test_raw_file_kept_on_failure(tmp_vault: Path, tmp_config: dict[str, Any]) -> None:
    raw_file = write_raw(tmp_vault, "note.md", "NEXUS content")
    client = MagicMock()
    topic_resp = make_message('{"routes": [{"title":"NEXUS", "match": "new"}]}')
    client.messages.create.side_effect = [topic_resp, RuntimeError("API down")]
    result = bo.run(_client=client, _config=tmp_config)
    assert result == 1
    assert raw_file.exists()


def test_failure_records_attempt_count(tmp_vault: Path, tmp_config: dict[str, Any]) -> None:
    raw_file = write_raw(tmp_vault, "note.md", "Content")
    sha = bo.compute_sha256(raw_file)
    client = MagicMock()
    client.messages.create.side_effect = [make_message('{"routes": [{"title":"NEXUS", "match": "new"}]}'), RuntimeError("fail")]
    bo.run(_client=client, _config=tmp_config)
    processed = bo.load_processed(tmp_config)
    assert processed[sha]["status"] == "failed"
    assert processed[sha]["attempts"] == 1


def test_failure_stops_retrying_after_max_attempts(
    tmp_vault: Path, tmp_config: dict[str, Any]
) -> None:
    tmp_config["max_file_attempts"] = 2
    raw_file = write_raw(tmp_vault, "bad.md", "Content")

    def make_failing_client() -> MagicMock:
        c = MagicMock()
        c.messages.create.side_effect = [make_message('{"routes": [{"title":"NEXUS", "match": "new"}]}'), RuntimeError("fail")]
        return c

    bo.run(_client=make_failing_client(), _config=tmp_config)
    bo.run(_client=make_failing_client(), _config=tmp_config)

    third = MagicMock()
    bo.run(_client=third, _config=tmp_config)
    third.messages.create.assert_not_called()


# ---------------------------------------------------------------------------
# topics-registry.json (M1)
# ---------------------------------------------------------------------------

def test_topics_registry_updated_after_success(
    tmp_vault: Path, tmp_config: dict[str, Any]
) -> None:
    write_raw(tmp_vault, "note.md", "NEXUS content")
    client = make_client('{"routes": [{"title":"NEXUS", "match": "new"}]}', "# NEXUS\n\nWiki content.")
    bo.run(_client=client, _config=tmp_config)

    registry_path = tmp_vault / "_meta" / "topics-registry.json"
    assert registry_path.exists()
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    assert "NEXUS" in registry
    assert "NEXUS.md" in registry["NEXUS"]


def test_topics_registry_records_resolved_daily_folder_path(
    tmp_vault: Path, tmp_config: dict[str, Any]
) -> None:
    """A daily note's registry entry must point at the file that was
    ACTUALLY written (wiki/daily/{date}.md), not a reconstructed wiki-root
    path that was never created there (real bug found live 2026-07-25:
    update_topics_registry used to rebuild the path from wiki_folder alone)."""
    write_raw(tmp_vault, "2026-07-25.md", "Morning briefing content")
    client = MagicMock()
    client.messages.create.return_value = make_message("Synthesized briefing")
    bo.run(_client=client, _config=tmp_config)

    registry_path = tmp_vault / "_meta" / "topics-registry.json"
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    assert "2026-07-25" in registry
    registered_path = Path(registry["2026-07-25"])
    assert registered_path.exists()
    assert registered_path.parent.name == "daily"


def test_topics_registry_accumulates_across_runs(
    tmp_vault: Path, tmp_config: dict[str, Any]
) -> None:
    write_raw(tmp_vault, "note1.md", "NEXUS content")
    client1 = make_client('{"routes": [{"title":"NEXUS", "match": "new"}]}', "# NEXUS\n\nContent.")
    bo.run(_client=client1, _config=tmp_config)

    write_raw(tmp_vault, "note2.md", "Hermes content")
    client2 = make_client('{"routes": [{"title":"Hermes", "match": "new"}]}', "# Hermes\n\nContent.")
    bo.run(_client=client2, _config=tmp_config)

    registry = json.loads((tmp_vault / "_meta" / "topics-registry.json").read_text(encoding="utf-8"))
    assert "NEXUS" in registry
    assert "Hermes" in registry


# ---------------------------------------------------------------------------
# Topic detection — code-fence stripping
# ---------------------------------------------------------------------------

def test_detect_topics_strips_markdown_code_fences(
    tmp_config: dict[str, Any], mock_anthropic_client: MagicMock
) -> None:
    """Haiku often wraps JSON in ```json ... ``` fences — verify they are stripped."""
    fenced = '```json\n{"routes": [{"title":"NEXUS", "match": "new"}, {"title":"Unraid", "match": "new"}]}\n```'
    mock_anthropic_client.messages.create.side_effect = [make_message(fenced)]
    topics = bo.detect_topics("some note content", tmp_config, mock_anthropic_client)
    assert topics == ["NEXUS", "Unraid"]


def test_detect_topics_strips_plain_code_fences(
    tmp_config: dict[str, Any], mock_anthropic_client: MagicMock
) -> None:
    fenced = '```\n{"routes": [{"title":"Hermes", "match": "new"}]}\n```'
    mock_anthropic_client.messages.create.side_effect = [make_message(fenced)]
    topics = bo.detect_topics("some note content", tmp_config, mock_anthropic_client)
    assert topics == ["Hermes"]


# ---------------------------------------------------------------------------
# Telegram notification
# ---------------------------------------------------------------------------

def test_telegram_notification_sent(
    tmp_vault: Path, tmp_config: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")
    http_client = MagicMock()
    bo.send_telegram_notification(tmp_config, "Test message", http_client=http_client)
    http_client.post.assert_called_once()
    call_args = http_client.post.call_args
    assert "test-token" in call_args.args[0]
    assert "sendMessage" in call_args.args[0]
    assert call_args.kwargs["json"]["chat_id"] == "12345"
    assert call_args.kwargs["json"]["text"] == "Test message"


def test_telegram_notification_skipped_when_token_missing(
    tmp_vault: Path, tmp_config: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")
    http_client = MagicMock()
    bo.send_telegram_notification(tmp_config, "Test", http_client=http_client)
    http_client.post.assert_not_called()


def test_telegram_notification_skipped_when_chat_id_missing(
    tmp_vault: Path, tmp_config: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    http_client = MagicMock()
    bo.send_telegram_notification(tmp_config, "Test", http_client=http_client)
    http_client.post.assert_not_called()


# ---------------------------------------------------------------------------
# sanitize_topic_name
# ---------------------------------------------------------------------------

def test_sanitize_topic_name_spaces_become_dashes() -> None:
    assert bo.sanitize_topic_name("Home Assistant") == "Home-Assistant"


def test_sanitize_topic_name_strips_special_chars() -> None:
    assert bo.sanitize_topic_name("Topic/Name!") == "TopicName"


def test_sanitize_topic_name_empty_falls_back() -> None:
    assert bo.sanitize_topic_name("!!!") == "Uncategorized"


# ---------------------------------------------------------------------------
# Unified run() -- parallel path (previously had ZERO test coverage; this is
# where the silent-data-loss / usage-cap / secondary-route bugs all lived)
# ---------------------------------------------------------------------------

def test_run_parallel_path_processes_multiple_files(
    tmp_vault: Path, tmp_config: dict[str, Any]
) -> None:
    """max_parallel_files > 1 must still process every file correctly."""
    tmp_config["max_parallel_files"] = 4
    write_raw(tmp_vault, "note1.md", "NEXUS content")
    write_raw(tmp_vault, "note2.md", "Hermes content")

    client = MagicMock()
    client.messages.create.side_effect = [
        make_message('{"routes": [{"title":"NEXUS", "match": "new"}]}'),
        make_message('{"routes": [{"title":"Hermes", "match": "new"}]}'),
        make_message("# NEXUS\n\nContent."),
        make_message("# Hermes\n\nContent."),
    ]
    result = bo.run(_client=client, _config=tmp_config)
    assert result == 0
    assert (tmp_vault / "wiki" / "NEXUS.md").exists()
    assert (tmp_vault / "wiki" / "Hermes.md").exists()
    assert not (tmp_vault / "raw" / "note1.md").exists()
    assert not (tmp_vault / "raw" / "note2.md").exists()


def test_run_routing_failure_keeps_raw_and_records_failure_not_success(
    tmp_vault: Path, tmp_config: dict[str, Any]
) -> None:
    """A routing exception must NOT delete the raw file / record success.

    This is the bug all three reviews flagged: the old parallel path's
    _route_one caught every exception and returned an empty route list,
    which process_file treated as "nothing to route" -- deleting the raw
    file and reporting success while the note's content went nowhere.
    """
    tmp_config["max_parallel_files"] = 2
    raw_file = write_raw(tmp_vault, "note.md", "Content")
    sha = bo.compute_sha256(raw_file)

    client = MagicMock()
    client.messages.create.side_effect = RuntimeError("routing API down")
    result = bo.run(_client=client, _config=tmp_config)

    assert result == 1
    assert raw_file.exists(), "raw file must survive a routing failure for retry"
    processed = bo.load_processed(tmp_config)
    assert processed[sha]["status"] == "failed"
    assert processed[sha]["attempts"] == 1


def test_run_usage_capped_aborts_without_per_file_failure_spam(
    tmp_vault: Path, tmp_config: dict[str, Any]
) -> None:
    """_APIUsageCapped during routing must abort the run, not record N failures."""
    tmp_config["max_parallel_files"] = 1
    raw_file = write_raw(tmp_vault, "note.md", "Content")
    sha = bo.compute_sha256(raw_file)

    capped_response = MagicMock()
    capped_response.status_code = 400
    capped_response.request = MagicMock()
    capped_response.headers = {}

    client = MagicMock()
    client.messages.create.side_effect = anthropic.APIStatusError(
        "usage limits exceeded", response=capped_response, body={}
    )
    result = bo.run(_client=client, _config=tmp_config)

    # An abort is deliberate, not a per-file failure -- matches the original
    # sequential path's behavior (failed_count is never incremented on abort).
    assert result == 0
    assert raw_file.exists()  # aborted, not failed -- kept for retry, not attempt-counted
    processed = bo.load_processed(tmp_config)
    assert sha not in processed, "an aborted run must not record a failed attempt"


def test_run_aborts_when_catalog_empty_but_wiki_has_markdown_files(
    tmp_vault: Path, tmp_config: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """build_wiki_catalog returning [] while wiki/ genuinely has *.md pages can
    only mean its own catastrophic except-path fired (see the elevated .error
    log there). Routing/synthesizing against an empty catalog in that case
    would treat every existing page as new and duplicate the whole wiki, so
    run() must abort BEFORE any routing/synthesis (§5.3 criterion 34 / §6.4
    criterion 48) -- not just log a warning and carry on.

    Uses capsys (not caplog): run() itself calls setup_logging(), which does
    logging.basicConfig(..., force=True) -- that replaces the root logger's
    handlers wholesale, including caplog's, so caplog.text reads empty for
    any test that invokes run() directly. basicConfig's own
    StreamHandler(sys.stdout) still lands in capsys's stdout capture.

    The genuinely-empty-wiki fast path (no *.md at all -- guard must NOT
    fire, run() proceeds normally to 0) is already pinned down by
    test_run_parallel_path_processes_multiple_files above: tmp_vault's wiki/
    starts with zero *.md files, catalog comes back [], and that test
    asserts result == 0 with new pages created -- exercising exactly the
    guard's False branch on a real fixture, not a mock.
    """
    (tmp_vault / "wiki" / "Existing.md").write_text("# Existing\n\nSome content.", encoding="utf-8")
    write_raw(tmp_vault, "note.md", "New content to process")

    monkeypatch.setattr(bo, "build_wiki_catalog", lambda *a, **k: [])
    mock_telegram = MagicMock()
    monkeypatch.setattr(bo, "send_telegram_notification", mock_telegram)

    client = MagicMock()

    result = bo.run(_client=client, _config=tmp_config)

    assert result == 1
    client.messages.create.assert_not_called()  # no routing/synthesis occurred
    assert (tmp_vault / "raw" / "note.md").exists()  # raw file untouched, never consumed

    captured_out = capsys.readouterr().out
    assert "[ERROR]" in captured_out
    assert "catalog" in captured_out.lower()

    mock_telegram.assert_called_once()
    assert mock_telegram.call_args.kwargs.get("priority") == "high"


def test_group_files_by_shared_pages_unions_on_any_shared_route() -> None:
    """Two files sharing a SECONDARY (non-primary) route must land in one group.

    This is the race the old primary-only grouping (key = routes[0][1]) missed:
    file A's route[1] and file B's route[0] targeting the same page were never
    serialized against each other.
    """
    page_a = Path("/vault/wiki/A.md")
    page_b = Path("/vault/wiki/B.md")
    routing_results = [
        (Path("fileA.md"), "shaA", [("A", page_a, False), ("B", page_b, False)], None),
        (Path("fileB.md"), "shaB", [("B", page_b, False)], None),
        (Path("fileC.md"), "shaC", [("C", Path("/vault/wiki/C.md"), True)], None),
    ]
    groups = bo._group_files_by_shared_pages(routing_results)
    assert len(groups) == 2  # {fileA, fileB} share page B; fileC stands alone
    sizes = sorted(len(g) for g in groups.values())
    assert sizes == [1, 2]


def test_group_files_by_shared_pages_gives_routing_failures_singleton_groups() -> None:
    routing_results = [
        (Path("fileA.md"), "shaA", None, RuntimeError("boom")),
        (Path("fileB.md"), "shaB", None, RuntimeError("boom2")),
    ]
    groups = bo._group_files_by_shared_pages(routing_results)
    assert len(groups) == 2


# ---------------------------------------------------------------------------
# OpenRouter truncation normalization
# ---------------------------------------------------------------------------

def test_openrouter_length_finish_reason_normalized_to_max_tokens(
    tmp_config: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """OpenRouter signals truncation as finish_reason="length"; callers only
    check for the literal string "max_tokens" -- verify _call_api normalizes."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-or-key")
    client = MagicMock()
    client.messages.create.side_effect = anthropic.APITimeoutError(request=MagicMock())

    or_response = {
        "choices": [{"message": {"content": "truncated text"}, "finish_reason": "length"}]
    }
    with patch("brain_organizer.time.sleep"), patch("brain_organizer.httpx.post") as mock_post:
        mock_post.return_value = MagicMock(
            status_code=200, json=lambda: or_response, raise_for_status=lambda: None,
        )
        with pytest.raises(ValueError, match="max_tokens"):
            bo.synthesize_wiki("Topic", "content", "", tmp_config, client)


# ---------------------------------------------------------------------------
# Large-page splice failure must raise, not silently return unchanged content
# ---------------------------------------------------------------------------

def test_large_page_splice_failure_raises_instead_of_dropping_content(
    tmp_config: dict[str, Any],
) -> None:
    tmp_config["large_page_threshold_chars"] = 10  # force the 5b branch
    existing = "# Topic\n\n## Existing\n\n" + ("x" * 50)
    client = MagicMock()
    # A response with no "## " section at all defeats the splice parser's
    # section_chunks list (stays empty), but the real failure mode we're
    # testing is any exception during splicing -- patch re.split to force one.
    client.messages.create.return_value = make_message("## Existing\nnew stuff")
    with patch("brain_organizer.re.split", side_effect=RuntimeError("boom")):
        with pytest.raises(ValueError, match="splice failed"):
            bo.synthesize_wiki("Topic", "new content", existing, tmp_config, client)


def test_large_page_splice_defuses_hallucinated_link_in_spliced_chunk(
    tmp_config: dict[str, Any],
) -> None:
    """Defect D (spec #1 crit 10/11): branch-5b previously returned spliced
    content before _defuse_unknown_wikilinks ever ran, so a large-page merge
    kept whatever raw [[links]] the model emitted, broken or not. Mirrors
    test_synthesize_wiki_defuses_hallucinated_link_in_create_branch above,
    but forces the large-page splice branch instead of the create branch.
    """
    tmp_config["large_page_threshold_chars"] = 10  # force the 5b branch
    catalog = [{"title": "NEXUS", "filename": "NEXUS.md", "path_str": "x", "headers": "", "summary": "NEXUS stuff"}]
    existing = "# Topic\n\n## Existing\n\n" + ("x" * 50)
    client = MagicMock()
    client.messages.create.return_value = make_message(
        "## Existing\n\nSee [[NEXUS]] and also [[project_version_scheme]] for context."
    )
    result = bo.synthesize_wiki("Topic", "new content", existing, tmp_config, client, catalog=catalog)
    assert "[[NEXUS]]" in result
    assert "[[project_version_scheme]]" not in result
    assert "`project_version_scheme`" in result


def test_large_page_splice_replaces_matching_section_despite_header_wikilink_normalization(
    tmp_config: dict[str, Any],
) -> None:
    """Header-line-contains-a-wikilink edge case (flagged during review): the
    splice loop must extract header_line BEFORE _defuse_unknown_wikilinks
    runs. If it extracted it after, a header referencing a catalog page in a
    different case ("[[nexus]]") would be rewritten to a display-preserving
    alias ("[[NEXUS|nexus]]") before the section-match regex ever saw it,
    while existing_content's copy stayed literal -- so the two would no
    longer match and the section would be silently duplicated (appended as
    new) instead of replaced in place.
    """
    tmp_config["large_page_threshold_chars"] = 10  # force the 5b branch
    catalog = [{"title": "NEXUS", "filename": "NEXUS.md", "path_str": "x", "headers": "", "summary": "NEXUS stuff"}]
    existing = "# Topic\n\n## [[nexus]] Notes\n\noldbody\n" + ("x" * 50)
    client = MagicMock()
    client.messages.create.return_value = make_message("## [[nexus]] Notes\n\nnewbody")
    result = bo.synthesize_wiki("Topic", "new content", existing, tmp_config, client, catalog=catalog)
    assert result.count("## ") == 1  # section replaced in place, not duplicated
    assert "oldbody" not in result
    assert "newbody" in result


# ---------------------------------------------------------------------------
# Empty / suspiciously-short synthesis result guard
# ---------------------------------------------------------------------------

def test_synthesize_wiki_raises_on_empty_result(tmp_config: dict[str, Any]) -> None:
    client = MagicMock()
    client.messages.create.return_value = make_message("   ")
    with pytest.raises(ValueError, match="empty"):
        bo.synthesize_wiki("Topic", "content", "", tmp_config, client)


def test_synthesize_wiki_raises_on_suspiciously_short_merge(tmp_config: dict[str, Any]) -> None:
    existing = "# Topic\n\n" + ("Important existing content. " * 20)
    client = MagicMock()
    client.messages.create.return_value = make_message("# Topic\n\nshort")
    with pytest.raises(ValueError, match="suspiciously short"):
        bo.synthesize_wiki("Topic", "new info", existing, tmp_config, client)


# ---------------------------------------------------------------------------
# APIConnectionError must retry + fall back, not propagate immediately
# ---------------------------------------------------------------------------

def test_api_connection_error_retries_then_succeeds(tmp_config: dict[str, Any]) -> None:
    client = MagicMock()
    client.messages.create.side_effect = [
        anthropic.APIConnectionError(request=MagicMock()),
        make_message('{"routes": [{"title":"NEXUS", "match": "new"}]}'),
    ]
    with patch("brain_organizer.time.sleep"):
        topics = bo.detect_topics("content", tmp_config, client)
    assert topics == ["NEXUS"]
    assert client.messages.create.call_count == 2


# ---------------------------------------------------------------------------
# Backup pruning
# ---------------------------------------------------------------------------

def test_prune_old_backups_removes_only_stale_entries(
    tmp_vault: Path, tmp_config: dict[str, Any]
) -> None:
    import time as _time

    backups = tmp_vault / "raw" / "backups"
    old = backups / "old.md"
    fresh = backups / "fresh.md"
    old.write_text("old", encoding="utf-8")
    fresh.write_text("fresh", encoding="utf-8")

    old_time = _time.time() - 40 * 86400  # 40 days old
    os_stat_ns = old_time * 1e9
    import os as _os
    _os.utime(old, (old_time, old_time))

    tmp_config["backup_retention_days"] = 30
    bo._prune_old_backups(tmp_config, logging.getLogger("test"))

    assert not old.exists()
    assert fresh.exists()


# ---------------------------------------------------------------------------
# route_topics no longer takes existing_registry (dead param removed)
# ---------------------------------------------------------------------------

def test_route_topics_signature_has_no_registry_param(tmp_config: dict[str, Any]) -> None:
    import inspect
    params = list(inspect.signature(bo.route_topics).parameters)
    assert "existing_registry" not in params
    assert params == ["content", "catalog", "config", "client"]


# ---------------------------------------------------------------------------
# setup_logging uses a bounded RotatingFileHandler, not an unbounded FileHandler
# (spec #2 SS6.5 -- organizer.log must not grow without limit).
# ---------------------------------------------------------------------------

def test_setup_logging_attaches_rotating_file_handler_with_size_limit(
    tmp_config: dict[str, Any],
) -> None:
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    try:
        bo.setup_logging(tmp_config)

        rotating = [h for h in root.handlers if isinstance(h, RotatingFileHandler)]
        assert len(rotating) == 1, root.handlers
        handler = rotating[0]
        assert handler.maxBytes == 5 * 1024 * 1024
        assert handler.backupCount == 3

        # A plain (non-rotating) FileHandler must no longer be present.
        plain_file_handlers = [
            h for h in root.handlers
            if type(h) is logging.FileHandler
        ]
        assert plain_file_handlers == []
    finally:
        for h in root.handlers:
            h.close()
        root.handlers = saved_handlers
        root.setLevel(saved_level)
