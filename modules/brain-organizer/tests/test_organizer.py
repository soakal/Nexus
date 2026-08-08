"""Unit tests for brain_organizer.py."""
from __future__ import annotations

import json
import logging
import re
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
    """Build a mock client with a routing response, a tag-suggestion response,
    then a wiki synthesis response.

    routes_text must already be in the new routes JSON shape:
        '{"routes": [{"title":"...", "match": "new"}]}'
    """
    client = MagicMock()
    client.messages.create.side_effect = [
        make_message(routes_text),
        make_message('{"tags": []}'),
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
    # tmp_config's max_file_attempts now comes from _CONFIG_DEFAULTS (2, matching
    # production) -- explicitly raise it here so "attempts=2" is genuinely
    # UNDER the cap, which is the behavior this test names and exercises.
    tmp_config["max_file_attempts"] = 5
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


def _catalog_entry(title: str, filename_stem: str) -> dict[str, Any]:
    return {
        "title": title,
        "filename": f"{filename_stem}.md",
        "path_str": f"/vault/wiki/{filename_stem}.md",
        "headers": "",
        "summary": "",
    }


def test_route_topics_ranking_prefers_lexical_match_over_alphabetical_position(
    tmp_config: dict[str, Any],
) -> None:
    """§4.3 / spec criteria 16-17 core fix: with router_catalog_ranking=True
    (tmp_config's default), a page whose headers lexically match the note
    content must be pulled into the rich prompt window even though it sits
    deep in the catalog (index 150 of 200) and is alphabetically
    indistinguishable from 199 same-shaped peers -- displacing an
    alphabetically-earlier but lexically-irrelevant page that the old
    catalog[:max_in_prompt] positional truncation would have kept instead."""
    catalog: list[dict[str, Any]] = []
    for i in range(200):
        entry = _catalog_entry(f"Page {i:03d}", f"page-{i:03d}")
        if i == 150:
            entry["headers"] = "zorblatt quantum flux"
        catalog.append(entry)
    cfg = dict(tmp_config, catalog_max_pages_in_prompt=5)

    client = MagicMock()
    client.messages.create.return_value = make_message(
        '{"routes": [{"match": "new", "title": "Something Brand New"}]}'
    )
    bo.route_topics(
        "Need to log details about zorblatt quantum flux today.", catalog, cfg, client,
    )
    prompt = client.messages.create.call_args.kwargs["messages"][0]["content"]

    assert "ALL OTHER PAGES" in prompt
    rich_block, index_block = prompt.split("\n\nALL OTHER PAGES", 1)

    # the lexically-relevant deep page is pulled into the rich window...
    assert "Page 150" in rich_block
    # ...displacing the alphabetically-next-earliest irrelevant page, which
    # the old positional catalog[:5] truncation would have kept in slot 5
    # instead -- it must now surface only via the §4.4 index, not be lost.
    assert "Page 004" not in rich_block
    assert "Page 004 (page-004)" in index_block


def test_route_topics_index_covers_pages_beyond_rich_block(
    tmp_config: dict[str, Any],
) -> None:
    """§4.4 / spec criteria 16-18: pages beyond the alphabetical-truncation
    window must still surface somewhere in the prompt (via the appended
    'ALL OTHER PAGES' index), while the rich numbered block stays exactly
    catalog_max_pages_in_prompt entries — unchanged truncation count."""
    rich = [_catalog_entry(f"Rich Page {i}", f"rich-page-{i}") for i in range(3)]
    far = [_catalog_entry(f"Far Page {i}", f"far-page-{i}") for i in range(5)]
    catalog = rich + far  # simulates a catalog far larger than the prompt window
    cfg = dict(tmp_config, catalog_max_pages_in_prompt=3)

    client = MagicMock()
    client.messages.create.return_value = make_message(
        '{"routes": [{"match": "new", "title": "Something Brand New"}]}'
    )
    bo.route_topics("some content", catalog, cfg, client)

    prompt = client.messages.create.call_args.kwargs["messages"][0]["content"]
    assert "ALL OTHER PAGES" in prompt

    rich_block = prompt.split("\n\nALL OTHER PAGES")[0]
    numbered_entries = re.findall(r"(?m)^\d+\. ", rich_block)
    # criterion 18: rich numbered block unchanged at exactly max_in_prompt entries
    assert len(numbered_entries) == 3

    # criteria 16/17: a page far outside the rich block (last of 8, well beyond
    # the top-3 window) must still have its title appear in the prompt, via the
    # "Title (filename-stem)" index line, no summary.
    assert "Far Page 4 (far-page-4)" in prompt
    # and every catalog title (rich + far) appears somewhere in the combined prompt
    for entry in catalog:
        assert entry["title"] in prompt


def test_route_topics_index_entry_missing_filename_degrades_to_title_stem(
    tmp_config: dict[str, Any],
) -> None:
    """§4.4 fix: a catalog entry with no "filename" key must not KeyError
    when building the title-only index — it degrades to its own title as
    the stem, matching the identical entry.get("filename") fallback already
    used elsewhere (route_topics' _resolve_existing lookup at :1567,
    _rank_catalog_by_relevance's headers/summary access pattern, etc.).
    Large enough catalog (rich=3, far=5 with catalog_max_pages_in_prompt=3)
    so the filename-less entry falls into the §4.4 index branch, not the
    rich window."""
    rich = [_catalog_entry(f"Rich Page {i}", f"rich-page-{i}") for i in range(3)]
    far = [_catalog_entry(f"Far Page {i}", f"far-page-{i}") for i in range(4)]
    no_filename_entry = {
        "title": "Filenameless Page",
        "path_str": "/vault/wiki/Filenameless Page.md",
        "headers": "",
        "summary": "",
    }
    catalog = rich + far + [no_filename_entry]
    cfg = dict(tmp_config, catalog_max_pages_in_prompt=3)

    client = MagicMock()
    client.messages.create.return_value = make_message(
        '{"routes": [{"match": "new", "title": "Something Brand New"}]}'
    )
    # Must not raise KeyError building the §4.4 index for the filename-less entry.
    bo.route_topics("some content", catalog, cfg, client)

    prompt = client.messages.create.call_args.kwargs["messages"][0]["content"]
    assert "ALL OTHER PAGES" in prompt
    index_block = prompt.split("\n\nALL OTHER PAGES", 1)[1]
    # degrades to its own title as the stem: "Filenameless Page (Filenameless Page)"
    assert "Filenameless Page (Filenameless Page)" in index_block


def test_route_topics_router_catalog_ranking_off_is_byte_identical(
    tmp_config: dict[str, Any],
) -> None:
    """Spec criterion 20: with router_catalog_ranking=False, the prompt's
    catalog block must be byte-identical to the untouched plain
    catalog[:max_in_prompt] positional slice (brain_organizer.route_topics's
    flag-off branch) -- no ranking, no appended index block -- regardless of
    what the (unrelated) ranking-on path would have selected for the same
    catalog.

    Deliberately ordered rich-before-far but alphabetically far < rich, so
    that if the flag-off path ever accidentally started using
    _rank_catalog_by_relevance's alphabetical tie-break instead of the plain
    slice, the selected/ordered entries would visibly differ and this test
    would catch it (comparing against an on-run cannot: ranking-on legally
    picks a different composition than the flag-off slice, which is exactly
    why the old round-trip-reconstruction version of this test broke)."""
    rich = [_catalog_entry(f"Rich Page {i}", f"rich-page-{i}") for i in range(3)]
    far = [_catalog_entry(f"Far Page {i}", f"far-page-{i}") for i in range(2)]
    catalog = rich + far
    cfg_off = dict(tmp_config, catalog_max_pages_in_prompt=3, router_catalog_ranking=False)

    client_off = MagicMock()
    client_off.messages.create.return_value = make_message(
        '{"routes": [{"match": "new", "title": "Something Brand New"}]}'
    )
    bo.route_topics("some content", catalog, cfg_off, client_off)
    prompt_off = client_off.messages.create.call_args.kwargs["messages"][0]["content"]

    assert "ALL OTHER PAGES" not in prompt_off

    # Reproduce the untouched flag-off branch's line format directly
    # (brain_organizer.route_topics, "else: catalog_pages = catalog[:max_in_prompt]"
    # followed by the "{i}. {title}. {summary}" formatting for entries with no
    # headers) -- independent of any flag-on/ranking run.
    expected_lines = [
        f"{i}. {page['title']}. {page.get('summary', '')}"
        for i, page in enumerate(catalog[:3], 1)
    ]
    expected_block = "\n".join(expected_lines)

    prefix = "EXISTING WIKI PAGES (route to these whenever possible):\n"
    catalog_block_off = prompt_off.split("\n\nNOTE TO ROUTE:")[0].split(prefix, 1)[1]
    assert catalog_block_off == expected_block


@pytest.mark.parametrize("llm_title", [
    "Bug-Fixes",  # exact filename stem, not the stored title
    "bug fixes",  # case-insensitive title
    "bug-fixes",  # case-insensitive stem
])
def test_route_topics_resolve_existing_tolerant_matching(
    tmp_config: dict[str, Any], caplog: pytest.LogCaptureFixture, llm_title: str,
) -> None:
    """§4.5 / spec criterion 19: an 'existing' route whose title doesn't
    exactly match the catalog's stored title casing/form must still resolve
    to the real page via _resolve_existing's tolerant lookup order, with no
    hallucination warning logged."""
    catalog = [_catalog_entry("Bug Fixes", "Bug-Fixes")]
    client = MagicMock()
    client.messages.create.return_value = make_message(
        json.dumps({"routes": [{"match": "existing", "title": llm_title}]})
    )

    with caplog.at_level(logging.WARNING):
        routes = bo.route_topics("some content", catalog, tmp_config, client)

    assert len(routes) == 1
    title, path, is_new = routes[0]
    assert title == "Bug Fixes"
    assert is_new is False
    assert path == Path(catalog[0]["path_str"])
    assert "hallucinated" not in caplog.text


def test_route_topics_dedups_two_routes_naming_same_page(
    tmp_config: dict[str, Any],
) -> None:
    """spec #2 crit 39 (:789): two routes in the same response that both name
    the same existing page must de-duplicate to a single result via
    seen_paths -- not appear twice in the returned route list."""
    catalog = [_catalog_entry("NEXUS", "NEXUS")]
    client = MagicMock()
    client.messages.create.return_value = make_message(
        json.dumps(
            {
                "routes": [
                    {"match": "existing", "title": "NEXUS"},
                    {"match": "existing", "title": "NEXUS"},
                ]
            }
        )
    )

    routes = bo.route_topics("some content", catalog, tmp_config, client)

    assert len(routes) == 1
    title, path, is_new = routes[0]
    assert title == "NEXUS"
    assert path == Path(catalog[0]["path_str"])
    assert is_new is False


def test_route_topics_hallucinated_existing_title_logs_warning_and_falls_through_to_new(
    tmp_config: dict[str, Any], caplog: pytest.LogCaptureFixture,
) -> None:
    """spec #2 crit 40: a "match":"existing" route naming a title that is
    NOT present anywhere in the catalog must log the hallucination warning
    and fall through to the "new" path (re-checked via the near-dup guard),
    not be silently dropped or raise."""
    catalog = [_catalog_entry("Real Page", "Real-Page")]
    client = MagicMock()
    client.messages.create.return_value = make_message(
        json.dumps({"routes": [{"match": "existing", "title": "Ghost Page"}]})
    )

    with caplog.at_level(logging.WARNING):
        routes = bo.route_topics("some content", catalog, tmp_config, client)

    assert "hallucinated" in caplog.text
    assert len(routes) == 1
    title, path, is_new = routes[0]
    assert title == "Ghost Page"
    assert is_new is True


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
# _is_features_digest / _features_digest_route / _deterministic_route
# (spec #2 §7.2, crits 51-54)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("stem", [
    "claude-features-digest-2026-08-02",
    "claude-features-digest-2026-08-02_20260802T120508Z",  # collision-suffixed
    "CLAUDE-FEATURES-DIGEST-2026-08-02",                    # case-insensitive
])
def test_is_features_digest_matches_digest_stems(stem: str) -> None:
    assert bo._is_features_digest(stem) is True


