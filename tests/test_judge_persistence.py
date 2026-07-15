"""Persistence coverage for the judge-gate ActionLog migration shim
(backend/database.py::_ensure_actionlog_columns) and the safety API's
exposure of the two judge fields (backend/api/safety.py::list_actions).

Reuses the in-memory StaticPool engine + monkeypatched backend.database.engine
fixture pattern and the safety_client/auth_headers fixtures already
established in tests/test_action_judge.py and tests/test_safety_broker.py.
"""

import json

import pytest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlmodel import Session, SQLModel, create_engine

from sqlmodel.pool import StaticPool

# Ensure all tables (incl. ActionLog) are registered on SQLModel.metadata.
import backend.database  # noqa: F401,E402
from backend.database import ActionLog, _ensure_actionlog_columns


def make_engine():
    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(eng)
    return eng


@pytest.fixture
def eng(monkeypatch):
    e = make_engine()
    monkeypatch.setattr("backend.database.engine", e)
    return e


def _actionlog_columns(eng) -> set[str]:
    with eng.connect() as conn:
        return {row[1] for row in conn.execute(text("PRAGMA table_info(actionlog)"))}


def test_ensure_actionlog_columns_idempotent(eng):
    # create_all already created judge_verdict/judge_reason on a fresh table,
    # so this exercises the "column already present" no-op branch of
    # _safe_add_column both times it's called here.
    _ensure_actionlog_columns()
    cols_after_first = _actionlog_columns(eng)
    assert "judge_verdict" in cols_after_first
    assert "judge_reason" in cols_after_first

    # Calling it again must not raise and must leave the columns intact.
    _ensure_actionlog_columns()
    cols_after_second = _actionlog_columns(eng)
    assert "judge_verdict" in cols_after_second
    assert "judge_reason" in cols_after_second
    assert cols_after_first == cols_after_second


@pytest.fixture
def safety_client(tmp_path, monkeypatch):
    vault_key = tmp_path / ".vault.key"
    vault_file = tmp_path / "nexus.vault"
    vault_key.write_bytes(b"A" * 32)
    vault_file.write_text("{}")
    monkeypatch.chdir(tmp_path)

    test_engine = make_engine()
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
        from backend.database import get_session
        from backend.main import app
        app.dependency_overrides[get_session] = override_session
        with TestClient(app) as c:
            c._engine = test_engine
            yield c
        app.dependency_overrides.clear()


def _seed_action(eng, **kw):
    defaults = dict(
        actor="agent", kind="obsidian_task", target="vault",
        payload_json=json.dumps({"note_path": "tasks.md", "task_text": "do thing"}),
        risk="low", reversibility="reversible", decision="needs_confirm",
        result_json=None, idempotency_key=None,
        judge_verdict="veto", judge_reason="test veto",
    )
    defaults.update(kw)
    with Session(eng) as s:
        row = ActionLog(**defaults)
        s.add(row)
        s.commit()
        s.refresh(row)
        return row.id


def test_list_actions_exposes_judge_fields(safety_client, auth_headers):
    eng = safety_client._engine
    _seed_action(eng, target="vault")

    resp = safety_client.get("/api/safety/actions", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert "judge_verdict" in data[0]
    assert "judge_reason" in data[0]
    assert data[0]["judge_verdict"] == "veto"
    assert data[0]["judge_reason"] == "test veto"
