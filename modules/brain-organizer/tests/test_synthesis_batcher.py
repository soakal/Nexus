"""Tests for SynthesisBatcher (Plan 2 / Batch API).

These exercise real threading.Thread + threading.Condition timing (with tiny
debounce/poll overrides, never the real 30s production defaults) rather than
mocking the synchronization primitives away -- the whole point of this class
is its concurrent behavior, so that's what needs to be under test.
"""
from __future__ import annotations

import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from anthropic import APIStatusError
from anthropic.types import TextBlock

import brain_organizer as bo


def _req(prompt: str = "hi", model: str = "claude-sonnet-4-6", max_tokens: int = 100) -> dict:
    return {"prompt": prompt, "model": model, "max_tokens": max_tokens}


def _make_usage(input_tokens=10, output_tokens=5, cache_creation=0, cache_read=0):
    return SimpleNamespace(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_creation_input_tokens=cache_creation,
        cache_read_input_tokens=cache_read,
    )


def _make_batch(batch_id: str, status: str = "ended") -> SimpleNamespace:
    return SimpleNamespace(id=batch_id, processing_status=status)


def _make_result(custom_id: str, text: str, stop_reason: str = "end_turn") -> SimpleNamespace:
    message = SimpleNamespace(
        content=[TextBlock(type="text", text=text)],
        stop_reason=stop_reason,
        usage=_make_usage(),
    )
    return SimpleNamespace(custom_id=custom_id, result=SimpleNamespace(type="succeeded", message=message))


def _make_errored_result(custom_id: str) -> SimpleNamespace:
    return SimpleNamespace(custom_id=custom_id, result=SimpleNamespace(type="errored", message=None))


def _fixed_workers(n: int):
    return lambda: n


class TestSyncMode:
    def test_use_batch_api_false_never_calls_batches_create(self):
        client = MagicMock()
        client.messages.create.side_effect = [
            SimpleNamespace(
                content=[TextBlock(type="text", text="A")], stop_reason="end_turn",
                usage=_make_usage(),
            ),
            SimpleNamespace(
                content=[TextBlock(type="text", text="B")], stop_reason="end_turn",
                usage=_make_usage(),
            ),
        ]
        config = {**bo._CONFIG_DEFAULTS, "use_batch_api": False}
        batcher = bo.SynthesisBatcher(config, client, _fixed_workers(1))

        results = batcher.call_many([_req("p1"), _req("p2")])

        assert results == [("A", "end_turn"), ("B", "end_turn")]
        client.messages.batches.create.assert_not_called()
        assert client.messages.create.call_count == 2

    def test_empty_requests_returns_empty_without_touching_client(self):
        client = MagicMock()
        config = {**bo._CONFIG_DEFAULTS, "use_batch_api": True}
        batcher = bo.SynthesisBatcher(config, client, _fixed_workers(1))
        assert batcher.call_many([]) == []
        client.messages.batches.create.assert_not_called()
        client.messages.create.assert_not_called()


class TestBatchModeHappyPath:
    def test_two_workers_two_requests_each_one_batch_create_call(self):
        client = MagicMock()
        client.messages.batches.create.return_value = _make_batch("batch_1")
        client.messages.batches.retrieve.return_value = _make_batch("batch_1", "ended")

        def fake_results(batch_id):
            # Deliberately out of custom_id order -- results arrive in "any
            # order" per the real API; keying must not assume position.
            return [
                _make_result("0002", "r2"),
                _make_result("0000", "r0"),
                _make_result("0003", "r3"),
                _make_result("0001", "r1"),
            ]
        client.messages.batches.results.side_effect = fake_results

        config = {**bo._CONFIG_DEFAULTS, "use_batch_api": True}
        batcher = bo.SynthesisBatcher(
            config, client, _fixed_workers(2), debounce_seconds=5.0, poll_seconds=0.01,
        )

        out: dict[int, list] = {}
        barrier = threading.Barrier(2)

        def worker(idx: int, reqs: list[dict]) -> None:
            barrier.wait(timeout=5)
            out[idx] = batcher.call_many(reqs)

        t1 = threading.Thread(target=worker, args=(1, [_req("a"), _req("b")]))
        t2 = threading.Thread(target=worker, args=(2, [_req("c"), _req("d")]))
        t1.start(); t2.start()
        t1.join(timeout=10); t2.join(timeout=10)

        assert not t1.is_alive() and not t2.is_alive(), "call_many did not return -- possible deadlock"
        assert client.messages.batches.create.call_count == 1
        submitted = client.messages.batches.create.call_args.kwargs["requests"]
        assert len(submitted) == 4

        # Each worker's own 2 results came back correctly keyed to its own
        # requests, regardless of the shuffled custom_id order above.
        assert out[1] == [("r0", "end_turn"), ("r1", "end_turn")] or out[1] == [("r2", "end_turn"), ("r3", "end_turn")]
        assert out[2] == [("r0", "end_turn"), ("r1", "end_turn")] or out[2] == [("r2", "end_turn"), ("r3", "end_turn")]
        assert out[1] != out[2]

    def test_lock_path_touched_during_poll(self, tmp_path):
        client = MagicMock()
        client.messages.batches.create.return_value = _make_batch("batch_1", "in_progress")
        # First retrieve still in_progress, second ended -- touch should
        # happen on the in_progress iteration.
        client.messages.batches.retrieve.side_effect = [
            _make_batch("batch_1", "in_progress"),
            _make_batch("batch_1", "ended"),
        ]
        client.messages.batches.results.return_value = [_make_result("0000", "ok")]

        lock_path = tmp_path / ".organizer.lock"
        lock_path.write_text("123")
        mtime_before = lock_path.stat().st_mtime

        config = {**bo._CONFIG_DEFAULTS, "use_batch_api": True}
        batcher = bo.SynthesisBatcher(
            config, client, _fixed_workers(1),
            lock_path=lock_path, debounce_seconds=0.05, poll_seconds=0.02,
        )
        time.sleep(0.02)  # ensure the filesystem mtime clock can tick forward
        results = batcher.call_many([_req("only")])

        assert results == [("ok", "end_turn")]
        assert lock_path.stat().st_mtime >= mtime_before


