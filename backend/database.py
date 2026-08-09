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
    delivered: bool = False
    obsidian_path: str | None = None
    created_at: datetime = Field(default_factory=datetime.utcnow)


# Retained: no longer written (Trends feature removed 2026-07-07 — Grafana/UptimeKuma
# cover this externally). Historical rows drain to empty via prune_old_trend_snapshots'
# existing retention window, then this table simply stays empty. Not dropped: SQLModel
# create_all() never removes tables, so deleting this class would do nothing on the
# live prod db while adding fresh-DB/test divergence risk for zero benefit.
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
    kind: str                # ha_service | vm_power | ...
    target: str
    payload_json: str
    risk: str                # low | medium | high | unclassifiable
    reversibility: str       # reversible | reversible_by_inverse | irreversible | unknown
    decision: str            # allowed | needs_confirm | forbidden | executed | failed (FINAL state)
    result_json: str | None = None
    idempotency_key: str | None = Field(default=None, index=True)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    # Judge-gate verdict on this action, written by the broker's veto-update path
    # after the fact. "approve" | "veto" | "error"; None until the judge runs.
    judge_verdict: str | None = None
    # Judge's rationale for the verdict, app-level capped at 300 chars at write
    # time (no DB-level length constraint — matches this file's existing
    # convention of plain TEXT columns for nullable strings).
    judge_reason: str | None = None
    # Set by broker.confirm_action at the TOP of the call — BEFORE the TTL
    # check, kill-switch re-check, and dispatch — so (confirmed_at -
    # created_at) is pure human reaction time, uncontaminated by dispatch
    # latency (median 1.9s, but 19-30s for protonmail_delete). None means this
    # row was never confirmed by a human — also what makes "allowed→executed"
    # distinguishable from "needs_confirm→confirmed→executed" (today those two
    # rows are otherwise identical). A row that stamps this and THEN hits
    # expired/forbidden still means something ("he tapped, too late") —
    # deliberately not cleared in that case.
    confirmed_at: datetime | None = Field(default=None)


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
    # Persisted alert cooldown — survives process restarts so stuck-delivery
    # Telegram alerts don't fire on every boot while the queue is stuck.
    last_dead_letter_alert_at: datetime | None = Field(default=None)
    # ISO local date (e.g. "2026-07-20") of the last budget early-warning —
    # a date string, not a timestamp, so day rollover re-arms it for free.
    last_budget_warn_day: str | None = Field(default=None)
    # Watermark for the weekly facts-digest job (backend/agents/facts_digest.py) —
    # facts with last_seen_at/created_at after this instant are "new since last digest".
    last_facts_digest_at: datetime | None = Field(default=None)
    # 401-burst alert claim state (backend/safety/governor.py::claim_auth_burst_alert).
    # JSON object mapping each currently-claimed client identity to the UTC ISO
    # timestamp of the last tick it was STILL producing failures:
    #     {"1.2.3.4": "2026-08-05T12:34:56.789012", ...}
    # Presence of a source means "already paged, stay silent" -- so a restart
    # mid-storm does not re-page -- and that source re-arms once IT ALONE has
    # been quiet for the configured window.
    #
    # Replaced the original `auth_burst_alert_sources` CSV + a single shared
    # `auth_burst_alert_at` timestamp (2026-08-05): one perpetually-storming
    # source kept refreshing the shared timestamp, so a different, already-quiet
    # source stayed claimed and its next storm was never paged; and the set
    # could only ever be cleared all-or-nothing, so it grew without bound (and
    # across restarts) for as long as any one source kept storming.
    #
    # A JSON TEXT blob rather than one column per source or a side table:
    # SystemState is a singleton control row, and `*_json` TEXT columns are
    # already this schema's idiom for structured values (Task.result_json,
    # TaskStep.output_json, StateSnapshot.payload_json).
    auth_burst_alert_json: str | None = Field(default=None)
    # Kinds promoted to auto-allow for agent/autonomous actors, CSV. ONLY ever
    # written by a human-confirmed policy_promote action (broker.py) — never
    # by the learner directly. Filtered at read time against a hardcoded
    # _NEVER_PROMOTABLE floor, so a stale/hand-edited value can never grant
    # more than the code permits. Same CSV-on-singleton-row idiom as
    # muted_notify_kinds below, for the same reason (a handful of kinds,
    # not a relational store).
    policy_auto_allow_kinds: str | None = Field(default=None)
    # Kinds demoted to always-forbidden for agent/autonomous actors, CSV.
    # Safe to auto-apply without asking (tightening only removes capability —
    # unlike auto_allow, which always requires a human confirm). Always wins
    # over auto_allow if a kind somehow ends up in both.
    policy_forbid_kinds: str | None = Field(default=None)
    # NEXUS Telegram bot's persistent chat() conversation — survives a NEXUS
    # restart so a multi-turn Telegram conversation doesn't silently reset.
    # /clear sets this back to None to start a fresh Conversation.
    telegram_conversation_id: int | None = Field(default=None)
    # Runtime per-kind notify mute (Telegram /mute /unmute /muted, Phase 2b).
    # CSV, same singleton-row idiom as policy_auto_allow_kinds. Distinct from
    # the static Settings.phone_suppressed_kinds (.env, requires a restart) —
    # this is Brian's own on-the-fly "stop pinging me about X" control.
    muted_notify_kinds: str | None = Field(default=None)
    # Monthly watch for whether Anthropic has shipped a public API-credit-
    # balance endpoint (backend/agents/anthropic_balance_watch.py) — there is
    # none today, a known gap (anthropics/claude-code#47574, open, "not
    # planned"). Persisted as "state:state_reason" (e.g. "closed:not_planned")
    # so a check-to-check CHANGE (not just a fixed calendar date) is what
    # triggers a Telegram notice, same reasoning as auth_burst_alert_json's
    # edge-trigger-and-persist idiom above.
    anthropic_balance_watch_last_issue_signal: str | None = Field(default=None)
    anthropic_balance_watch_last_comment_count: int | None = Field(default=None)
    # Last HTTP status seen probing the one concrete candidate endpoint the
    # community has proposed (GET /v1/organizations/balance) — 404 today,
    # live-verified. A change away from 404 is a strong, direct signal
    # independent of whether the GitHub issue itself was ever updated.
    anthropic_balance_watch_last_probe_status: int | None = Field(default=None)


