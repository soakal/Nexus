import logging
import pathlib
from datetime import datetime

from sqlalchemy import event, text
from sqlmodel import Field, Session, SQLModel, create_engine

logger = logging.getLogger(__name__)

DB_PATH = pathlib.Path("nexus.db")
DATABASE_URL = f"sqlite:///{DB_PATH}"

engine = create_engine(
    DATABASE_URL,
    echo=False,
    connect_args={"check_same_thread": False, "timeout": 30},
)


@event.listens_for(engine, "connect")
def _set_sqlite_pragmas(dbapi_connection, connection_record):
    """Apply WAL + busy-timeout pragmas on every new connection.

    Harmless on :memory:/StaticPool test engines — WAL silently stays in
    'memory' mode there. We never assert the result, just execute.
    """
    try:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.close()
    except Exception as e:  # pragma: no cover - defensive
        logger.warning(f"Failed to set SQLite pragmas: {e}")


class Task(SQLModel, table=True):
    model_config = {"protected_namespaces": ()}

    id: int | None = Field(default=None, primary_key=True)
    prompt: str
    status: str = "pending"  # pending | running | success | failed | stopped
    plan_json: str | None = None
    result_json: str | None = None
    model_used: str = "sonnet"
    steps_taken: int = 0
    cancel_requested: bool = False
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class TaskStep(SQLModel, table=True):
    model_config = {"protected_namespaces": ()}

    id: int | None = Field(default=None, primary_key=True)
    task_id: int = Field(index=True)
    step_index: int  # 1-based
    prompt: str
    description: str = ""
    status: str = "pending"  # pending | running | done | failed
    output_json: str | None = None
    attempts: int = 0
    idempotency_key: str = ""
    heartbeat_at: datetime | None = None
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


