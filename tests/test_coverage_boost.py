"""Targeted tests to bring coverage from 72% to >=80%."""
import asyncio
import pathlib
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi.testclient import TestClient
from sqlmodel import SQLModel, Session, create_engine
from sqlmodel.pool import StaticPool


# ---------------------------------------------------------------------------
# Shared fixture — full app with in-memory DB
# ---------------------------------------------------------------------------

def _make_engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return engine


@pytest.fixture
def nexus_client(tmp_path, monkeypatch):
    (tmp_path / ".vault.key").write_bytes(b"A" * 32)
    (tmp_path / "nexus.vault").write_text("{}")
    monkeypatch.chdir(tmp_path)

    test_engine = _make_engine()

    def override_session():
        with Session(test_engine) as session:
            yield session

    with patch("backend.database.create_db_and_tables"), \
         patch("backend.scheduler.setup_scheduler"), \
         patch("backend.scheduler.scheduler") as sched, \
         patch("backend.agents.memo_watcher.start_watcher_blocking"), \
         patch("backend.agents.memo_watcher.stop_watcher", new_callable=AsyncMock), \
         patch("backend.state_workers.prime_state_workers", new_callable=AsyncMock):
        sched.running = False
        from backend.main import app
        from backend.database import get_session
        app.dependency_overrides[get_session] = override_session
        with TestClient(app) as c:
            yield c
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# backend/secrets/migrations.py
# ---------------------------------------------------------------------------

def test_generate_vault_key_creates_new_key(tmp_path):
    key_file = tmp_path / ".vault.key"
    import backend.secrets.migrations as mig
    original = mig.KEY_PATH
    try:
        mig.KEY_PATH = key_file
        mig.generate_vault_key()
        assert key_file.exists()
        assert len(key_file.read_bytes()) == 44  # Fernet key is base64 44 bytes
    finally:
        mig.KEY_PATH = original


def test_generate_vault_key_skips_existing(tmp_path):
    key_file = tmp_path / ".vault.key"
    key_file.write_bytes(b"existing_key_content")
    import backend.secrets.migrations as mig
    original = mig.KEY_PATH
    try:
        mig.KEY_PATH = key_file
        mig.generate_vault_key()
        assert key_file.read_bytes() == b"existing_key_content"
    finally:
        mig.KEY_PATH = original


def test_import_env_file_imports_secrets(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# comment\n"
        "ANTHROPIC_API_KEY=sk-test\n"
        "HASS_HOST=http://localhost:8123\n"  # NON_SECRET → skipped
        "GITHUB_TOKEN=ghp_test\n"
        "\n"
        "NOEQUAL\n"
    )
    import backend.secrets.migrations as mig
    with patch("backend.secrets.migrations.set_secret") as mock_set:
        imported, skipped = mig.import_env_file(str(env_file))
    assert imported == 2
    assert skipped == 1
    mock_set.assert_any_call("ANTHROPIC_API_KEY", "sk-test")
    mock_set.assert_any_call("GITHUB_TOKEN", "ghp_test")


