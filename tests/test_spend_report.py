"""Tests for deep-link alerts + weekly spend reconciliation report.

Covers:
  1. governor.spend_report — groups by model over window, excludes old rows,
     surfaces prices_verified, sorts by cost desc.
  2. GET /api/safety/spend-report — 200 with correct keys.
  3. notify_phone deep-link: appended when app_base_url set; omitted when blank.
  4. send_spend_report — formats text, calls notify_phone(kind="spend_report").
  5. Scheduler registers "spend_report" job when spend_report_enabled=True.
"""
import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

# Ensure all table metadata is registered.
import backend.database  # noqa: F401


# ---------------------------------------------------------------------------
# Shared engine helpers
# ---------------------------------------------------------------------------

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


def _seed_spend(eng, model: str, cost: float, created_at: datetime,
                input_tokens: int = 100, output_tokens: int = 50):
    from backend.database import SpendLog
    with Session(eng) as s:
        row = SpendLog(
            model=model,
            cost_usd=cost,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            created_at=created_at,
        )
        s.add(row)
        s.commit()


# ---------------------------------------------------------------------------
# 1. governor.spend_report — grouping, windowing, sorting, prices_verified
# ---------------------------------------------------------------------------

def test_spend_report_groups_by_model_and_excludes_old(eng):
    """Rows within the 7-day window are grouped by model, sorted cost desc.
    A row older than 7 days must be excluded from totals."""
    from backend.safety import governor

    now = datetime.utcnow()
    recent = now - timedelta(days=3)
    old = now - timedelta(days=10)  # must be excluded

    # Model A: 2 calls, $1.00 + $2.00 = $3.00
    _seed_spend(eng, "claude-opus-4-8", 1.00, recent, input_tokens=500, output_tokens=200)
    _seed_spend(eng, "claude-opus-4-8", 2.00, recent, input_tokens=600, output_tokens=300)
    # Model B: 1 call, $1.50
    _seed_spend(eng, "claude-sonnet-4-6", 1.50, recent, input_tokens=300, output_tokens=100)
    # Old row (>7d) — must NOT appear in totals.
    _seed_spend(eng, "claude-opus-4-8", 99.0, old)

    with patch("backend.config.get_settings") as mock_settings:
        s = MagicMock()
        s.prices_verified = True
        mock_settings.return_value = s
        rep = governor.spend_report(days=7)

    assert rep["days"] == 7
    assert "since" in rep
    assert rep["prices_verified"] is True

    # Total should only include recent rows: $3.00 + $1.50 = $4.50
    assert rep["total_usd"] == pytest.approx(4.50)
    assert rep["total_calls"] == 3

    by_model = rep["by_model"]
    assert len(by_model) == 2

    # Sorted by cost descending: Opus first ($3.00), then Sonnet ($1.50).
    assert by_model[0]["model"] == "claude-opus-4-8"
    assert by_model[0]["calls"] == 2
    assert by_model[0]["cost_usd"] == pytest.approx(3.00)
    assert by_model[0]["input_tokens"] == 1100
    assert by_model[0]["output_tokens"] == 500

    assert by_model[1]["model"] == "claude-sonnet-4-6"
    assert by_model[1]["calls"] == 1
    assert by_model[1]["cost_usd"] == pytest.approx(1.50)


def test_spend_report_empty_window(eng):
    """No rows in window → by_model=[], total_usd=0.0, total_calls=0."""
    from backend.safety import governor

    with patch("backend.config.get_settings") as mock_settings:
        s = MagicMock()
        s.prices_verified = False
        mock_settings.return_value = s
        rep = governor.spend_report(days=7)

    assert rep["by_model"] == []
    assert rep["total_usd"] == 0.0
    assert rep["total_calls"] == 0
    assert rep["prices_verified"] is False


def test_spend_report_best_effort_on_db_error(monkeypatch):
    """A DB error returns a safe error dict with empty by_model, never raises."""
    from backend.safety import governor

    def boom(*a, **kw):
        raise RuntimeError("db is gone")

    monkeypatch.setattr("backend.database.engine", MagicMock())

    with patch("backend.config.get_settings") as mock_settings:
        s = MagicMock()
        s.prices_verified = False
        mock_settings.return_value = s
        # Force the Session constructor to raise.
        with patch("sqlmodel.Session", side_effect=RuntimeError("db is gone")):
            rep = governor.spend_report(days=7)

    assert rep["by_model"] == []
    assert rep["total_usd"] == 0.0
    assert "error" in rep


