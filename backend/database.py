import pathlib
from datetime import datetime

from sqlmodel import Field, Session, SQLModel, create_engine

DB_PATH = pathlib.Path("nexus.db")
DATABASE_URL = f"sqlite:///{DB_PATH}"

engine = create_engine(DATABASE_URL, echo=False)


class Task(SQLModel, table=True):
    model_config = {"protected_namespaces": ()}

    id: int | None = Field(default=None, primary_key=True)
    prompt: str
    status: str = "pending"  # pending | running | success | failed
    plan_json: str | None = None
    result_json: str | None = None
    model_used: str = "sonnet"
    steps_taken: int = 0
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class AgentRun(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    task_id: int | None = None
    agent_type: str  # orchestrator | briefing | voice | memo_watcher
    model: str
    prompt_snippet: str
    output_snippet: str
    success: bool
    duration_ms: int
    created_at: datetime = Field(default_factory=datetime.utcnow)


class Briefing(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    content: str
    context_json: str | None = None
    delivered_to_hermes: bool = False
    obsidian_path: str | None = None
    created_at: datetime = Field(default_factory=datetime.utcnow)


class TrendSnapshot(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    source: str   # unraid | channels | adguard
    metric: str   # storage_used_gb | blocked_pct
    value: float
    captured_at: datetime = Field(default_factory=datetime.utcnow)


class UptimeSample(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    source: str
    ok: bool
    latency_ms: int | None = None
    checked_at: datetime = Field(default_factory=datetime.utcnow)


class SpeedtestSample(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    download_mbps: float = 0.0
    upload_mbps: float = 0.0
    ping_ms: float = 0.0
    checked_at: datetime = Field(default_factory=datetime.utcnow)


class PendingDelivery(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    payload_json: str
    delivery_type: str  # notify | action
    attempts: int = 0
    last_attempt: datetime | None = None
    created_at: datetime = Field(default_factory=datetime.utcnow)


class MemoLog(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    filename: str
    title: str
    obsidian_path: str
    duration_s: float | None = None
    created_at: datetime = Field(default_factory=datetime.utcnow)


class KnownDevice(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    mac: str = Field(unique=True)
    first_seen: datetime = Field(default_factory=datetime.utcnow)
    last_seen: datetime = Field(default_factory=datetime.utcnow)
    hostname: str | None = None


class Conversation(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    title: str = "New conversation"
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class ChatMessage(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    conversation_id: int = Field(index=True)
    role: str  # user | assistant
    content: str
    created_at: datetime = Field(default_factory=datetime.utcnow)


def create_db_and_tables():
    SQLModel.metadata.create_all(engine)


def get_session():
    with Session(engine) as session:
        yield session
