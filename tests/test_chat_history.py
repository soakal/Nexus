from datetime import datetime, timedelta

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine


def _make_engine():
    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(eng)
    return eng


def test_db_load_history_returns_most_recent_in_chronological_order(monkeypatch):
    from backend.database import ChatMessage
    eng = _make_engine()
    base = datetime(2026, 1, 1)
    with Session(eng) as s:
        for i in range(25):
            s.add(ChatMessage(
                conversation_id=1, role="user", content=f"m{i}",
                created_at=base + timedelta(minutes=i),
            ))
        s.commit()
    monkeypatch.setattr("backend.database.engine", eng)

    from backend.agents.chat import _db_load_history
    hist = _db_load_history(1, limit=10)

    # The 10 MOST RECENT messages (m15..m24), returned oldest-first.
    assert [h["content"] for h in hist] == [f"m{i}" for i in range(15, 25)]


def test_db_load_history_shorter_than_limit_returns_all_in_order(monkeypatch):
    from backend.database import ChatMessage
    eng = _make_engine()
    base = datetime(2026, 1, 1)
    with Session(eng) as s:
        for i in range(3):
            s.add(ChatMessage(
                conversation_id=2, role="assistant", content=f"c{i}",
                created_at=base + timedelta(minutes=i),
            ))
        s.commit()
    monkeypatch.setattr("backend.database.engine", eng)

    from backend.agents.chat import _db_load_history
    hist = _db_load_history(2, limit=20)
    assert [h["content"] for h in hist] == ["c0", "c1", "c2"]