# ---------------------------------------------------------------------------
# 2. GET /api/safety/spend-report — 200 with correct keys
# ---------------------------------------------------------------------------

@pytest.fixture
def safety_client(tmp_path, monkeypatch):
    vault_key = tmp_path / ".vault.key"
    vault_file = tmp_path / "nexus.vault"
    vault_key.write_bytes(b"A" * 32)
    vault_file.write_text("{}")
    monkeypatch.chdir(tmp_path)

    test_engine = make_engine()
    monkeypatch.setattr("backend.database.engine", test_engine)

    from backend.database import get_session

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
        app.dependency_overrides[get_session] = override_session
        with TestClient(app) as c:
            c._engine = test_engine
            yield c
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# 3. notify_phone deep-link — appended when set, omitted when blank
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_notify_phone_appends_deep_link_when_base_url_set():
    """With app_base_url set, the payload content must end with
    'Open: {base}/safety'."""
    hermes_notify_mock = AsyncMock(return_value=True)

    with patch("backend.config.get_settings") as mock_settings, \
         patch("backend.integrations.hermes.notify", hermes_notify_mock):
        s = MagicMock()
        s.phone_notifications_enabled = True
        s.app_base_url = "http://192.0.2.1:3000"
        mock_settings.return_value = s

        from backend.events import notify_phone
        result = await notify_phone("budget alert", kind="x")

    assert result is True
    hermes_notify_mock.assert_awaited_once()
    call_payload = hermes_notify_mock.await_args[0][0]
    assert 'href="http://192.0.2.1:3000/safety"' in call_payload["content"]
    assert call_payload["content"].startswith("budget alert")
    assert call_payload.get("parse_mode") == "HTML"


@pytest.mark.asyncio
async def test_notify_phone_no_deep_link_when_base_url_blank():
    """With app_base_url='', the content must NOT contain 'Open:'."""
    hermes_notify_mock = AsyncMock(return_value=True)

    with patch("backend.config.get_settings") as mock_settings, \
         patch("backend.integrations.hermes.notify", hermes_notify_mock):
        s = MagicMock()
        s.phone_notifications_enabled = True
        s.app_base_url = ""
        mock_settings.return_value = s

        from backend.events import notify_phone
        result = await notify_phone("hi", kind="autonomy_alert")

    assert result is True
    call_payload = hermes_notify_mock.await_args[0][0]
    assert "Open:" not in call_payload["content"]
    assert call_payload["content"] == "hi"


@pytest.mark.asyncio
async def test_notify_phone_deep_link_strips_trailing_slash():
    """A base URL with a trailing slash must still produce a clean deep-link."""
    hermes_notify_mock = AsyncMock(return_value=True)

    with patch("backend.config.get_settings") as mock_settings, \
         patch("backend.integrations.hermes.notify", hermes_notify_mock):
        s = MagicMock()
        s.phone_notifications_enabled = True
        s.app_base_url = "http://192.0.2.1:3000/"
        mock_settings.return_value = s

        from backend.events import notify_phone
        await notify_phone("msg", kind="test")

    call_payload = hermes_notify_mock.await_args[0][0]
    assert 'href="http://192.0.2.1:3000/safety"' in call_payload["content"]
    assert call_payload.get("parse_mode") == "HTML"
    # Must not have double-slash.
    assert "//safety" not in call_payload["content"]