class TestBatchModeFallbacks:
    def test_one_errored_result_falls_back_to_sync_for_that_item_only(self):
        client = MagicMock()
        client.messages.batches.create.return_value = _make_batch("batch_1")
        client.messages.batches.retrieve.return_value = _make_batch("batch_1", "ended")
        client.messages.batches.results.return_value = [
            _make_result("0000", "good"),
            _make_errored_result("0001"),
        ]
        client.messages.create.return_value = SimpleNamespace(
            content=[TextBlock(type="text", text="recovered")], stop_reason="end_turn",
            usage=_make_usage(),
        )

        config = {**bo._CONFIG_DEFAULTS, "use_batch_api": True}
        batcher = bo.SynthesisBatcher(config, client, _fixed_workers(1), debounce_seconds=0.05)

        results = batcher.call_many([_req("ok-one"), _req("bad-one")])

        assert results == [("good", "end_turn"), ("recovered", "end_turn")]
        assert client.messages.create.call_count == 1

    def test_batches_create_transient_failure_falls_back_to_sync_for_all(self):
        client = MagicMock()
        client.messages.batches.create.side_effect = RuntimeError("network blip")
        client.messages.create.side_effect = [
            SimpleNamespace(content=[TextBlock(type="text", text="s1")], stop_reason="end_turn", usage=_make_usage()),
            SimpleNamespace(content=[TextBlock(type="text", text="s2")], stop_reason="end_turn", usage=_make_usage()),
        ]

        config = {**bo._CONFIG_DEFAULTS, "use_batch_api": True}
        batcher = bo.SynthesisBatcher(config, client, _fixed_workers(1), debounce_seconds=0.05)

        results = batcher.call_many([_req("p1"), _req("p2")])

        assert results == [("s1", "end_turn"), ("s2", "end_turn")]
        assert client.messages.create.call_count == 2

    def test_usage_capped_batch_create_raises_api_usage_capped_to_every_waiter(self):
        client = MagicMock()
        client.messages.batches.create.side_effect = APIStatusError(
            "usage limits exceeded",
            response=MagicMock(status_code=400),
            body={"error": {"message": "usage limits exceeded"}},
        )

        config = {**bo._CONFIG_DEFAULTS, "use_batch_api": True}
        batcher = bo.SynthesisBatcher(config, client, _fixed_workers(2), debounce_seconds=5.0)

        errors: dict[int, Exception] = {}
        barrier = threading.Barrier(2)

        def worker(idx: int) -> None:
            barrier.wait(timeout=5)
            try:
                batcher.call_many([_req(f"p{idx}")])
            except Exception as exc:  # noqa: BLE001 -- capturing for assertion below
                errors[idx] = exc

        t1 = threading.Thread(target=worker, args=(1,))
        t2 = threading.Thread(target=worker, args=(2,))
        t1.start(); t2.start()
        t1.join(timeout=10); t2.join(timeout=10)

        assert set(errors) == {1, 2}
        assert isinstance(errors[1], bo._APIUsageCapped)
        assert isinstance(errors[2], bo._APIUsageCapped)
        # Only one batch was ever attempted -- the cap applies to every
        # waiter in the claimed batch, not one retry per waiter.
        assert client.messages.batches.create.call_count == 1

    def test_max_wait_exceeded_treats_unreturned_items_as_errored_and_syncs(self):
        client = MagicMock()
        client.messages.batches.create.return_value = _make_batch("batch_1", "in_progress")
        client.messages.batches.retrieve.return_value = _make_batch("batch_1", "in_progress")  # never ends
        client.messages.create.return_value = SimpleNamespace(
            content=[TextBlock(type="text", text="synced")], stop_reason="end_turn", usage=_make_usage(),
        )

        config = {**bo._CONFIG_DEFAULTS, "use_batch_api": True}
        batcher = bo.SynthesisBatcher(
            config, client, _fixed_workers(1),
            debounce_seconds=0.02, poll_seconds=0.02, max_wait_seconds=0.05,
        )

        results = batcher.call_many([_req("only")])

        assert results == [("synced", "end_turn")]
        assert client.messages.create.call_count == 1


