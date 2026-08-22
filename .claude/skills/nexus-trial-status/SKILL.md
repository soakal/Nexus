---
name: nexus-trial-status
description: Answer "how's the model trial going" without re-deriving trial_report.py's per-label comparator semantics every time — which labels are judged on JSON shape, which on text equality, which on a decision field, where the raw data lives, and which files must be edited in lockstep. Use when reading Trial A/B numbers, changing comparator logic, or explaining a disagreement rate.
---

# Reading the NEXUS model trial

Two unrelated trials share one report module (`backend/agents/trial_report.py`, gated by
`trial_report_enabled`, delivered as a **ProtonMail draft** — never sent):

- **Trial A** — `google/gemini-2.5-flash-lite` shadowing Haiku-tier classification calls. Real
  calls are unaffected; the shadow call is a fire-and-forget background task
  (`router._maybe_shadow` / `_run_shadow_call`) that can never add latency or a failure mode to
  the real response.
- **Trial B** — Gemini 2.5 Pro replacing Sonnet for the nightly brain-organizer wiki synthesis.
  Runs in a **subprocess** (`modules/brain-organizer/trial/`), which is why its spend never
  touches SpendLog.

Trial B was stopped 2026-08-22 (NO-GO, unrelated reasons). The mechanisms below are still live and
still what a future trial runs on.

## The per-label comparator table — the thing worth not re-deriving

There is no single "agreement rate". Each label is judged by whichever comparator is meaningful
for its output shape, and mixing them into one percentage is a bug that has already happened once.

| Label | Comparator | Harmful direction |
|---|---|---|
| `mail_junk_classify` | case-insensitive text equality | real `KEEP`, shadow `JUNK` (trashes real mail) |
| `mail_reply_classify` | case-insensitive text equality | real `NO`, shadow `YES` |
| `action_judge` | **decision field** `allow` (bool) | real `allow:false`, shadow `allow:true` (lets something through) |
| `goal_criteria_eval` | **decision field** `met` (bool) | none defined |
| `facts_extract` | JSON parseability **+ top-level key set** | n/a |
| `goal_proposer` | JSON parseability **+ top-level key set** | n/a |

**The decision-field comparator is new as of 2026-08-22.** Before that, `action_judge` and
`goal_criteria_eval` were compared by raw text equality. Their schemas are
`{"allow": bool, "confidence": float, "reason": str}` and `{"met": bool, ...}` — the free-text
`reason` is never phrased identically by two models, so those labels logged ~100% disagreement by
construction, and that fiction was blended into the digest's single headline number. It reported
~41% where the real mail-classifier rate — the only thing GO/NO-GO criterion A#1 is about — was
~18%. If you see a historical ~41% figure quoted anywhere, that is what it was.

Two consequences that still matter:

1. **Rows logged before 2026-08-22 carry a wrong `agree` field** for those two labels. Both
   readers therefore **recompute** agreement from `out_a`/`out_b` for decision-shaped labels
   instead of trusting the logged `agree`. Don't "simplify" that back to reading the field.
2. `_HARMFUL_DIRECTION["action_judge"]` used to grep for the literal string `"veto"`. The judge
   never emits that word — `"veto"` only appears in the *derived* verdict the broker writes to
   `ActionLog`. So it scored zero harmful rows no matter what happened. It now parses `allow`.

The shape half of the JSON comparator is also new (same day): criterion A#3 says "parseable JSON
of the expected shape ≥99%" and both readers only ever called `json.loads`. A shadow model
returning immaculate JSON with entirely different field names scored a clean pass. Shape = the
top-level key set; for the array-valued labels that's the **first element's** keys (the array
itself has none and its length legitimately varies), and an empty array's shape is the empty set.
This immediately found something real: `goal_proposer` is 100% parseable and only ~40% same-shape.

## Where everything lives

- **`/var/lib/nexus/logs/shadow.jsonl`** — one JSON object per shadow call: `ts`, `label`,
  `model_a`/`model_b`, `prompt` (4000 chars), `out_a`/`out_b`, `agree`, `latency_ms`. `out_a`/
  `out_b` are truncated at **8000** chars as of commit `376e7ac`; the previous 2000 cut long
  `facts_extract`/`goal_proposer` arrays mid-array, which then read as a parse failure for *both*
  models even though the output was well-formed. 8000 comfortably covers the shadow call's own
  `max_tokens=4096`.
