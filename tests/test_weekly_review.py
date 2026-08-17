from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

import backend.database  # noqa: F401 — register all models on metadata
from backend.agents import weekly_review
from backend.database import ActionLog, OutcomeFlag, SpendLog


@pytest.fixture
def eng(monkeypatch):
    e = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(e)
    monkeypatch.setattr("backend.database.engine", e)
    return e


def _settings(**overrides):
    s = MagicMock()
    s.weekly_review_enabled = True
    for k, v in overrides.items():
        setattr(s, k, v)
    return s


# ---------------------------------------------------------------------------
# Aggregation queries
# ---------------------------------------------------------------------------

def test_db_action_log_counts_groups_by_kind_and_decision(eng):
    now = datetime.utcnow()
    with Session(eng) as s:
        s.add(ActionLog(actor="agent", kind="unraid_docker", target="plex", payload_json="{}",
                         risk="low", reversibility="reversible", decision="executed", created_at=now))
        s.add(ActionLog(actor="agent", kind="unraid_docker", target="jellyfin", payload_json="{}",
                         risk="low", reversibility="reversible", decision="executed", created_at=now))
        s.add(ActionLog(actor="agent", kind="unraid_docker", target="plex", payload_json="{}",
                         risk="low", reversibility="reversible", decision="failed", created_at=now))
        s.add(ActionLog(actor="agent", kind="vm_power", target="100", payload_json="{}",
                         risk="high", reversibility="unknown", decision="executed",
                         created_at=now - timedelta(days=30)))  # outside window
        s.commit()

    rows = weekly_review._db_action_log_counts(now - timedelta(days=7))
    by_kind_decision = {(r["kind"], r["decision"]): r["count"] for r in rows}
    assert by_kind_decision[("unraid_docker", "executed")] == 2
    assert by_kind_decision[("unraid_docker", "failed")] == 1
    assert ("vm_power", "executed") not in by_kind_decision


def test_db_mute_candidates_matches_threshold_and_false_positive(eng):
    now = datetime.utcnow()
    with Session(eng) as s:
        # Qualifies: surfaced >= 10, has a false_positive row
        s.add(OutcomeFlag(source="homelab_watch", check="unraid_temp",
                           fingerprint="homelab_watch:unraid_temp", summary="hot disk",
                           status="false_positive", surfaced_count=11, last_surfaced_at=now))
        # Same kind, open status -- surfaced sums with the row above
        s.add(OutcomeFlag(source="homelab_watch", check="unraid_temp",
                           fingerprint="homelab_watch:unraid_temp", summary="hot disk again",
                           status="open", surfaced_count=3, last_surfaced_at=now))
        # Below threshold -- excluded
        s.add(OutcomeFlag(source="homelab_watch", check="vzdump_failed",
                           fingerprint="homelab_watch:vzdump_failed", summary="backup failed",
                           status="false_positive", surfaced_count=2, last_surfaced_at=now))
        # No false_positive row -- excluded even though surfaced is high
        s.add(OutcomeFlag(source="homelab_watch", check="garage_open",
                           fingerprint="homelab_watch:garage_open", summary="garage open",
                           status="resolved", surfaced_count=15, last_surfaced_at=now))
        s.commit()

    candidates = weekly_review._db_mute_candidates(now - timedelta(days=7), already_muted=set())
    kinds = {c["kind"] for c in candidates}
    assert kinds == {"homelab_disk_temp"}
    entry = next(c for c in candidates if c["kind"] == "homelab_disk_temp")
    assert entry["surfaced"] == 14
    assert entry["false_positive"] == 1


def test_db_mute_candidates_excludes_already_muted(eng):
    now = datetime.utcnow()
    with Session(eng) as s:
        s.add(OutcomeFlag(source="homelab_watch", check="unraid_temp",
                           fingerprint="homelab_watch:unraid_temp", summary="hot disk",
                           status="false_positive", surfaced_count=20, last_surfaced_at=now))
        s.commit()

    candidates = weekly_review._db_mute_candidates(now - timedelta(days=7), already_muted={"homelab_disk_temp"})
    assert candidates == []


def test_db_mute_candidates_capped_at_three(eng):
    now = datetime.utcnow()
    checks = ["unraid_temp", "vzdump_failed", "unraid_array", "garage_open"]
    with Session(eng) as s:
        for i, check in enumerate(checks):
            s.add(OutcomeFlag(source="homelab_watch", check=check,
                               fingerprint=f"homelab_watch:{check}", summary="x",
                               status="false_positive", surfaced_count=20 - i, last_surfaced_at=now))
        s.commit()

    candidates = weekly_review._db_mute_candidates(now - timedelta(days=7), already_muted=set())
    assert len(candidates) == 3
    # sorted by surfaced descending
    assert [c["surfaced"] for c in candidates] == sorted((c["surfaced"] for c in candidates), reverse=True)


