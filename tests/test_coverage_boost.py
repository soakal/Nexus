"""Targeted tests to bring coverage from 72% to >=80%."""
import asyncio
import os
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
         patch("backend.agents.memo_watcher.stop_watcher", new_callable=AsyncMock):
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


# ---------------------------------------------------------------------------
# backend/integrations/openrouter.py
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_openrouter_fetch_success():
    from backend.integrations import openrouter
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = {"data": [{"id": "m1"}, {"id": "m2"}]}

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(return_value=mock_resp)

    with patch("httpx.AsyncClient", return_value=mock_client):
        data = await openrouter.fetch()
    assert data.available is True
    assert data.model_count == 2


@pytest.mark.asyncio
async def test_openrouter_health_check_true():
    from backend.integrations import openrouter
    with patch("backend.integrations.openrouter._get_data", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = openrouter.OpenRouterData(available=True, model_count=5)
        result = await openrouter.health_check()
    assert result is True


@pytest.mark.asyncio
async def test_openrouter_health_check_exception():
    from backend.integrations import openrouter
    with patch("backend.integrations.openrouter._get_data", new_callable=AsyncMock, side_effect=Exception("err")):
        result = await openrouter.health_check()
    assert result is False


@pytest.mark.asyncio
async def test_openrouter_no_api_key():
    from backend.integrations import openrouter
    with patch("backend.config.get_settings") as mock_settings:
        mock_settings.return_value.openrouter_api_key = property(
            lambda self: (_ for _ in ()).throw(KeyError("OPENROUTER_API_KEY"))
        )
        # Simpler: patch get_settings to raise on attribute access
        settings_obj = MagicMock()
        type(settings_obj).openrouter_api_key = property(
            lambda s: (_ for _ in ()).throw(KeyError("OPENROUTER_API_KEY"))
        )
        mock_settings.return_value = settings_obj
        with pytest.raises(Exception):
            await openrouter._get_data()


# ---------------------------------------------------------------------------
# backend/scheduler.py
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_scheduler_run_briefing_success():
    from backend.scheduler import _run_briefing
    with patch("backend.agents.briefing.run_briefing", new_callable=AsyncMock, return_value="briefing text"):
        await _run_briefing()  # Should not raise


@pytest.mark.asyncio
async def test_scheduler_run_briefing_exception_swallowed():
    from backend.scheduler import _run_briefing
    with patch("backend.agents.briefing.run_briefing", new_callable=AsyncMock, side_effect=Exception("boom")):
        await _run_briefing()  # Exception is caught, should not raise


@pytest.mark.asyncio
async def test_scheduler_retry_pending():
    from backend.scheduler import _retry_pending_deliveries
    with patch("backend.integrations.hermes.deliver_pending", new_callable=AsyncMock) as mock_dp:
        await _retry_pending_deliveries()
    mock_dp.assert_called_once()


@pytest.mark.asyncio
async def test_scheduler_retry_pending_exception_swallowed():
    from backend.scheduler import _retry_pending_deliveries
    with patch("backend.integrations.hermes.deliver_pending", new_callable=AsyncMock, side_effect=Exception("hermes down")):
        await _retry_pending_deliveries()  # Should not raise


def test_setup_scheduler_adds_jobs():
    from backend.scheduler import setup_scheduler, scheduler
    with patch.object(scheduler, "add_job") as mock_add:
        setup_scheduler("07:30", "America/New_York")
    assert mock_add.call_count == 18
    ids_set = set()
    for c in mock_add.call_args_list:
        ids_set.add(c.kwargs.get("id"))
    assert ids_set == {
        "morning_briefing",
        "retention_prune",
        "retry_deliveries",
        "record_uptime",
        "brain_spend_ingest",
        "record_speedtest",
        "step_watchdog",
        "goal_proposer",
        "mail_autodraft",
        "autonomy_digest",
        "db_checkpoint",
        "db_backup",
        "vault_backup",
        "watchdog",
        "spend_report",
        "goal_recurrence",
        "brain_organizer",
        "wiki_fragmentation_report",
    }


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
    ("nexus-session-2026-06-25b-ha-cover-lock-fix", "nexus ha cover lock fix"),
    ("NEXUS-save-2026-06-24", "NEXUS"),
    ("2026-06-28", ""),                       # daily file → empty topic
    ("nexus-session-20260625b-fix", "nexus fix"),  # compact date form
    ("CWI-AI-2026-06-25-redesign", "CWI AI redesign"),
    ("2026-07-07-session-bb94406a-faf4-4f0e-833a-47d1a55df36c", ""),  # UUID → empty topic
    ("nexus-save-bb94406a-faf4-4f0e-833a-47d1a55df36c-ha-fix", "nexus ha fix"),  # UUID stripped, real words kept
])
def test_wiki_filename_hint_strips_dates_and_noise(stem, expected):
    from backend.agents.wiki_ingest import _filename_hint
    assert _filename_hint(stem) == expected


