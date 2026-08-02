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

import brain_organizer as bo  # noqa: E402 -- must follow the sys.path insert above


def _make_message(text: str, stop_reason: str = "end_turn") -> MagicMock:
    """Build a mock anthropic.Message with a real TextBlock so isinstance checks pass."""
    msg = MagicMock()
    msg.content = [TextBlock(type="text", text=text)]
    msg.stop_reason = stop_reason
    return msg


@pytest.fixture(autouse=True)
def _no_real_secrets_in_tests(monkeypatch: pytest.MonkeyPatch) -> None:
    """brain_organizer.py calls load_dotenv() at import time, pulling real
    secrets from modules/brain-organizer/.env into os.environ for the whole
    test process. send_telegram_notification() reads TELEGRAM_BOT_TOKEN/
    TELEGRAM_CHAT_ID straight from the environment -- so without this, any
    failure-path test (e.g. test_raw_file_kept_on_failure) fires a REAL POST
    to the real Telegram bot. This is the exact same class of bug that
    previously produced the "note.md"/"bad.md" Telegram spam via a leaked
    HERMES_HOST/HERMES_WEBHOOK_SECRET (now retired) -- the new Telegram
    secrets get the identical treatment. Strip them for every test,
    unconditionally.
    """
    for var in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "ANTHROPIC_API_KEY", "OPENROUTER_API_KEY"):
        monkeypatch.delenv(var, raising=False)


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
    """Config dict derived from _CONFIG_DEFAULTS -- the same defaults
    production's validate_config() fills in -- plus the required-key path
    overrides that point everything at the isolated tmp vault/log/processed
    locations. This is deliberate (spec #2 criterion 27): a test fixture
    built from hand-picked literals can silently drift from production's
    real thresholds (that drift is exactly what F4 found), whereas deriving
    from _CONFIG_DEFAULTS means a test can never exercise a different value
    than production without saying so explicitly.
    """
    return {
        **bo._CONFIG_DEFAULTS,
        "vault_path": str(tmp_vault),
        "raw_folder": "raw",
        "wiki_folder": "wiki",
        "backup_folder": "raw/backups",
        "meta_folder": "_meta",
        "logs_folder": str(tmp_path / "logs"),
        "processed_file": str(tmp_path / "processed.json"),
        "haiku_model": "claude-haiku-4-5-20251001",
        "sonnet_model": "claude-sonnet-4-6",
        # Pin sequential processing: the vast majority of these tests write a
        # single raw file and assert on a specific mock side_effect call
        # order -- without this explicit pin, every run() call in this
        # fixture's tests would use _CONFIG_DEFAULTS's production value (4,
        # a multi-worker ThreadPoolExecutor) instead of the historical
        # implicit sequential default. Tests that deliberately exercise the
        # parallel path already override this key themselves (see
        # test_run_parallel_path_processes_multiple_files et al).
        "max_parallel_files": 1,
    }


@pytest.fixture
def mock_anthropic_client() -> MagicMock:
    """Mock Anthropic client pre-loaded with canned route + wiki responses."""
    client = MagicMock()
    client.messages.create.side_effect = [
        _make_message('{"routes": [{"title": "NEXUS", "match": "new"}]}'),
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
