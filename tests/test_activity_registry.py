"""Unit tests for backend/activity.py -- the in-memory Pulse registry.

No FastAPI/DB involved: pure dict/deque state behind a lock. Covers the
begin/end/pulse lifecycle, the ticker ring, thread-safety, the staleness
sweep, and the "never raises" contract every mutator promises.
"""
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from backend import activity


@pytest.fixture(autouse=True)
def _reset():
    activity.reset_registry()
    yield
    activity.reset_registry()


def test_begin_creates_running_entry():
    activity.begin("job:homelab_watch", "job", "homelab_watch")
    snap = activity.snapshot()
    entries = {e["actor_id"]: e for e in snap["entries"]}
    e = entries["job:homelab_watch"]
    assert e["actor_type"] == "job"
    assert e["status"] == "running"
    assert e["started_at"] is not None
    assert e["label"] == "homelab_watch"


def test_end_transitions_to_ok_and_computes_duration():
    activity.begin("job:x", "job", "x")
    activity.end("job:x", True)
    e = {e["actor_id"]: e for e in activity.snapshot()["entries"]}["job:x"]
    assert e["status"] == "ok"
    assert e["started_at"] is None  # cleared on end
    assert e["last_run_at"] is not None
    assert e["last_duration_ms"] is not None
    assert e["last_duration_ms"] >= 0
    assert e["last_error"] is None


def test_end_transitions_to_error_with_truncated_message():
    activity.begin("job:x", "job", "x")
    activity.end("job:x", False, "boom " * 100)
    e = {e["actor_id"]: e for e in activity.snapshot()["entries"]}["job:x"]
    assert e["status"] == "error"
    assert len(e["last_error"]) <= 200


def test_end_is_noop_for_unknown_actor():
    """A MISSED scheduler event (no matching begin()) must not create a
    phantom entry or raise."""
    activity.end("job:never-began", True)
    assert activity.snapshot()["entries"] == []


def test_pulse_appends_ticker_event():
    activity.pulse("broker", "action", "ha_service allowed")
    events = activity.snapshot()["events"]
    assert len(events) == 1
    assert events[0]["actor_id"] == "broker"
    assert events[0]["event"] == "action"
    assert events[0]["summary"] == "ha_service allowed"
    assert "ts" in events[0]


def test_pulse_merges_detail_into_existing_entry_only():
    # No entry for "worker:0" yet -- detail merge is a silent no-op.
    activity.pulse("worker:0", "progress", "step 1", detail={"step_index": 1})
    assert activity.snapshot()["entries"] == []

    activity.begin("worker:0", "worker", "task 42")
    activity.pulse("worker:0", "progress", "step 2", detail={"step_index": 2, "total_steps": 5})
    e = {e["actor_id"]: e for e in activity.snapshot()["entries"]}["worker:0"]
    assert e["detail"] == {"step_index": 2, "total_steps": 5}


def test_update_detail_does_not_touch_status_or_ticker():
    activity.begin("task:1", "task", "prompt")
    activity.update_detail("task:1", {"step_index": 2, "total_steps": 4})
    e = {e["actor_id"]: e for e in activity.snapshot()["entries"]}["task:1"]
    assert e["status"] == "running"
    assert e["detail"] == {"step_index": 2, "total_steps": 4}
    assert activity.snapshot()["events"] == []


def test_remove_drops_entry_entirely():
    activity.begin("task:1", "task", "prompt")
    activity.remove("task:1")
    assert activity.snapshot()["entries"] == []


def test_remove_unknown_actor_is_noop():
    activity.remove("task:does-not-exist")
    # No real removal happened -- must not falsely tell a connected client
    # to delete an entry it never had.
    delta = activity.drain_dirty()
    assert delta is None


def test_remove_populates_removed_channel_for_next_drain():
    """A connected client only learns an entry is gone via drain_dirty()'s
    'removed' list -- the entry itself drops out of _entries immediately,
    but without this channel a client's local state would show a finished
    task as permanently 'running' forever."""
    activity.begin("task:1", "task", "prompt")
    activity.drain_dirty()  # clear the begin()'s own dirty state
    activity.remove("task:1")
    delta = activity.drain_dirty()
    assert delta["removed"] == ["task:1"]
    assert delta["entries"] == []
    # Draining again must not repeat the removal.
    assert activity.drain_dirty() is None


def test_sweep_stale_populates_removed_channel():
    from datetime import datetime, timedelta

    activity.begin("job:stale", "job", "stale")
    activity.end("job:stale", True)
    activity._entries["job:stale"].last_run_at = (datetime.utcnow() - timedelta(hours=25)).isoformat()
    activity.drain_dirty()

    assert activity.sweep_stale() == 1
    delta = activity.drain_dirty()
    assert delta["removed"] == ["job:stale"]


