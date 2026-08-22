---
name: nexus-action-trace
description: Answer "why did NEXUS do that" or "why didn't it" for a side-effecting action — the real gate order in broker.execute_action with file:line at each stage, what each decision value means, and the fail-safe-veto trap that makes a judge outage look identical to a real veto in the log. Use when an automation fired unexpectedly, silently didn't fire, or is stuck waiting on confirmation.
---

# Tracing a NEXUS action

Every side effect goes through one function: `backend/safety/broker.py::execute_action`
(`:821`). There is no second path — if something happened, it has an `ActionLog` row; if it has no
row, `execute_action` was never called and the bug is upstream in whatever should have called it.

## The real gate order

Stages run in this order, and the **first one that refuses wins**. Line numbers are as of
2026-08-22.

1. **Payload serializability** — `broker.py:838`. A non-JSON-serializable payload `raise`s
   `ValueError` *before any DB write*. This is the one failure with no `ActionLog` row, on purpose:
   it's a programming error, not a policy outcome.
2. **Kill switch** — `broker.py:868-910`. For `AGENT`/`AUTONOMOUS` actors only, reads
   `governor.get_system_state()`; if `autonomy_enabled` is false the action is logged
   `forbidden` with reason `autonomy_disabled` and stops. `USER` actions bypass this entirely.
3. **Classification** — `classify(kind, payload)`, `broker.py:115`, called at `:912`. Pure
   function, maps kind+payload to `(Risk, Reversibility)`. **Fails closed:** an unrecognised HA
   domain returns `HIGH/UNKNOWN`, not `MEDIUM`, precisely so an unlisted domain can't be
   auto-allowed for a non-user actor.
4. **Policy decision** — `decide(...)`, `broker.py:~240-290`, called at `:913`. Order inside:
   `USER` → always allowed; `forbid` list → forbidden; **irreversible → forbidden unless
   `confirmed`** (checked before risk, and auto-allow cannot reach it); `auto_allow` list;
   `HIGH`/`UNCLASSIFIABLE` → `needs_confirm`; otherwise allowed. Agent `MEDIUM` is permitted.
5. **ActionLog row written** — `broker.py:924`, *before* dispatch, so the intent survives a crash
   between decision and side effect.
6. **Return early** if the decision is `forbidden`/`needs_confirm` (`:936`). Nothing dispatched.
   Only `needs_confirm` sends a phone alert with Confirm/Reject buttons — `forbidden` is silent.
7. **Throttle / circuit breaker** — `throttle.allow(...)`, `backend/safety/throttle.py:25`, called
   at `broker.py:963`. `AGENT`/`AUTONOMOUS` only. Flips the already-written `allowed` row to
   `forbidden` in place and pushes a "NEXUS blocked '<kind>'" notification.
8. **Action judge** — `broker.py:996-1049`, calling `judge.evaluate_action`. Runs *after* the
   throttle passes and *before* the attempt is recorded against the throttle window, so a veto
   never counts against the actor's rate cap. Skipped for `confirmed=True`, for kinds in
   `action_judge_exempt_kinds` (default `{"send_notification"}`), and when
   `action_judge_mode == "off"`.
9. **`throttle.record_attempt`**, then **dispatch** via `_DISPATCHERS[kind]` → the row's final
   decision becomes `executed` or `failed`.

### Not in this pipeline, despite the names

- **`backend/safety/contracts.py`** is *not* a policy classifier. It's the integration
  response-shape canary ("still 200 OK, but quietly wrong"), read on a schedule by
  `watchdog.check_integration_contracts`. It has nothing to do with action gating. Don't start a
  trace there.
- **`governor.check_budget`** (`governor.py:528`) gates **LLM spend**, not action dispatch.
  `execute_action` touches the governor only for `get_system_state` (kill switch + the
  `policy_auto_allow_kinds`/`policy_forbid_kinds` sets). A budget problem reaches an action
  indirectly, via the judge — see below.

## The mode question: shadow blocks nothing

`settings.action_judge_mode` is **`"shadow"`** and has been since the judge shipped. In shadow the
verdict is written to the row (`judge_verdict`, `judge_reason`) and dispatch proceeds anyway. Only
`"enforce"` flips the row to `needs_confirm` and pages for approval.