@pytest.mark.parametrize("stem", [
    "NEXUS",
    "2026-08-02",
    "claude-features-digest",              # missing date -- must not match
    "Daily-Operations-Log-2026-07-08",     # ordinary daily-note stem
])
def test_is_features_digest_rejects_ordinary_stems(stem: str) -> None:
    assert bo._is_features_digest(stem) is False


def test_features_digest_route_returns_none_for_non_digest(tmp_path: Path) -> None:
    assert bo._features_digest_route("NEXUS-session-notes", [], tmp_path, tmp_path / "daily") is None


def test_features_digest_route_creates_new_page_when_absent(tmp_path: Path) -> None:
    """Lookup-or-create mirrors _daily_note_route's Daily-Log branch: with no
    matching catalog entry, route to a fresh wiki/Claude-Features-Digest.md."""
    wiki_folder = tmp_path / "wiki"
    daily_folder = wiki_folder / "daily"

    routes = bo._features_digest_route(
        "claude-features-digest-2026-08-02", [], wiki_folder, daily_folder
    )

    assert routes == [("Claude Features Digest", wiki_folder / "Claude-Features-Digest.md", True)]


def test_features_digest_route_reuses_existing_catalog_entry(tmp_path: Path) -> None:
    """Lookup-or-create, existing branch: an already-registered
    Claude-Features-Digest.md catalog entry is reused verbatim (is_new=False),
    same pattern as _daily_note_route_reuses_existing_daily_log_catalog_entry."""
    wiki_folder = tmp_path / "wiki"
    daily_folder = wiki_folder / "daily"
    catalog = [{
        "title": "Claude Features Digest", "filename": "Claude-Features-Digest.md",
        "path_str": str(wiki_folder / "Claude-Features-Digest.md"), "headers": "", "summary": "",
    }]

    routes = bo._features_digest_route(
        "claude-features-digest-2026-08-02_20260802T120508Z", catalog, wiki_folder, daily_folder
    )

    assert routes == [("Claude Features Digest", wiki_folder / "Claude-Features-Digest.md", False)]


def test_deterministic_route_dispatches_daily_note(tmp_path: Path) -> None:
    """Row 1 (_is_daily_note/_daily_note_route) via the dispatcher must
    return _daily_note_route's exact result -- crit 51."""
    wiki_folder = tmp_path / "wiki"
    daily_folder = wiki_folder / "daily"

    direct = bo._daily_note_route("Daily-Operations-Log-2026-07-08", [], wiki_folder, daily_folder)
    dispatched = bo._deterministic_route("Daily-Operations-Log-2026-07-08", [], wiki_folder, daily_folder)

    assert dispatched == direct


def test_deterministic_route_dispatches_features_digest(tmp_path: Path) -> None:
    """Row 2 (_is_features_digest/_features_digest_route) via the
    dispatcher -- crit 52."""
    wiki_folder = tmp_path / "wiki"
    daily_folder = wiki_folder / "daily"

    routes = bo._deterministic_route(
        "claude-features-digest-2026-08-02", [], wiki_folder, daily_folder
    )

    assert routes == [("Claude Features Digest", wiki_folder / "Claude-Features-Digest.md", True)]


def test_deterministic_route_returns_none_for_ordinary_stem(tmp_path: Path) -> None:
    """Crit 53: an ordinary topical stem must fall through to None so
    route_topics still runs for everything else."""
    wiki_folder = tmp_path / "wiki"
    daily_folder = wiki_folder / "daily"

    assert bo._deterministic_route("NEXUS-session-notes", [], wiki_folder, daily_folder) is None


def test_source_routes_every_row_has_a_dispatch_test() -> None:
    """Crit 54 drift guard: every (predicate, router) row registered in
    _SOURCE_ROUTES must be one this file exercises through
    _deterministic_route above -- appending a new row without adding a
    matching dispatch test fails this assertion instead of silently
    shipping untested. (Same pattern as
    backend/safety/contracts.py::test_every_integration_is_covered.)"""
    rows_with_dispatch_tests = {
        (bo._is_daily_note, bo._daily_note_route),
        (bo._is_features_digest, bo._features_digest_route),
    }
    assert set(bo._SOURCE_ROUTES) == rows_with_dispatch_tests


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


def test_extract_page_entry_frontmatter_no_h1_summary_not_leaked(tmp_path: Path) -> None:
    """spec #3 criteria 8-9: a page with frontmatter and no H1 must not leak
    the frontmatter block into 'summary' (the live Charlee Health Report.md
    bug), and must expose the new 'tags' key parsed from that frontmatter."""
    f = tmp_path / "page.md"
    f.write_text(
        "---\n"
        "title: Something\n"
        "tags:\n"
        "  - alpha\n"
        "  - beta\n"
        "---\n"
        "\n"
        "Real prose paragraph here.\n"
        "\n"
        "## Header One\n",
        encoding="utf-8",
    )

    entry = bo._extract_page_entry(f)

    assert entry["summary"] == "Real prose paragraph here."
    assert "title:" not in entry["summary"]
    assert "tags:" not in entry["summary"]
    assert entry["title"] == "page"  # no H1 present -> falls back to stem
    assert entry["headers"] == "Header One"
    assert entry["tags"] == ["alpha", "beta"]


def test_extract_page_entry_frontmatter_with_h1_matches_pre_change_behavior(
    tmp_path: Path,
) -> None:
    """spec #3 criterion 10 (refactor-safety): a page with frontmatter AND an
    H1 after it must parse title/headers/summary identically to before this
    cycle's fm-aware scan-start change."""
    f = tmp_path / "page.md"
    f.write_text(
        "---\n"
        "title: Something\n"
        "---\n"
        "\n"
        "# My Title\n"
        "\n"
        "Some body text.\n"
        "\n"
        "## Header One\n",
        encoding="utf-8",
    )

    entry = bo._extract_page_entry(f)

    assert entry["title"] == "My Title"
    assert entry["headers"] == "Header One"
    assert entry["summary"] == "Some body text."
    assert entry["tags"] == []


def test_extract_page_entry_post_h1_pseudo_frontmatter_not_leaked_into_summary(
    tmp_path: Path,
) -> None:
    """spec #3 criterion 48: a page shaped like the live Code-Review.md bug --
    H1, then a run containing both a literal '---' line and 'key:' lines that
    _find_frontmatter never sees (it isn't at position 0), then a header --
    must not leak 'category: Tools' into 'summary'. The parser must skip the
    whole pseudo-frontmatter run and stop at the header immediately after."""
    f = tmp_path / "Code-Review.md"
    f.write_text(
        "# Code Review\n"
        "\n"
        "---\n"
        "category: Tools\n"
        "date: 2026-06-13\n"
        "\n"
        "## Overview\n"
        "\n"
        "Real prose paragraph here.\n",
        encoding="utf-8",
    )

    entry = bo._extract_page_entry(f)

    assert entry["summary"] == ""
    assert "category:" not in entry["summary"]
    assert "date:" not in entry["summary"]


