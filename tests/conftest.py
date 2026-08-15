import pytest
import asyncio
import os
from unittest.mock import AsyncMock, MagicMock, patch
from pathlib import Path

from sqlmodel import SQLModel, create_engine

# Set test env before any imports
os.environ.setdefault("HASS_HOST", "http://localhost:8123")
os.environ.setdefault("UNIFI_HOST", "https://localhost")
os.environ.setdefault("UNIFI_USERNAME", "admin")
os.environ.setdefault("UNRAID_HOST", "192.168.1.1")
os.environ.setdefault("CHANNELS_HOST", "http://localhost:8089")
os.environ.setdefault("ADGUARD_HOST", "http://localhost:3000")
os.environ.setdefault("ADGUARD_USER", "admin")
os.environ.setdefault("GITHUB_USERNAME", "testuser")

# ASSIGNMENT, not setdefault -- must beat both a real value already in the
# shell AND the cwd .env file (process env outranks pydantic-settings'
# env_file). backend/secrets/vault.py::set_secret() calls backup_vault() on
# EVERY secret write, best-effort -- any test that touches set_secret()
# without its own settings mock inherits whatever unraid_backup_path
# resolves to. Left unset, that's backend/config.py's real Windows-share
# default, and on POSIX with no .env in pytest's cwd, backup_vault()'s
# rclone sync mirror-deletes that real Unraid share -- live-reproduced
# 2026-08-14 (twice: once from this repo's own test runs, once from an
# independent verification pass), including deletion of the real dated
# backup history. Forcing this here, before any backend import, means
# get_settings()'s cache (populated lazily on first Settings()) can only
# ever be built from the safe empty value.
os.environ["UNRAID_BACKUP_PATH"] = ""

# Mock secrets so vault isn't required in tests
MOCK_SECRETS = {
    "ANTHROPIC_API_KEY": "sk-ant-test-key",
    "HASS_TOKEN": "test-hass-token",
    "UNIFI_PASSWORD": "test-password",
    "UNRAID_API_KEY": "test-unraid-key",
    "GITHUB_TOKEN": "test-github-token",
    "OPENWEATHER_API_KEY": "test-weather-key",
    "OPENROUTER_API_KEY": "test-openrouter-key",
    "ADGUARD_PASS": "test-adguard-pass",
    "NEXUS_API_KEY": "test-nexus-key",
    "PROTONMAIL_MCP_URL": "http://test-mcp:8080/mcp",
    "PROTONMAIL_ACCOUNT": "test-proton-account",
    "TELEGRAM_BOT_TOKEN": "test-telegram-token",
    "TELEGRAM_CHAT_ID": "12345",
    "GOOGLE_CALENDAR_ICAL_URL": "https://calendar.google.com/test.ics",
    "APPLE_CALENDAR_ICAL_URL": "https://p.icloud.com/test.ics",
}