def test_import_env_file_strips_quotes(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text('MY_SECRET="quoted_value"\n')
    import backend.secrets.migrations as mig
    with patch("backend.secrets.migrations.set_secret") as mock_set:
        imported, _ = mig.import_env_file(str(env_file))
    mock_set.assert_called_once_with("MY_SECRET", "quoted_value")
    assert imported == 1


def test_import_env_file_not_found(tmp_path):
    import backend.secrets.migrations as mig
    with pytest.raises(FileNotFoundError):
        mig.import_env_file(str(tmp_path / "missing.env"))


# backend/integrations/openrouter.py -- moved to its own tests/test_openrouter.py
# (2026-08-05, extended fetch() to also read GET /api/v1/key credit data;
# the old single-mocked-client-response tests here couldn't distinguish the
# two calls that now happen inside fetch()).

# ---------------------------------------------------------------------------
# backend/scheduler.py
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_scheduler_run_briefing_success():
    from backend.scheduler import _run_briefing
    with patch("backend.agents.briefing.run_briefing", new_callable=AsyncMock, return_value="briefing text"):
        await _run_briefing()  # Should not raise


@pytest.mark.asyncio
async def test_scheduler_run_briefing_exception_notifies_and_reraises():
    from backend.scheduler import _run_briefing
    with patch("backend.agents.briefing.run_briefing", new_callable=AsyncMock, side_effect=Exception("boom")), \
         patch("backend.events.notify_phone", new_callable=AsyncMock) as mock_notify:
        with pytest.raises(Exception, match="boom"):
            await _run_briefing()
    mock_notify.assert_awaited_once()
    assert "Morning briefing failed" in mock_notify.await_args[0][0]


@pytest.mark.asyncio
async def test_scheduler_retry_pending():
    from backend.scheduler import _retry_pending_deliveries
    with patch("backend.integrations.telegram.deliver_pending", new_callable=AsyncMock) as mock_dp:
        await _retry_pending_deliveries()
    mock_dp.assert_called_once()


@pytest.mark.asyncio
async def test_scheduler_retry_pending_exception_reraises():
    from backend.scheduler import _retry_pending_deliveries
    with patch("backend.integrations.telegram.deliver_pending", new_callable=AsyncMock, side_effect=Exception("telegram down")):
        with pytest.raises(Exception, match="telegram down"):
            await _retry_pending_deliveries()


def test_setup_scheduler_adds_jobs(monkeypatch):
    from datetime import datetime
    import backend.config as config_mod
    import backend.scheduler as sched_mod
    from backend.scheduler import setup_scheduler, scheduler
    # Far-future so the one-off infisical_soak_reminder job always registers,
    # regardless of the real current date.
    monkeypatch.setattr(sched_mod, "INFISICAL_SOAK_REMINDER_AT", datetime(2099, 1, 1, 9, 0))
    # This test counts jobs under FULL configuration -- conftest.py forces
    # UNRAID_BACKUP_PATH="" suite-wide (real-backup test isolation, unrelated
    # to this test's own concern), which would also silently skip the
    # vault_backup/knowledge_backup job registrations this test wants to
    # count. Give it back a real-looking (but fake) UNC path, scoped to this
    # test only.
    monkeypatch.setenv("UNRAID_BACKUP_PATH", "\\\\test-host\\test-share")
    monkeypatch.setattr(config_mod, "_settings_instance", None)
    with patch.object(scheduler, "add_job") as mock_add:
        setup_scheduler("07:30", "America/New_York")
    # Baseline was 25 jobs (this assumes modules/brain-organizer/venv exists,
    # same as it does on the real running instance -- see the "brain_organizer"
    # job's venv-presence guard in setup_scheduler; a bare `git worktree add`
    # checkout without that gitignored venv will register 24 instead and this
    # one assertion will legitimately fail there until that venv is set up
    # too) +4 "state_refresh_{30,60,300,600}s" (2026-08-05, see
    # backend/state_workers.py -- one job per COLLECTOR_GROUPS interval,
    # registered via register_state_workers()) +1 "anthropic_balance_watch"
    # (2026-08-05, monthly) -1 "hermes_soak_reminder" (removed 2026-08-09,
    # Hermes fully decommissioned) = 29, +1 "knowledge_backup" (2026-08-14,
    # Linux port) = 30. These deltas landed as separate commits; if any
    # flips back off, drop this count and its id below deliberately, not as
    # a side effect of an unrelated change. +1 "weekly_review" (2026-08-17).
    # +1 "obligations_check" (2026-08-21, Obligation tracker).
    expected_count = 32
    assert mock_add.call_count == expected_count
    ids_set = set()
    for c in mock_add.call_args_list:
        ids_set.add(c.kwargs.get("id"))
    expected_ids = {
        "morning_briefing",
        "retention_prune",
        "retry_deliveries",
        "record_uptime",
        "state_refresh_30s",
        "state_refresh_60s",
        "state_refresh_300s",
        "state_refresh_600s",
        "brain_spend_ingest",
        "secret_fallback_drain",
        "record_speedtest",
        "step_watchdog",
        "goal_proposer",
        "mail_autodraft",
        "autonomy_digest",
        "db_checkpoint",
        "db_backup",
        "vault_backup",
        "watchdog",
        "homelab_watch",
        "homelab_digest",
        "spend_report",
        "goal_recurrence",
        "brain_organizer",
        "wiki_fragmentation_report",
        "infisical_soak_reminder",
        "facts_digest",
        "calibration_recompute",
        "anthropic_balance_watch",
        "knowledge_backup",
        "weekly_review",
        "obligations_check",
    }
    assert ids_set == expected_ids


def test_auth_burst_check_adds_no_scheduler_job(monkeypatch):
    """The 401-burst watchdog check rides the EXISTING 5-min `watchdog` job
    (see backend/agents/watchdog.py::run_watchdog) rather than registering its
    own scheduler job — matches the same choice already made for
    check_budget_warning. If a future change moves it to its own job, this
    test and test_setup_scheduler_adds_jobs's call_count must both be
    updated together, deliberately."""
    from datetime import datetime
    import backend.config as config_mod
    import backend.scheduler as sched_mod
    from backend.scheduler import setup_scheduler, scheduler
    monkeypatch.setattr(sched_mod, "INFISICAL_SOAK_REMINDER_AT", datetime(2099, 1, 1, 9, 0))
    # See test_setup_scheduler_adds_jobs for why this is needed (conftest's
    # UNRAID_BACKUP_PATH="" test-isolation default would otherwise also
    # silently skip vault_backup/knowledge_backup registration here).
    monkeypatch.setenv("UNRAID_BACKUP_PATH", "\\\\test-host\\test-share")
    monkeypatch.setattr(config_mod, "_settings_instance", None)
    with patch.object(scheduler, "add_job") as mock_add:
        setup_scheduler("07:30", "America/New_York")
    ids_set = {c.kwargs.get("id") for c in mock_add.call_args_list}
    assert "auth_burst" not in ids_set
    assert "auth_failure" not in ids_set
    # See test_setup_scheduler_adds_jobs for the +1 knowledge_backup
    # (2026-08-14), +1 weekly_review (2026-08-17), and +1 obligations_check
    # (2026-08-21) explanations.
    assert mock_add.call_count == 32


def test_morning_briefing_disabled_skips_job(monkeypatch):
    """MORNING_BRIEFING_ENABLED=false must skip only that one job -- the
    hour/minute parse it shares with homelab_digest's briefing_time+5
    computation stays unconditional, so homelab_digest is unaffected."""
    from datetime import datetime
    import backend.config as config_mod
    import backend.scheduler as sched_mod
    from backend.scheduler import setup_scheduler, scheduler
    monkeypatch.setattr(sched_mod, "INFISICAL_SOAK_REMINDER_AT", datetime(2099, 1, 1, 9, 0))
    monkeypatch.setenv("UNRAID_BACKUP_PATH", "\\\\test-host\\test-share")
    monkeypatch.setenv("MORNING_BRIEFING_ENABLED", "false")
    monkeypatch.setattr(config_mod, "_settings_instance", None)
    with patch.object(scheduler, "add_job") as mock_add:
        setup_scheduler("07:30", "America/New_York")
    ids_set = {c.kwargs.get("id") for c in mock_add.call_args_list}
    assert "morning_briefing" not in ids_set
    assert "homelab_digest" in ids_set
    expected_count = 32 - 1
    assert mock_add.call_count == expected_count


def test_morning_briefing_enabled_default_is_true():
    """Class default must stay True -- a fresh checkout registers the job
    normally with no override needed."""
    from backend.config import Settings
    assert Settings.model_fields["morning_briefing_enabled"].default is True


# ---------------------------------------------------------------------------
# backend/api/agents.py — WebSocketManager
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ws_manager_connect():
    from backend.api.agents import WebSocketManager
    mgr = WebSocketManager()
    ws = AsyncMock()
    await mgr.connect(ws)
    ws.accept.assert_called_once()
    assert ws in mgr.active


@pytest.mark.asyncio
async def test_ws_manager_disconnect_present():
    from backend.api.agents import WebSocketManager
    mgr = WebSocketManager()
    ws = MagicMock()
    mgr.active.append(ws)
    mgr.disconnect(ws)
    assert ws not in mgr.active


def test_ws_manager_disconnect_absent():
    from backend.api.agents import WebSocketManager
    mgr = WebSocketManager()
    ws = MagicMock()
    mgr.disconnect(ws)  # Should not raise


@pytest.mark.asyncio
async def test_ws_manager_broadcast_all():
    from backend.api.agents import WebSocketManager
    mgr = WebSocketManager()
    ws1 = AsyncMock()
    ws2 = AsyncMock()
    mgr.active = [ws1, ws2]
    await mgr.broadcast("ping")
    ws1.send_text.assert_called_once_with("ping")
    ws2.send_text.assert_called_once_with("ping")


@pytest.mark.asyncio
async def test_ws_manager_broadcast_removes_dead():
    from backend.api.agents import WebSocketManager
    mgr = WebSocketManager()
    dead = AsyncMock()
    dead.send_text.side_effect = Exception("gone")
    alive = AsyncMock()
    mgr.active = [dead, alive]
    await mgr.broadcast("msg")
    assert dead not in mgr.active
    assert alive in mgr.active


# ---------------------------------------------------------------------------
# backend/api/voice.py — upload endpoint
# ---------------------------------------------------------------------------

def test_voice_upload_invalid_format(nexus_client, auth_headers):
    resp = nexus_client.post(
        "/api/voice/upload",
        files={"file": ("recording.txt", b"data", "text/plain")},
        headers=auth_headers,
    )
    assert resp.status_code == 400


def test_voice_upload_wav_success(nexus_client, auth_headers):
    with patch("backend.agents.voice.process_audio", new_callable=AsyncMock) as mock_proc:
        mock_proc.return_value = {"transcript": "hello world", "intent": "QUERY"}
        resp = nexus_client.post(
            "/api/voice/upload",
            files={"file": ("clip.wav", b"RIFF....WAV", "audio/wav")},
            headers=auth_headers,
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["transcript"] == "hello world"


def test_voice_upload_mp3_success(nexus_client, auth_headers):
    with patch("backend.agents.voice.process_audio", new_callable=AsyncMock) as mock_proc:
        mock_proc.return_value = {"transcript": "test", "intent": "NOTE"}
        resp = nexus_client.post(
            "/api/voice/upload",
            files={"file": ("note.mp3", b"ID3....", "audio/mpeg")},
            headers=auth_headers,
        )
    assert resp.status_code == 200


def test_voice_upload_requires_auth(nexus_client):
    resp = nexus_client.post(
        "/api/voice/upload",
        files={"file": ("clip.wav", b"data", "audio/wav")},
    )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# backend/api/channels.py — trigger_recording
# ---------------------------------------------------------------------------

def test_channels_trigger_recording_success(nexus_client, auth_headers):
    with patch("backend.integrations.channels_dvr.trigger_recording", new_callable=AsyncMock) as mock_rec:
        mock_rec.return_value = {"ok": True, "program_id": "p1"}
        resp = nexus_client.post(
            "/api/channels/record",
            json={"program_id": "p1"},
            headers=auth_headers,
        )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


def test_channels_trigger_recording_no_program_id(nexus_client, auth_headers):
    resp = nexus_client.post(
        "/api/channels/record",
        json={},
        headers=auth_headers,
    )
    assert resp.status_code == 400


def test_channels_trigger_recording_not_found(nexus_client, auth_headers):
    with patch("backend.integrations.channels_dvr.trigger_recording", new_callable=AsyncMock) as mock_rec:
        mock_rec.side_effect = ValueError("Program not found")
        resp = nexus_client.post(
            "/api/channels/record",
            json={"program_id": "bad-id"},
            headers=auth_headers,
        )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# backend/agents/voice.py — process_audio dispatch
#
# process_audio() opens/closes a real AgentTrace row (see test_voice_trace.py)
# via backend.database.engine -- these dispatch tests must swap in the same
# throwaway in-memory engine, or they write straight into the live nexus.db.
# ---------------------------------------------------------------------------

@pytest.fixture
def _voice_trace_engine(monkeypatch):
    from sqlmodel import SQLModel, create_engine
    from sqlmodel.pool import StaticPool
    import backend.database  # noqa: F401 -- registers all tables on metadata

    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(eng)
    monkeypatch.setattr("backend.database.engine", eng)
    return eng


@pytest.mark.asyncio
async def test_voice_process_audio_query(_voice_trace_engine):
    from backend.agents.voice import process_audio
    with patch("backend.agents.voice.transcribe", new_callable=AsyncMock, return_value="what time is it"), \
         patch("backend.agents.voice.route_intent", new_callable=AsyncMock, return_value={
             "intent": "QUERY", "confidence": 0.95,
             "extracted_action": "what time is it", "parameters": {}
         }), \
         patch("backend.agents.router.sonnet", new_callable=AsyncMock, return_value="It is noon."):
        result = await process_audio("/fake/audio.wav")
    assert result["intent"] == "QUERY"
    assert result["response"] == "It is noon."
    assert result["transcript"] == "what time is it"


@pytest.mark.asyncio
async def test_voice_process_audio_briefing(_voice_trace_engine):
    from backend.agents.voice import process_audio
    with patch("backend.agents.voice.transcribe", new_callable=AsyncMock, return_value="give me a briefing"), \
         patch("backend.agents.voice.route_intent", new_callable=AsyncMock, return_value={
             "intent": "BRIEFING", "confidence": 0.9,
             "extracted_action": "give me a briefing", "parameters": {}
         }), \
         patch("backend.agents.briefing.run_briefing", new_callable=AsyncMock, return_value="Morning briefing text"):
        result = await process_audio("/fake/audio.wav")
    assert result["intent"] == "BRIEFING"
    assert "Morning briefing text" in result["response"]


@pytest.mark.asyncio
async def test_voice_process_audio_home_control(_voice_trace_engine):
    from backend.agents.voice import process_audio
    with patch("backend.agents.voice.transcribe", new_callable=AsyncMock, return_value="turn on living room lights"), \
         patch("backend.agents.voice.route_intent", new_callable=AsyncMock, return_value={
             "intent": "HOME_CONTROL", "confidence": 0.88,
             "extracted_action": "turn on lights",
             "parameters": {"domain": "light", "service": "turn_on", "data": {}}
         }), \
         patch("backend.integrations.homeassistant.call_service", new_callable=AsyncMock, return_value={"result": "ok"}):
        result = await process_audio("/fake/audio.wav")
    assert result["intent"] == "HOME_CONTROL"
    assert "Home Assistant" in result["response"]


@pytest.mark.asyncio
async def test_voice_process_audio_note(_voice_trace_engine):
    from backend.agents.voice import process_audio
    with patch("backend.agents.voice.transcribe", new_callable=AsyncMock, return_value="remember to buy milk"), \
         patch("backend.agents.voice.route_intent", new_callable=AsyncMock, return_value={
             "intent": "NOTE", "confidence": 0.85,
             "extracted_action": "remember to buy milk", "parameters": {}
         }), \
         patch("backend.integrations.obsidian.create_note", new_callable=AsyncMock, return_value="NEXUS/Voice Notes/note.md"):
        result = await process_audio("/fake/audio.wav")
    assert result["intent"] == "NOTE"
    assert "NEXUS/Voice Notes/note.md" in result["response"]


@pytest.mark.asyncio
async def test_voice_process_audio_task(_voice_trace_engine):
    from backend.agents.voice import process_audio
    from backend.agents.orchestrator import TaskResult
    with patch("backend.agents.voice.transcribe", new_callable=AsyncMock, return_value="summarize my emails"), \
         patch("backend.agents.voice.route_intent", new_callable=AsyncMock, return_value={
             "intent": "TASK", "confidence": 0.92,
             "extracted_action": "summarize my emails", "parameters": {}
         }), \
         patch("backend.agents.orchestrator.run_task", new_callable=AsyncMock,
               return_value=TaskResult(success=True, output=["summary done"])):
        result = await process_audio("/fake/audio.wav")
    assert result["intent"] == "TASK"
    assert result["response"] == "Task complete"
    assert result["task_result"]["success"] is True


@pytest.mark.asyncio
async def test_voice_route_intent_parses_json():
    from backend.agents.voice import route_intent
    raw_response = '{"intent": "QUERY", "confidence": 0.9, "extracted_action": "test", "parameters": {}}'
    with patch("backend.agents.router.haiku", new_callable=AsyncMock, return_value=raw_response):
        result = await route_intent("test query")
    assert result["intent"] == "QUERY"
    assert result["confidence"] == 0.9


@pytest.mark.asyncio
async def test_voice_transcribe_whisper_api():
    import sys
    from backend.agents.voice import transcribe

    mock_openai_client = MagicMock()
    mock_openai_client.audio.transcriptions.create.return_value = MagicMock(text="transcribed text")

    mock_openai_module = MagicMock()
    mock_openai_module.OpenAI.return_value = mock_openai_client

    mock_settings = MagicMock()
    mock_settings.whisper_api = True

    mock_file = MagicMock()
    mock_file.__enter__ = MagicMock(return_value=mock_file)
    mock_file.__exit__ = MagicMock(return_value=False)

    with patch.dict("sys.modules", {"openai": mock_openai_module}), \
         patch("backend.config.get_settings", return_value=mock_settings), \
         patch("builtins.open", return_value=mock_file):
        result = await transcribe("/fake/audio.wav")
    assert result == "transcribed text"


# ---------------------------------------------------------------------------
# backend/agents/wiki_ingest.py — anti-fragmentation helpers
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("stem,expected", [
    ("2026-07-01", True),                # bare date — the observed bug
    ("2026-06-25b", True),                # date + session-style letter suffix
    ("Morning-Briefing-2026-06-28", True),  # explicit "briefing" in the name
    ("daily-ops-log", False),             # "daily" in the name but no date/event-hermes- prefix — no longer hijacked (2026-07-25 hardening)
    ("the-manual", False),                # genuine reference doc, unaffected
    ("2026-07-01-quarterly-report", False),  # date PLUS real content — not a bare daily note
])
def test_wiki_is_daily_note(stem, expected):
    from backend.agents.wiki_ingest import _is_daily_note
    assert _is_daily_note(stem) is expected