def test_extract_page_entry_h1_then_colon_prose_not_swallowed_as_pseudo_frontmatter(
    tmp_path: Path,
) -> None:
    """Highest-risk regression per the engineer's own note: an H1 followed
    immediately by real prose that merely contains a colon (e.g. 'Note: ...')
    and no '---' anywhere must NOT be treated as a pseudo-frontmatter run --
    the prose must still become the summary."""
    f = tmp_path / "page.md"
    f.write_text(
        "# My Title\n"
        "\n"
        "Note: this is real prose, not frontmatter.\n"
        "\n"
        "## Header One\n",
        encoding="utf-8",
    )

    entry = bo._extract_page_entry(f)

    assert entry["summary"] == "Note: this is real prose, not frontmatter."


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


def test_build_wiki_catalog_respects_summary_chars_bound(tmp_path: Path) -> None:
    """spec #2 criterion 25: build_wiki_catalog must thread its summary_chars
    param through to _extract_page_entry (not silently use the 300-char
    default) -- both bounding the produced summary AND actually truncating
    prose that would otherwise be longer, so this isn't vacuously true of
    short input."""
    wiki_folder = tmp_path / "wiki"
    meta_folder = tmp_path / "_meta"
    wiki_folder.mkdir()
    meta_folder.mkdir()
    long_body = "word " * 200  # far longer than either bound below
    (wiki_folder / "Alpha.md").write_text(
        f"# Alpha\n\n{long_body}\n", encoding="utf-8"
    )

    default_pages = bo.build_wiki_catalog(wiki_folder, meta_folder)
    # Force a full re-parse (bypass the mtime-cache fast path) for the bounded call.
    (meta_folder / "wiki-catalog.json").unlink()
    bounded_pages = bo.build_wiki_catalog(wiki_folder, meta_folder, summary_chars=20)

    assert len(default_pages[0]["summary"]) > 20
    assert len(bounded_pages[0]["summary"]) <= 20
    assert bounded_pages[0]["summary"] == default_pages[0]["summary"][:20]


def test_build_wiki_catalog_excludes_daily_and_processed_subfolders(
    tmp_path: Path,
) -> None:
    """Criterion 16 (docs/brain-organizer-robustness-spec.md:191) / §3.3(b):
    build_wiki_catalog's wiki_folder.glob("*.md") scan is deliberately
    non-recursive -- a page in wiki/daily/ or wiki/processed/ must never
    appear in the catalog, only wiki/*.md itself. A blanket rglob would
    silently widen this (the same class of bug build_link_index's docstring
    documents for the shared-resolver side)."""
    wiki_folder = tmp_path / "wiki"
    meta_folder = tmp_path / "_meta"
    wiki_folder.mkdir()
    meta_folder.mkdir()
    (wiki_folder / "Alpha.md").write_text("# Alpha\n\nRoot page.\n", encoding="utf-8")

    daily_folder = wiki_folder / "daily"
    daily_folder.mkdir()
    (daily_folder / "2026-07-25.md").write_text(
        "# 2026-07-25\n\nDaily page.\n", encoding="utf-8"
    )

    processed_folder = wiki_folder / "processed"
    processed_folder.mkdir()
    (processed_folder / "Old-Note.md").write_text(
        "# Old\n\nProcessed page.\n", encoding="utf-8"
    )

    pages = bo.build_wiki_catalog(wiki_folder, meta_folder)

    assert [p["title"] for p in pages] == ["Alpha"]


def test_build_wiki_catalog_three_pages_sorted_case_insensitive_and_persists_cache(
    tmp_path: Path,
) -> None:
    """spec #2 crit 31: build_wiki_catalog over a 3-page tmp wiki returns 3
    entries sorted by lowercased title -- not filename/insertion order and
    not a raw case-sensitive sort (which would put every uppercase-leading
    title before any lowercase-leading one) -- and persists the result to
    wiki-catalog.json.

    Filenames are deliberately decoupled from title alphabetical order
    (file1/file2/file3 holding Cherry/apple/Banana) so the glob's own
    sorted() pass can't accidentally produce the expected order on its
    own -- only the pages.sort(key=title.lower()) call can.
    """
    wiki_folder = tmp_path / "wiki"
    meta_folder = tmp_path / "_meta"
    wiki_folder.mkdir()
    meta_folder.mkdir()
    (wiki_folder / "file1.md").write_text("# Cherry\n\nCherry body.\n", encoding="utf-8")
    (wiki_folder / "file2.md").write_text("# apple\n\nApple body.\n", encoding="utf-8")
    (wiki_folder / "file3.md").write_text("# Banana\n\nBanana body.\n", encoding="utf-8")

    pages = bo.build_wiki_catalog(wiki_folder, meta_folder)

    assert len(pages) == 3
    assert [p["title"] for p in pages] == ["apple", "Banana", "Cherry"]

    cache_path = meta_folder / "wiki-catalog.json"
    assert cache_path.exists()
    cache = json.loads(cache_path.read_text(encoding="utf-8"))
    assert cache["parser_version"] == bo._CATALOG_PARSER_VERSION
    assert [p["title"] for p in cache["pages"]] == ["apple", "Banana", "Cherry"]


def test_build_wiki_catalog_corrupt_cache_json_falls_back_to_full_rebuild(
    tmp_path: Path,
) -> None:
    """spec #2 crit 33: a wiki-catalog.json that isn't parsable JSON must not
    raise -- build_wiki_catalog's cache-load path (the inner `except
    Exception:` around json.load) must treat it as a full cache miss and
    rebuild the catalog from the real page content on disk."""
    wiki_folder = tmp_path / "wiki"
    meta_folder = tmp_path / "_meta"
    wiki_folder.mkdir()
    meta_folder.mkdir()
    page = wiki_folder / "Alpha.md"
    page.write_text("# Alpha\n\nReal body text.\n", encoding="utf-8")

    cache_path = meta_folder / "wiki-catalog.json"
    cache_path.write_text("{not valid json at all", encoding="utf-8")

    pages = bo.build_wiki_catalog(wiki_folder, meta_folder)  # must not raise

    assert len(pages) == 1
    assert pages[0]["title"] == "Alpha"
    assert pages[0]["summary"] == "Real body text."

    # The corrupt cache is atomically replaced with a valid rebuilt one.
    rebuilt = json.loads(cache_path.read_text(encoding="utf-8"))
    assert rebuilt["parser_version"] == bo._CATALOG_PARSER_VERSION
    assert len(rebuilt["pages"]) == 1


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
# process_file — Phase 2 frontmatter/tags preservation (spec #3 §3.3/§3.4)
# ---------------------------------------------------------------------------

def test_process_file_merge_drops_frontmatter_still_preserves_category_and_tag(
    tmp_vault: Path, tmp_config: dict[str, Any]
) -> None:
    """Criterion 35: a 5a merge whose synthesized content drops the
    frontmatter block entirely must still end up with both the pre-existing
    non-tag key (category) and the pre-existing tag (alpha) in the written
    file — restored from `orig_fm`/`existing_tags` captured in Phase 1."""
    wiki_path = tmp_vault / "wiki" / "Alpha.md"
    existing = (
        "---\n"
        "category: Work\n"
        "tags: [alpha]\n"
        "---\n"
        "# Alpha\n\n"
        "Old note.\n"
    )
    wiki_path.write_text(existing, encoding="utf-8")

    f = write_raw(tmp_vault, "note.md", "New info about Alpha")
    client = MagicMock()
    # Simulates the LLM returning "the complete updated Wiki document" with
    # the frontmatter block silently dropped (the exact regression §3.4 guards).
    client.messages.create.return_value = make_message(
        "# Alpha\n\nMerged body content with the frontmatter dropped by the LLM.\n"
    )

    bo.process_file(
        f, tmp_config, client, logging.getLogger("test"), catalog=[],
        _routes=[("Alpha", wiki_path, False)],
    )

    result = wiki_path.read_text(encoding="utf-8")
    assert "category: Work" in result
    assert "alpha" in result


def test_process_file_no_existing_frontmatter_writes_byte_identical_content(
    tmp_vault: Path, tmp_config: dict[str, Any]
) -> None:
    """A brand-new route with no pre-existing wiki page (no frontmatter,
    no tags) must skip _write_frontmatter_tags entirely — the written file
    is byte-identical to what synthesize_wiki returned, and no stray empty
    ``---`` block is ever emitted."""
    wiki_path = tmp_vault / "wiki" / "Beta.md"
    # _call_api() strips() the raw model response before synthesize_wiki ever
    # sees it -- assert against that already-stripped form, not the padded
    # mock text, so "byte-identical" means what process_file actually wrote.
    synthesized = "# Beta\n\nBrand fresh page with no frontmatter at all.\n".strip()

    f = write_raw(tmp_vault, "note.md", "New info about Beta")
    client = MagicMock()
    client.messages.create.return_value = make_message(synthesized)

    bo.process_file(
        f, tmp_config, client, logging.getLogger("test"), catalog=[],
        _routes=[("Beta", wiki_path, True)],
    )

    result = wiki_path.read_text(encoding="utf-8")
    assert result == synthesized
    assert "---" not in result


