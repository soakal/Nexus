"""Shared fixtures for Brain Organizer tests."""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from anthropic.types import TextBlock

# Make the module root importable regardless of where pytest is invoked from
sys.path.insert(0, str(Path(__file__).parent.parent))


def _make_message(text: str, stop_reason: str = "end_turn") -> MagicMock:
    """Build a mock anthropic.Message with a real TextBlock so isinstance checks pass."""
    msg = MagicMock()
    msg.content = [TextBlock(type="text", text=text)]
    msg.stop_reason = stop_reason
    return msg


@pytest.fixture
def tmp_vault(tmp_path: Path) -> Path:
    """Minimal vault directory structure."""
    vault = tmp_path / "Brain"
    (vault / "raw" / "backups").mkdir(parents=True)
    (vault / "wiki").mkdir(parents=True)
    (vault / "_meta").mkdir(parents=True)
    return vault


@pytest.fixture
def tmp_config(tmp_path: Path, tmp_vault: Path) -> dict[str, Any]:
    """Config dict pointing at tmp_vault with isolated log and processed paths."""
    return {
        "vault_path": str(tmp_vault),
        "raw_folder": "raw",
        "wiki_folder": "wiki",
        "backup_folder": "raw/backups",
        "meta_folder": "_meta",
        "logs_folder": str(tmp_path / "logs"),
        "processed_file": str(tmp_path / "processed.json"),
        "mcp_port": 8765,
        "mcp_host": "0.0.0.0",
        "haiku_model": "claude-haiku-4-5-20251001",
        "sonnet_model": "claude-sonnet-4-6",
        "sonnet_max_tokens": 8192,
        "max_file_chars": 50000,
        "hermes_host": "",
        "api_provider": "anthropic",
        "max_file_attempts": 5,
        "mcp_write_token": "",
    }


@pytest.fixture
def mock_anthropic_client() -> MagicMock:
    """Mock Anthropic client pre-loaded with canned topic + wiki responses."""
    client = MagicMock()
    client.messages.create.side_effect = [
        _make_message('{"topics": ["NEXUS"]}'),
        _make_message("# NEXUS\n\n## Overview\n\nNEXUS is a personal AI OS."),
    ]
    return client


@pytest.fixture
def wiki_app(tmp_config: dict[str, Any]):
    """Flask test client backed by an isolated tmp vault."""
    from mcp_server import create_app
    app = create_app(config=tmp_config)
    app.config["TESTING"] = True
    return app.test_client()