So: **"the judge vetoed it" is not, today, a reason anything was blocked.** If an action didn't
happen and the row says `judge_verdict: veto`, the veto is a red herring — look at stages 2, 4,
and 7. As of 2026-08-22 the daily homelab digest carries an `Action judge` section aggregating
these shadow verdicts (`homelab_digest._section_judge` → `judge.verdict_summary`), which is where
to look for "what would enforce mode have blocked".

## The high-value trap: a fail-safe veto is indistinguishable from a real one

`judge.evaluate_action` **never raises**. Every failure path — timeout
(`action_judge_timeout_s`, default 20s), `BudgetExceeded`, an unparseable model response, any
unexpected exception — returns:

```python
{"allow": False, "confidence": 0.0, "reason": "...", "verdict": "error"}
```

That is a *fail-safe veto*: in enforce mode it would block exactly like a considered veto. The
`verdict` field distinguishes them (`"veto"` = the model's actual opinion, `"error"` = the judge
was down), but `allow: False` is identical, and a summary that sums the two tells you the opposite
of the truth about your automation — "12 actions would have been blocked" reading as
model-disagreement when it was 12 judge timeouts.

**So "why was this blocked?" always needs a judge-health check, not just the log.** Count
`verdict="error"` rows over the same window before concluding anything about the judge's opinions.
`verdict_summary` reports the two separately for this reason; keep them separate.

Two related smells worth knowing: a burst of `error` verdicts usually means `BudgetExceeded`
(check the daily spend against `daily_budget_usd`) or the model endpoint being slow, not a policy
change. And `judge_reason` is app-capped at 300 chars at write time.

## Where to pull evidence

**The ActionLog endpoint** — `backend/api/safety.py:68`. Bearer-authed; returns `actor`, `kind`,
`target`, `payload`, `risk`, `reversibility`, `decision`, `result`, `judge_verdict`,
`judge_reason`, `created_at`/`updated_at`, and `confirmed_at`.

`confirmed_at` is worth understanding: it's stamped at the **top** of `confirm_action`, before the
TTL check and dispatch, so `confirmed_at - created_at` is pure human reaction time. It's also the
only thing that distinguishes `allowed→executed` from `needs_confirm→confirmed→executed`, whose
rows are otherwise identical. A row that stamps it and *then* expires still means something ("he
tapped, too late") and is deliberately not cleared.

```bash
ssh -i ~/.ssh/id_ed25519 root@100.84.21.43 'cd /var/lib/nexus
KEY=$(/opt/nexus/venv/bin/python -c "import sys; sys.path.insert(0,\"/opt/nexus\");
from backend.secrets.manager import get_secret; print(get_secret(\"NEXUS_API_KEY\"))" | tail -1)
curl -s -H "Authorization: Bearer $KEY" "http://127.0.0.1:8000/api/safety/actions?limit=20"' | jq .
```

**`/api/safety/status`** — same auth. `autonomy_enabled` (stage 2), `today_spend_usd` vs
`daily_budget_usd` (the `error`-verdict explanation), `scheduler_running`, and `running_sha`. A
`running_sha` mismatch against `git -C /opt/nexus rev-parse HEAD` means the fix you're looking at
in the source **is not the code that ran**.

**The Safety page's judge badge** — `frontend/src/pages/Safety.jsx:1341`. Each action row renders
a `judge: <verdict>` badge next to the decision badge whenever `judge_verdict` is non-null, and the
judge reason gets a full-width **wrapping** line, never ellipsis-truncated — deliberately, because
shadow-mode review depends on reading the whole reason on a phone.

**Direct DB**, when you need to aggregate: see `nexus-remote-python` (and its cwd trap — the wrong
cwd returns zero rows and no error, which reads exactly like "the action never happened").

## Fast triage

- **No `ActionLog` row at all** → `execute_action` was never called. Look at the caller (a task
  step, a scheduler job, a Telegram handler), not the broker.
- **`forbidden`, `result.reason == "autonomy_disabled"`** → stage 2, kill switch. Someone paused
  autonomy.
- **`forbidden`, reason mentions a rate/window** → stage 7, throttle or a tripped circuit breaker.
- **`forbidden`, irreversible kind, no confirmation** → stage 4. By design; `protonmail_send` and
  friends are unpromotable by construction, not by policy.
- **`needs_confirm`, still sitting there** → the phone notification went out and nobody tapped;
  check the confirm TTL (`action_confirm_ttl_seconds`).
- **`executed` but nothing visibly happened** → the gate is not your problem. Read `result_json`;
  the dispatcher succeeded or recorded its own error.
