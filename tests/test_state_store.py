from datetime import datetime, timedelta

from backend import state_store


def test_snapshot_survives_memory_reset():
    state_store.store_success("k1", {"healthy": True}, ttl_seconds=60)
    state_store.reset_memory_cache()
    snap = state_store.get_snapshot("k1")
    assert snap["data"] == {"healthy": True}
    assert snap["freshness"] == "fresh"


def test_failure_preserves_last_success():
    state_store.store_success("k2", {"healthy": True}, ttl_seconds=60)
    snap = state_store.store_failure("k2", "boom")
    assert snap["data"] == {"healthy": True}
    assert snap["freshness"] == "stale"
    assert snap["error"] == "boom"


def test_expired_snapshot_is_stale():
    state_store.store_success("k3", {"a": 1}, ttl_seconds=60)
    # Force the persisted expires_at into the past directly, bypassing the
    # process-local cache so get_snapshot re-reads from the DB row.
    from backend.database import StateSnapshot, engine
    from sqlmodel import Session

    with Session(engine) as session:
        row = session.get(StateSnapshot, "k3")
        row.expires_at = datetime.utcnow() - timedelta(minutes=2)
        session.add(row)
        session.commit()
    state_store.reset_memory_cache()
    snap = state_store.get_snapshot("k3")
    assert snap["data"] == {"a": 1}
    assert snap["freshness"] == "stale"


def test_get_snapshots_batches_cold_cache_reads():
    state_store.store_success("k4", {"a": 1}, ttl_seconds=60)
    state_store.reset_memory_cache()
    result = state_store.get_snapshots(["k4", "k5-never-observed"])
    assert result["k4"]["freshness"] == "fresh"
    assert result["k5-never-observed"]["freshness"] == "never_observed"
    assert result["k5-never-observed"]["data"] is None


def test_malformed_persisted_payload_is_unavailable():
    from backend.database import StateSnapshot, engine
    from sqlmodel import Session

    with Session(engine) as session:
        session.add(StateSnapshot(key="k6", payload_json="{not json", status="fresh"))
        session.commit()
    state_store.reset_memory_cache()
    snap = state_store.get_snapshot("k6")
    assert snap["data"] is None
    assert snap["freshness"] == "unavailable"