def test_process_file_existing_frontmatter_without_tags_key_and_empty_tags_leaves_frontmatter_untouched(
    tmp_vault: Path, tmp_config: dict[str, Any]
) -> None:
    """Pins the Security-flagged edge case (STEP's Phase 2 gate is
    `existing_tags OR orig_fm`, not `existing_tags AND orig_fm`): a page
    that has frontmatter but no prior `tags:` key still triggers
    `_write_frontmatter_tags` this cycle (because `orig_fm` alone is
    truthy — needed so §3.4's category/date restore still fires even when
    a page never had tags). With `existing_tags == []` and `suggest_tags`'s
    mocked response here not valid JSON (falls back to `[]` per crit 30),
    `note_tags` stays `[]`, so the call is effectively
    `_write_frontmatter_tags(content, [])`. Spec #3 crit 44: an empty tags
    list must never CREATE a bare `tags:` key where none existed — the
    frontmatter (and the rest of the document) comes out byte-identical to
    what synthesize_wiki produced, `category` included. Supersedes this
    test's pre-cycle-26 name/assertions, which pinned the old (buggy)
    bare-`tags:`-insertion behavior from before `suggest_tags` was wired
    into `process_file`."""
    wiki_path = tmp_vault / "wiki" / "Gamma.md"
    existing = (
        "---\n"
        "category: Work\n"
        "---\n"
        "# Gamma\n\n"
        "Old body.\n"
    )
    wiki_path.write_text(existing, encoding="utf-8")

    f = write_raw(tmp_vault, "note.md", "New info about Gamma")
    client = MagicMock()
    # LLM preserves the frontmatter block verbatim this time (not the §3.4
    # drop case above) -- content still has its own "---" block, so Phase 2
    # splices directly into wiki_content's own block rather than
    # reconstructing from orig_fm. Also serves as suggest_tags's (Phase 0)
    # response, which is not valid JSON -- note_tags falls back to [].
    client.messages.create.return_value = make_message(
        "---\n"
        "category: Work\n"
        "---\n"
        "# Gamma\n\n"
        "Updated body with enough length to pass synthesize_wiki's merge "
        "length-ratio guard.\n"
    )

    bo.process_file(
        f, tmp_config, client, logging.getLogger("test"), catalog=[],
        _routes=[("Gamma", wiki_path, False)],
    )

    lines = wiki_path.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "---"
    assert lines[1] == "category: Work"
    assert lines[2] == "---"
    assert lines[3] == "# Gamma"
    assert "tags:" not in wiki_path.read_text(encoding="utf-8")


def test_process_file_new_page_writes_phase0_suggested_tags_to_frontmatter(
    tmp_vault: Path, tmp_config: dict[str, Any]
) -> None:
    """Spec #3 §3.3/§3.4 end-to-end wiring proof: for a brand-new route (no
    existing wiki file, so existing_tags == [] and orig_fm == None), Phase 0's
    suggest_tags -> _reconcile_tags output must actually land in the written
    file's frontmatter via Phase 2's extended gate (`... or note_tags`) --
    not just exist as unwired helper functions. Two client calls prove the
    real end-to-end flow: [0] is the Phase 0 tag-suggestion call, [1] is
    synthesize_wiki. catalog=[] means both proposed tags are "brand-new"
    (not in vocabulary) to _reconcile_tags's stage 6 -- tag_max_new_per_note
    is raised to 2 here (default 1, see test_reconcile_tags_caps_brand_new_
    tags_at_max_new_per_note for the pinned default-cap behavior) so this
    test can assert both survive, proving the wiring rather than the cap."""
    wiki_path = tmp_vault / "wiki" / "Delta.md"
    synthesized = "# Delta\n\nBrand new page about home automation.\n"

    tmp_config["tag_max_new_per_note"] = 2
    f = write_raw(tmp_vault, "note.md", "New info about home automation gear")
    client = MagicMock()
    client.messages.create.side_effect = [
        make_message('{"tags": ["home-automation", "smart-home"]}'),
        make_message(synthesized),
    ]

    bo.process_file(
        f, tmp_config, client, logging.getLogger("test"), catalog=[],
        _routes=[("Delta", wiki_path, True)],
    )

    assert client.messages.create.call_count == 2
    result = wiki_path.read_text(encoding="utf-8")
    assert "tags:" in result
    assert "home-automation" in result
    assert "smart-home" in result
    assert result.rstrip("\n").endswith("Brand new page about home automation.")


def test_process_file_tags_disabled_makes_no_extra_llm_call(
    tmp_vault: Path, tmp_config: dict[str, Any]
) -> None:
    """Crit 40: tags_enabled=False is the single rollback lever -- it must
    short-circuit Phase 0 entirely (no suggest_tags call at all), leaving
    the client's call count/order identical to the pre-Phase-0 world (one
    synthesis call for an already-routed, non-daily note)."""
    tmp_config["tags_enabled"] = False
    wiki_path = tmp_vault / "wiki" / "Epsilon.md"

    f = write_raw(tmp_vault, "note.md", "New info about Epsilon")
    client = MagicMock()
    client.messages.create.return_value = make_message("# Epsilon\n\nContent.\n")

    bo.process_file(
        f, tmp_config, client, logging.getLogger("test"), catalog=[],
        _routes=[("Epsilon", wiki_path, True)],
    )

    assert client.messages.create.call_count == 1
    result = wiki_path.read_text(encoding="utf-8")
    assert "tags:" not in result


def test_process_file_completes_when_suggest_tags_api_call_raises(
    tmp_vault: Path, tmp_config: dict[str, Any]
) -> None:
    """Criterion 32: a raw exception raised inside suggest_tags's _call_api
    call must not fail the note. suggest_tags already catches this itself
    and returns [] (its own docstring names crit 32 by number); Phase 0's
    surrounding try/except in process_file is a second line of defense for
    anything that slips past it. Either way, the note must still complete
    end to end: the routed wiki page is written and the raw file is
    deleted, exactly as if tag suggestion had never failed."""
    wiki_path = tmp_vault / "wiki" / "Zeta.md"
    f = write_raw(tmp_vault, "note.md", "New info about Zeta")
    client = MagicMock()
    # [0] is the Phase 0 tag-suggestion call (raises), [1] is synthesis.
    client.messages.create.side_effect = [
        RuntimeError("boom"),
        make_message("# Zeta\n\nSynthesized content.\n"),
    ]

    result = bo.process_file(
        f, tmp_config, client, logging.getLogger("test"), catalog=[],
        _routes=[("Zeta", wiki_path, True)],
    )

    assert result == ["Zeta"]
    assert wiki_path.exists()
    # _call_api strips the raw model response before synthesize_wiki ever
    # sees it -- assert against that already-stripped form.
    assert wiki_path.read_text(encoding="utf-8") == "# Zeta\n\nSynthesized content."
    assert not f.exists()


def test_process_file_5a_merge_unions_existing_and_new_tags_alpha_first(
    tmp_vault: Path, tmp_config: dict[str, Any]
) -> None:
    """Criterion 34: 5a merge path -- existing `tags: [alpha]` plus a
    Phase-0-suggested new tag `beta` must union to both tags present, with
    the pre-existing `alpha` first (Phase 2's `final_tags = existing_tags +
    [... not already present]` order at brain_organizer.py:2372-2374)."""
    wiki_path = tmp_vault / "wiki" / "Eta.md"
    existing = (
        "---\n"
        "tags: [alpha]\n"
        "---\n"
        "# Eta\n\n"
        "Old note.\n"
    )
    wiki_path.write_text(existing, encoding="utf-8")

    f = write_raw(tmp_vault, "note.md", "New info about Eta")
    client = MagicMock()
    client.messages.create.side_effect = [
        make_message('{"tags": ["beta"]}'),
        make_message(
            "# Eta\n\nMerged body content with enough length to pass "
            "synthesize_wiki's merge length-ratio guard.\n"
        ),
    ]

    bo.process_file(
        f, tmp_config, client, logging.getLogger("test"), catalog=[],
        _routes=[("Eta", wiki_path, False)],
    )

    lines = wiki_path.read_text(encoding="utf-8").splitlines()
    tags_idx = lines.index("tags:")
    assert lines[tags_idx + 1] == "  - alpha"
    assert lines[tags_idx + 2] == "  - beta"


def test_process_file_5b_splice_gains_tags_unspliced_sections_byte_identical(
    tmp_vault: Path, tmp_config: dict[str, Any]
) -> None:
    """Criterion 36: a large-page (5b splice) merge must still gain tags in
    its frontmatter via Phase 2's union, while any "## " section the splice
    response didn't touch stays byte-identical."""
    tmp_config["large_page_threshold_chars"] = 10  # force the 5b branch
    wiki_path = tmp_vault / "wiki" / "Iota.md"
    existing = "# Topic\n\n## Existing\n\noldbody\n\n## Other\n\nother content here"
    wiki_path.write_text(existing, encoding="utf-8")

    f = write_raw(tmp_vault, "note.md", "New info about Topic")
    client = MagicMock()
    client.messages.create.side_effect = [
        make_message('{"tags": ["gamma"]}'),
        make_message("## Existing\n\nnewbody"),
    ]

    bo.process_file(
        f, tmp_config, client, logging.getLogger("test"), catalog=[],
        _routes=[("Topic", wiki_path, False)],
    )

    result = wiki_path.read_text(encoding="utf-8")
    assert "tags:" in result
    assert "  - gamma" in result
    # The untouched "## Other" section is byte-identical to the original.
    assert "## Other\n\nother content here" in result
    assert "newbody" in result
    assert "oldbody" not in result