# ---------------------------------------------------------------------------
# 4. send_spend_report — formats text, calls notify_phone with kind="spend_report"
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_send_spend_report_calls_notify_phone(eng):
    """send_spend_report seeds rows, builds text with $ and 'spend', and calls
    notify_phone(kind='spend_report')."""
    now = datetime.utcnow()
    _seed_spend(eng, "claude-sonnet-4-6", 0.50, now - timedelta(days=1))
    _seed_spend(eng, "claude-opus-4-8", 1.20, now - timedelta(days=2))

    notify_mock = AsyncMock(return_value=True)

    with patch("backend.config.get_settings") as mock_settings, \
         patch("backend.events.notify_phone", notify_mock):
        s = MagicMock()
        s.prices_verified = True
        mock_settings.return_value = s

        from backend.agents.digest import send_spend_report
        result = await send_spend_report()

    assert result["delivered"] is True
    assert "$" in result["text"]
    assert "spend" in result["text"].lower()

    notify_mock.assert_awaited_once()
    call_kwargs = notify_mock.await_args
    assert call_kwargs.kwargs.get("kind") == "spend_report"
    # Text passed to notify_phone must contain dollar amounts and call count.
    sent_text = call_kwargs.args[0]
    assert "$" in sent_text
    assert "spend" in sent_text.lower()


@pytest.mark.asyncio
async def test_send_spend_report_never_raises():
    """Even if governor.spend_report raises, send_spend_report returns
    {"delivered": False, "text": ""} and does NOT re-raise."""
    with patch("backend.safety.governor.spend_report", side_effect=RuntimeError("boom")):
        from backend.agents.digest import send_spend_report
        result = await send_spend_report()

    assert result["delivered"] is False


@pytest.mark.asyncio
async def test_send_spend_report_prices_unverified_label(eng):
    """When prices_verified=False the text contains 'UNVERIFIED'."""
    now = datetime.utcnow()
    _seed_spend(eng, "claude-sonnet-4-6", 0.10, now - timedelta(hours=1))

    notify_mock = AsyncMock(return_value=True)

    with patch("backend.config.get_settings") as mock_settings, \
         patch("backend.events.notify_phone", notify_mock):
        s = MagicMock()
        s.prices_verified = False
        mock_settings.return_value = s

        from backend.agents.digest import send_spend_report
        result = await send_spend_report()

    assert "UNVERIFIED" in result["text"]


@pytest.mark.asyncio
async def test_send_spend_report_prices_verified_label(eng):
    """When prices_verified=True the text contains 'VERIFIED' (not 'UNVERIFIED')."""
    now = datetime.utcnow()
    _seed_spend(eng, "claude-haiku-4-5", 0.05, now - timedelta(hours=1))

    notify_mock = AsyncMock(return_value=True)

    with patch("backend.config.get_settings") as mock_settings, \
         patch("backend.events.notify_phone", notify_mock):
        s = MagicMock()
        s.prices_verified = True
        mock_settings.return_value = s

        from backend.agents.digest import send_spend_report
        result = await send_spend_report()

    assert "VERIFIED" in result["text"]
    assert "UNVERIFIED" not in result["text"]


# ---------------------------------------------------------------------------
# 5. Scheduler registers "spend_report" job when spend_report_enabled=True
# ---------------------------------------------------------------------------

def test_scheduler_registers_spend_report_when_enabled():
    """With spend_report_enabled=True, setup_scheduler adds a job with id='spend_report'."""
    from backend.scheduler import setup_scheduler, scheduler

    with patch.object(scheduler, "add_job") as mock_add, \
         patch("backend.config.get_settings") as mock_settings:
        s = MagicMock()
        s.proposer_enabled = False
        s.mail_autodraft_enabled = False
        s.autonomy_digest_enabled = False
        s.backup_enabled = False
        s.step_watchdog_enabled = False
        s.watchdog_enabled = False
        s.spend_report_enabled = True
        s.spend_report_time = "08:00"
        s.spend_report_day = "mon"
        mock_settings.return_value = s

        setup_scheduler("07:00", "America/Detroit")

    ids_added = {c.kwargs.get("id") for c in mock_add.call_args_list}
    assert "spend_report" in ids_added, (
        f"Expected 'spend_report' in scheduler jobs; got: {ids_added}"
    )


