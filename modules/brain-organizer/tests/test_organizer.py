"""Unit tests for brain_organizer.py."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import brain_organizer as bo
from anthropic.types import TextBlock

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def write_raw(vault: Path, name: str, content: str) -> Path:
    f = vault / "raw" / name
    f.write_text(content, encoding="utf-8")
    return f


def make_message(text: str) -> MagicMock:
    """Build a mock Message with a real TextBlock so isinstance checks pass."""
    msg = MagicMock()
    msg.content = [TextBlock(type="text", text=text)]
    return msg


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
    # 2 < max_file_attempts (5), so file is retry-eligible
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


# ---------------------------------------------------------------------------
# backup_file
# ---------------------------------------------------------------------------

def test_backup_before_processing(tmp_vault: Path, tmp_config: dict[str, Any]) -> None:
    f = write_raw(tmp_vault, "note.md", "Backup me")
    backup_path = bo.backup_file(tmp_config, f)
    assert backup_path.exists()
    assert backup_path.read_text(encoding="utf-8") == "Backup me"
    backup_folder = tmp_vault / "raw" / "backups"
    assert backup_path.parent == backup_folder
    assert "note.md" in backup_path.name


def test_backup_creates_timestamped_filename(tmp_vault: Path, tmp_config: dict[str, Any]) -> None:
    f = write_raw(tmp_vault, "note.md", "content")
    backup_path = bo.backup_file(tmp_config, f)
    assert backup_path.name.endswith("note.md")
    # UTC timestamp prefix — contains dashes in date/time portion
    assert backup_path.name.count("-") >= 4


# ---------------------------------------------------------------------------
# detect_topics
# ---------------------------------------------------------------------------

def test_topic_detection_returns_valid_json(tmp_config: dict[str, Any]) -> None:
    client = MagicMock()
    client.messages.create.return_value = make_message('{"topics": ["NEXUS", "Home Assistant"]}')
    topics = bo.detect_topics("Some content about NEXUS and Home Assistant", tmp_config, client)
    assert topics == ["NEXUS", "Home Assistant"]


def test_topic_detection_falls_back_on_bad_json(tmp_config: dict[str, Any]) -> None:
    client = MagicMock()
    client.messages.create.return_value = make_message("not json at all")
    topics = bo.detect_topics("content", tmp_config, client)
    assert topics == ["Uncategorized"]


def test_topic_detection_falls_back_on_empty_list(tmp_config: dict[str, Any]) -> None:
    client = MagicMock()
    client.messages.create.return_value = make_message('{"topics": []}')
    topics = bo.detect_topics("content", tmp_config, client)
    assert topics == ["Uncategorized"]


def test_topic_detection_caps_at_five(tmp_config: dict[str, Any]) -> None:
    client = MagicMock()
    many = ["A", "B", "C", "D", "E", "F", "G"]
    client.messages.create.return_value = make_message(json.dumps({"topics": many}))
    topics = bo.detect_topics("content", tmp_config, client)
    assert len(topics) <= 5


def test_topic_detection_uses_haiku_model(tmp_config: dict[str, Any]) -> None:
    client = MagicMock()
    client.messages.create.return_value = make_message('{"topics": ["Test"]}')
    bo.detect_topics("content", tmp_config, client)
    call_kwargs = client.messages.create.call_args
    assert call_kwargs.kwargs["model"] == tmp_config["haiku_model"]


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


# ---------------------------------------------------------------------------
# processed.json tracking
# ---------------------------------------------------------------------------

def test_processed_json_tracking(tmp_vault: Path, tmp_config: dict[str, Any], mock_anthropic_client: MagicMock) -> None:
    write_raw(tmp_vault, "note.md", "NEXUS content")
    bo.run(_client=mock_anthropic_client, _config=tmp_config)
    # Run a second time — processed.json should skip the file
    mock_anthropic_client.messages.create.reset_mock()
    bo.run(_client=mock_anthropic_client, _config=tmp_config)
    # Second run should not call the API at all
    mock_anthropic_client.messages.create.assert_not_called()


def _make_run_client(topic_text: str, wiki_text: str) -> MagicMock:
    client = MagicMock()
    topic_resp = MagicMock()
    topic_resp.content = [TextBlock(type="text", text=topic_text)]
    wiki_resp = MagicMock()
    wiki_resp.content = [TextBlock(type="text", text=wiki_text)]
    client.messages.create.side_effect = [topic_resp, wiki_resp]
    return client


def test_raw_file_deleted_after_success(tmp_vault: Path, tmp_config: dict[str, Any]) -> None:
    raw_file = write_raw(tmp_vault, "note.md", "NEXUS content")
    client = _make_run_client('{"topics": ["NEXUS"]}', "# NEXUS\n\nWiki content.")
    result = bo.run(_client=client, _config=tmp_config)
    assert result == 0
    assert not raw_file.exists()


def test_raw_file_kept_on_failure(tmp_vault: Path, tmp_config: dict[str, Any]) -> None:
    raw_file = write_raw(tmp_vault, "note.md", "NEXUS content")

    client = MagicMock()
    # Topic detection succeeds, wiki synthesis explodes
    topic_resp = MagicMock()
    topic_resp.content = [TextBlock(type="text", text='{"topics": ["NEXUS"]}')]
    client.messages.create.side_effect = [topic_resp, RuntimeError("API down")]

    result = bo.run(_client=client, _config=tmp_config)
    assert result == 1
    assert raw_file.exists()


def test_failure_records_attempt_count(tmp_vault: Path, tmp_config: dict[str, Any]) -> None:
    raw_file = write_raw(tmp_vault, "note.md", "Content")
    sha = bo.compute_sha256(raw_file)

    client = MagicMock()
    topic_resp = MagicMock()
    topic_resp.content = [TextBlock(type="text", text='{"topics": ["NEXUS"]}')]
    client.messages.create.side_effect = [topic_resp, RuntimeError("fail")]

    bo.run(_client=client, _config=tmp_config)

    processed = bo.load_processed(tmp_config)
    assert sha in processed
    assert processed[sha]["status"] == "failed"
    assert processed[sha]["attempts"] == 1


def test_failure_stops_retrying_after_max_attempts(tmp_vault: Path, tmp_config: dict[str, Any]) -> None:
    tmp_config["max_file_attempts"] = 2
    raw_file = write_raw(tmp_vault, "bad.md", "Content")
    sha = bo.compute_sha256(raw_file)

    def make_failing_client() -> MagicMock:
        client = MagicMock()
        topic_resp = MagicMock()
        topic_resp.content = [TextBlock(type="text", text='{"topics": ["NEXUS"]}')]
        client.messages.create.side_effect = [topic_resp, RuntimeError("fail")]
        return client

    # Run twice to exhaust max_file_attempts=2
    bo.run(_client=make_failing_client(), _config=tmp_config)
    bo.run(_client=make_failing_client(), _config=tmp_config)

    # Third run: file is still in raw/, but scan should skip it
    third_client = MagicMock()
    bo.run(_client=third_client, _config=tmp_config)
    third_client.messages.create.assert_not_called()


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
    tmp_vault: Path, tmp_config: dict[str, Any]
) -> None:
    tmp_config["hermes_host"] = "http://HERMES_HOST_HERE"
    http_client = MagicMock()

    bo.send_hermes_notification(tmp_config, "Test", http_client=http_client)

    http_client.post.assert_not_called()


def test_hermes_notification_skipped_when_host_empty(
    tmp_vault: Path, tmp_config: dict[str, Any]
) -> None:
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