def test_wiki_match_existing_page_basic():
    from backend.agents.wiki_ingest import _match_existing_page
    known = ["NEXUS", "Hermes", "AdGuard"]
    assert _match_existing_page("nexus ha cover lock fix", known) == "NEXUS"
    assert _match_existing_page("NEXUS", known) == "NEXUS"


def test_wiki_match_existing_page_hyphenated_stem():
    # Regression: a hyphenated page stem (CWI-AI) must still match a hint
    # built from a hyphenated filename ("CWI AI redesign").
    from backend.agents.wiki_ingest import _match_existing_page
    known = ["NEXUS", "CWI-AI"]
    assert _match_existing_page("CWI AI redesign", known) == "CWI-AI"


def test_wiki_match_existing_page_no_match():
    from backend.agents.wiki_ingest import _match_existing_page
    known = ["NEXUS", "Hermes"]
    assert _match_existing_page("brand new topic", known) is None
    assert _match_existing_page("", known) is None


@pytest.mark.parametrize("raw,expected", [
    ("NEXUS.md", "NEXUS"),       # no double .md
    ("wiki/Foo", "Foo"),          # path segments stripped
    ("..\\..\\evil", "evil"),    # no traversal
    ("  ", "Inbox"),
    (None, "Inbox"),
    ("none", "Inbox"),
    ("null", "Inbox"),
    ("NEXUS", "NEXUS"),
])
def test_wiki_clean_page_name(raw, expected):
    from backend.agents.wiki_ingest import _clean_page_name
    assert _clean_page_name(raw) == expected


def test_wiki_looks_like_reference_doc():
    from backend.agents.wiki_ingest import _looks_like_reference_doc
    ref = "\n".join(f"# Section {i}" for i in range(10))
    assert _looks_like_reference_doc(ref) is True
    note = "Fixed the HA cover lock.\n- did x\n- did y\n# One header"
    assert _looks_like_reference_doc(note) is False


@pytest.mark.parametrize("stem,expected", [
    ("2026-07-01", True),                # bare date — the observed bug
    ("2026-06-25b", True),                # date + session-style letter suffix
    ("Morning-Briefing-2026-06-28", True),  # explicit "briefing" in the name
    ("daily-ops-log", True),              # explicit "daily" in the name
    ("the-manual", False),                # genuine reference doc, unaffected
    ("2026-07-01-quarterly-report", False),  # date PLUS real content — not a bare daily note
])
def test_wiki_is_daily_note(stem, expected):
    from backend.agents.wiki_ingest import _is_daily_note
    assert _is_daily_note(stem) is expected


def test_wiki_is_session_file_by_name():
    from backend.agents.wiki_ingest import _is_session_file

    class _F:
        def __init__(self, stem):
            self.stem = stem
        def read_text(self, **kw):
            return ""

    assert _is_session_file(_F("2026-06-28")) is True
    assert _is_session_file(_F("nexus-session-2026-06-25b")) is True
    assert _is_session_file(_F("NEXUS")) is False