def test_begin_after_remove_clears_pending_removal():
    """An actor removed then immediately re-begun (before the removal was
    ever drained) must not have the client told to delete an entry that, by
    the time the delta ships, exists again."""
    activity.begin("task:1", "task", "prompt")
    activity.remove("task:1")
    activity.begin("task:1", "task", "prompt again")
    delta = activity.drain_dirty()
    assert delta["removed"] == []
    assert [e["actor_id"] for e in delta["entries"]] == ["task:1"]


def test_ring_capped_at_200():
    for i in range(250):
        activity.pulse("x", "tick", str(i))
    events = activity.snapshot()["events"]
    assert len(events) == 200
    # Oldest dropped, newest kept.
    assert events[0]["summary"] == "50"
    assert events[-1]["summary"] == "249"


def test_label_and_error_truncated_to_200_chars():
    activity.begin("job:x", "job", "L" * 500)
    e = {e["actor_id"]: e for e in activity.snapshot()["entries"]}["job:x"]
    assert len(e["label"]) == 200

    activity.end("job:x", False, "E" * 500)
    e = {e["actor_id"]: e for e in activity.snapshot()["entries"]}["job:x"]
    assert len(e["last_error"]) == 200


def test_pulse_event_fields_truncated():
    activity.pulse("x" * 200, "e" * 200, "s" * 500)
    ev = activity.snapshot()["events"][0]
    assert len(ev["event"]) == 80
    assert len(ev["summary"]) == 200


def test_pending_events_bounded_even_when_never_drained():
    """Regression for a real blocking bug found in Opus verification:
    _pending_events must be capped the same way _ring is. The broadcaster
    only calls drain_dirty() when a client is connected -- with nobody
    watching (the normal state, ~100% of the time), an uncapped list would
    grow forever (~2MB per 5000 pulses, measured) for the whole process
    lifetime, then dump its entire backlog onto the event loop in one
    json.dumps + send_text the moment someone finally opens Pulse."""
    for i in range(5000):
        activity.pulse("x", "tick", str(i))
    assert len(activity._pending_events) <= 200


def test_removed_leaks_unbounded_without_discard_undelivered():
    """Regression for a SECOND real bug found in a follow-up Opus verify
    pass: fix #4 made trace actor ids unique per instance
    (trace:{kind}:{trace_id}), so every completed chat/briefing/proposer/
    voice trace calls remove() -- and remove() feeds _removed, which
    nothing bounds on its own (unlike _pending_events/_ring, it has no
    maxlen). Confirms the raw set really does grow past 200 without help --
    the fix is discard_undelivered(), tested separately below, not a cap on
    _removed itself (an id genuinely due for delivery must not be dropped
    while a client IS connected)."""
    for i in range(300):
        activity.begin(f"trace:chat:{i}", "trace", "x")
        activity.remove(f"trace:chat:{i}")
    assert len(activity._removed) > 200


def test_discard_undelivered_clears_pending_and_removed_not_entries():
    activity.begin("job:x", "job", "x")
    activity.pulse("job:x", "note", "hello")
    activity.remove("job:x")
    activity.begin("job:y", "job", "y")  # a live entry that must survive

    assert len(activity._pending_events) > 0
    assert len(activity._removed) > 0

    activity.discard_undelivered()

    assert len(activity._pending_events) == 0
    assert len(activity._removed) == 0
    # entries/dirty are untouched -- a fresh connection still needs full
    # current state via snapshot(), and dirty isn't a leak vector on its own.
    ids = {e["actor_id"] for e in activity.snapshot()["entries"]}
    assert ids == {"job:y"}
    assert activity.drain_dirty()["entries"]  # job:y's begin() is still pending


def test_drain_dirty_returns_none_when_nothing_changed():
    assert activity.drain_dirty() is None


def test_drain_dirty_returns_and_clears_changes():
    activity.begin("job:x", "job", "x")
    activity.pulse("job:x", "note", "hello")
    delta = activity.drain_dirty()
    assert delta is not None
    assert len(delta["entries"]) == 1
    assert delta["entries"][0]["actor_id"] == "job:x"
    assert len(delta["events"]) == 1

    # Second drain with no new changes returns None.
    assert activity.drain_dirty() is None


def test_drain_dirty_only_includes_changed_entries():
    activity.begin("job:a", "job", "a")
    activity.drain_dirty()  # clear the initial dirty state
    activity.begin("job:b", "job", "b")
    delta = activity.drain_dirty()
    ids = [e["actor_id"] for e in delta["entries"]]
    assert ids == ["job:b"]


