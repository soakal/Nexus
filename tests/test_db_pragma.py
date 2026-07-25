import pytest
from sqlalchemy import text


@pytest.fixture
def file_db(tmp_path, monkeypatch):
    """A fresh on-disk SQLite engine wired into backend.database, with the
    connect-event pragma handler attached (mirrors the production engine)."""
    import backend.database as bd

    db_path = tmp_path / "nexus.db"
    monkeypatch.setattr(bd, "DB_PATH", db_path)

    from sqlalchemy import event
    from sqlmodel import create_engine

    eng = create_engine(
        f"sqlite:///{db_path}",
        echo=False,
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    event.listen(eng, "connect", bd._set_sqlite_pragmas)
    monkeypatch.setattr(bd, "engine", eng)
    return bd, eng


def test_wal_and_busy_timeout_on_file_engine(file_db):
    bd, eng = file_db
    from sqlmodel import SQLModel

    SQLModel.metadata.create_all(eng)

    with eng.connect() as conn:
        journal_mode = conn.execute(text("PRAGMA journal_mode")).scalar()
        busy_timeout = conn.execute(text("PRAGMA busy_timeout")).scalar()

    assert str(journal_mode).lower() == "wal"
    assert busy_timeout == 30000


def test_ensure_task_columns_idempotent(file_db, monkeypatch):
    """Creating the task table WITHOUT cancel_requested, then running
    _ensure_task_columns twice, adds the column exactly once and never errors."""
    bd, eng = file_db

    # Build a legacy `task` table that lacks cancel_requested.
    with eng.begin() as conn:
        conn.execute(text(
            "CREATE TABLE task ("
            "id INTEGER PRIMARY KEY, prompt TEXT, status TEXT, "
            "plan_json TEXT, result_json TEXT, model_used TEXT, "
            "steps_taken INTEGER, created_at TEXT, updated_at TEXT)"
        ))

    def cols():
        with eng.connect() as conn:
            return {row[1] for row in conn.execute(text("PRAGMA table_info(task)"))}

    assert "cancel_requested" not in cols()

    bd._ensure_task_columns()
    assert "cancel_requested" in cols()

    # Second run must be a harmless no-op (no duplicate-column error).
    bd._ensure_task_columns()
    assert "cancel_requested" in cols()


def test_ensure_system_state_columns_adds_auth_burst_columns(file_db):
    """Creating systemstate WITHOUT the 401-burst columns, then running
    _ensure_system_state_columns twice, adds them exactly once and never errors."""
    bd, eng = file_db

    with eng.begin() as conn:
        conn.execute(text(
            "CREATE TABLE systemstate ("
            "id INTEGER PRIMARY KEY, autonomy_enabled BOOLEAN, "
            "daily_budget_usd REAL, per_task_budget_usd REAL, updated_at TEXT)"
        ))

    def cols():
        with eng.connect() as conn:
            return {row[1] for row in conn.execute(text("PRAGMA table_info(systemstate)"))}

    assert "auth_burst_alert_sources" not in cols()
    assert "auth_burst_alert_at" not in cols()

    bd._ensure_system_state_columns()
    assert "auth_burst_alert_sources" in cols()
    assert "auth_burst_alert_at" in cols()

    # Second run must be a harmless no-op (no duplicate-column error).
    bd._ensure_system_state_columns()
    assert "auth_burst_alert_sources" in cols()
    assert "auth_burst_alert_at" in cols()
