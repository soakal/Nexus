"""Tests for the protonmail_send broker kind (Proton Mail NEXUS integration, Task 4).

Covers: classify band, user-EXECUTED, agent-FORBIDDEN (IRREVERSIBLE unconfirmed),
agent-confirmed-EXECUTED, kill-switch-FORBIDDEN, dispatch-failure-FAILED.
"""

import pytest
from unittest.mock import AsyncMock, patch

from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

import backend.database  # noqa: F401,E402


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


def _all_logs(eng):
    from backend.database import ActionLog

    with Session(eng) as s:
        return s.exec(select(ActionLog).order_by(ActionLog.created_at)).all()


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


def test_classify_protonmail_send():
    from backend.safety.broker import classify, Risk, Reversibility

    assert classify("protonmail_send", {}) == (Risk.HIGH, Reversibility.IRREVERSIBLE)
    assert classify("protonmail_send", {"recipients": ["a@example.com"]}) == (
        Risk.HIGH,
        Reversibility.IRREVERSIBLE,
    )


@pytest.mark.asyncio
async def test_protonmail_send_user_executed(eng):
    """actor=user is always ALLOWED regardless of IRREVERSIBLE risk."""
    _seed_state(eng, autonomy=True)

    from backend.safety.broker import execute_action, Decision

    with patch(
        "backend.integrations.protonmail.send_email",
        new_callable=AsyncMock,
        return_value={"ok": True, "detail": "sent"},
    ) as se:
        res = await execute_action(
            actor="user",
            kind="protonmail_send",
            target="a@example.com",
            payload={"recipients": ["a@example.com"], "subject": "hi", "body": "hello"},
        )

    assert res.decision == Decision.EXECUTED
    se.assert_awaited_once()
    _, kwargs = se.call_args
    assert kwargs["recipients"] == ["a@example.com"]
    assert kwargs["subject"] == "hi"
    assert kwargs["body"] == "hello"

    logs = _all_logs(eng)
    assert len(logs) == 1
    assert logs[0].actor == "user"
    assert logs[0].kind == "protonmail_send"
    assert logs[0].decision == "executed"


@pytest.mark.asyncio
async def test_protonmail_send_agent_unconfirmed_forbidden(eng):
    """IRREVERSIBLE + agent + unconfirmed -> FORBIDDEN (never needs_confirm), no dispatch."""
    _seed_state(eng, autonomy=True)

    from backend.safety.broker import execute_action, Decision

    with patch(
        "backend.integrations.protonmail.send_email", new_callable=AsyncMock
    ) as se:
        res = await execute_action(
            actor="agent",
            kind="protonmail_send",
            target="a@example.com",
            payload={"recipients": ["a@example.com"], "subject": "hi", "body": "hello"},
        )

    assert res.decision == Decision.FORBIDDEN
    se.assert_not_awaited()


@pytest.mark.asyncio
async def test_protonmail_send_agent_confirmed_executed(eng):
    """IRREVERSIBLE + agent + confirmed=True -> ALLOWED/EXECUTED."""
    _seed_state(eng, autonomy=True)

    from backend.safety.broker import execute_action, Decision

    with patch(
        "backend.integrations.protonmail.send_email",
        new_callable=AsyncMock,
        return_value={"ok": True, "detail": "sent"},
    ) as se:
        res = await execute_action(
            actor="agent",
            kind="protonmail_send",
            target="a@example.com",
            payload={"recipients": ["a@example.com"], "subject": "hi", "body": "hello"},
            confirmed=True,
        )

    assert res.decision == Decision.EXECUTED
    se.assert_awaited_once()


@pytest.mark.asyncio
async def test_protonmail_send_kill_switch_forbidden(eng):
    """Autonomy OFF -> agent/autonomous FORBIDDEN before classify/dispatch."""
    _seed_state(eng, autonomy=False)

    from backend.safety.broker import execute_action, Decision

    with patch(
        "backend.integrations.protonmail.send_email", new_callable=AsyncMock
    ) as se:
        res = await execute_action(
            actor="agent",
            kind="protonmail_send",
            target="a@example.com",
            payload={"recipients": ["a@example.com"], "subject": "hi", "body": "hello"},
        )

    assert res.decision == Decision.FORBIDDEN
    se.assert_not_awaited()


