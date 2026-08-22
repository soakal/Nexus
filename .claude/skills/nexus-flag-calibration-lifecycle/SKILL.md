---
name: nexus-flag-calibration-lifecycle
description: Explain why an OutcomeFlag did or didn't page and what the calibration loop is actually doing to it — the record_flag_ex four-branch dedup, the honest-denominator FP rate, hysteresis and probation, and the headline trap that the nightly "Calibration: auto-suppressing" page is currently a lie because calibration_suppression_enabled is still False. Use when a flag pages that shouldn't, goes silent when it should page, or an "auto-suppressing" notification needs interpreting.
---

# The OutcomeFlag / calibration lifecycle

Spans three files — `backend/agents/outcomes.py` (the flag write path and `should_page` gate),
`backend/agents/calibration.py` (the nightly recompute), `backend/scheduler.py` (job
registration). Citations at `7aa3615`, 2026-08-22.

## The headline trap: "auto-suppressing" pages while suppressing nothing

The nightly recompute activates hints and fires one
`Calibration: auto-suppressing {fingerprint} (...)` phone page per newly-activated hint
(`calibration.py:327-334`) — with no check of `calibration_suppression_enabled` anywhere in that
path. Meanwhile `should_page` returns `(True, None)` — page anyway — whenever that flag is off
(`outcomes.py:369`), and it is off: `calibration_suppression_enabled: bool = False`, "THE behavior
change — off for the soak" (`backend/config.py:451-452`).

So today the page's wording is misleading: the hint is genuinely active, the measurement behind it
is real, but **nothing is actually being suppressed.** Two deliberately separate switches:

- `calibration_enabled` — measurement, harmless, default `True`.
- `calibration_suppression_enabled` — behavior, default `False`.

A one-shot `calibration_soak_reminder` `DateTrigger` fires 2026-09-05 09:00
(`scheduler.py:35`, registered `:1114-1121`) precisely because flipping the second flag — spec
§9.5 step 7 — is an ops decision, not a code step; `_calibration_soak_reminder`
(`scheduler.py:618`) deliberately does not flip it itself.

## `record_flag_ex`: the four-branch dedup

1. **Existing open/needs_follow_up row** → bump `surfaced_count`, return its id, no new row.
2. **Deferred, still within its window** → `{"id": None, "surface": False, "reason": "deferred"}`
   (`:441-445`).
3. **False positive, within the cooldown** (default 30 days, `:449`) → silent,
   `reason="false_positive_cooldown"` (`:446-451`).
4. **Else** → insert, race-safe via a unique-index re-SELECT on `ux_outcomeflag_open`.

A `should_page` "no" doesn't stop the write. The row is inserted and *then* stamped
`suppressed=True` + `suppressed_reason` (`:472`), and suppressed rows vanish from default
`open_flags()` reads — pass `include_suppressed=True` (`_db_open_flags`, `:222-235`) to see them.
**A "missing" flag may be sitting right there, suppressed.**

## The honest denominator, hysteresis, and stickiness

Verdict counting excludes rows whose `resolved_by` starts with `auto:` and excludes suppressed rows
(`calibration.py:160-161`, `:402-403`) — a flag that cleared itself isn't evidence of a false alarm.

| Value | Number |
|---|---|
| Activate threshold (`calibration_fp_threshold`) | 0.60 (`:107`) |
| Clear threshold (`calibration_clear_threshold`) | 0.40 (`:108`) |
| Mandatory re-probation after clearing | 30 days (`:198`, "§2.5(b) — unconditional") |
| Brian's un-suppress override, sticky for | 90 days (`calibration_override_days`, `:507`) |
| Measurement window | 30 days (`calibration_window_days`, `:105`) |

## Resolving a flag has side effects

`status="resolved"` is not just bookkeeping:

- For `homelab_watch` flags with an `expected:*` check, resolve syncs the `ExpectedResource`
  baseline to the observed state (`_maybe_sync_expected_resource`, `:513-541`) — "this is fine now,
  stop flagging it." Without this, the mismatch re-flags forever.
- For `source=="obligation"` flags, resolve confirms and advances the obligation (`:588-604`, gated
  on `status == "resolved"` at `:599`).

A `false_positive` or `deferred` resolution deliberately does neither. Picking the wrong close
status silently changes downstream behavior, not just the flag's own row.

## Fast triage

```bash
ssh -i ~/.ssh/id_ed25519 root@100.84.21.43 'cd /var/lib/nexus && PYTHONPATH=/opt/nexus /opt/nexus/venv/bin/python -' <<'PY'
from sqlmodel import Session, select
from backend.database import OutcomeFlag, engine
with Session(engine) as s:
    for f in s.exec(select(OutcomeFlag).where(OutcomeFlag.fingerprint.contains("YOUR_FINGERPRINT"))):
        print(f.id, f.status, "suppressed" if f.suppressed else "not-suppressed",
              f"surfaced={f.surfaced_count}", f.resolved_by, f.summary)
PY
```

(cwd matters — see `nexus-remote-python`.)

- Paged but "shouldn't have" → suppression is off for the soak, working exactly as configured.
- Got the "auto-suppressing" page → nothing is suppressed yet, it's a measurement announcement.
- Flag missing from `/flags` → query with `include_suppressed=True` before concluding it was never
  raised.
- Same flag re-pages after a Resolve → check whether it's an `expected:*` or obligation flag closed
  as `false_positive`/`deferred` instead of `resolved`.
- A recurring flag suddenly quiet with no hint active → check branches 2/3 of the dedup above
  (deferred window, FP cooldown) — that's `record_flag_ex`, not calibration.