def test_process_file_no_changes_splice_with_no_new_tags_leaves_file_byte_identical(
    tmp_vault: Path, tmp_config: dict[str, Any]
) -> None:
    """Criterion 37: a large-page splice response of NO_CHANGES combined
    with zero newly suggested tags must round-trip the file byte-for-byte
    -- the 5b splice short-circuit returns existing_content verbatim, and
    Phase 2's union reproduces the identical existing tags list, so
    _write_frontmatter_tags hits its own documented no-op guarantee
    (crit 14) rather than silently rewriting the canonical block."""
    tmp_config["large_page_threshold_chars"] = 10  # force the 5b branch
    wiki_path = tmp_vault / "wiki" / "Theta.md"
    existing = (
        "---\n"
        "tags:\n"
        "  - alpha\n"
        "---\n"
        "# Topic\n\n## Existing\n\noldbody\n\n## Other\n\nother content here"
    )
    wiki_path.write_text(existing, encoding="utf-8")

    f = write_raw(tmp_vault, "note.md", "Nothing new about Topic")
    client = MagicMock()
    client.messages.create.side_effect = [
        make_message('{"tags": []}'),
        make_message("NO_CHANGES"),
    ]

    bo.process_file(
        f, tmp_config, client, logging.getLogger("test"), catalog=[],
        _routes=[("Topic", wiki_path, False)],
    )

    assert wiki_path.read_text(encoding="utf-8") == existing


def test_process_file_daily_log_page_at_wiki_root_is_tagged(
    tmp_vault: Path, tmp_config: dict[str, Any]
) -> None:
    """Criterion 39: Daily-Log.md is a real wiki-root topic page, not a
    date-stemmed page under daily_folder (see
    test_daily_note_route_non_date_stem_falls_back_to_daily_log_at_root),
    so Phase 0's `not all(p.is_relative_to(daily_folder) ...)` gate must
    still fire for it and it must be tagged like any other page."""
    wiki_folder = tmp_vault / "wiki"
    wiki_path = wiki_folder / "Daily-Log.md"

    f = write_raw(
        tmp_vault, "event-hermes-hermes-daily-digest-20260724T120009Z.md",
        "Morning digest content",
    )
    client = MagicMock()
    client.messages.create.side_effect = [
        make_message('{"tags": ["digest-summary"]}'),
        make_message("# Daily-Log\n\nDigest content synthesized.\n"),
    ]

    bo.process_file(
        f, tmp_config, client, logging.getLogger("test"), catalog=[],
        _routes=[("Daily-Log", wiki_path, True)],
    )

    assert client.messages.create.call_count == 2
    result = wiki_path.read_text(encoding="utf-8")
    assert "tags:" in result
    assert "digest-summary" in result


def test_process_file_date_stem_note_under_daily_folder_skips_tagging(
    tmp_vault: Path, tmp_config: dict[str, Any]
) -> None:
    """Criterion 38: a date-stem note whose ONLY route resolves under
    daily_folder must skip Phase 0's suggest_tags call entirely (date pages
    are a per-day journal -- tagging 365/year is noise) and the written
    page must carry no frontmatter block, unlike the Daily-Log-at-root
    sibling case pinned by test_process_file_daily_log_page_at_wiki_root_is_tagged
    (criterion 39) just above."""
    daily_folder = tmp_vault / "wiki" / "daily"
    wiki_path = daily_folder / "2026-08-02.md"

    f = write_raw(tmp_vault, "2026-08-02.md", "Notes from today")
    client = MagicMock()
    # Single-element side_effect: a suggest_tags call would be consumed here
    # and StopIteration would fire on the (nonexistent) synthesis call --
    # this is what makes the call-count assertion below load-bearing.
    client.messages.create.side_effect = [
        make_message("# 2026-08-02\n\nSynthesized daily content.\n"),
    ]

    bo.process_file(
        f, tmp_config, client, logging.getLogger("test"), catalog=[],
        _routes=[("2026-08-02", wiki_path, True)],
    )

    assert client.messages.create.call_count == 1
    result = wiki_path.read_text(encoding="utf-8")
    assert not result.startswith("---")


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


def test_defuse_unknown_wikilinks_rejects_dated_sibling_fuzzy_match() -> None:
    """Regression for the wrong-day cross-link bug: find_similar_page scores
    "Morning Briefing — 2026-06-29" at 0.947 similarity against the catalog
    title "Morning Briefing 2026-06-28" (confirmed below), which is high
    enough to pass the default threshold on its own -- but the two link
    targets name different days. The dated-sibling guard must compare digit
    runs and refuse this match, falling through to step 7's backtick-defuse
    instead of cross-linking onto the wrong day's page.
    """
    catalog = [
        {
            "title": "Morning Briefing 2026-06-28",
            "filename": "Morning-Briefing-2026-06-28.md",
            "path_str": "x",
            "headers": "",
            "summary": "",
        }
    ]
    target = "Morning Briefing — 2026-06-29"
    assert bo.find_similar_page(target, catalog) is catalog[0]  # confirms the fuzzy match fires on its own
    text = f"See [[{target}]] for today's notes."
    result = bo._defuse_unknown_wikilinks(text, "Other Topic", catalog)
    assert result == f"See `{target}` for today's notes."
    assert "Morning-Briefing-2026-06-28" not in result


def test_defuse_unknown_wikilinks_rejects_dated_sibling_via_filename_stem_axis() -> None:
    """Cycle 33 regression: the title-axis digit guard alone is not enough --
    an entry's title can be undated ("Council Loop") while its filename stem
    is dated ("Council-Loop-Build-2026-07-01"), and the REWRITE emits the
    filename stem, not the title. Before this cycle's fix the mismatch slipped
    through silently (title-axis digit lists were both empty -> "equal", so
    the guard never even looked at the filename), rewriting
    [[Council-Loop]] onto the wrong dated page. The filename-stem axis must
    be checked independently and reject the match, falling through to step
    7's backtick-defuse instead.
    """
    catalog = [
        {
            "title": "Council Loop",
            "filename": "Council-Loop-Build-2026-07-01.md",
            "path_str": "x",
            "headers": "",
            "summary": "",
        }
    ]
    target = "Council-Loop"
    assert bo.find_similar_page(target, catalog) is catalog[0]  # confirms the fuzzy match fires on its own
    text = f"See [[{target}]] for details."
    result = bo._defuse_unknown_wikilinks(text, "Other Topic", catalog)
    assert result == f"See `{target}` for details."
    assert "Council-Loop-Build-2026-07-01" not in result


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


def test_synthesize_wiki_stats_accumulator_counts_rewritten_and_backticked(
    tmp_config: dict[str, Any]
) -> None:
    """spec #2 criterion 47 / last 2 of criterion 46's 7 fields: the optional
    `stats` kwarg threaded through synthesize_wiki -> _defuse_unknown_wikilinks
    must tally exactly what steps 3-6 rewrote ("rewritten") and what step 7
    backtick-defused ("backticked") -- and must NOT move for links steps 1/2
    leave untouched (an already-correct stem, and a self-reference) -- all in
    one synthesis call, mirroring
    test_synthesize_wiki_defuses_hallucinated_link_in_create_branch above.
    """
    catalog = [
        {"title": "Bug Fixes", "filename": "Bug-Fixes.md", "path_str": "x", "headers": "", "summary": ""},
    ]
    client = MagicMock()
    client.messages.create.return_value = make_message(
        "# Topic\n\nSee [[Bug Fixes]], [[Bug-Fixes]], [[Topic]], and [[Unknown Thing]] for context."
    )
    stats: dict[str, int] = {}
    result = bo.synthesize_wiki("Topic", "content", "", tmp_config, client, catalog=catalog, stats=stats)

    # step 3 (exact title match) rewrites "Bug Fixes" -> aliased stem link
    assert "[[Bug-Fixes|Bug Fixes]]" in result
    # step 1 (already an exact stem) and step 2 (self-reference) are untouched
    assert "[[Bug-Fixes]]" in result
    assert "[[Topic]]" in result
    # step 7 (unresolvable) backtick-defuses
    assert "`Unknown Thing`" in result

    assert stats == {"rewritten": 1, "backticked": 1}


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
        # Pins em-dash punctuation stripping (§5.3 criterion 35). The
        # "develop" stem (not "development") is deliberate: "ment" was
        # adopted into _STEM_SUFFIXES from consolidate_wiki.py's copy
        # (§8.2/criterion 57) -- do not "fix" this back to "development".
        ("Front—End Development", "frontend develop"),
    ],
)
def test_normalize_title(raw: str, expected: str) -> None:
    assert bo._normalize_title(raw) == expected


def test_normalize_title_ment_suffix_matches_deployment_and_deploy() -> None:
    """Criterion 57 (§8.4): the "ment" suffix adopted from consolidate_wiki.py
    must make "Deployment" and "Deploy" normalize to the same stem.
    """
    assert bo._normalize_title("Deployment") == bo._normalize_title("Deploy")
    assert bo._normalize_title("Deployment") == "deploy"
    assert bo._normalize_title("Deploy") == "deploy"


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


def test_find_similar_page_collapses_deployment_deploy_pair() -> None:
    """§8.2's own recommendation: the nightly near-dup guard must now treat
    "Deployment" and "Deploy" as the same page (criterion 57's "ment" stem
    adoption, exercised through the actual consumer of _normalize_title).
    """
    catalog = [{"title": "Deploy", "filename": "Deploy.md", "path_str": "x", "headers": "", "summary": ""}]
    assert bo.find_similar_page("Deployment", catalog) is catalog[0]


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


def test_sanitize_topic_name_truncates_long_titles() -> None:
    # A run-on route title (Haiku occasionally proposes a full sentence as
    # a "topic") must not become a filename long enough to blow past
    # Windows' 260-char MAX_PATH once combined with the vault path + temp
    # file's uuid suffix -- see the 2026-08-06 SOP-Factory-Logo-Fix
    # incident, where this produced a real "No such file or directory".
    words = " ".join(f"Word{i}" for i in range(40))
    result = bo.sanitize_topic_name(words)
    assert len(result) <= bo._TOPIC_NAME_MAX_CHARS
    assert not result.endswith("-")
    assert result.startswith("Word0-Word1-Word2")