@pytest.mark.asyncio
async def test_protonmail_send_dispatch_failure_recorded_failed(eng):
    """send_email raising IntegrationError -> Decision.FAILED, never re-raised."""
    _seed_state(eng, autonomy=True)

    from backend.integrations.protonmail import IntegrationError
    from backend.safety.broker import execute_action, Decision

    with patch(
        "backend.integrations.protonmail.send_email",
        new_callable=AsyncMock,
        side_effect=IntegrationError("mailbox unavailable"),
    ):
        res = await execute_action(
            actor="user",
            kind="protonmail_send",
            target="a@example.com",
            payload={"recipients": ["a@example.com"], "subject": "hi", "body": "hello"},
        )

    assert res.decision == Decision.FAILED
    logs = _all_logs(eng)
    assert len(logs) == 1
    assert logs[0].decision == "failed"


def test_protonmail_send_in_dispatchers():
    from backend.safety.broker import _DISPATCHERS

    assert "protonmail_send" in _DISPATCHERS


# ===========================================================================
# protonmail_archive — LOW / REVERSIBLE_BY_INVERSE
# ===========================================================================

def test_classify_protonmail_archive():
    from backend.safety.broker import classify, Risk, Reversibility

    assert classify("protonmail_archive", {}) == (Risk.LOW, Reversibility.REVERSIBLE_BY_INVERSE)


@pytest.mark.asyncio
async def test_protonmail_archive_user_executed(eng):
    _seed_state(eng, autonomy=True)
    from backend.safety.broker import execute_action, Decision

    with patch(
        "backend.integrations.protonmail.archive_email",
        new_callable=AsyncMock,
        return_value={"ok": True, "detail": "archived"},
    ) as ae:
        res = await execute_action(
            actor="user",
            kind="protonmail_archive",
            target="1",
            payload={"email_id": "1"},
        )

    assert res.decision == Decision.EXECUTED
    ae.assert_awaited_once_with("1", mailbox=None)
    logs = _all_logs(eng)
    assert len(logs) == 1
    assert logs[0].actor == "user"
    assert logs[0].kind == "protonmail_archive"
    assert logs[0].decision == "executed"


@pytest.mark.asyncio
async def test_protonmail_archive_agent_unconfirmed_executed(eng):
    """LOW band -> ALLOWED even for an unconfirmed agent (deliberate choice —
    leaves the door open for future auto-archive automation)."""
    _seed_state(eng, autonomy=True)
    from backend.safety.broker import execute_action, Decision

    with patch(
        "backend.integrations.protonmail.archive_email",
        new_callable=AsyncMock,
        return_value={"ok": True, "detail": "archived"},
    ) as ae:
        res = await execute_action(
            actor="agent",
            kind="protonmail_archive",
            target="1",
            payload={"email_id": "1"},
        )

    assert res.decision == Decision.EXECUTED
    ae.assert_awaited_once()


@pytest.mark.asyncio
async def test_protonmail_archive_kill_switch_forbidden(eng):
    _seed_state(eng, autonomy=False)
    from backend.safety.broker import execute_action, Decision

    with patch(
        "backend.integrations.protonmail.archive_email", new_callable=AsyncMock
    ) as ae:
        res = await execute_action(
            actor="agent",
            kind="protonmail_archive",
            target="1",
            payload={"email_id": "1"},
        )

    assert res.decision == Decision.FORBIDDEN
    ae.assert_not_awaited()