@pytest.fixture(scope="session", autouse=True)
def _isolate_test_database(tmp_path_factory):
    """Repoint backend.database's engine/DB_PATH at a throwaway session-scoped
    temp-file SQLite DB before any test runs, so no pytest run can ever write
    into the live repo-root nexus.db (backend/database.py's DB_PATH is
    cwd-relative -- a plain `pytest` invocation from the repo root was writing
    real rows, e.g. leaked OutcomeFlag rows, into Brian's production DB).

    Session-scoped so it applies once, first, before the earliest test's own
    fixtures run. The ubiquitous per-test pattern used across the suite --
    `monkeypatch.setattr("backend.database.engine", <own StaticPool/tmp_path
    engine>)` (and the `file_db`/`env` fixtures that also set `DB_PATH`) --
    still wins for the duration of its own test: monkeypatch restores whatever
    value was live when it patched, i.e. THIS session's temp engine, never the
    original live one, so nothing here fights those existing fixtures.
    """
    import backend.database as db

    db_path = tmp_path_factory.mktemp("nexus_test_db") / "test_nexus.db"
    test_engine = create_engine(
        f"sqlite:///{db_path}",
        echo=False,
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    SQLModel.metadata.create_all(test_engine)

    original_engine = db.engine
    original_db_path = db.DB_PATH
    db.engine = test_engine
    db.DB_PATH = db_path

    yield

    db.engine = original_engine
    db.DB_PATH = original_db_path


@pytest.fixture(autouse=True)
def mock_secrets(monkeypatch):
    """Patch secret access to return test values without a real vault or Infisical.

    Forces secrets_backend=vault so the manager routing seam never picks
    Infisical mid-test, and hard-guards infisical_client's network calls so a
    misconfigured test can't accidentally reach out over the network.
    """
    def fake_get_secret(key, fallback_env=True):
        if key in MOCK_SECRETS:
            return MOCK_SECRETS[key]
        if fallback_env and key in os.environ:
            return os.environ[key]
        raise KeyError(f"Secret '{key}' not in test mock")

    monkeypatch.setenv("SECRETS_BACKEND", "vault")
    monkeypatch.setattr("backend.secrets.manager.get_secret", fake_get_secret)
    monkeypatch.setattr("backend.secrets.vault.get_secret", lambda k: MOCK_SECRETS.get(k, (_ for _ in ()).throw(KeyError(k))))

    def _network_guard(*args, **kwargs):
        raise AssertionError("infisical_client attempted a real network call during a test")

    monkeypatch.setattr("backend.secrets.infisical_client._request", _network_guard)
    monkeypatch.setattr("backend.secrets.infisical_client.warm_up", lambda: False)


@pytest.fixture(autouse=True)
def _isolate_backup_targets(monkeypatch, tmp_path):
    """Second layer of defense (the env override above is the first) against
    a test reaching a real Unraid share: even if some test's own fixture
    explicitly sets a real-looking unraid_backup_path (e.g. to test
    backup_vault's own UNC-handling logic), backend.backup._run_rclone is
    the ONE subprocess seam every rclone invocation in that module goes
    through -- patching it here to hard-fail means a test that forgets to
    re-patch it for its own purposes gets a loud, immediate error instead of
    a silent real write.

    Monkeypatch layering makes this compatible with tests that legitimately
    want to exercise the real rclone-dispatch path: a test-level
    monkeypatch.setattr on `_run_rclone` or `_rclone_sync` wins for the
    duration of that test and monkeypatch restores to what was live when
    it patched -- i.e. THIS guard, never the real function -- exactly the
    same reasoning _isolate_test_database's docstring documents for engines.
    tests/test_vault_backup.py's own `env`-fixture tests use a non-UNC tmp
    `share` path -- pure shutil, never reaches _run_rclone at all -- so they
    are unaffected by this guard without needing to patch anything.
    """
    monkeypatch.setattr("backend.backup._STAGING_ROOT", tmp_path / "unraid_staging")

    def _guard(*args, **kwargs):
        raise AssertionError(
            "test attempted a real rclone invocation via backend.backup._run_rclone "
            "-- patch _run_rclone or _rclone_sync in your own test"
        )

    monkeypatch.setattr("backend.backup._run_rclone", _guard)


@pytest.fixture(autouse=True)
def reset_caches():
    """Clear all async_ttl_cache state before each test so a cached health_check /
    fetch result from one test can't leak into the next (the caches hold
    module-level state that otherwise persists for the whole session)."""
    from backend.cache import reset_all_caches
    reset_all_caches()
    try:
        from backend.state_store import reset_memory_cache as _reset_state_store
        _reset_state_store()
    except Exception:
        pass
    try:
        from backend.agents.worker_pool import reset_pool
        reset_pool()
    except Exception:
        pass
    try:
        from backend.api.trigger import _reset_rate_limit
        _reset_rate_limit()
    except Exception:
        pass
    try:
        from backend.safety.authfail import reset as _reset_authfail
        _reset_authfail()
    except Exception:
        pass
    try:
        from backend.secrets.fallback_log import reset as _reset_fallback_log
        _reset_fallback_log()
    except Exception:
        pass
    yield


@pytest.fixture(autouse=True)
def _isolate_claude_rate_limits(tmp_path, monkeypatch):
    """Point claude_usage's file seam at a per-test temp path that does NOT
    exist, so no test can ever read Brian's real ~/.claude/rate-limits.json
    (which every active Claude Code session on this machine rewrites -- a test
    reading it would be non-deterministic and machine-dependent). Default state
    for the whole suite is therefore 'no capture yet'; a test that wants data
    writes the file itself. Same principle as _isolate_test_database above.
    """
    path = tmp_path / "claude" / "rate-limits.json"
    monkeypatch.setattr("backend.integrations.claude_usage._rate_limits_path", lambda: path)
    return path


@pytest.fixture(autouse=True)
def _isolate_openrouter_fetch(request, monkeypatch):
    """briefing.py's gather calls openrouter.fetch() directly (2026-08-05,
    Claude/OpenRouter usage tracker build) -- unlike every other integration
    run_briefing() gathers, no existing test patches it (openrouter wasn't in
    that gather until this change), so leaving this unmocked would make every
    pre-existing run_briefing() test fire a real outbound HTTPS call to
    openrouter.ai. Default to a safe unavailable result; a test that wants to
    exercise real OpenRouter data in a briefing patches this locally
    (innermost patch wins, same pattern as auto_mock_opus_verify above).
    Skipped for test_openrouter.py, which directly tests the real fetch()."""
    if request.module.__name__ == "test_openrouter":
        return
    from backend.integrations.openrouter import OpenRouterData

    async def _fake_fetch():
        return OpenRouterData(available=False)

    monkeypatch.setattr("backend.integrations.openrouter.fetch", _fake_fetch)


@pytest.fixture
def api_key():
    return "test-nexus-key"


@pytest.fixture
def auth_headers(api_key):
    return {"Authorization": f"Bearer {api_key}"}


@pytest.fixture(autouse=True)
def action_judge_off_by_default(monkeypatch):
    """Force the action-judge gate OFF for every test by default.

    Wiring `judge.evaluate_action` into `broker.execute_action` (council cycle
    5) means any pre-existing test exercising `execute_action` through the
    real settings singleton would otherwise fall through to the judge's real
    "shadow" default and attempt an actual model call. Tests that specifically
    want to exercise the judge gate override this per-test, e.g.
    `monkeypatch.setattr(get_settings(), "action_judge_mode", "enforce")`
    (innermost patch wins, same pattern as `auto_mock_opus_verify` below).
    """
    from backend.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "action_judge_mode", "off")


