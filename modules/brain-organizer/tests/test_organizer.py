"""Unit tests for brain_organizer.py."""
from __future__ import annotations

import json
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


def make_client(topic_text: str, wiki_text: str, stop_reason: str = "end_turn") -> MagicMock:
    client = MagicMock()
    client.messages.create.side_effect = [
        make_message(topic_text),
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
    assert client.messages.create.call_args.kwargs["model"] == tmp_config["haiku_model"]


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
# API retry + OpenRouter fallback
# ---------------------------------------------------------------------------

def test_api_retries_on_timeout_then_succeeds(tmp_config: dict[str, Any]) -> None:
    client = MagicMock()
    client.messages.create.side_effect = [
        anthropic.APITimeoutError(request=MagicMock()),
        make_message('{"topics": ["NEXUS"]}'),
    ]
    topics = bo.detect_topics("content", tmp_config, client)
    assert topics == ["NEXUS"]
    assert client.messages.create.call_count == 2


def test_api_retries_on_rate_limit_then_succeeds(tmp_config: dict[str, Any]) -> None:
    client = MagicMock()
    client.messages.create.side_effect = [
        anthropic.RateLimitError(message="rate limited", response=MagicMock(), body={}),
        make_message('{"topics": ["NEXUS"]}'),
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
        "choices": [{"message": {"content": '{"topics": ["NEXUS"]}'}, "finish_reason": "stop"}]
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
    topic_resp = make_message('{"topics": ["NEXUS", "Hermes"]}')
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
    client = make_client('{"topics": ["NEXUS"]}', "# NEXUS\n\nWiki content.")
    result = bo.run(_client=client, _config=tmp_config)
    assert result == 0
    assert not raw_file.exists()


def test_raw_file_kept_on_failure(tmp_vault: Path, tmp_config: dict[str, Any]) -> None:
    raw_file = write_raw(tmp_vault, "note.md", "NEXUS content")
    client = MagicMock()
    topic_resp = make_message('{"topics": ["NEXUS"]}')
    client.messages.create.side_effect = [topic_resp, RuntimeError("API down")]
    result = bo.run(_client=client, _config=tmp_config)
    assert result == 1
    assert raw_file.exists()


def test_failure_records_attempt_count(tmp_vault: Path, tmp_config: dict[str, Any]) -> None:
    raw_file = write_raw(tmp_vault, "note.md", "Content")
    sha = bo.compute_sha256(raw_file)
    client = MagicMock()
    client.messages.create.side_effect = [make_message('{"topics": ["NEXUS"]}'), RuntimeError("fail")]
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
        c.messages.create.side_effect = [make_message('{"topics": ["NEXUS"]}'), RuntimeError("fail")]
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
    client = make_client('{"topics": ["NEXUS"]}', "# NEXUS\n\nWiki content.")
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
    client1 = make_client('{"topics": ["NEXUS"]}', "# NEXUS\n\nContent.")
    bo.run(_client=client1, _config=tmp_config)

    write_raw(tmp_vault, "note2.md", "Hermes content")
    client2 = make_client('{"topics": ["Hermes"]}', "# Hermes\n\nContent.")
    bo.run(_client=client2, _config=tmp_config)

    registry = json.loads((tmp_vault / "_meta" / "topics-registry.json").read_text(encoding="utf-8"))
    assert "NEXUS" in registry
    assert "Hermes" in registry


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