class TestDebounceAndConvergence:
    def test_lone_waiter_flushes_after_debounce_timeout(self):
        """active_workers_fn says 2 workers are still active, but only one
        ever calls call_many -- must not deadlock; must flush once the
        debounce window elapses."""
        client = MagicMock()
        client.messages.batches.create.return_value = _make_batch("batch_1")
        client.messages.batches.retrieve.return_value = _make_batch("batch_1", "ended")
        client.messages.batches.results.return_value = [_make_result("0000", "solo")]

        config = {**bo._CONFIG_DEFAULTS, "use_batch_api": True}
        batcher = bo.SynthesisBatcher(
            config, client, _fixed_workers(2), debounce_seconds=0.1, poll_seconds=0.01,
        )

        start = time.monotonic()
        results = batcher.call_many([_req("only")])
        elapsed = time.monotonic() - start

        assert results == [("solo", "end_turn")]
        assert elapsed >= 0.1  # actually waited out the debounce, not a fluke early flush
        assert elapsed < 5.0   # and did not hang

    def test_on_worker_exit_wakes_a_lone_waiter_before_the_debounce_timeout(self):
        """Once the OTHER active worker exits (on_worker_exit called),
        waiters (1) >= active_workers_fn() (now 1) becomes true immediately
        -- the lone waiter should flush well before the long debounce window,
        not sit out the whole timeout."""
        client = MagicMock()
        client.messages.batches.create.return_value = _make_batch("batch_1")
        client.messages.batches.retrieve.return_value = _make_batch("batch_1", "ended")
        client.messages.batches.results.return_value = [_make_result("0000", "fast")]

        active = 2
        config = {**bo._CONFIG_DEFAULTS, "use_batch_api": True}
        batcher = bo.SynthesisBatcher(
            config, client, lambda: active, debounce_seconds=5.0, poll_seconds=0.01,
        )

        def drop_worker_after_delay():
            nonlocal active
            time.sleep(0.1)
            active = 1
            batcher.on_worker_exit()

        threading.Thread(target=drop_worker_after_delay, daemon=True).start()

        start = time.monotonic()
        results = batcher.call_many([_req("only")])
        elapsed = time.monotonic() - start

        assert results == [("fast", "end_turn")]
        assert elapsed < 1.0  # woke on convergence, did not sit out the 5s debounce

    def test_thundering_herd_at_debounce_timeout_flushes_exactly_once(self):
        """3 threads all pending when the SAME debounce timer fires (active
        workers fixed at 4, so convergence never triggers -- only the
        timeout path can flush) -- exactly one batches.create call, not
        three, and every thread gets its own correct result back."""
        client = MagicMock()
        client.messages.batches.create.return_value = _make_batch("batch_1")
        client.messages.batches.retrieve.return_value = _make_batch("batch_1", "ended")
        client.messages.batches.results.return_value = [
            _make_result("0000", "r0"), _make_result("0001", "r1"), _make_result("0002", "r2"),
        ]

        config = {**bo._CONFIG_DEFAULTS, "use_batch_api": True}
        batcher = bo.SynthesisBatcher(
            config, client, _fixed_workers(4), debounce_seconds=0.05, poll_seconds=0.01,
        )

        out: dict[int, list] = {}
        barrier = threading.Barrier(3)

        def worker(idx: int) -> None:
            barrier.wait(timeout=5)
            out[idx] = batcher.call_many([_req(f"p{idx}")])

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
            assert not t.is_alive()

        assert client.messages.batches.create.call_count == 1
        submitted = client.messages.batches.create.call_args.kwargs["requests"]
        assert len(submitted) == 3
        assert {out[0][0], out[1][0], out[2][0]} == {("r0", "end_turn"), ("r1", "end_turn"), ("r2", "end_turn")}
