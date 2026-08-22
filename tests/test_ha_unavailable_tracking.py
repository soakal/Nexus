"""Tests for the per-entity HA unavailable-duration tracker
(backend/database.py::HaEntityUnavailable) and its read paths in
backend/integrations/homeassistant.py.

Covers the lifecycle the feature exists for: a newly-unavailable entity gets
tracked, a recovered one gets untracked, and a still-unavailable one's
duration grows (first_seen_unavailable_at never resets while it stays
unavailable) -- plus the age-bucketed report/summary text and the
suppress/unsuppress ("known-dead, stop counting") triage action.
"""
from datetime import datetime, timedelta

import pytest
from sqlmodel import Session, select

from backend.database import HaEntityUnavailable
from backend.integrations import homeassistant


@pytest.fixture(autouse=True)
def _clean_ha_unavailable_table():
    """HaEntityUnavailable lives in the session-scoped shared test DB (see
    conftest.py::_isolate_test_database) -- clear it before AND after every
    test in this file so cross-test pollution can't mask (or fake) a bug.

    Reads backend.database.engine fresh via module attribute access (not a
    top-level `from backend.database import engine`) -- the session-scoped
    isolation fixture patches that attribute AFTER this file is collected,
    so a top-level import would bind the stale pre-patch engine."""
    import backend.database as db

    def _clear():
        with Session(db.engine) as session:
            for row in session.exec(select(HaEntityUnavailable)).all():
                session.delete(row)
            session.commit()
    _clear()
    yield
    _clear()


def _row(entity_id):
    import backend.database as db
    with Session(db.engine) as session:
        return session.exec(
            select(HaEntityUnavailable).where(HaEntityUnavailable.entity_id == entity_id)
        ).first()


@pytest.mark.asyncio
async def test_newly_unavailable_entity_gets_tracked():
    await homeassistant._track_unavailable([
        {"entity_id": "sensor.orphan_zigbee", "state": "unavailable"},
    ])
    row = _row("sensor.orphan_zigbee")
    assert row is not None
    assert row.suppressed is False
    assert row.first_seen_unavailable_at == row.last_checked_at


@pytest.mark.asyncio
async def test_unknown_state_also_tracked():
    await homeassistant._track_unavailable([
        {"entity_id": "climate.upstairs", "state": "unknown"},
    ])
    assert _row("climate.upstairs") is not None


@pytest.mark.asyncio
async def test_recovered_entity_is_untracked():
    await homeassistant._track_unavailable([
        {"entity_id": "sensor.orphan_zigbee", "state": "unavailable"},
    ])
    assert _row("sensor.orphan_zigbee") is not None

    await homeassistant._track_unavailable([
        {"entity_id": "sensor.orphan_zigbee", "state": "on"},
    ])
    assert _row("sensor.orphan_zigbee") is None


@pytest.mark.asyncio
async def test_still_unavailable_grows_duration_without_resetting_first_seen():
    await homeassistant._track_unavailable([
        {"entity_id": "sensor.orphan_zigbee", "state": "unavailable"},
    ])

    # Simulate this having been unavailable for 10 days already.
    import backend.database as db
    backdated = datetime.utcnow() - timedelta(days=10)
    with Session(db.engine) as session:
        r = session.exec(
            select(HaEntityUnavailable).where(HaEntityUnavailable.entity_id == "sensor.orphan_zigbee")
        ).first()
        r.first_seen_unavailable_at = backdated
        r.last_checked_at = backdated
        session.add(r)
        session.commit()

    # A later poll, still unavailable.
    await homeassistant._track_unavailable([
        {"entity_id": "sensor.orphan_zigbee", "state": "unavailable"},
    ])

    row = _row("sensor.orphan_zigbee")
    # first_seen must stay the backdated value -- a re-poll must never reset
    # the clock on an entity that's still down.
    assert abs((row.first_seen_unavailable_at - backdated).total_seconds()) < 2
    # last_checked_at DID advance to ~now -- this is what "duration grows" means.
    assert (datetime.utcnow() - row.last_checked_at).total_seconds() < 5
    assert row.last_checked_at > row.first_seen_unavailable_at


