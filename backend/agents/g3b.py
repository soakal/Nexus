"""G3b alert-grouping runtime: bounded pre-send work + fire-and-forget post-send work.

Plan: .graph-plan-2026-10-30/g3/plan-v12-g3b.md §5.3.3, §5.3.7, §5.3.8. Nothing here runs unless
`correlate.enabled()` is true (or, for release pages, leftover grouped rows exist).

- Pre-send jobs (the grouping decision, the V1 send gate) run on a dedicated 2-thread pool with an
  admission cap, awaited under ONE elapsed-time budget per page; on expiry the caller fails open
  (the page goes out exactly as today) and late writes are compensated after the worker returns.
- Post-send jobs (stamps, give-backs, compensations) go to one daemon thread through a bounded queue
  and are never awaited, so bookkeeping can never delay a page.
- Every DB statement uses a short-busy-timeout sqlite3 connection, never the shared engine (30 s).
"""
from __future__ import annotations

import asyncio
import logging
import queue
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any

logger = logging.getLogger(__name__)

PRESEND_BUDGET_S = 1.0
GATE_BUSY_S = 1.0
ADMISSION_CAP = 8
POST_QUEUE_MAX = 256

_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="g3b")
_inflight = 0
_inflight_lock = threading.Lock()
_shutting_down = False


class FailOpen(Exception):
    """Pre-send G3b work did not (or could not) finish within its budget: send as today."""


class AbandonToken:
    def __init__(self) -> None:
        self.op = uuid.uuid4().hex
        self._ev = threading.Event()

    def abandon(self) -> None:
        self._ev.set()

    def is_set(self) -> bool:
        return self._ev.is_set()


def gate_conn() -> sqlite3.Connection:
    """Short-timeout autocommit connection to the live DB file (tests monkeypatch this)."""
    import backend.database as bd

    c = sqlite3.connect(str(bd.DB_PATH), timeout=GATE_BUSY_S, isolation_level=None, check_same_thread=False)
    c.execute(f"PRAGMA busy_timeout = {int(GATE_BUSY_S * 1000)}")
    c.row_factory = sqlite3.Row
    return c


def deadline_from_now() -> float:
    return time.monotonic() + PRESEND_BUDGET_S


def db_ts(dt) -> str:
    """The text form SQLAlchemy stores a SQLite DATETIME in (always 6 fractional digits): string order == time order."""
    return dt.strftime("%Y-%m-%d %H:%M:%S.%f")


def _admit() -> bool:
    global _inflight
    with _inflight_lock:
        if _shutting_down or _inflight >= ADMISSION_CAP:
            return False
        _inflight += 1
        return True


def _release(_f: Any = None) -> None:
    global _inflight
    with _inflight_lock:
        _inflight = max(0, _inflight - 1)


async def run_presend(fn: Callable, *args, deadline: float, on_late: Callable[[Any], None] | None = None) -> Any:
    """Run fn(*args, token) on the G3b pool within the remaining budget. Raises FailOpen on an expired
    budget, admission refusal, submit failure, timeout or error. On timeout/error after submission the
    token is abandoned and `on_late(result)` runs once the worker really returns (concurrent future)."""
    remaining = deadline - time.monotonic()
    token = AbandonToken()  # built before admission: nothing between admit and submit can raise and leak a slot
    if remaining <= 0 or not _admit():
        raise FailOpen("budget expired or admission refused")
    try:
        cf: Future = _POOL.submit(fn, *args, token)
    except BaseException as e:  # shutdown, interpreter teardown
        _release()
        raise FailOpen(f"submit failed: {e!r}") from e
    cf.add_done_callback(_release)

    def _abandon() -> None:
        token.abandon()
        if on_late is not None:
            def _late(f: Future) -> None:
                try:
                    res = f.result()
                except BaseException:
                    return  # the worker failed or was cancelled before running: it committed nothing
                try:
                    on_late(res)
                except Exception as ex:
                    logger.warning(f"g3b late-completion handler failed: {ex}")
            cf.add_done_callback(_late)

    try:
        return await asyncio.wait_for(asyncio.shield(asyncio.wrap_future(cf)), timeout=remaining)
    except asyncio.CancelledError:
        _abandon()  # the caller went away: the worker may still commit, so compensate after it returns
        raise
    except Exception as e:
        _abandon()
        raise FailOpen(f"pre-send work did not finish: {e!r}") from e


def restart() -> None:
    """App startup (and tests sharing one process): a fresh pool, admission open."""
    global _POOL, _shutting_down, _inflight
    with _inflight_lock:
        if _shutting_down:
            _POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="g3b")
        _shutting_down = False
        _inflight = 0


def shutdown() -> None:
    """Lifespan shutdown: refuse new pre-send work and drop queued jobs (never waits)."""
    global _shutting_down
    _shutting_down = True
    try:
        _POOL.shutdown(wait=False, cancel_futures=True)
    except Exception:
        pass


# ------------------------------------------------------------------ post-send work (never awaited)

_post_q: queue.Queue = queue.Queue(maxsize=POST_QUEUE_MAX)
_post_keys: set = set()
_post_lock = threading.Lock()
_post_thread: threading.Thread | None = None
_post_busy = 0
post_drops = 0


def _post_worker() -> None:
    global _post_busy
    while True:
        key, fn = _post_q.get()
        with _post_lock:
            _post_keys.discard(key)
            _post_busy += 1
        try:
            fn()
        except Exception as e:
            logger.warning(f"g3b post-send job {key!r} failed: {e}")
        finally:
            with _post_lock:
                _post_busy -= 1


def post(key: tuple, fn: Callable[[], None]) -> None:
    """Fire-and-forget. Identical keys coalesce; a full queue drops the job (counted). Never raises."""
    global _post_thread, post_drops
    try:
        with _post_lock:
            if key in _post_keys:
                return
            if _post_thread is None or not _post_thread.is_alive():
                _post_thread = threading.Thread(target=_post_worker, name="g3b-post", daemon=True)
                _post_thread.start()
            _post_q.put_nowait((key, fn))
            _post_keys.add(key)
    except Exception as e:
        post_drops += 1
        logger.warning(f"g3b post-send job {key!r} dropped: {e!r}")


def drain_post(timeout: float = 5.0) -> bool:
    """Test helper: wait until the post queue is empty and idle."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        with _post_lock:
            if not _post_keys and _post_q.empty() and _post_busy == 0:
                return True
        time.sleep(0.01)
    return False