def test_sweep_stale_drops_old_entries_only():
    from datetime import datetime, timedelta

    activity.begin("job:fresh", "job", "fresh")
    activity.end("job:fresh", True)

    activity.begin("job:stale", "job", "stale")
    activity.end("job:stale", True)
    # Force the stale entry's last_run_at into the past, bypassing the
    # public API (this is exactly what sweep_stale is meant to catch).
    stale_entry = activity._entries["job:stale"]
    stale_entry.last_run_at = (datetime.utcnow() - timedelta(hours=25)).isoformat()

    dropped = activity.sweep_stale()
    assert dropped == 1
    ids = {e["actor_id"] for e in activity.snapshot()["entries"]}
    assert ids == {"job:fresh"}


def test_sweep_stale_tolerates_malformed_timestamp():
    """A malformed timestamp on ONE entry must not abort the sweep for
    every other entry -- pin this via a second, genuinely-stale entry that
    still gets dropped in the same call, proving the per-item try/except
    is what's tolerating the bad one, not sweep_stale's outer catch
    swallowing the whole function (which would report 0 either way)."""
    from datetime import datetime, timedelta

    activity.begin("job:malformed", "job", "x")
    activity._entries["job:malformed"].last_run_at = "not-a-timestamp"
    activity._entries["job:malformed"].started_at = None

    activity.begin("job:genuinely-stale", "job", "y")
    activity._entries["job:genuinely-stale"].last_run_at = (
        datetime.utcnow() - timedelta(hours=25)
    ).isoformat()
    activity._entries["job:genuinely-stale"].started_at = None

    dropped = activity.sweep_stale()
    assert dropped == 1
    ids = {e["actor_id"] for e in activity.snapshot()["entries"]}
    assert ids == {"job:malformed"}  # survived; the malformed one is skipped, not crashed past


def test_mutators_never_raise_on_poisoned_entry(monkeypatch):
    """Inject a broken entries dict and confirm every mutator still degrades
    silently rather than propagating -- same contract as events.publish.
    Pre-populates one real STALE entry (via dict.__setitem__, bypassing our
    own poisoned override) so remove()/sweep_stale() have something to
    actually fail on touching -- an empty poisoned dict would let both
    trivially "pass" without exercising their pop()/__delitem__ paths."""
    from datetime import datetime, timedelta

    class _Poisoned(dict):
        def __setitem__(self, *a, **k):
            raise RuntimeError("poisoned")

        def get(self, *a, **k):
            raise RuntimeError("poisoned")

        def pop(self, *a, **k):
            raise RuntimeError("poisoned")

        def __delitem__(self, *a, **k):
            raise RuntimeError("poisoned")

    poisoned = _Poisoned()
    stale = activity.ActivityEntry(
        actor_id="x", actor_type="job",
        last_run_at=(datetime.utcnow() - timedelta(hours=25)).isoformat(),
    )
    dict.__setitem__(poisoned, "x", stale)
    monkeypatch.setattr(activity, "_entries", poisoned)

    activity.begin("x", "job", "x")                # must not raise (poisoned get())
    activity.end("x", True)                        # must not raise (poisoned get())
    activity.pulse("x", "e", "s", detail={"a": 1})  # must not raise (poisoned get())
    activity.update_detail("x", {})                 # must not raise (poisoned get())
    activity.remove("x")                            # must not raise (poisoned pop())
    assert activity.sweep_stale() == 0              # must not raise (poisoned __delitem__ on the real stale entry)

    events = activity.snapshot()["events"]
    assert len(events) == 1  # pulse's ring append still succeeded (the ring is a plain deque, never poisoned)


def test_reset_registry_clears_everything():
    activity.begin("job:x", "job", "x")
    activity.pulse("job:x", "e", "s")
    activity.reset_registry()
    assert activity.snapshot() == {"entries": [], "events": []}
    assert activity.drain_dirty() is None


def test_thread_safety_concurrent_mutation_and_snapshot():
    """Mutate from many threads (mirrors _record_trace_span running inside a
    run_in_executor worker thread) while repeatedly snapshotting -- must not
    corrupt state or raise, and every write must be visible afterward."""
    def worker(i):
        for j in range(50):
            activity.begin(f"worker:{i}", "worker", f"task {j}")
            activity.pulse(f"worker:{i}", "tick", str(j))
            activity.end(f"worker:{i}", j % 2 == 0)

    errors = []

    def snapshotter():
        for _ in range(200):
            try:
                activity.snapshot()
                activity.drain_dirty()
            except Exception as e:
                errors.append(e)

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(worker, i) for i in range(6)]
        futures.append(pool.submit(snapshotter))
        for f in futures:
            f.result(timeout=10)

    assert errors == []
    entries = {e["actor_id"] for e in activity.snapshot()["entries"]}
    assert entries == {f"worker:{i}" for i in range(6)}
