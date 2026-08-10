# Spec — Traces detail discoverability + Pulse detail wiring

Branch `feat/traces-pulse-detail`, worktree `C:\Users\Brian\Documents\Agentic os\nexus-traces-pulse-detail`.
Planner: Fable. Writer implements from this spec alone. Scope is EXACTLY: `frontend/src/pages/Traces.jsx`,
`frontend/src/pages/Pulse.jsx`, `backend/agents/worker_pool.py`, `backend/agents/router.py`, plus tests.
`backend/activity.py` itself needs **zero changes** (verified: `detail` is already serialized by
`asdict()` in both `snapshot()` and `drain_dirty()`, and `GET /api/activity` / the WS snapshot both
return `snapshot()` verbatim — no wire-format work anywhere in this batch).

## Design decisions (read before implementing)

**Traces — option (b), preview-on-collapsed-row, NOT auto-expand.** Reasoning: option (a)
(auto-expanding every `hasDetail` span's input/output block when a trace is expanded) unrolls up to
1000 chars of monospace text per span × N spans — a 5-span chat trace becomes a wall of text, which
is exactly the noise the constraint forbids. Option (b) adds ONE dim, single-line, CSS-truncated
preview per span row: the reader can *see that content exists and what it starts with* at a glance,
which is the actual discoverability failure ("detail is two clicks deep and the chevron is easy to
miss"), while click-to-expand keeps carrying the full text. A 10-span trace gains 10 short dim
lines — scannable, not noisy. The "thin traces at the top" half of the complaint is deliberately
NOT addressed with a filter: `span_count` is already shown on every collapsed row ("no spans" /
"N spans"), and hiding 0-span traces would hide real activity (proposer ticks, /status turns).
Listed as an open question for Brian instead.

**Pulse — per actor type:**
- `worker:{id}` — YES, two changes. The prompt is already in `label` at `begin()` time
  (`worker_pool.py:400`) but board cards never render `label`; fix is mostly frontend. Backend adds
  one `update_detail` call carrying `task_id` so the card can say *which* task, not just its prompt.
- `trace:{kind}:{id}` — YES, minimal. A web-search chat turn runs 20–40s; today its Now Running row
  is just a label + elapsed timer. `router._record_trace_span` already fires on every span
  completion and `router._open_trace_kinds` already maps trace_id→kind, so attaching
  "last completed span" detail to the trace's board entry is a 6-line keyed `update_detail` — it
  shows "classify done (840ms), now answering" progression during long turns. We do NOT attempt
  span-*start* detail (spans are recorded at completion; adding start-time wiring would touch every
  call site for sub-second visibility — fails the Pulse design principle).
- `job:{id}` — NO, skipped. The APScheduler event object carries nothing beyond
  `job_id`/`exception` (verified, `scheduler.py:525-541`), the runs are milliseconds, and
  "last ran Xm ago · Yms · OK/ERROR + last_error" is already the complete story. Adding detail
  would require touching ~29 job functions individually, breaking the one-listener choke-point
  design.
- The Now Running task progress bar (`Pulse.jsx:227-240`) is untouched.

**Detail shapes** (each actor type keeps its own; frontend gates on `actor_type` + field presence,
never on shape alone):
- `task`: `{step_index, total_steps, description}` — existing, untouched.
- `worker`: `{"task_id": <int>}` — new.
- `trace`: `{"last_span": <str ≤200>, "span_type": <str>, "duration_ms": <int|null>}` — new.

---

## 1. Traces.jsx — collapsed span rows get a one-line content preview

**File:** `frontend/src/pages/Traces.jsx`, inside the span map (currently lines ~261-335).

**Change:** In the span row, after the `{s.error && ...}` block and before the `{isOpen && ...}`
block, add: when `hasDetail && !isOpen`, render a full-width single-line preview span:

```jsx
{hasDetail && !isOpen && (
  <span style={{
    flexBasis: '100%', fontSize: '11px', color: '#5d6982',
    fontFamily: "'JetBrains Mono', monospace",
    overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
  }}>
    {(s.input_summary || s.output_summary).replace(/\s+/g, ' ').slice(0, 160)}
  </span>
)}
```

Notes: `replace(/\s+/g, ' ')` collapses newlines (summaries are often multi-line) so the CSS
ellipsis works; `.slice(0, 160)` bounds the DOM text, CSS does the visual truncation at any
viewport width. The preview uses `input_summary` first, falling back to `output_summary` — same
precedence the expanded view implies. Keep the chevron, click handler, and expanded block exactly
as they are.

**Acceptance criteria:**
1. A collapsed span row with `input_summary` and/or `output_summary` shows a dim one-line preview;
   opening the row (click) hides the preview and shows the existing full Input/Output blocks;
   collapsing restores the preview.
2. A span with neither summary renders exactly as before (no empty preview line, cursor stays
   `default`).
3. No other behavior in Traces.jsx changes (search, filters, trace expand, span totals strip).
4. `cd frontend && npm run build` succeeds.

## 2. worker_pool.py — worker entries carry the picked-up task id

**File:** `backend/agents/worker_pool.py`, `_worker_loop`, the existing `try` block at ~398-403.

**Change:** Immediately after the existing `activity.begin(f"worker:{worker_id}", "worker",
prompt[:200])` line, inside the SAME try/except, add:

```python
activity.update_detail(f"worker:{worker_id}", {"task_id": task_id})
```

Do NOT clear detail at `end()` — `end()` doesn't touch detail today and the frontend gates the
task-id chip on `status === 'running'` (item 4). A stale `task_id` on an idle entry is invisible
by construction.

**Acceptance criteria:**
1. New test in `tests/test_activity_wiring.py` (extend the existing worker busy/idle transition
   test pattern with a stubbed `run_task`): after a worker picks up task N,
   `activity.snapshot()`'s `worker:{id}` entry has `detail == {"task_id": N}` and
   `label == prompt[:200]`; after the stubbed task finishes, status is `ok`/`error` per the stub
   and the test does not assert detail is cleared (it persists, by design).
2. The `prompt is None` early-`continue` path (task row deleted before pickup) still never calls
   `begin`/`update_detail` — existing regression tests stay green.
3. A poisoned `activity.update_detail` (monkeypatched to raise) does not prevent the task from
   running — covered by it living inside the existing try/except; add one assertion to the test
   above or reuse the existing poisoned-mutator pattern.

## 3. router.py — trace entries carry the last completed span

**File:** `backend/agents/router.py`, `_record_trace_span`, between the existing ticker-pulse
try/except (ends ~line 417) and the `if trace_id is None: return` line (~419).

**Change:** Add a second, independent best-effort block:

```python
try:
    if trace_id is not None:
        _kind = _open_trace_kinds.get(trace_id)
        if _kind is not None:
            from backend import activity
            activity.update_detail(f"trace:{_kind}:{trace_id}", {
                "last_span": str(name)[:200],
                "span_type": span_type,
                "duration_ms": duration_ms,
            })
except Exception:
    pass
```

Why this is safe/correct (Writer: preserve these properties, don't "improve" them away):
- `_record_trace_span` runs in a `run_in_executor` worker thread; `activity.update_detail` is
  sync, lock-guarded, and never raises — safe from a non-loop thread, per `activity.py`'s module
  docstring. Reading the module-level `_open_trace_kinds` dict from the thread is fine (CPython
  dict read, no iteration).
- Orchestrator trace_ids are NOT in `_open_trace_kinds` (only `router.open_trace` populates it, and
  the orchestrator deliberately has no `trace:*` board entry — `task:{id}` carries its detail).
  The `.get()` miss makes this a silent no-op there, which is exactly right: it can never clobber
  the task progress-bar detail.
- `update_detail` is a no-op for an already-closed trace (entry removed by `close_trace`) — a span
  recorded after close (racy finalization) mutates nothing.
- Must be its own try/except, separate from both the pulse block and the DB write: a failure here
  can neither suppress the ticker pulse nor the `TraceSpan` insert.

**Acceptance criteria:**
1. New test: open a trace via `router.open_trace(kind="chat", label=...)`, call
   `_record_trace_span` with that trace_id and a name/duration — `activity.snapshot()` shows the
   `trace:chat:{id}` entry with `detail == {"last_span": ..., "span_type": ..., "duration_ms": ...}`.
   After `close_trace`, the entry is gone (existing behavior, assert it to pin the no-clobber
   property).
2. `_record_trace_span` with `trace_id=None` and with a trace_id not present in
   `_open_trace_kinds` (e.g. `999999`) mutates no activity entry and does not raise.
3. Constraint test (mirror `test_activity_wiring.py`'s existing poisoned-pulse test): monkeypatch
   `activity.update_detail` to raise — the `TraceSpan` DB row is still written and the ticker
   pulse still fires.
4. `str(name)[:200]` truncation is asserted for a >200-char name.

## 4. Pulse.jsx — render slots for the new detail

**File:** `frontend/src/pages/Pulse.jsx`.

**Change A — Actors board cards (block at ~262-285), two additions per card:**

1. In the status line (the `fontSize: 11` div at ~273-279), for the running case only, append the
   task id when present:

```jsx
{e.status === 'running'
  ? `running${e.actor_type === 'worker' && e.detail?.task_id ? ` · task #${e.detail.task_id}` : ''} · ${fmtElapsed(e.started_at, nowTick)}`
  : /* unchanged idle/last-ran branch */}
```

2. After the status line (before the `last_error` block), a label line — rendered only when the
   label exists AND differs from the stripped actor id (job cards' label IS the job id, so this
   rule keeps them unchanged while worker cards gain their prompt):

```jsx
{e.label && e.label !== e.actor_id.replace(/^(job|worker|loop|task):/, '') ? (
  <div style={{ fontSize: '11px', color: '#8a96ad', marginTop: '3px', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
    {e.label}
  </div>
) : null}
```

An idle worker's card thus reads: status dot + `0`, "last ran 2m ago · 34.5s · OK", then the last
prompt it worked on — the label is already sitting in the entry, this just stops dropping it.

**Change B — Now Running strip (block at ~220-246):** after the existing task-progress-bar
conditional (`{e.actor_type === 'task' && ... : null}`), add a sibling conditional for trace
entries:

```jsx
{e.actor_type === 'trace' && e.detail?.last_span ? (
  <div style={{ marginTop: '4px', fontSize: '11px', color: '#8a96ad', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
    last: {e.detail.last_span}{e.detail.duration_ms != null ? ` · ${fmtMs(e.detail.duration_ms)}` : ''}
  </div>
) : null}
```

Do NOT touch the task progress bar itself, the grouping logic, the `trace`-has-no-board-card
exclusion (line ~173), or the ticker.

**Acceptance criteria:**
1. A running worker card shows `running · task #N · Xs`; an idle worker card shows its last prompt
   as a truncated single line; job cards render byte-identically to before (label === stripped id).
2. A running trace row in Now Running with `detail.last_span` shows the "last: …" line; a trace
   entry with empty detail (`{}` — the state before its first span completes) renders exactly as
   today.
3. A `task` entry's progress bar renders exactly as today (no regression from the sibling
   conditional).
4. All new text nodes are single-line CSS-truncated (`overflow hidden / textOverflow ellipsis /
   whiteSpace nowrap`) — nothing can wrap a card or row at 375px width.
5. `cd frontend && npm run build` succeeds.

## 5. Bookkeeping

1. Full backend suite green: `pytest` (baseline on this branch's master parent: 1904 passed,
   1 skipped, 3 known-unrelated failures — the two hardcoded scheduler-job-count asserts and the
   time-of-day-flaky proposer test documented in CLAUDE.md; no NEW failures allowed).
2. `npm run build` clean.
3. Add a short dated entry to this repo's `CLAUDE.md` describing the batch (follow the existing
   entry style: what/why/files, note the "no serialization change needed — asdict already carries
   detail" finding so nobody re-investigates it).
4. No new DB tables/columns, no changes to `backend/activity.py`, no new dependencies, no changes
   outside the five files named in this spec plus tests + CLAUDE.md.

## Explicitly out of scope
- Any `backend/activity.py` mutation-contract change (mutators already never raise; nothing here
  relaxes or needs to extend that).
- Job (`job:{id}`) detail — skipped with reasoning above.
- Loop entries (`loop:memo_watcher`, `loop:telegram_poller`) — they are never `begin()`'d, so they
  have no board entries at all today ("Loops" group never renders). Pre-existing, observed while
  planning, deliberately not fixed here — flagged as an open question.
- Traces list filtering/sorting by span count.
- The `/ws/logs` auth follow-up flagged in the original Pulse build.

## Open questions for Brian (none block implementation)
1. Traces: want an optional "has spans" filter toggle to push 0-span proposer/status traces out of
   the default view? (Deliberately not built here — `span_count` is already visible per row.)
2. Pulse: the "Loops" actor group never renders because loop actors only `pulse()` and are never
   `begin()`'d — worth a tiny follow-up (`begin`/`end` around memo processing + a poller heartbeat
   entry), or is the ticker enough for those?
3. Idle worker cards will now show the last task's prompt text (first ~1 line). Fine, or prefer it
   only while running?