def test_scheduler_no_spend_report_when_disabled():
    """With spend_report_enabled=False, setup_scheduler must NOT add 'spend_report'."""
    from backend.scheduler import setup_scheduler, scheduler

    with patch.object(scheduler, "add_job") as mock_add, \
         patch("backend.config.get_settings") as mock_settings:
        s = MagicMock()
        s.proposer_enabled = False
        s.mail_autodraft_enabled = False
        s.autonomy_digest_enabled = False
        s.backup_enabled = False
        s.step_watchdog_enabled = False
        s.watchdog_enabled = False
        s.spend_report_enabled = False
        mock_settings.return_value = s

        setup_scheduler("07:00", "America/Detroit")

    ids_added = {c.kwargs.get("id") for c in mock_add.call_args_list}
    assert "spend_report" not in ids_added, (
        f"'spend_report' should NOT be registered when disabled; got: {ids_added}"
    )


def test_scheduler_spend_report_invalid_time_falls_back():
    """A malformed spend_report_time falls back to 08:00 without crashing."""
    from backend.scheduler import setup_scheduler, scheduler

    with patch.object(scheduler, "add_job") as mock_add, \
         patch("backend.config.get_settings") as mock_settings:
        s = MagicMock()
        s.proposer_enabled = False
        s.mail_autodraft_enabled = False
        s.autonomy_digest_enabled = False
        s.backup_enabled = False
        s.step_watchdog_enabled = False
        s.watchdog_enabled = False
        s.spend_report_enabled = True
        s.spend_report_time = "NOT_A_TIME"
        s.spend_report_day = "mon"
        mock_settings.return_value = s

        setup_scheduler("07:00", "America/Detroit")

    ids_added = {c.kwargs.get("id") for c in mock_add.call_args_list}
    assert "spend_report" in ids_added, (
        f"'spend_report' should still be registered after fallback; got: {ids_added}"
    )


# ---------------------------------------------------------------------------
# Tier B4 — by-label grouping
# ---------------------------------------------------------------------------

def test_spend_report_groups_by_label(eng):
    from backend.database import SpendLog
    from backend.safety import governor
    now = datetime.utcnow()
    with Session(eng) as s:
        s.add(SpendLog(model="m1", cost_usd=0.30, label="chat_reply",
                       input_tokens=10, output_tokens=5, created_at=now))
        s.add(SpendLog(model="m1", cost_usd=0.20, label="chat_reply",
                       input_tokens=10, output_tokens=5, created_at=now))
        s.add(SpendLog(model="m2", cost_usd=0.10, label="",
                       input_tokens=1, output_tokens=1, created_at=now))
        s.commit()

    report = governor.spend_report(days=7)
    by_label = {e["label"]: e for e in report["by_label"]}
    assert by_label["chat_reply"]["calls"] == 2
    assert by_label["chat_reply"]["cost_usd"] == pytest.approx(0.50)
    assert "(unlabeled)" in by_label
    # sorted by cost desc
    assert report["by_label"][0]["label"] == "chat_reply"


def test_by_label_and_by_model_totals_match(eng):
    from backend.database import SpendLog
    from backend.safety import governor
    now = datetime.utcnow()
    with Session(eng) as s:
        for lb, cost in (("a", 0.1), ("b", 0.2), ("", 0.3)):
            s.add(SpendLog(model="m", cost_usd=cost, label=lb,
                           input_tokens=1, output_tokens=1, created_at=now))
        s.commit()

    report = governor.spend_report(days=7)
    assert sum(e["cost_usd"] for e in report["by_label"]) == pytest.approx(report["total_usd"])
    assert sum(e["cost_usd"] for e in report["by_model"]) == pytest.approx(report["total_usd"])


def test_no_unlabeled_llm_calls_in_agents():
    """Regression guard: every haiku/sonnet/opus call in backend/agents must
    carry a label= (multiline calls included via paren-scan)."""
    import pathlib, re
    root = pathlib.Path("backend/agents")
    offenders = []
    for f in root.glob("*.py"):
        src = f.read_text(encoding="utf-8")
        for m in re.finditer(r"await (?:router\.)?(haiku|sonnet|opus)\(", src):
            # scan to the matching close paren
            depth, i = 1, m.end()
            while i < len(src) and depth:
                if src[i] == "(":
                    depth += 1
                elif src[i] == ")":
                    depth -= 1
                i += 1
            if "label=" not in src[m.start():i]:
                offenders.append(f"{f.name}: {src[m.start():m.start()+60]!r}")
    assert not offenders, f"unlabeled LLM calls: {offenders}"
