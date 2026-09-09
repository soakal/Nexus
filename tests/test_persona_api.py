"""Persona API tests — voice-profile passthrough + auth."""

from unittest.mock import patch, AsyncMock

import pytest
from sqlalchemy import create_engine
from sqlmodel import SQLModel

AUTH = {"Authorization": "Bearer test-nexus-key"}


@pytest.fixture
def persona_client(tmp_path, monkeypatch):
    """TestClient fixture, same boot-patch stack as test_facts_audit's
    facts_client — this router never touches the DB directly (it delegates
    to mail_drafts.get_voice_profile()) but the app still needs to boot."""
    (tmp_path / ".vault.key").write_bytes(b"A" * 32)
    (tmp_path / "nexus.vault").write_text("{}")
    monkeypatch.chdir(tmp_path)

    test_engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=__import__("sqlalchemy.pool", fromlist=["StaticPool"]).StaticPool,
    )
    SQLModel.metadata.create_all(test_engine)
    monkeypatch.setattr("backend.database.engine", test_engine)

    with patch("backend.database.create_db_and_tables"), \
         patch("backend.scheduler.setup_scheduler"), \
         patch("backend.scheduler.scheduler") as sched, \
         patch("backend.agents.memo_watcher.start_watcher_blocking"), \
         patch("backend.agents.memo_watcher.stop_watcher", new_callable=AsyncMock), \
         patch("backend.state_workers.prime_state_workers", new_callable=AsyncMock):
        sched.running = False
        from fastapi.testclient import TestClient
        from backend.main import app
        with TestClient(app) as c:
            yield c


def test_requires_auth(persona_client):
    assert persona_client.get("/api/persona/voice-profile").status_code in (401, 403)


def test_voice_profile_passes_through(persona_client):
    with patch("backend.agents.mail_drafts.get_voice_profile", new_callable=AsyncMock,
               return_value="Casual, short sentences, signs off 'thanks, B'."):
        resp = persona_client.get("/api/persona/voice-profile", headers=AUTH)
    assert resp.status_code == 200
    assert resp.json() == {"voice": "Casual, short sentences, signs off 'thanks, B'."}