class StateSnapshot(SQLModel, table=True):
    """Latest durable observation for one dashboard state key.

    `payload_json` always holds the last SUCCESSFUL payload. A failed refresh
    updates status/error/attempted_at without touching payload_json, so a
    reader can distinguish stale-but-useful data from never having any.
    """

    key: str = Field(primary_key=True)
    payload_json: str | None = None
    status: str = "never_observed"  # fresh | stale | error | never_observed
    observed_at: datetime | None = Field(default=None, index=True)
    attempted_at: datetime = Field(default_factory=datetime.utcnow, index=True)
    expires_at: datetime | None = Field(default=None, index=True)
    error: str | None = None
    schema_version: int = 1


# Agent/LLM trace observability (council w-observability). One row per
# traced run of a chat/briefing/orchestrator/proposer/voice entry point.
# Opened at entry, closed in a finally block with status ok|error. Purely
# additive, read-only-from-the-outside layer -- never gates control flow.
# Created by create_all (new table, no _ensure_ migration shim needed).
class AgentTrace(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    kind: str                # chat | briefing | orchestrator | proposer | voice
    label: str
    task_id: int | None = Field(default=None, index=True)
    started_at: datetime = Field(default_factory=datetime.utcnow, index=True)
    ended_at: datetime | None = None
    status: str = "running"  # running | ok | error
    error: str | None = None


# One row per LLM call or tool call made within an AgentTrace, written
# best-effort from router.py's _record_spend choke point (llm_call) and
# run_with_tools()'s tool-dispatch loop (tool_call). parent_span_id is None
# for top-level spans within a trace, set for nested spans.
# Created by create_all (new table, no _ensure_ migration shim needed).
class TraceSpan(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    trace_id: int = Field(index=True)
    parent_span_id: int | None = Field(default=None, index=True)
    span_type: str            # llm_call | tool_call
    name: str                 # model name for llm_call, tool name for tool_call
    started_at: datetime = Field(default_factory=datetime.utcnow)
    ended_at: datetime | None = None
    duration_ms: int | None = None
    input_summary: str | None = None    # truncated to 1000 chars
    output_summary: str | None = None   # truncated to 1000 chars
    tokens_in: int | None = None
    tokens_out: int | None = None
    cost_usd: float | None = None
    error: str | None = None


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
    # One-line distillation of the completed goal's Task result — read by the
    # daily digest so completed work stops vanishing into result_json.
    outcome_summary: str | None = None
    # Recurring-goal fields (cadence + category + success_criteria + next_eval_at).
    # cadence=None means one-shot; "daily"|"weekly"|"monthly" enables recurrence.
    cadence: str | None = None
    category: str | None = None
    success_criteria: str | None = None
    next_eval_at: datetime | None = None
    # Human pause switch: a disabled goal is kept but never auto-dispatched by the
    # recurring scheduler. Re-enable to resume. Does not affect a one-shot goal's
    # existing state; it just gates future recurrence ticks.
    disabled: bool = False
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


def _ensure_actionlog_columns():
    """Idempotently add columns introduced after the original `actionlog` table shipped.

    Each column is added independently via _safe_add_column so a race on one
    column never aborts the others. Best-effort — a failure here is logged but
    never fatal to startup. No-op on a fresh DB (create_all already made the
    column) and on test :memory: engines.
    """
    _safe_add_column("actionlog", "judge_verdict", "TEXT")
    _safe_add_column("actionlog", "judge_reason", "TEXT")
    _safe_add_column("actionlog", "confirmed_at", "TIMESTAMP")


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
    _safe_add_column("goal", "disabled", "BOOLEAN DEFAULT 0")
    _safe_add_column("goal", "outcome_summary", "TEXT")
    try:
        with engine.connect() as conn:
            # Hard backstop against the TOCTOU race in goals.propose(): its
            # debounce check (SELECT for an active duplicate) and the insert are
            # separate round-trips, so two concurrent propose() calls with the
            # same fingerprint could both pass the "no duplicate" check before
            # either inserts. This partial unique index makes the DB itself
            # reject the second insert; propose() catches the IntegrityError and
            # returns the same "debounced" result the pre-check already gives.
            # fingerprint != '' excludes blank-fingerprint rows (some direct
            # Goal(...) construction in tests never sets one) from the
            # constraint -- only propose()'s real, always-computed fingerprints
            # are covered.
            conn.execute(text(
                "CREATE UNIQUE INDEX IF NOT EXISTS ux_goal_fingerprint_active "
                "ON goal(fingerprint) WHERE status IN ('proposed','approved','running') "
                "AND fingerprint != ''"
            ))
            conn.commit()
    except Exception as e:  # pragma: no cover - defensive
        logger.warning(f"_ensure_goal_columns index create failed: {e}")


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


def _ensure_system_state_columns():
    """Add columns introduced to SystemState after the initial schema shipped.

    Only needed for existing DBs — create_all already adds these for fresh ones.
    Best-effort: a failure is logged but never fatal to startup.
    """
    _safe_add_column("systemstate", "last_dead_letter_alert_at", "TIMESTAMP")
    _safe_add_column("systemstate", "last_budget_warn_day", "TEXT")
    _safe_add_column("systemstate", "last_facts_digest_at", "TIMESTAMP")
    # Replaced auth_burst_alert_sources (TEXT) + auth_burst_alert_at (TIMESTAMP)
    # 2026-08-05. Those two are deliberately NOT dropped from existing DBs --
    # this shim only ever ADDs (see _safe_add_column), SQLite DROP COLUMN is a
    # table rebuild, and two orphaned nullable columns cost nothing. SQLAlchemy
    # only ever SELECTs declared columns, so they are invisible to the app.
    _safe_add_column("systemstate", "auth_burst_alert_json", "TEXT")
    _safe_add_column("systemstate", "policy_auto_allow_kinds", "TEXT")
    _safe_add_column("systemstate", "policy_forbid_kinds", "TEXT")
    _safe_add_column("systemstate", "telegram_conversation_id", "INTEGER")
    _safe_add_column("systemstate", "muted_notify_kinds", "TEXT")
    _safe_add_column("systemstate", "anthropic_balance_watch_last_issue_signal", "TEXT")
    _safe_add_column("systemstate", "anthropic_balance_watch_last_comment_count", "INTEGER")
    _safe_add_column("systemstate", "anthropic_balance_watch_last_probe_status", "INTEGER")


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


# Single-row cache (id=1) of Brian's writing-voice summary, distilled from his
# Sent-folder mail. Same singleton-row idiom as SystemState. New table (create_all
# only, no _ensure_ shim) — missing row is a legitimate "never built yet" state,
# handled lazily by backend/agents/mail_drafts.py::get_voice_profile.
class MailVoiceProfile(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    summary: str = ""
    sample_count: int = 0
    updated_at: datetime = Field(default_factory=datetime.utcnow)


# Dedup ledger for the mail auto-draft scheduler job (backend/agents/mail_drafts.py).
# One row per inbox email_id ever considered, so the same email is never
# reprocessed/redrafted on a later tick. New table (create_all only) — the unique
# index on email_id is the race-safety backstop (insert-then-catch-IntegrityError),
# same idiom as Goal.fingerprint's ux_goal_fingerprint_active.
class ProcessedMailId(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    email_id: str = Field(unique=True, index=True)
    drafted: bool = False
    # Added 2026-07-23 alongside the auto-junk-cleanup feature — True once this
    # email has been moved to Trash by autodraft_tick's junk branch.
    trashed: bool = False
    processed_at: datetime = Field(default_factory=datetime.utcnow)


# Single-row cache (id=1) of a distilled "what Brian deletes" profile, sampled
# from his real Trash folder (sender+subject only, no body). Same singleton-row
# idiom as MailVoiceProfile/SystemState. New table (create_all only, no _ensure_
# shim) — missing row is a legitimate "never built yet" state, handled lazily by
# backend/agents/mail_drafts.py::get_junk_profile.
class MailJunkProfile(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    summary: str = ""
    sample_count: int = 0
    updated_at: datetime = Field(default_factory=datetime.utcnow)


# Durable signal for the infisical -> legacy vault secret-fallback event
# (backend/secrets/manager.py::get_secret). One row per secret KEY (aggregate),
# NOT one row per event: `Settings` secret properties (backend/config.py
# ~lines 346-473) call get_secret() on every attribute read -- nothing caches
# them -- so during a sustained Infisical outage the fallback branch could
# fire thousands of times per hour. An append-only event table would
# accumulate thousands of byte-identical rows carrying zero extra
# information; this table is bounded by construction (~20 rows ever, one per
# secret key), the same hard-bounds reasoning backend/safety/authfail.py
# applies to its own hot, attacker-reachable path. Holds the key NAME only --
# the secret VALUE is never stored, logged, or returned anywhere. Created by
# create_all (new table, no _ensure_ migration shim needed).
class SecretFallback(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    secret_key: str = Field(unique=True, index=True)
    backend_from: str = "infisical"
    backend_to: str = "vault"
    event_count: int = 0
    first_at: datetime = Field(default_factory=datetime.utcnow)
    last_at: datetime = Field(default_factory=datetime.utcnow)


# Outcome Tracker (docs/outcome-tracker-spec.md) — wired end-to-end as of the
# 2026-08-01 rollout (see this repo's own CLAUDE.md "Outcome Tracker" section).
# One row per raised-and-tracked observation from homelab_watch/watchdog/
# briefing/contracts/manual sources, keyed for dedup by `fingerprint`
# (f"{source}:{check}") the same way Goal.fingerprint dedups goals. `severity`
# reuses ActionLog.risk/Goal.risk's low|medium|high vocabulary rather than
# inventing a new one. New table (create_all only), plus one index shim
# (_ensure_outcomeflag_index below) for the partial unique "at most one open
# flag per fingerprint" backstop — see ux_goal_fingerprint_active for the prior
# art this mirrors. homelab_watch.py/watchdog.py/briefing.py/telegram_*/
# api/safety.py all write and read through backend/agents/outcomes.py.
class OutcomeFlag(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    source: str = Field(index=True)      # homelab_watch | watchdog | briefing | contracts | manual
    check: str                            # garage_open | stale_prs | ha_alerts | vm:{vmid} | ...
    fingerprint: str = Field(default="", index=True)   # f"{source}:{check}" — dedup key
    summary: str                          # one-line human-readable, <=300 chars, plain text
    detail: str | None = None             # optional longer body / JSON blob
    severity: str = "medium"              # low | medium | high  (matches ActionLog.risk / Goal.risk)
    status: str = "open"                  # open | resolved | deferred | false_positive | needs_follow_up
    resolved_at: datetime | None = None
    resolved_by: str | None = None        # "telegram" | "api" | "auto:condition_cleared" | "auto:expired"
    resolution_note: str | None = None
    deferred_until: datetime | None = None
    action_log_id: int | None = Field(default=None, index=True)  # set only when a broker action accompanied this flag
    surfaced_count: int = 1               # incremented each time the source re-observes while still open
    last_surfaced_at: datetime = Field(default_factory=datetime.utcnow)
    created_at: datetime = Field(default_factory=datetime.utcnow, index=True)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    suppressed: bool = False              # written by record_flag when a hint gated it
    suppressed_reason: str | None = None  # "calibration:homelab_watch:garage_open fp_rate=0.71 n=7"


# Calibration Loop (docs/calibration-loop-spec.md) §1.3 — a nightly-computed,
# per-fingerprint snapshot of OutcomeFlag's human verdicts, used to decide
# whether a rule's Telegram page should be auto-suppressed. Brand new table,
# no migration shim needed (matches OutcomeFlag/SecretFallback/TaskOutcome):
# the fingerprint uniqueness is declared on the model and handled by
# create_all, unlike ux_outcomeflag_open it is unconditional.
class CalibrationHint(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    fingerprint: str = Field(unique=True, index=True)   # f"{source}:{check}", same key OutcomeFlag uses
    context_bucket: str = "all"        # v1 always "all" — see §1.4
    status: str = "active"             # active | expired | overridden_off
    # --- the frozen evidence that justified the current status ---
    verdict_count: int = 0             # denominator: human-verdicted rows in window (§2.3)
    false_positive_count: int = 0      # numerator
    fp_rate: float = 0.0
    auto_cleared_count: int = 0        # resolved_by LIKE 'auto:%' — excluded from both, shown in /calibration
    suppressed_surfacings: int = 0     # sum(surfaced_count) over suppressed rows — "how loud it still is"
    max_severity: str = "medium"       # highest severity seen in window; gates §3.4
    window_days: int = 30
    reason: str = ""                   # human-readable, rendered verbatim by /calibration
    # --- state machine ---
    first_active_at: datetime | None = None   # when suppression STARTED (never reset by a recompute)
    expires_at: datetime | None = None        # mandatory re-probation, §2.5
    override_by: str | None = None            # "telegram" | "api"
    override_at: datetime | None = None
    override_until: datetime | None = None    # nightly job refuses to re-activate before this
    override_note: str | None = None
    computed_at: datetime = Field(default_factory=datetime.utcnow)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


def _ensure_outcomeflag_columns():
    """Idempotently add the calibration-loop columns to an OutcomeFlag table
    that predates them. Best-effort — never fatal to startup."""
    _safe_add_column("outcomeflag", "suppressed", "BOOLEAN DEFAULT 0")
    _safe_add_column("outcomeflag", "suppressed_reason", "VARCHAR")


def _ensure_briefing_columns():
    """Rename Briefing.delivered_to_hermes -> delivered (2026-08-09, Hermes
    fully decommissioned). The column was write-only (never read anywhere),
    so this is a pure rename, not a data migration. Idempotent + race-safe,
    same discipline as _safe_add_column: a concurrent boot that already
    renamed it is a no-op, not a failure."""
    try:
        with engine.connect() as conn:
            cols = {row[1] for row in conn.execute(text("PRAGMA table_info(briefing)"))}
            if "delivered" in cols:
                return  # already renamed (or a fresh DB created it directly)
            if "delivered_to_hermes" in cols:
                conn.execute(text("ALTER TABLE briefing RENAME COLUMN delivered_to_hermes TO delivered"))
                conn.commit()
                return
            # Neither column present (pre-Briefing-table DB, unlikely but cheap to guard).
            conn.execute(text("ALTER TABLE briefing ADD COLUMN delivered BOOLEAN DEFAULT 0"))
            conn.commit()
    except Exception as e:
        if "duplicate column" in str(e).lower():
            return  # a racing boot renamed/added it first — fine, idempotent
        logger.warning(f"_ensure_briefing_columns failed: {e}")


def _ensure_processedmail_columns():
    """Idempotently add columns introduced after ProcessedMailId originally
    shipped (2026-07-23, same day — this table already exists in Brian's live
    DB from earlier today, so a shim is needed even though the table itself is
    "new"). Best-effort — never fatal to startup."""
    _safe_add_column("processedmailid", "trashed", "BOOLEAN DEFAULT 0")


def _ensure_outcomeflag_index():
    """Partial unique index: at most one OPEN flag per fingerprint. Hard backstop
    against record_flag()'s check-then-insert TOCTOU, exactly as
    ux_goal_fingerprint_active backstops goals.propose(). fingerprint != ''
    excludes directly-constructed test rows, same carve-out."""
    try:
        with engine.connect() as conn:
            conn.execute(text(
                "CREATE UNIQUE INDEX IF NOT EXISTS ux_outcomeflag_open "
                "ON outcomeflag(fingerprint) WHERE status = 'open' AND fingerprint != ''"
            ))
            conn.commit()
    except Exception as e:
        logger.warning(f"_ensure_outcomeflag_index create failed: {e}")


def create_db_and_tables():
    SQLModel.metadata.create_all(engine)
    _ensure_task_columns()
    _ensure_actionlog_columns()
    _ensure_spendlog_columns()
    _ensure_conversation_columns()
    _ensure_goal_columns()
    _ensure_goal_recurrence_columns()
    _ensure_fact_columns()
    _ensure_system_state_columns()
    _ensure_system_state()
    _ensure_processedmail_columns()
    _ensure_outcomeflag_columns()
    _ensure_outcomeflag_index()
    _ensure_briefing_columns()


def get_session():
    with Session(engine) as session:
        yield session