# Opus verifier outcome — one row per durable task, written after all steps
# finish (before the final success/failure status is committed). The verdict
# is the honest success gate: a confident "failure" can flip an otherwise-done
# task to "failed"; success/partial/uncertain always finalizes "success".
# Created by create_all (new table, no _ensure_ migration shim needed).
class TaskOutcome(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    task_id: int = Field(index=True)
    verdict: str          # "success" | "failure" | "partial" | "uncertain"
    confidence: float = 0.0
    reason: str = ""
    grounded: bool = False   # True if a real read-only tool-read backed the verdict
    evidence: str | None = None
    model: str = "opus"
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
    summary: str | None = None
    summarized_through_id: int | None = None


class ChatMessage(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    conversation_id: int = Field(index=True)
    role: str  # user | assistant
    content: str
    created_at: datetime = Field(default_factory=datetime.utcnow)


# Durable entity/fact store (Tier 2.3c). Facts are extracted from chat,
# stored with a confidence that decays with age, can be SUPERSEDED when a
# newer value contradicts an older one, and the most relevant active facts
# are injected into the chat memory block. Created by create_all (new table,
# no _ensure_ migration shim needed).
class Fact(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    subject: str = Field(index=True)        # e.g. "user", "unraid", "garage"
    predicate: str = Field(index=True)      # e.g. "prefers", "named", "located_at"
    value: str                              # the fact value
    confidence: float = 0.6                 # 0..1 at write time
    source: str = "chat"                    # chat | manual | extracted
    conversation_id: int | None = None
    superseded_by: int | None = None        # id of the Fact that replaced this; None = active
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    last_seen_at: datetime = Field(default_factory=datetime.utcnow)
    dismissed_at: datetime | None = None    # set by soft-dismiss; excluded from recall/audit


# Immutable audit log of every side-effecting action that passed through the
# policy-gated action broker (backend/safety/broker.py). App code only INSERTs a
# row (the intent/gate decision) then UPDATEs it with the dispatch outcome — it
# NEVER deletes an ActionLog row (immutable by convention).
class ActionLog(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    actor: str               # user | agent | autonomous
    kind: str                # ha_service | hermes_relay | ...
    target: str
    payload_json: str
    risk: str                # low | medium | high | unclassifiable
    reversibility: str       # reversible | reversible_by_inverse | irreversible | unknown
    decision: str            # allowed | needs_confirm | forbidden | executed | failed (FINAL state)
    result_json: str | None = None
    idempotency_key: str | None = Field(default=None, index=True)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


# Per-call cost/usage ledger written best-effort by the agent router
# (backend/agents/router.py::_record_spend). One row per billed LLM call. The
# cost governor (backend/safety/governor.py) sums cost_usd over time windows to
# enforce daily / per-task budgets. Created by create_all (new table, no shim).
class SpendLog(SQLModel, table=True):
    model_config = {"protected_namespaces": ()}

    id: int | None = Field(default=None, primary_key=True)
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    cost_usd: float = 0.0
    label: str = ""
    task_id: int | None = Field(default=None, index=True)
    created_at: datetime = Field(default_factory=datetime.utcnow, index=True)


# Single-row runtime control table (id=1) for the global kill switch + budgets.
# Seeded idempotently by _ensure_system_state(). The governor reads/writes row 1.
class SystemState(SQLModel, table=True):
    model_config = {"protected_namespaces": ()}

    id: int | None = Field(default=None, primary_key=True)
    autonomy_enabled: bool = True
    daily_budget_usd: float = 25.0
    per_task_budget_usd: float = 5.0
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class Goal(SQLModel, table=True):
    """Durable objective with a propose → approve → running → completed|failed|abandoned
    state machine.  Humans propose and approve via the /api/goals router; on approval a
    durable Task is dispatched.  Never auto-initiates — autonomy substrate only."""

    id: int | None = Field(default=None, primary_key=True)
    actor: str = "user"                 # user | agent | autonomous
    title: str
    description: str                    # becomes the durable Task prompt on approve
    status: str = "proposed"           # proposed|approved|running|completed|failed|abandoned
    confidence: float = 0.6
    risk: str = "medium"               # low|medium|high|unclassifiable
    reversibility: str = "unknown"     # reversible|reversible_by_inverse|irreversible|unknown
    fingerprint: str = Field(default="", index=True)
    attempts: int = 0
    backoff_until: datetime | None = None
    task_id: int | None = None
    proposal_at: datetime = Field(default_factory=datetime.utcnow)
    approved_by: str | None = None
    approved_at: datetime | None = None
    expires_at: datetime | None = None
    rejection_reason: str | None = None
    # Recurring-goal fields (cadence + category + success_criteria + next_eval_at).
    # cadence=None means one-shot; "daily"|"weekly"|"monthly" enables recurrence.
    cadence: str | None = None
    category: str | None = None
    success_criteria: str | None = None
    next_eval_at: datetime | None = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


def _safe_add_column(table: str, column: str, ddl_type: str) -> None:
    """Idempotently + race-safely add one column.

    A concurrent boot that already added it ('duplicate column name') is treated
    as success, not failure. Non-duplicate errors are logged as warnings and do
    NOT propagate — each column is independent; one failure must never abort
    sibling columns in the same _ensure_* call.
    """
    try:
        with engine.connect() as conn:
            cols = {row[1] for row in conn.execute(text(f"PRAGMA table_info({table})"))}
            if column in cols:
                return
            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type}"))
            conn.commit()
    except Exception as e:
        if "duplicate column" in str(e).lower():
            return  # a racing boot added it first — fine, idempotent
        logger.warning(f"_safe_add_column {table}.{column} failed: {e}")


def _ensure_task_columns():
    """Idempotently add columns introduced after the original `task` table shipped.

    Each column is added independently via _safe_add_column so a race on one
    column never aborts the others. No-op on a fresh DB (create_all already made
    the column) and on test :memory: engines.
    """
    _safe_add_column("task", "cancel_requested", "BOOLEAN DEFAULT 0")


def _ensure_spendlog_columns():
    """Idempotently add columns introduced after the original `spendlog` table shipped.

    Each column is added independently via _safe_add_column so a race on one
    column never aborts the others. Best-effort — a failure here is logged but
    never fatal to startup. No-op on a fresh DB (create_all already made the
    column) and on test :memory: engines.
    """
    _safe_add_column("spendlog", "task_id", "INTEGER")
    try:
        with engine.connect() as conn:
            conn.execute(
                text("CREATE INDEX IF NOT EXISTS ix_spendlog_task_id ON spendlog(task_id)")
            )
            conn.commit()
    except Exception as e:  # pragma: no cover - defensive
        logger.warning(f"_ensure_spendlog_columns index create failed: {e}")


def _ensure_conversation_columns():
    """Idempotently add columns introduced after the original `conversation` table shipped.

    Each column is added independently via _safe_add_column so a race on one
    column never aborts the others. Best-effort — a failure here is logged but
    never fatal to startup. No-op on a fresh DB (create_all already made the
    column) and on test :memory: engines.
    """
    _safe_add_column("conversation", "summary", "TEXT")
    _safe_add_column("conversation", "summarized_through_id", "INTEGER")


def _ensure_goal_columns():
    """Idempotently add columns introduced after the original `goal` table shipped.

    Each column is added independently via _safe_add_column so a race on one
    column never aborts the others. Best-effort — a failure here is logged but
    never fatal to startup. No-op on a fresh DB (create_all already made the
    column) and on test :memory: engines.
    """
    _safe_add_column("goal", "rejection_reason", "TEXT")


def _ensure_goal_recurrence_columns():
    """Idempotently add the four recurring-goal columns introduced in Tier 3 (council w33gixx93).

    Separate from _ensure_goal_columns so existing deployments get an additive-only
    migration — no mutation of the original shim. Each column is independent so a
    failure on one never aborts the others. Best-effort — never fatal to startup.
    No-op on a fresh DB (create_all already made the columns) and on :memory: engines.
    """
    _safe_add_column("goal", "cadence", "TEXT")
    _safe_add_column("goal", "category", "TEXT")
    _safe_add_column("goal", "success_criteria", "TEXT")
    _safe_add_column("goal", "next_eval_at", "TIMESTAMP")


def _ensure_fact_columns():
    """Idempotently add columns introduced after the original `fact` table shipped.

    Each column is added independently via _safe_add_column so a race on one
    column never aborts the others. Best-effort — a failure here is logged but
    never fatal to startup. No-op on a fresh DB (create_all already made the
    column) and on test :memory: engines.
    """
    _safe_add_column("fact", "dismissed_at", "TIMESTAMP")


def _ensure_system_state():
    """Idempotently seed the single SystemState row (id=1).

    No-op if the row already exists. Defaults come from Settings (.env-overridable)
    with literal fallbacks if Settings can't be read. Defensive: a failure here is
    logged but never fatal to startup. Tolerates a racing duplicate-id=1 insert
    (IntegrityError → rollback and continue — the other boot's row is fine).
    """
    try:
        from backend.config import get_settings

        try:
            s = get_settings()
            autonomy = bool(getattr(s, "autonomy_enabled_default", True))
            daily = float(getattr(s, "daily_budget_usd", 25.0))
            per_task = float(getattr(s, "per_task_budget_usd", 5.0))
        except Exception:
            autonomy, daily, per_task = True, 25.0, 5.0

        with Session(engine) as session:
            if session.get(SystemState, 1) is None:
                try:
                    from sqlalchemy.exc import IntegrityError
                    session.add(SystemState(
                        id=1,
                        autonomy_enabled=autonomy,
                        daily_budget_usd=daily,
                        per_task_budget_usd=per_task,
                    ))
                    session.commit()
                except IntegrityError:
                    # A racing boot inserted id=1 first — that row is fine.
                    session.rollback()
    except Exception as e:  # pragma: no cover - defensive
        logger.warning(f"_ensure_system_state failed: {e}")


def create_db_and_tables():
    SQLModel.metadata.create_all(engine)
    _ensure_task_columns()
    _ensure_spendlog_columns()
    _ensure_conversation_columns()
    _ensure_goal_columns()
    _ensure_goal_recurrence_columns()
    _ensure_fact_columns()
    _ensure_system_state()


def get_session():
    with Session(engine) as session:
        yield session