def test_sanitize_topic_name_truncation_falls_back_when_no_hyphen() -> None:
    # A single unbroken token longer than the cap has no hyphen boundary
    # to cut at -- must hard-truncate rather than collapse to empty/"Uncategorized".
    result = bo.sanitize_topic_name("A" * 150)
    assert result == "A" * bo._TOPIC_NAME_MAX_CHARS


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
        make_message('{"tags": []}'),
        make_message('{"tags": []}'),
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


def test_run_usage_capped_during_phase0_tag_suggestion_aborts_before_synthesis(
    tmp_vault: Path, tmp_config: dict[str, Any]
) -> None:
    """_APIUsageCapped raised from Phase 0's tag-suggestion call (suggest_tags)
    must abort the run exactly like a routing-time cap, not be swallowed by
    process_file's blanket `except Exception` and fall through to Phase 1
    synthesis.

    Uses a claude-features-digest-*.md filename so `_deterministic_route`
    matches and routing makes ZERO API calls -- the file's very FIRST
    `_call_api` is therefore Phase 0's `suggest_tags` call, exactly the path
    the two `except _APIUsageCapped: raise` re-raises (suggest_tags and
    process_file's Phase 0 handler) protect. Without those re-raises, the
    capped exception would be logged and swallowed as "tag suggestion
    failed... continuing untagged", and process_file would fall through to
    Phase 1 synthesis, which issues a SECOND `_call_api` call (and aborts
    there instead) -- so `call_count == 1` (not 2) is the assertion that
    actually distinguishes fixed from broken behavior; result/raw-file/
    processed-ledger alone would pass either way.
    """
    tmp_config["max_parallel_files"] = 1
    raw_file = write_raw(tmp_vault, "claude-features-digest-2026-01-01.md", "Digest content")
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

    assert result == 0
    assert raw_file.exists(), "aborted, not failed -- kept for retry, not attempt-counted"
    processed = bo.load_processed(tmp_config)
    assert sha not in processed, "an aborted run must not record a failed attempt"
    assert client.messages.create.call_count == 1, (
        "Phase 0's call must be the ONLY API call -- a swallowed cap would "
        "fall through to Phase 1 synthesis and call again"
    )


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


# ---------------------------------------------------------------------------
# run() summary -- always emitted, with created=/merged=/uncategorized=/
# catalog=/raw_remaining= counters (spec #2 criterion 45 / 5 of 46's 7 fields)
# ---------------------------------------------------------------------------

