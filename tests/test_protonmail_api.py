"""Tests for POST /api/protonmail/send + GET /api/protonmail/status (Task 5)."""

import pytest
from unittest.mock import AsyncMock, patch
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool
from fastapi.testclient import TestClient

import backend.database  # noqa: F401 — registers all tables on metadata


def make_test_engine():
    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(eng)
    return eng


def _seed_state(eng, autonomy: bool = True):
    from backend.database import SystemState
    with Session(eng) as s:
        row = s.get(SystemState, 1)
        if row is None:
            row = SystemState(id=1)
            s.add(row)
        row.autonomy_enabled = autonomy
        row.daily_budget_usd = 25.0
        row.per_task_budget_usd = 5.0
        s.commit()


def _all_logs(eng):
    from backend.database import ActionLog
    with Session(eng) as s:
        return s.exec(select(ActionLog).order_by(ActionLog.created_at)).all()


@pytest.fixture
def pm_client(tmp_path, monkeypatch):
    vault_key = tmp_path / ".vault.key"
    vault_file = tmp_path / "nexus.vault"
    vault_key.write_bytes(b"A" * 32)
    vault_file.write_text("{}")
    monkeypatch.chdir(tmp_path)

    test_engine = make_test_engine()
    monkeypatch.setattr("backend.database.engine", test_engine)

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
            yield c, test_engine
        app.dependency_overrides.clear()


