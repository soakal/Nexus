"""Unit tests for brain_organizer.py."""
from __future__ import annotations

import json
import logging
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
])
def test_is_daily_note_matches_dated_and_briefing_stems(stem: str) -> None:
    assert bo._is_daily_note(stem) is True


@pytest.mark.parametrize("stem", ["NEXUS", "nexus-session-2026-06-25b-ha-cover-lock-fix"])
def test_is_daily_note_rejects_non_daily_stems(stem: str) -> None:
    assert bo._is_daily_note(stem) is False


def test_daily_note_route_returns_none_for_non_daily(tmp_path: Path) -> None:
    assert bo._daily_note_route("NEXUS-session-notes", [], tmp_path) is None


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
    routes = bo._daily_note_route("Daily-Operations-Log-2026-07-08", [], tmp_path)
    assert routes == [("2026-07-08", tmp_path / "2026-07-08.md", True)]


def test_daily_note_route_reuses_existing_date_page(tmp_path: Path) -> None:
    catalog = [{
        "title": "2026-07-08", "filename": "2026-07-08.md",
        "path_str": str(tmp_path / "2026-07-08.md"), "headers": "", "summary": "",
    }]
    routes = bo._daily_note_route("Morning-Briefing-2026-07-08", catalog, tmp_path)
    assert routes == [("2026-07-08", tmp_path / "2026-07-08.md", False)]


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
    assert result == text  # left alone -- find_similar_page recognizes it


def test_defuse_unknown_wikilinks_allows_self_reference() -> None:
    text = "This page is about [[My New Topic]] specifically."
    result = bo._defuse_unknown_wikilinks(text, "My New Topic", [])
    assert result == text


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
# Hermes notification
# ---------------------------------------------------------------------------

def test_hermes_notification_sent(tmp_vault: Path, tmp_config: dict[str, Any]) -> None:
    tmp_config["hermes_host"] = "http://hermes.local:5000"
    http_client = MagicMock()
    bo.send_hermes_notification(tmp_config, "Test message", http_client=http_client)
    http_client.post.assert_called_once()
    call_args = http_client.post.call_args
    assert "notify" in call_args.args[0]
    assert call_args.kwargs["json"]["message"] == "Test message"


def test_hermes_notification_skipped_when_host_is_placeholder(
    tmp_vault: Path, tmp_config: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("HERMES_HOST", raising=False)
    tmp_config["hermes_host"] = "http://HERMES_HOST_HERE"
    http_client = MagicMock()
    bo.send_hermes_notification(tmp_config, "Test", http_client=http_client)
    http_client.post.assert_not_called()


def test_hermes_notification_skipped_when_host_empty(
    tmp_vault: Path, tmp_config: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("HERMES_HOST", raising=False)
    tmp_config["hermes_host"] = ""
    http_client = MagicMock()
    bo.send_hermes_notification(tmp_config, "Test", http_client=http_client)
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