def test_run_zero_file_summary_still_emits_and_counts_stuck_raw_file(
    tmp_vault: Path, tmp_config: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing-to-process must still emit a summary (criterion 45), and
    raw_remaining must be a direct disk count -- not scan_raw_folder, which
    permanently skips a zero-byte/stuck file (F2) -- so a stuck file is
    visible in the signal even on a run that never got past the early exit.
    """
    write_raw(tmp_vault, "stuck.md", "")  # zero-byte: scan_raw_folder will never pick this up

    mock_telegram = MagicMock()
    monkeypatch.setattr(bo, "send_telegram_notification", mock_telegram)

    result = bo.run(_client=MagicMock(), _config=tmp_config)

    assert result == 0
    mock_telegram.assert_called_once()
    summary = mock_telegram.call_args[0][1]
    assert "✅ Files processed: 0" in summary
    assert "created=0 merged=0 uncategorized=0 catalog=0 raw_remaining=1" in summary


def test_run_summary_counts_created_merged_uncategorized_catalog_for_real_run(
    tmp_vault: Path, tmp_config: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A run that actually processes files must report counters that reflect
    what was actually processed: a new page (created), a merge into an
    existing page (merged), and the Uncategorized fallback (uncategorized --
    checked BEFORE is_new, since the fallback route always carries
    is_new=True and must not be miscounted as "created"). catalog= must
    reflect the post-run catalog (existing page + newly created pages), and
    raw_remaining=0 once every file is consumed.

    route_topics/synthesize_wiki are monkeypatched (content-keyed, not
    call-order-keyed) rather than mocking the Anthropic client directly --
    run()'s Phase A routes files across a small ThreadPoolExecutor even at
    max_parallel_files=1, so a client-level side_effect list has no
    guaranteed per-file ordering; a content-keyed fake sidesteps that race
    entirely.
    """
    wiki_folder = tmp_vault / "wiki"
    (wiki_folder / "Existing.md").write_text("# Existing\n\nOld content.", encoding="utf-8")
    write_raw(tmp_vault, "new-note.md", "content about a brand new topic")
    write_raw(tmp_vault, "existing-note.md", "content about an existing topic")
    write_raw(tmp_vault, "vague-note.md", "content nobody can classify")

    def fake_route_topics(content, catalog, config, client):
        if "brand new topic" in content:
            return [("NewTopic", wiki_folder / "NewTopic.md", True)]
        if "existing topic" in content:
            return [("Existing", wiki_folder / "Existing.md", False)]
        return [("Uncategorized", wiki_folder / "Uncategorized.md", True)]

    monkeypatch.setattr(bo, "route_topics", fake_route_topics)
    monkeypatch.setattr(
        bo, "synthesize_wiki", lambda topic, *a, **k: f"# {topic}\n\nSynthesized."
    )
    mock_telegram = MagicMock()
    monkeypatch.setattr(bo, "send_telegram_notification", mock_telegram)

    result = bo.run(_client=MagicMock(), _config=tmp_config)

    assert result == 0
    assert not (tmp_vault / "raw" / "new-note.md").exists()
    assert not (tmp_vault / "raw" / "existing-note.md").exists()
    assert not (tmp_vault / "raw" / "vague-note.md").exists()

    mock_telegram.assert_called_once()
    summary = mock_telegram.call_args[0][1]
    # 1 new page (NewTopic) + 1 merge (Existing) + 1 Uncategorized fallback;
    # catalog = pre-existing "Existing" + newly-created NewTopic + Uncategorized.
    assert "created=1 merged=1 uncategorized=1 catalog=3 raw_remaining=0" in summary


def test_run_summary_reports_links_rewritten_and_backticked_counts(
    tmp_vault: Path, tmp_config: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """spec #2 criterion 47 / last 2 of criterion 46's 7 fields: run()'s
    summary must surface links_rewritten=/links_backticked= counts derived
    from _defuse_unknown_wikilinks's stats accumulator, threaded through
    synthesize_wiki -> process_file -> run()'s _record_success (folded under
    state_lock) -> _send_run_summary. Only route_topics is faked (same
    content-keyed pattern as
    test_run_summary_counts_created_merged_uncategorized_catalog_for_real_run
    above) -- synthesize_wiki and _defuse_unknown_wikilinks run for real, so
    the counters reflect an actual title-match rewrite and an actual
    backtick-defuse produced in the same run.
    """
    wiki_folder = tmp_vault / "wiki"
    (wiki_folder / "Bug-Fixes.md").write_text("# Bug Fixes\n\nKnown issues.", encoding="utf-8")
    write_raw(tmp_vault, "note.md", "content about a new topic")

    def fake_route_topics(content, catalog, config, client):
        return [("NewTopic", wiki_folder / "NewTopic.md", True)]

    monkeypatch.setattr(bo, "route_topics", fake_route_topics)
    client = MagicMock()
    client.messages.create.return_value = make_message(
        "# NewTopic\n\nSee [[Bug Fixes]] and also [[Unknown Thing]] for context."
    )
    mock_telegram = MagicMock()
    monkeypatch.setattr(bo, "send_telegram_notification", mock_telegram)

    result = bo.run(_client=client, _config=tmp_config)

    assert result == 0
    assert (wiki_folder / "NewTopic.md").exists()

    mock_telegram.assert_called_once()
    summary = mock_telegram.call_args[0][1]
    assert "links_rewritten=1 links_backticked=1" in summary


def test_run_summary_reports_tags_added_and_tags_new_vocabulary_counts(
    tmp_vault: Path, tmp_config: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Robustness spec §6.3 -- run()'s summary must surface tags_added= and
    tags_new_vocabulary= counts, threaded from process_file's Phase 0
    (suggest_tags -> _reconcile_tags, compared case-insensitively against the
    catalog-derived vocabulary) and Phase 2 (len(final_tags) -
    len(existing_tags) for the written page) through run()'s
    _record_success (folded under state_lock) -> _send_run_summary. Same
    route_topics-faked pattern as
    test_run_summary_reports_links_rewritten_and_backticked_counts above.

    A pre-existing wiki page tagged "existing-tag" seeds the catalog-derived
    vocabulary with exactly one entry. The Phase 0 tag-suggestion call
    proposes ["existing-tag", "new-vocab-tag"] -- one vocabulary match, one
    brand-new tag (default tag_max_new_per_note=1 allows exactly one new tag
    through _reconcile_tags, and a vocabulary match never counts against
    that cap). The destination page is brand new (existing_tags == []), so
    both proposed tags land in its frontmatter:
      - tags_new_vocabulary must be 1 (only "new-vocab-tag" is absent from
        the vocabulary; "existing-tag" is not new).
      - tags_added must be 2 (both survive onto a page with no prior tags).
    Asserting these as two DIFFERENT numbers (not just both correct as the
    same value) is the point -- it fails if the two counters get swapped or
    conflated.
    """
    wiki_folder = tmp_vault / "wiki"
    (wiki_folder / "ExistingPage.md").write_text(
        "---\ntags:\n  - existing-tag\n---\n# ExistingPage\n\nSome content.",
        encoding="utf-8",
    )
    write_raw(tmp_vault, "note.md", "content about a new topic")

    def fake_route_topics(content, catalog, config, client):
        return [("NewTopic", wiki_folder / "NewTopic.md", True)]

    monkeypatch.setattr(bo, "route_topics", fake_route_topics)
    client = MagicMock()
    client.messages.create.side_effect = [
        make_message('{"tags": ["existing-tag", "new-vocab-tag"]}'),
        make_message("# NewTopic\n\nSynthesized body about a new topic."),
    ]
    mock_telegram = MagicMock()
    monkeypatch.setattr(bo, "send_telegram_notification", mock_telegram)

    result = bo.run(_client=client, _config=tmp_config)

    assert result == 0
    written = (wiki_folder / "NewTopic.md").read_text(encoding="utf-8")
    assert "existing-tag" in written
    assert "new-vocab-tag" in written

    mock_telegram.assert_called_once()
    summary = mock_telegram.call_args[0][1]
    assert "tags_added=2 tags_new_vocabulary=1" in summary


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


def test_large_page_splice_last_section_no_trailing_newline_leaves_no_stale_content(
    tmp_config: dict[str, Any],
) -> None:
    """Regression for spec #2 crit 41 / spec #1 crit 11 (defect 1): the body-line
    regex used to require each body line to end in "\\n" to be consumed, so when
    the replaced section was the LAST thing in the page and the raw file had no
    trailing newline, that final unterminated line fell outside the match and
    survived -- stale old content glued underneath the freshly spliced-in new
    section, silently corrupting the page forever. The fix's `(?:\\n|$)`
    alternative must consume that last line too, producing byte-clean output.
    """
    tmp_config["large_page_threshold_chars"] = 10  # force the 5b branch
    existing = "# Topic\n\n## Existing\n\noldSTALEbody"  # no trailing "\n" -- deliberate
    client = MagicMock()
    client.messages.create.return_value = make_message("## Existing\n\nnewbody")
    result = bo.synthesize_wiki("Topic", "new content", existing, tmp_config, client)
    assert "STALE" not in result  # no trace of the old body survives anywhere
    assert result == "# Topic\n\n## Existing\n\nnewbody\n"


def test_large_page_splice_restores_blank_line_before_following_section(
    tmp_config: dict[str, Any],
) -> None:
    """Regression for spec #2 crit 41 / spec #1 crit 11 (defect 2): when the
    replaced section is followed by another untouched "## " section, the old
    replacement logic consumed the blank-line separator along with the matched
    body and never put it back, joining the new content directly onto the next
    header with no blank line. The fix must restore exactly one blank line, and
    the untouched following section's content must be byte-identical.
    """
    tmp_config["large_page_threshold_chars"] = 10  # force the 5b branch
    existing = "# Topic\n\n## Existing\n\noldbody\n\n## Other\n\nother content here"
    client = MagicMock()
    client.messages.create.return_value = make_message("## Existing\n\nnewbody")
    result = bo.synthesize_wiki("Topic", "new content", existing, tmp_config, client)
    assert result == (
        "# Topic\n\n## Existing\n\nnewbody\n\n## Other\n\nother content here"
    )


def test_large_page_splice_is_idempotent_on_repeated_run(
    tmp_config: dict[str, Any],
) -> None:
    """Light bonus check (Security's note): re-running the splice a second time
    with the same returned section must not drift the spacing further -- running
    it twice should converge to the same byte-identical output as running it
    once, not double up or lose the separator on repeat application.
    """
    tmp_config["large_page_threshold_chars"] = 10  # force the 5b branch
    existing = "# Topic\n\n## Existing\n\noldbody\n\n## Other\n\nother content here"
    client = MagicMock()
    client.messages.create.return_value = make_message("## Existing\n\nnewbody")
    result_once = bo.synthesize_wiki("Topic", "new content", existing, tmp_config, client)

    client2 = MagicMock()
    client2.messages.create.return_value = make_message("## Existing\n\nnewbody")
    result_twice = bo.synthesize_wiki("Topic", "new content", result_once, tmp_config, client2)
    assert result_twice == result_once


def test_large_page_splice_appends_new_section_absent_from_original_at_end(
    tmp_config: dict[str, Any],
) -> None:
    """spec #2 crit 42: a splice response containing a "## Gamma" section
    that has no matching header anywhere in the existing page must be
    appended at the end (the section-match regex finds no match, so the
    loop falls into its "else: append" branch), with everything before it
    left byte-identical."""
    tmp_config["large_page_threshold_chars"] = 10  # force the 5b branch
    existing = "# Topic\n\n## Alpha\n\nalpha body here"  # no trailing "\n"
    client = MagicMock()
    client.messages.create.return_value = make_message("## Gamma\n\ngamma content here")

    result = bo.synthesize_wiki("Topic", "new content", existing, tmp_config, client)

    assert result.startswith(existing)  # everything before the append is byte-identical
    assert result == existing + "\n\n## Gamma\n\ngamma content here\n"
    assert "## Alpha" in result
    assert result.index("## Gamma") > result.index("## Alpha")


def test_large_page_splice_discards_leading_preamble_before_first_heading(
    tmp_config: dict[str, Any],
) -> None:
    """spec #2 crit 43 (:997): a splice response with freeform preamble text
    before its first "## " heading must have that preamble discarded
    entirely -- raw_chunks's leading non-"## " chunk is filtered out of
    section_chunks, so only the real "## " block reaches the splice and the
    preamble text must not appear anywhere in the result.

    The edited section is deliberately NOT the page's last section: if the
    preamble filter at brain_organizer.py:2006 were deleted, the unfiltered
    preamble chunk would find no header match and get appended at true EOF,
    where "## Later"'s untouched body would no longer be the last thing in
    the file to swallow it back out -- so a leaked preamble is observable
    here, unlike with a single-section fixture.
    """
    tmp_config["large_page_threshold_chars"] = 10  # force the 5b branch
    existing = "# Topic\n\n## Existing\n\noldbody\n\n## Later\n\nlaterbody\n"
    client = MagicMock()
    client.messages.create.return_value = make_message(
        "Here's the updated section based on your request:\n\n## Existing\n\nnewbody"
    )

    result = bo.synthesize_wiki("Topic", "new content", existing, tmp_config, client)

    assert "Here's the updated section" not in result
    assert result == "# Topic\n\n## Existing\n\nnewbody\n\n## Later\n\nlaterbody\n"


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


# ---------------------------------------------------------------------------
# validate_config (spec #2 SS4.3 F4, acceptance criteria 19-24)
# ---------------------------------------------------------------------------

def test_validate_config_raises_naming_missing_required_key(tmp_config: dict[str, Any]) -> None:
    del tmp_config["vault_path"]
    with pytest.raises(ValueError, match="vault_path"):
        bo.validate_config(tmp_config)


def test_validate_config_unknown_key_logs_warning_and_does_not_raise(
    tmp_config: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    tmp_config["catalog_max_pages_in_promt"] = 60  # typo'd key
    with caplog.at_level(logging.WARNING, logger="brain_organizer"):
        merged = bo.validate_config(tmp_config)
    assert "catalog_max_pages_in_promt" in caplog.text
    assert merged["catalog_max_pages_in_promt"] == 60


def test_validate_config_wrong_type_on_known_optional_key_raises(tmp_config: dict[str, Any]) -> None:
    tmp_config["max_file_chars"] = "not-a-number"
    with pytest.raises(ValueError, match="max_file_chars"):
        bo.validate_config(tmp_config)


def test_validate_config_out_of_range_numeric_raises(tmp_config: dict[str, Any]) -> None:
    tmp_config["new_page_similarity_threshold"] = 1.5
    with pytest.raises(ValueError, match="new_page_similarity_threshold"):
        bo.validate_config(tmp_config)


def test_validate_config_fills_absent_optional_keys_from_defaults(tmp_config: dict[str, Any]) -> None:
    # tmp_config is now derived from _CONFIG_DEFAULTS, so these keys are
    # present by construction -- delete them here to simulate the
    # absent-key case this test actually exercises.
    del tmp_config["backup_retention_days"]
    del tmp_config["large_page_threshold_chars"]
    merged = bo.validate_config(tmp_config)
    assert merged["backup_retention_days"] == 30
    assert merged["large_page_threshold_chars"] == 20000


def test_validate_config_does_not_mutate_input(tmp_config: dict[str, Any]) -> None:
    original = dict(tmp_config)
    bo.validate_config(tmp_config)
    assert tmp_config == original


def test_validate_config_defaults_match_real_config_json_values() -> None:
    """Regression for the F4 drift: three inline literals (large_page_threshold_chars,
    sonnet_max_tokens, max_file_attempts) had gone stale relative to config.json.

    This asserts against _CONFIG_DEFAULTS directly, NOT against
    validate_config(real_config)'s merged output -- real_config.json supplies
    all three of these keys itself, so a merged-output assertion is vacuous:
    the config's own value always wins over the default regardless of what
    the default is, and would still pass even if _CONFIG_DEFAULTS reverted to
    the old stale literals (sonnet_max_tokens=8192, large_page_threshold_chars
    =35000, max_file_attempts=5). Checking _CONFIG_DEFAULTS itself is what
    actually pins the default in place.
    """
    config_path = Path(__file__).parent.parent / "config.json"
    with config_path.open(encoding="utf-8") as fh:
        real_config = json.load(fh)

    assert bo._CONFIG_DEFAULTS["large_page_threshold_chars"] == real_config["large_page_threshold_chars"] == 20000
    assert bo._CONFIG_DEFAULTS["sonnet_max_tokens"] == real_config["sonnet_max_tokens"] == 16384
    assert bo._CONFIG_DEFAULTS["max_file_attempts"] == real_config["max_file_attempts"] == 2


def test_validate_config_real_config_json_keys_and_defaults_do_not_drift() -> None:
    """Spec SS4.3 criterion 24: bidirectional drift test.

    Every key in the real config.json must be covered by _CONFIG_REQUIRED or
    _CONFIG_DEFAULTS (otherwise validate_config would silently warn about a
    key that IS actually read, or a shipped key would go unvalidated) --
    and every key in _CONFIG_DEFAULTS must actually appear in config.json
    (otherwise a default is silently covering a key that was never actually
    shipped in production, which is exactly how backup_retention_days and
    daily_folder drifted this cycle without anything catching it)."""
    config_path = Path(__file__).parent.parent / "config.json"
    with config_path.open(encoding="utf-8") as fh:
        real_config = json.load(fh)

    known_keys = set(bo._CONFIG_REQUIRED) | set(bo._CONFIG_DEFAULTS)

    missing_from_known = set(real_config) - known_keys
    assert not missing_from_known, (
        f"config.json has keys not in _CONFIG_REQUIRED/_CONFIG_DEFAULTS: {missing_from_known}"
    )

    defaults_never_shipped = set(bo._CONFIG_DEFAULTS) - set(real_config)
    assert not defaults_never_shipped, (
        f"_CONFIG_DEFAULTS has keys never present in config.json: {defaults_never_shipped}"
    )


@pytest.mark.parametrize(
    "key,value",
    [
        ("tag_max_per_note", -1),
        ("tag_max_new_per_note", -1),
        ("tag_max_per_page", -1),
        ("tag_vocabulary_max_in_prompt", 10001),
    ],
)
def test_validate_config_out_of_range_tag_keys_raises(
    tmp_config: dict[str, Any], key: str, value: int
) -> None:
    """Security's auto-fix this cycle added the four new numeric tag config
    keys to _CONFIG_RANGES (tag_max_per_note/tag_max_new_per_note/
    tag_max_per_page: 0-1000, tag_vocabulary_max_in_prompt: 0-10000).
    Exercises the real validate_config() range-check path per key, matching
    the existing test_validate_config_out_of_range_numeric_raises
    convention, rather than just inspecting _CONFIG_RANGES's dict contents
    (which the generic drift test above does not cover -- it only checks
    _CONFIG_DEFAULTS/config.json key parity, not _CONFIG_RANGES)."""
    tmp_config[key] = value
    with pytest.raises(ValueError, match=key):
        bo.validate_config(tmp_config)


# ---------------------------------------------------------------------------
# _normalize_tag / _reconcile_tags (spec #3 SS1.3 -- criteria 21-28)
# ---------------------------------------------------------------------------


def test_reconcile_tags_canonicalizes_onto_vocabulary_spelling() -> None:
    """spec #3 criterion 21: a proposed tag differing only in separator/case
    from an existing vocabulary entry is replaced with the vocabulary's own
    spelling, not the proposed spelling -- the corpus spelling always wins."""
    result = bo._reconcile_tags(["Home Automation"], ["home-automation"], [], bo._CONFIG_DEFAULTS)
    assert result == ["home-automation"]


def test_reconcile_tags_canonicalizes_via_stemming() -> None:
    """spec #3 criterion 22: _normalize_tag's _normalize_title stemming
    matches a plural proposed tag onto its singular vocabulary spelling."""
    result = bo._reconcile_tags(["startups"], ["startup"], [], bo._CONFIG_DEFAULTS)
    assert result == ["startup"]


def test_reconcile_tags_caps_brand_new_tags_at_max_new_per_note() -> None:
    """spec #3 criterion 23: with the default tag_max_new_per_note=1, three
    brand-new proposed tags against an empty vocabulary yield exactly one
    survivor -- surplus new tags are dropped, not renamed. The survivor is
    deterministically the first-proposed tag: stage 6 walks `fresh` in
    original order and stops accepting new tags the instant the cap is
    hit, so "alpha" (not an arbitrary member of the set) must win. A
    membership-only assertion here would pass even if a future change
    silently reordered the surviving tag."""
    result = bo._reconcile_tags(["alpha", "beta", "gamma"], [], [], bo._CONFIG_DEFAULTS)
    assert result == ["alpha"]


def test_reconcile_tags_drops_category_prefixed_value() -> None:
    """spec #3 criterion 24: a 'category/*' proposed tag is dropped.

    Also pins the engineer's deliberate ordering deviation from the spec's
    prose (slugify, THEN shape-filter for category/*): this implementation
    checks the category/ prefix on the lowercased/stripped raw value BEFORE
    slugify would strip the '/' the check depends on. Checking mixed case
    and surrounding whitespace confirms the guard fires on the raw value,
    not on the post-slugify shape (where the '/' would already be gone and
    the check could never match)."""
    result = bo._reconcile_tags(["category/projects"], [], [], bo._CONFIG_DEFAULTS)
    assert result == []
    result_mixed_case_and_whitespace = bo._reconcile_tags(
        ["  Category/Projects  "], [], [], bo._CONFIG_DEFAULTS
    )
    assert result_mixed_case_and_whitespace == []


def test_reconcile_tags_drops_tags_already_present_in_existing() -> None:
    """spec #3 criterion 25: a proposed tag already in `existing` (matched
    case-insensitively) is dropped rather than duplicated, even when it also
    canonicalizes onto a vocabulary spelling first."""
    result = bo._reconcile_tags(["Alpha"], ["alpha"], ["alpha"], bo._CONFIG_DEFAULTS)
    assert result == []


def test_reconcile_tags_at_page_cap_returns_empty_without_running_pipeline() -> None:
    """spec #3 criterion 26, and the engineer's stage-7-checked-first
    reordering: when len(existing) >= tag_max_per_page (default 12), the
    function short-circuits to [] even though `proposed` contains a tag that
    would otherwise cleanly survive every later stage (exact vocabulary
    match, not already in existing, well-shaped) -- confirming stage 7 runs
    before, and independent of, the rest of the pipeline. Nothing is ever
    removed from existing; growth just stops."""
    existing = [f"tag{i}" for i in range(12)]
    result = bo._reconcile_tags(["brand-new-topic"], ["brand-new-topic"], existing, bo._CONFIG_DEFAULTS)
    assert result == []


def test_reconcile_tags_truncates_to_max_per_note() -> None:
    """spec #3 criterion 27: six valid vocabulary tags are truncated to the
    default tag_max_per_note=5. All six are already-vocabulary spellings so
    the tag_max_new_per_note=1 cap (which only limits tags NOT already in
    the vocabulary) cannot itself explain the truncation."""
    vocab = [f"vocab-tag-{i}" for i in range(6)]
    result = bo._reconcile_tags(vocab, vocab, [], bo._CONFIG_DEFAULTS)
    assert result == vocab[:5]


def test_reconcile_tags_drops_junk_inputs_without_raising() -> None:
    """spec #3 criterion 28: empty string, single-char, purely-numeric,
    punctuation-only, and over-length (>30 char) proposed tags are all
    dropped by the shape filter, and none raises."""
    junk = ["", "a", "12345", "###", "x" * 40]
    result = bo._reconcile_tags(junk, [], [], bo._CONFIG_DEFAULTS)
    assert result == []


# ---------------------------------------------------------------------------
# Regression: stage 3 (canonicalize) can feed stage 4 (de-duplicate)
# multiple variant spellings of the same vocabulary entry -- Realist-caught
# defect in a prior revision of this cycle, fixed by adding the de-dup
# stage. Criterion 14 is the no-op idempotency guarantee these tests pin.
# ---------------------------------------------------------------------------


def test_reconcile_tags_dedups_variant_spellings_that_canonicalize_together() -> None:
    """Realist-requested regression test: three variant spellings of the
    same concept ("Home Automation", "home-automation", "home_automation")
    all canonicalize onto the single vocabulary entry "home-automation" via
    stage 3. Before the fix, nothing collapsed the three resulting
    duplicates and all three copies of "home-automation" would have been
    emitted, burning tag_max_per_note budget on one repeated concept.
    Stage 4 must collapse them to exactly one survivor."""
    result = bo._reconcile_tags(
        ["Home Automation", "home-automation", "home_automation"],
        ["home-automation"],
        [],
        bo._CONFIG_DEFAULTS,
    )
    assert result == ["home-automation"]


def test_reconcile_tags_dedup_output_round_trips_through_frontmatter_write_parse() -> None:
    """Realist-requested regression test guarding criterion 14 (no-op
    idempotency) at the boundary between _reconcile_tags's de-dup stage and
    the frontmatter read/write helpers it feeds in practice: writing
    _reconcile_tags's deduped output via _write_frontmatter_tags, parsing
    it back out via _parse_frontmatter_tags, and writing again with the
    re-parsed tags must reproduce the exact same bytes -- nothing lost,
    duplicated, or reordered on the way out and back in. If stage 4's
    dedup ever regressed and let duplicate canonical spellings through
    again, _parse_frontmatter_tags's own first-seen dedup (a different,
    unrelated code path) would silently absorb the surplus at parse time
    and this test would still catch the drift via the reparsed != result
    check below, rather than falsely passing."""
    proposed = ["Home Automation", "home-automation", "home_automation"]
    reconciled = bo._reconcile_tags(proposed, ["home-automation"], [], bo._CONFIG_DEFAULTS)
    assert reconciled == ["home-automation"]  # sanity: stage 4 already proven above

    original = "---\ntitle: Test\n---\nBody text.\n"
    written_once = bo._write_frontmatter_tags(original, reconciled)

    reparsed = bo._parse_frontmatter_tags(written_once)
    assert reparsed == reconciled  # nothing lost or duplicated round-tripping through disk

    written_twice = bo._write_frontmatter_tags(written_once, reparsed)
    assert written_twice == written_once  # byte-identical no-op -- criterion 14