- **Shadow spend** is metered normally under labels prefixed `shadow:` — so a forgotten trial is
  visible in the daily spend report even if nobody reads the log.
- **`/var/lib/nexus/brain-trial/nights/YYYY-MM-DD/`** — Trial B, one dir per night:
  `organizer.log`, `diff-trial.txt` (delta against a *frozen* baseline, not the live vault),
  `census.md`, and `rc`.
- **`/var/lib/nexus/brain-trial/credits-start.json`** — the OpenRouter `total_usage` stamp that
  makes criterion B#4 measurable (added 2026-08-22). Written once, never overwritten; the delta
  against a live `GET /api/v1/credits` read is the cost number. Absent = criterion **UNMEASURED**,
  which must never be scored as passed.
- **`/var/lib/nexus/logs/verdict-A.md` / `verdict-B.md`** — the end-of-trial verdict markers.

## Two conventions that have each burned someone

**Trial B night status comes from the `rc` file, never from grepping the log.** `run-trial.sh`
writes its own exit code to `rc`. A tail-grep for `Traceback` always found the script's own
trailing `exited N` line first (it lands last), never the actual traceback — so status read
"unknown" on genuinely failed nights. `rc` is authoritative; the log is only for the failure
detail line.

**Deleting a verdict marker re-fires a paid Opus call.** `send_trial_verdict` treats the marker as
its idempotency record: marker exists → no-op. The marker is written **before** the draft is saved,
so a failed draft still archives the verdict rather than paying twice. A `BudgetExceeded` verdict
call deliberately does *not* write the marker, so it retries tomorrow. Don't `rm` a marker to
"re-run the report" — you are re-running one Opus call per deletion.

## The duplication rule — edit both files, every time

`backend/agents/trial_report.py` and `tools/shadow_diff.py` **deliberately duplicate**
`_HARMFUL_DIRECTION`, `_FENCE_RE`, `_parseable`/`_loads`/`_shape`/`_same_shape`, and the
decision-field comparator. `tools/` never imports `backend/` (same discipline as
`tools/cleanup_calibration_contamination.py`), so there is no shared helper to reach for. Both
files carry comments saying so. Every relevant commit has touched both together —
`219eeb5` (fence stripping), and the 2026-08-22 comparator and shape commits. Keep the precedent:
if you change comparator logic in one, change it in the other in the same commit.

(Inside `backend/`, `trial_report.py` *does* import `router.shadow_agree`/`shadow_decision` rather
than re-implementing them — the duplication rule is a `tools/` boundary, not a general licence.)

## Getting the numbers

Fastest read, no backend import needed (stdlib only, safe to run from anywhere):

```bash
ssh -i ~/.ssh/id_ed25519 root@100.84.21.43 '/opt/nexus/venv/bin/python /opt/nexus/tools/shadow_diff.py'
```

Healthy output looks like (live, 2026-08-22):

```
284 shadow calls across 6 labels (/var/lib/nexus/logs/shadow.jsonl)

action_judge             n=  74  disagreed= 30 (40.5%)  <-- 30 HARMFUL  [decision-field comparator]
facts_extract            n=  12  shadow JSON-parseable: 9/12 (75%)  same key set: 9/12 (75%)
goal_criteria_eval       n=   8  disagreed=  0 (0.0%)  [decision-field comparator]
goal_proposer            n=  15  shadow JSON-parseable: 15/15 (100%)  same key set: 6/15 (40%)
mail_junk_classify       n= 116  disagreed= 22 (19.0%)
mail_reply_classify      n=  59  disagreed=  1 (1.7%)
```

Read it as: **criterion A#1 is the mail rows only** (19.0% / 1.7% against a ≤2% bar — `mail_junk`
is the problem). The `[decision-field comparator]` tag marks the labels that are reported
separately and are *not* part of that bar. `action_judge`'s 30 HARMFUL rows are the shadow model
approving actions the real judge vetoed — real signal that the old `"veto"` grep reported as zero.

For the rendered digest section rather than the raw table, build it directly (see
`nexus-remote-python` for the cwd trap — this one genuinely needs the live DB for the cost line):

```bash
ssh -i ~/.ssh/id_ed25519 root@100.84.21.43 'cd /var/lib/nexus && PYTHONPATH=/opt/nexus /opt/nexus/venv/bin/python -' <<'PY'
import asyncio
from backend.agents import trial_report
print(asyncio.run(trial_report.build_trial_report_text()))
PY
```
