"""Tests for brain_organizer_trial.py -- the deliberate fork used by Trial B
(the 2026-08 OpenRouter model-swap trial for the nightly wiki-synthesis run).

Only the two real changes from the production script need dedicated coverage
here: the `"/" in model` direct-routing branch, and reasoning_max_tokens
passthrough. Everything else in the file is byte-identical to
brain_organizer.py and already covered by test_organizer.py's own suite.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import anthropic
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import brain_organizer_trial as bot  # noqa: E402 -- must follow the sys.path insert above


@pytest.fixture(autouse=True)
def _isolate_usage_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """_call_api's successful path appends to _USAGE_LOG, which brain_spend.py
    ingests into real SpendLog rows on the live box -- same contamination
    class CLAUDE.md documents for the Unraid backup mirror and the
    calibration-table rows. Must never touch the real usage.jsonl."""
    monkeypatch.setattr(bot, "_USAGE_LOG", tmp_path / "usage.jsonl")


def test_openrouter_native_model_routes_directly_no_anthropic_attempt(
    tmp_config: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """An OpenRouter-native model id (contains '/') must go straight to
    OpenRouter -- the Anthropic client's messages.create must never be called."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-or-key")
    client = MagicMock()

    or_response = {
        "choices": [{"message": {"content": "synthesized wiki text"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 50},
    }

    with patch("brain_organizer_trial.httpx.post") as mock_post:
        mock_post.return_value = MagicMock(
            status_code=200,
            json=lambda: or_response,
            raise_for_status=lambda: None,
        )
        text, stop_reason = bot._call_api(
            "google/gemini-2.5-pro", [{"role": "user", "content": "x"}], 8192, tmp_config, client,
        )

    assert text == "synthesized wiki text"
    assert stop_reason == "stop"
    client.messages.create.assert_not_called()
    mock_post.assert_called_once()
    call_json = mock_post.call_args.kwargs["json"]
    assert call_json["model"] == "google/gemini-2.5-pro"
    assert "reasoning" not in call_json  # no reasoning_max_tokens in tmp_config


def test_openrouter_native_model_without_key_raises(
    tmp_config: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    client = MagicMock()

    with pytest.raises(RuntimeError, match="OPENROUTER_API_KEY"):
        bot._call_api("google/gemini-2.5-pro", [{"role": "user", "content": "x"}], 8192, tmp_config, client)

    client.messages.create.assert_not_called()


def test_reasoning_max_tokens_passed_through_when_configured(
    tmp_config: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-or-key")
    client = MagicMock()
    config = {**tmp_config, "reasoning_max_tokens": 2048}

    or_response = {
        "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }

    with patch("brain_organizer_trial.httpx.post") as mock_post:
        mock_post.return_value = MagicMock(
            status_code=200,
            json=lambda: or_response,
            raise_for_status=lambda: None,
        )
        bot._call_api("google/gemini-2.5-pro", [{"role": "user", "content": "x"}], 8192, config, client)

    call_json = mock_post.call_args.kwargs["json"]
    assert call_json["reasoning"] == {"max_tokens": 2048}


def test_openrouter_length_finish_reason_normalized_to_max_tokens(
    tmp_config: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reasoning model that exhausts max_tokens on thinking returns
    finish_reason='length' -- must normalize to Anthropic's 'max_tokens'
    sentinel, or synthesize_wiki's truncation guard never fires and a
    truncated response gets written as if complete."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-or-key")
    client = MagicMock()

    or_response = {
        "choices": [{"message": {"content": "truncated..."}, "finish_reason": "length"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 8192},
    }

    with patch("brain_organizer_trial.httpx.post") as mock_post:
        mock_post.return_value = MagicMock(
            status_code=200,
            json=lambda: or_response,
            raise_for_status=lambda: None,
        )
        _, stop_reason = bot._call_api(
            "google/gemini-2.5-pro", [{"role": "user", "content": "x"}], 8192, tmp_config, client,
        )

    assert stop_reason == "max_tokens"


def test_bare_anthropic_model_id_still_falls_back_to_openrouter_on_failure(
    tmp_config: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bare (non-OpenRouter) model id -- the production behavior -- must be
    unaffected by the trial's new routing branch: Anthropic is tried first,
    OpenRouter is only a same-model fallback on exhaustion, exactly as in
    brain_organizer.py."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-or-key")
    client = MagicMock()
    client.messages.create.side_effect = anthropic.APITimeoutError(request=MagicMock())

    or_response = {
        "choices": [{"message": {"content": "fallback text"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }

    with patch("brain_organizer_trial.time.sleep"), \
         patch("brain_organizer_trial.httpx.post") as mock_post:
        mock_post.return_value = MagicMock(
            status_code=200,
            json=lambda: or_response,
            raise_for_status=lambda: None,
        )
        text, stop_reason = bot._call_api(
            "claude-sonnet-4-6", [{"role": "user", "content": "x"}], 8192, tmp_config, client, max_retries=1,
        )

    assert text == "fallback text"
    call_json = mock_post.call_args.kwargs["json"]
    assert call_json["model"] == "anthropic/claude-sonnet-4-6"