@pytest.mark.asyncio
async def test_track_unavailable_ignores_known_noisy_entities():
    """Matches the pre-existing homelab_digest.py ignore-list behavior this
    module inherited -- iPads/Alexas/HA Cloud STT-TTS churn constantly and
    would drown the triage list; HA Cloud STT/TTS already has its own
    dedicated cloud_alerts mechanism in fetch()."""
    await homeassistant._track_unavailable([
        {"entity_id": "sensor.ipad_brian_battery", "state": "unavailable"},
        {"entity_id": "stt.home_assistant_cloud", "state": "unavailable"},
        {"entity_id": "button.doorbell_identify", "state": "unavailable"},
    ])
    assert _row("sensor.ipad_brian_battery") is None
    assert _row("stt.home_assistant_cloud") is None
    assert _row("button.doorbell_identify") is None


@pytest.mark.asyncio
async def test_track_unavailable_never_raises_on_db_error(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr("backend.integrations.homeassistant.Session", boom)
    # Must not raise.
    await homeassistant._track_unavailable([{"entity_id": "sensor.x", "state": "unavailable"}])


@pytest.mark.asyncio
async def test_unavailable_report_buckets_persistent_and_recent():
    import backend.database as db
    now = datetime.utcnow()
    with Session(db.engine) as session:
        session.add(HaEntityUnavailable(
            entity_id="sensor.persistent_dead",
            first_seen_unavailable_at=now - timedelta(days=30),
            last_checked_at=now,
        ))
        session.add(HaEntityUnavailable(
            entity_id="light.just_restarted",
            first_seen_unavailable_at=now - timedelta(minutes=2),
            last_checked_at=now,
        ))
        session.add(HaEntityUnavailable(
            entity_id="sensor.middling",
            first_seen_unavailable_at=now - timedelta(hours=12),
            last_checked_at=now,
        ))
        session.commit()

    report = await homeassistant.unavailable_report()
    assert report["total"] == 3
    assert report["persistent"] == 1
    assert report["recent"] == 1
    assert {i["entity_id"] for i in report["items"]} == {
        "sensor.persistent_dead", "light.just_restarted", "sensor.middling",
    }


@pytest.mark.asyncio
async def test_unavailable_report_excludes_suppressed_from_counts_but_lists_them():
    import backend.database as db
    now = datetime.utcnow()
    with Session(db.engine) as session:
        session.add(HaEntityUnavailable(
            entity_id="sensor.known_dead",
            first_seen_unavailable_at=now - timedelta(days=60),
            last_checked_at=now,
            suppressed=True,
        ))
        session.commit()

    report = await homeassistant.unavailable_report()
    assert report["total"] == 0
    assert report["persistent"] == 0
    assert len(report["items"]) == 1
    assert report["items"][0]["suppressed"] is True


@pytest.mark.asyncio
async def test_unavailable_report_degrades_on_db_error(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr("backend.integrations.homeassistant.Session", boom)
    report = await homeassistant.unavailable_report()
    assert report == {"total": 0, "persistent": 0, "recent": 0, "items": []}


def test_format_unavailable_summary_all_ok():
    assert homeassistant.format_unavailable_summary(
        {"total": 0, "persistent": 0, "recent": 0}
    ) == "all entities OK"


def test_format_unavailable_summary_actionable_buckets():
    text = homeassistant.format_unavailable_summary({"total": 15, "persistent": 12, "recent": 3})
    assert "12 unavailable >7 days (persistently dead)" in text
    assert "3 unavailable <1h (likely transient)" in text


def test_format_unavailable_summary_middling_bucket_shown():
    text = homeassistant.format_unavailable_summary({"total": 5, "persistent": 0, "recent": 0})
    assert text == "5 unavailable 1h-7d"


@pytest.mark.asyncio
async def test_set_unavailable_suppressed_toggles_and_missing_returns_false():
    await homeassistant._track_unavailable([
        {"entity_id": "sensor.orphan_zigbee", "state": "unavailable"},
    ])
    assert await homeassistant.set_unavailable_suppressed("sensor.orphan_zigbee", True) is True
    assert _row("sensor.orphan_zigbee").suppressed is True

    assert await homeassistant.set_unavailable_suppressed("sensor.orphan_zigbee", False) is True
    assert _row("sensor.orphan_zigbee").suppressed is False

    assert await homeassistant.set_unavailable_suppressed("sensor.never_tracked", True) is False