@pytest.fixture(autouse=True)
def auto_mock_opus_verify(request):
    """Auto-patch _opus_verify in the durable orchestrator path to return a
    permissive success dict by default.

    This prevents the new Opus verifier call (which itself calls run_with_tools)
    from interfering with pre-existing tests that patch run_with_tools and assert
    on its call count. Tests in test_learning_loop.py that need to control the
    verifier's behaviour patch _opus_verify themselves (innermost patch wins).

    Tests that directly unit-test _opus_verify (calling the real function) should
    be marked with @pytest.mark.real_opus_verify to skip this auto-mock so they
    get the actual implementation.
    """
    if request.node.get_closest_marker("real_opus_verify"):
        yield
        return

    _DEFAULT = {
        "verdict": "success",
        "confidence": 1.0,
        "reason": "auto-mocked verifier",
        "grounded": False,
        "evidence": None,
    }
    with patch(
        "backend.agents.orchestrator._opus_verify",
        new_callable=AsyncMock,
        return_value=_DEFAULT,
    ):
        yield


@pytest.fixture(autouse=True)
def mock_emit_event(request):
    """Auto-patch obsidian.emit_event to a no-op AsyncMock for every test.

    Goal-lifecycle transitions (approve/reject/reconcile_running) now call
    `obsidian.emit_event(...)`; without this, every test exercising those code
    paths would attempt a real HTTP call to the Brain MCP server. Tests that
    need to assert on emit_event's call args re-patch it locally inside their
    own `with patch(...)` block (innermost patch wins, same pattern as
    auto_mock_opus_verify above).

    Skipped for test_obsidian.py, which directly tests the real emit_event/
    _format_event implementation.
    """
    if request.module.__name__ == "test_obsidian":
        yield
        return
    with patch("backend.integrations.obsidian.emit_event", new_callable=AsyncMock):
        yield
