"""Where the optional brain-organizer module lives and how to launch it.

The module is a separate project under `modules/brain-organizer` with its own
venv. Three call sites spawn something out of it — the scheduler's nightly job,
the /api/brain-organizer/run endpoint, and main.py's MCP-server boot — so the
paths and the secret-injected subprocess environment live here once instead of
being re-derived (and drifting) in each.
"""
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

MODULE_DIR = Path(__file__).parent.parent / "modules" / "brain-organizer"
PYTHON_EXE = MODULE_DIR / "venv" / "Scripts" / "python.exe"
ORGANIZER_SCRIPT = MODULE_DIR / "brain_organizer.py"
MCP_SERVER_SCRIPT = MODULE_DIR / "mcp_server.py"
PROCESSED_JSON = MODULE_DIR / "processed.json"
LOG_FILE = MODULE_DIR / "logs" / "organizer.log"
CONFIG_JSON = MODULE_DIR / "config.json"

# Settings attribute -> environment variable the module reads.
_INJECTED_SECRETS = (
    ("anthropic_api_key", "ANTHROPIC_API_KEY"),
    ("openrouter_api_key", "OPENROUTER_API_KEY"),
    ("hermes_host", "HERMES_HOST"),
)


def is_installed() -> bool:
    """True when both the module venv and the organizer script are present."""
    return PYTHON_EXE.exists() and ORGANIZER_SCRIPT.exists()


def subprocess_env() -> dict:
    """The current environment plus the secrets the module needs.

    Injected from the NEXUS vault so the subprocess sees ANTHROPIC_API_KEY /
    OPENROUTER_API_KEY / HERMES_HOST even when the parent process doesn't
    export them. Best-effort per key: an unreadable secret is skipped rather
    than failing the launch.
    """
    env = os.environ.copy()
    try:
        from backend.config import get_settings
        settings = get_settings()
    except Exception as e:
        logger.warning(f"Brain Organizer: could not inject secrets from vault ({e}) — using inherited env")
        return env

    for attr, var in _INJECTED_SECRETS:
        try:
            val = getattr(settings, attr, None)
        except Exception:
            val = None
        if val:
            env[var] = str(val)
    return env