def test_send_success(pm_client, auth_headers):
    client, eng = pm_client
    _seed_state(eng, autonomy=True)

    with patch(
        "backend.integrations.protonmail.send_email",
        new_callable=AsyncMock,
        return_value={"ok": True, "detail": "sent"},
    ) as se:
        resp = client.post(
            "/api/protonmail/send",
            json={"recipients": ["a@example.com"], "subject": "hi", "body": "hello"},
            headers=auth_headers,
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    se.assert_awaited_once()

    logs = _all_logs(eng)
    assert len(logs) == 1
    assert logs[0].actor == "user"
    assert logs[0].kind == "protonmail_send"
    assert logs[0].decision == "executed"


def test_send_validation_missing_fields(pm_client, auth_headers):
    client, _eng = pm_client
    with patch(
        "backend.integrations.protonmail.send_email", new_callable=AsyncMock
    ) as se:
        resp = client.post(
            "/api/protonmail/send",
            json={"recipients": [], "subject": "", "body": ""},
            headers=auth_headers,
        )
    assert resp.status_code == 422
    se.assert_not_awaited()


def test_send_dispatch_failure_returns_502(pm_client, auth_headers):
    client, eng = pm_client
    _seed_state(eng, autonomy=True)

    from backend.integrations.protonmail import IntegrationError

    with patch(
        "backend.integrations.protonmail.send_email",
        new_callable=AsyncMock,
        side_effect=IntegrationError("mailbox down"),
    ):
        resp = client.post(
            "/api/protonmail/send",
            json={"recipients": ["a@example.com"], "subject": "hi", "body": "hello"},
            headers=auth_headers,
        )
    assert resp.status_code == 502


def test_send_unauthorized(pm_client):
    client, _eng = pm_client
    resp = client.post(
        "/api/protonmail/send",
        json={"recipients": ["a@example.com"], "subject": "hi", "body": "hello"},
    )
    assert resp.status_code == 401


def test_status_endpoint(pm_client, auth_headers):
    client, _eng = pm_client
    with patch(
        "backend.integrations.protonmail.health_check",
        new_callable=AsyncMock,
        return_value=True,
    ):
        resp = client.get("/api/protonmail/status", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json() == {"reachable": True}


def test_status_unauthorized(pm_client):
    client, _eng = pm_client
    resp = client.get("/api/protonmail/status")
    assert resp.status_code == 401


_LIST_JSON = '{"emails": [{"email_id": "1", "subject": "Hi", "sender": "a@b.com", "date": "2026-07-23T00:00:00Z"}], "total": 12}'
_UNREAD_JSON = '{"emails": [], "total": 3}'


def test_inbox_happy_path(pm_client, auth_headers):
    client, _eng = pm_client
    with patch(
        "backend.integrations.protonmail.list_recent",
        new_callable=AsyncMock,
        side_effect=[_LIST_JSON, _UNREAD_JSON],
    ):
        resp = client.get("/api/protonmail/inbox", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 12
    assert body["unread"] == 3
    assert body["emails"] == [
        {"email_id": "1", "subject": "Hi", "sender": "a@b.com", "date": "2026-07-23T00:00:00Z"}
    ]


def test_inbox_integration_error_502(pm_client, auth_headers):
    client, _eng = pm_client
    from backend.integrations.protonmail import IntegrationError
    with patch(
        "backend.integrations.protonmail.list_recent",
        new_callable=AsyncMock,
        side_effect=IntegrationError("down"),
    ):
        resp = client.get("/api/protonmail/inbox", headers=auth_headers)
    assert resp.status_code == 502


def test_inbox_malformed_json_502(pm_client, auth_headers):
    client, _eng = pm_client
    with patch(
        "backend.integrations.protonmail.list_recent",
        new_callable=AsyncMock,
        side_effect=["not json", "not json"],
    ):
        resp = client.get("/api/protonmail/inbox", headers=auth_headers)
    assert resp.status_code == 502


def test_inbox_cached_within_ttl(pm_client, auth_headers):
    client, _eng = pm_client
    with patch(
        "backend.integrations.protonmail.list_recent",
        new_callable=AsyncMock,
        side_effect=[_LIST_JSON, _UNREAD_JSON],
    ) as mock_list:
        resp1 = client.get("/api/protonmail/inbox", headers=auth_headers)
        resp2 = client.get("/api/protonmail/inbox", headers=auth_headers)
    assert resp1.status_code == 200
    assert resp2.status_code == 200
    assert resp1.json() == resp2.json()
    assert mock_list.await_count == 2  # one gather (2 calls) total, second request hit cache


def test_inbox_unauthorized(pm_client):
    client, _eng = pm_client
    resp = client.get("/api/protonmail/inbox")
    assert resp.status_code == 401


_EMAIL_JSON = '{"emails": [{"email_id": "1", "subject": "Hi", "sender": "a@b.com", "date": "2026-07-23T00:00:00Z", "body": "Full body text."}]}'


def test_email_preview_success(pm_client, auth_headers):
    client, _eng = pm_client
    with patch(
        "backend.integrations.protonmail.read_email",
        new_callable=AsyncMock,
        return_value=_EMAIL_JSON,
    ) as mock_read:
        resp = client.get("/api/protonmail/email/1?mailbox=Drafts&page=2", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["email_id"] == "1"
    assert body["body"] == "Full body text."
    mock_read.assert_awaited_once_with("1", page=2, mailbox="Drafts")


def test_email_preview_defaults_omit_mailbox(pm_client, auth_headers):
    client, _eng = pm_client
    with patch(
        "backend.integrations.protonmail.read_email",
        new_callable=AsyncMock,
        return_value=_EMAIL_JSON,
    ) as mock_read:
        client.get("/api/protonmail/email/1", headers=auth_headers)
    mock_read.assert_awaited_once_with("1", page=1, mailbox=None)


def test_email_preview_not_found_404(pm_client, auth_headers):
    client, _eng = pm_client
    with patch(
        "backend.integrations.protonmail.read_email",
        new_callable=AsyncMock,
        return_value='{"emails": []}',
    ):
        resp = client.get("/api/protonmail/email/999", headers=auth_headers)
    assert resp.status_code == 404


def test_email_preview_integration_error_502(pm_client, auth_headers):
    client, _eng = pm_client
    from backend.integrations.protonmail import IntegrationError
    with patch(
        "backend.integrations.protonmail.read_email",
        new_callable=AsyncMock,
        side_effect=IntegrationError("down"),
    ):
        resp = client.get("/api/protonmail/email/1", headers=auth_headers)
    assert resp.status_code == 502


def test_email_preview_unauthorized(pm_client):
    client, _eng = pm_client
    resp = client.get("/api/protonmail/email/1")
    assert resp.status_code == 401


def test_archive_success(pm_client, auth_headers):
    client, eng = pm_client
    _seed_state(eng, autonomy=True)
    with patch(
        "backend.integrations.protonmail.archive_email",
        new_callable=AsyncMock,
        return_value={"ok": True, "detail": "archived"},
    ) as ae:
        resp = client.post(
            "/api/protonmail/archive", json={"email_id": "1"}, headers=auth_headers
        )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    ae.assert_awaited_once()
    logs = _all_logs(eng)
    assert len(logs) == 1
    assert logs[0].kind == "protonmail_archive"
    assert logs[0].decision == "executed"


def test_archive_blank_email_id_422(pm_client, auth_headers):
    client, _eng = pm_client
    with patch("backend.integrations.protonmail.archive_email", new_callable=AsyncMock) as ae:
        resp = client.post("/api/protonmail/archive", json={"email_id": ""}, headers=auth_headers)
    assert resp.status_code == 422
    ae.assert_not_awaited()


def test_archive_dispatch_failure_502(pm_client, auth_headers):
    client, eng = pm_client
    _seed_state(eng, autonomy=True)
    from backend.integrations.protonmail import IntegrationError
    with patch(
        "backend.integrations.protonmail.archive_email",
        new_callable=AsyncMock,
        side_effect=IntegrationError("down"),
    ):
        resp = client.post("/api/protonmail/archive", json={"email_id": "1"}, headers=auth_headers)
    assert resp.status_code == 502


def test_archive_invalidates_inbox_cache(pm_client, auth_headers):
    client, eng = pm_client
    _seed_state(eng, autonomy=True)
    with patch(
        "backend.integrations.protonmail.list_recent",
        new_callable=AsyncMock,
        side_effect=[_LIST_JSON, _UNREAD_JSON, _LIST_JSON, _UNREAD_JSON],
    ) as mock_list, \
         patch(
        "backend.integrations.protonmail.archive_email",
        new_callable=AsyncMock,
        return_value={"ok": True, "detail": "archived"},
    ):
        client.get("/api/protonmail/inbox", headers=auth_headers)
        client.post("/api/protonmail/archive", json={"email_id": "1"}, headers=auth_headers)
        client.get("/api/protonmail/inbox", headers=auth_headers)
    # 2 gather calls per inbox fetch x 2 fetches = 4 -- cache was invalidated
    # after archive, so the second /inbox call did NOT hit the 30s cache.
    assert mock_list.await_count == 4


def test_archive_unauthorized(pm_client):
    client, _eng = pm_client
    resp = client.post("/api/protonmail/archive", json={"email_id": "1"})
    assert resp.status_code == 401


def test_delete_success(pm_client, auth_headers):
    client, eng = pm_client
    _seed_state(eng, autonomy=True)
    with patch(
        "backend.integrations.protonmail.trash_email",
        new_callable=AsyncMock,
        return_value={"ok": True, "detail": "deleted"},
    ) as de:
        resp = client.post(
            "/api/protonmail/delete", json={"email_id": "1"}, headers=auth_headers
        )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    de.assert_awaited_once()
    logs = _all_logs(eng)
    assert len(logs) == 1
    assert logs[0].kind == "protonmail_delete"
    assert logs[0].decision == "executed"


def test_delete_blank_email_id_422(pm_client, auth_headers):
    client, _eng = pm_client
    with patch("backend.integrations.protonmail.trash_email", new_callable=AsyncMock) as de:
        resp = client.post("/api/protonmail/delete", json={"email_id": ""}, headers=auth_headers)
    assert resp.status_code == 422
    de.assert_not_awaited()


def test_delete_dispatch_failure_502(pm_client, auth_headers):
    client, eng = pm_client
    _seed_state(eng, autonomy=True)
    from backend.integrations.protonmail import IntegrationError
    with patch(
        "backend.integrations.protonmail.trash_email",
        new_callable=AsyncMock,
        side_effect=IntegrationError("down"),
    ):
        resp = client.post("/api/protonmail/delete", json={"email_id": "1"}, headers=auth_headers)
    assert resp.status_code == 502


def test_delete_unauthorized(pm_client):
    client, _eng = pm_client
    resp = client.post("/api/protonmail/delete", json={"email_id": "1"})
    assert resp.status_code == 401