def test_kind_for_fingerprint_prefix_and_exact_matches():
    assert weekly_review._kind_for_fingerprint("homelab_watch:vm:100") == "homelab_vm_stopped"
    assert weekly_review._kind_for_fingerprint("homelab_watch:docker:plex") == "homelab_docker_stopped"
    assert weekly_review._kind_for_fingerprint("homelab_watch:unraid_array") == "homelab_array"
    assert weekly_review._kind_for_fingerprint("watchdog:budget_warn") is None


# ---------------------------------------------------------------------------
# run_weekly_review — full integration
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_weekly_review_skips_when_disabled(eng):
    with patch("backend.config.get_settings", return_value=_settings(weekly_review_enabled=False)):
        result = await weekly_review.run_weekly_review()
    assert result == {"skipped": "disabled"}


@pytest.mark.asyncio
async def test_run_weekly_review_skips_empty_week(eng):
    with patch("backend.config.get_settings", return_value=_settings()), \
         patch("backend.safety.governor.get_muted_notify_kinds", return_value=set()), \
         patch("backend.safety.governor.spend_report", return_value={"total_usd": 0.0, "total_calls": 0, "by_label": []}), \
         patch("backend.agents.outcomes.calibration_summary", new_callable=AsyncMock, return_value={}):
        result = await weekly_review.run_weekly_review()
    assert result == {"skipped": "no_activity"}


@pytest.mark.asyncio
async def test_run_weekly_review_delivers_memo_with_mute_buttons(eng):
    now = datetime.utcnow()
    with Session(eng) as s:
        s.add(ActionLog(actor="agent", kind="unraid_docker", target="plex", payload_json="{}",
                         risk="low", reversibility="reversible", decision="executed", created_at=now))
        s.add(OutcomeFlag(source="homelab_watch", check="unraid_temp",
                           fingerprint="homelab_watch:unraid_temp", summary="hot disk",
                           status="false_positive", surfaced_count=15, last_surfaced_at=now))
        s.commit()

    sonnet_mock = AsyncMock(return_value="A short memo.")
    notify_mock = AsyncMock(return_value=True)
    with patch("backend.config.get_settings", return_value=_settings()), \
         patch("backend.safety.governor.get_muted_notify_kinds", return_value=set()), \
         patch("backend.safety.governor.spend_report", return_value={"total_usd": 1.23, "total_calls": 5, "by_label": []}), \
         patch("backend.agents.outcomes.calibration_summary", new_callable=AsyncMock, return_value={}), \
         patch("backend.agents.router.sonnet", sonnet_mock), \
         patch("backend.events.notify_phone", notify_mock):
        result = await weekly_review.run_weekly_review()

    assert result == {"skipped": None, "delivered": True}
    sonnet_mock.assert_awaited_once()
    assert sonnet_mock.await_args.kwargs.get("label") == "weekly_review"
    notify_mock.assert_awaited_once()
    assert notify_mock.await_args.kwargs["kind"] == "weekly_review"
    assert notify_mock.await_args.kwargs["buttons"] == [
        {"text": "🔇 Mute homelab_disk_temp", "callback_data": "mute:on:homelab_disk_temp"}
    ]
    assert "A short memo." in notify_mock.await_args.args[0]


@pytest.mark.asyncio
async def test_run_weekly_review_llm_failure_falls_back_to_deterministic_memo(eng):
    now = datetime.utcnow()
    with Session(eng) as s:
        s.add(ActionLog(actor="agent", kind="unraid_docker", target="plex", payload_json="{}",
                         risk="low", reversibility="reversible", decision="executed", created_at=now))
        s.commit()

    notify_mock = AsyncMock(return_value=True)
    with patch("backend.config.get_settings", return_value=_settings()), \
         patch("backend.safety.governor.get_muted_notify_kinds", return_value=set()), \
         patch("backend.safety.governor.spend_report", return_value={"total_usd": 0.5, "total_calls": 2, "by_label": []}), \
         patch("backend.agents.outcomes.calibration_summary", new_callable=AsyncMock, return_value={}), \
         patch("backend.agents.router.sonnet", new_callable=AsyncMock, side_effect=RuntimeError("boom")), \
         patch("backend.events.notify_phone", notify_mock):
        result = await weekly_review.run_weekly_review()

    assert result == {"skipped": None, "delivered": True}
    notify_mock.assert_awaited_once()
    assert "$0.50" in notify_mock.await_args.args[0]


def test_weekly_review_kind_registered():
    from backend.events import NOTIFY_KINDS
    assert "weekly_review" in NOTIFY_KINDS