@pytest.mark.asyncio
async def test_protonmail_archive_dispatch_failure_failed(eng):
    _seed_state(eng, autonomy=True)
    from backend.integrations.protonmail import IntegrationError
    from backend.safety.broker import execute_action, Decision

    with patch(
        "backend.integrations.protonmail.archive_email",
        new_callable=AsyncMock,
        side_effect=IntegrationError("archive failed"),
    ):
        res = await execute_action(
            actor="user",
            kind="protonmail_archive",
            target="1",
            payload={"email_id": "1"},
        )

    assert res.decision == Decision.FAILED
    logs = _all_logs(eng)
    assert logs[0].decision == "failed"


def test_protonmail_archive_in_dispatchers():
    from backend.safety.broker import _DISPATCHERS

    assert "protonmail_archive" in _DISPATCHERS


# ===========================================================================
# protonmail_delete — LOW / REVERSIBLE_BY_INVERSE (fixed 2026-07-23: now moves
# to Trash via trash_email/move_emails, same band as protonmail_archive, since
# the hard-remove tool was found to actually permanently expunge)
# ===========================================================================

def test_classify_protonmail_delete():
    from backend.safety.broker import classify, Risk, Reversibility

    assert classify("protonmail_delete", {}) == (Risk.LOW, Reversibility.REVERSIBLE_BY_INVERSE)


@pytest.mark.asyncio
async def test_protonmail_delete_user_executed(eng):
    _seed_state(eng, autonomy=True)
    from backend.safety.broker import execute_action, Decision

    with patch(
        "backend.integrations.protonmail.trash_email",
        new_callable=AsyncMock,
        return_value={"ok": True, "detail": "moved"},
    ) as te:
        res = await execute_action(
            actor="user",
            kind="protonmail_delete",
            target="1",
            payload={"email_id": "1"},
        )

    assert res.decision == Decision.EXECUTED
    te.assert_awaited_once_with("1", mailbox=None)
    logs = _all_logs(eng)
    assert logs[0].kind == "protonmail_delete"
    assert logs[0].decision == "executed"


@pytest.mark.asyncio
async def test_protonmail_delete_agent_unconfirmed_executed(eng):
    """LOW band -> ALLOWED even for an unconfirmed agent/autonomous actor —
    this flip (was FORBIDDEN under the old HIGH/IRREVERSIBLE classification) is
    the whole point of the 2026-07-23 fix: it's a reversible Trash move now,
    same as archive."""
    _seed_state(eng, autonomy=True)
    from backend.safety.broker import execute_action, Decision

    with patch(
        "backend.integrations.protonmail.trash_email",
        new_callable=AsyncMock,
        return_value={"ok": True, "detail": "moved"},
    ) as te:
        res = await execute_action(
            actor="agent",
            kind="protonmail_delete",
            target="1",
            payload={"email_id": "1"},
        )

    assert res.decision == Decision.EXECUTED
    te.assert_awaited_once()


@pytest.mark.asyncio
async def test_protonmail_delete_kill_switch_forbidden(eng):
    _seed_state(eng, autonomy=False)
    from backend.safety.broker import execute_action, Decision

    with patch(
        "backend.integrations.protonmail.trash_email", new_callable=AsyncMock
    ) as te:
        res = await execute_action(
            actor="agent",
            kind="protonmail_delete",
            target="1",
            payload={"email_id": "1"},
        )

    assert res.decision == Decision.FORBIDDEN
    te.assert_not_awaited()


@pytest.mark.asyncio
async def test_protonmail_delete_dispatch_failure_failed(eng):
    _seed_state(eng, autonomy=True)
    from backend.integrations.protonmail import IntegrationError
    from backend.safety.broker import execute_action, Decision

    with patch(
        "backend.integrations.protonmail.trash_email",
        new_callable=AsyncMock,
        side_effect=IntegrationError("move failed"),
    ):
        res = await execute_action(
            actor="user",
            kind="protonmail_delete",
            target="1",
            payload={"email_id": "1"},
        )

    assert res.decision == Decision.FAILED
    logs = _all_logs(eng)
    assert logs[0].decision == "failed"


def test_protonmail_delete_in_dispatchers():
    from backend.safety.broker import _DISPATCHERS

    assert "protonmail_delete" in _DISPATCHERS
