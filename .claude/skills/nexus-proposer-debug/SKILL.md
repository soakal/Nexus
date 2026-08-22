---
name: nexus-proposer-debug
description: Explain a specific goal NEXUS proposed, or should have proposed and didn't — the context blocks assembled in proposer.py and each one's bound, every suppression mechanism (permanent rejection, fuzzy re-proposal matching, fingerprint debounce, night gate, hardcoded porch-light exclusion) and how each fails. Use when a goal keeps coming back, never appears, or looks like it came from nowhere.
---

# Debugging the goal proposer

`backend/agents/proposer.py::propose_goals_tick` runs every 6h. It assembles a context prompt,
asks Haiku for a JSON array of goals, then runs each proposal through deterministic filters before
`goals.propose()` ever sees it. Almost every "why did/didn't it propose X" question is answered by
either **a context block that was empty** or **a suppression that fired** — rarely by the model.

## Step 0, before theorising: read the goal's actual row

Do this first. It decides which half of this document you're in.

```bash
ssh -i ~/.ssh/id_ed25519 root@100.84.21.43 'cd /var/lib/nexus && PYTHONPATH=/opt/nexus /opt/nexus/venv/bin/python -' <<'PY'
from sqlmodel import Session, select
from backend.database import Goal, engine
with Session(engine) as s:
    for g in s.exec(select(Goal).order_by(Goal.id.desc()).limit(15)):
        print(f"{g.id:4d} {g.status:10s} perm={int(bool(g.permanently_rejected))} "
              f"fp={g.fingerprint} {g.title!r}")
        if g.rejection_reason:
            print(f"       reason: {g.rejection_reason}")
PY
```

(cwd matters — see `nexus-remote-python`. From the wrong directory this prints nothing and looks
like "the goal was never created".)

`rejection_reason` is the highest-value field. `expired: ...` means **nobody rejected it** — it
just went unanswered and got swept. That distinction is load-bearing (see below).

## The context blocks and their bounds

All assembled in `propose_goals_tick`, roughly `proposer.py:290-510`. Every enrichment gather is
best-effort: a failure degrades that block to `(none)` and never aborts the tick. **A silently
empty block is the most common cause of a missing goal** — if `UPTIME ANOMALIES` is empty because
the uptime query failed, the model cannot propose an investigation it was never told about.

| Block | Source | Bound |
|---|---|---|
| `LIVE STATE` | `_build_snapshot` over HA/Unraid/Channels/AdGuard/weather (`asyncio.gather`, `return_exceptions=True`) | whole snapshot; a failed integration degrades its slice only |
| `HA ENTITY STATES` | `_ha_entity_summary(ha)` | the `WATCH` entity map (lights, locks, covers) |
| `ALREADY-OPEN GOALS` | `goals._db_list_goals(None, 25)`, filtered to proposed/approved/running | 25 rows scanned |
| `RECENTLY COMPLETED` | `_db_recent_completed` | **12** |
| `RECENTLY FAILED` | `_db_recent_failed` | **8** |
| `NEXUS CURRENTLY CANNOT` | `NEXUS_CANNOT` constant, `proposer.py:51` | 5 fixed capability gaps |
| `UPTIME ANOMALIES` | `_db_uptime_anomalies`, 24h window | sources **currently down** for ≥ `_MIN_OUTAGE_SAMPLES` |
| `KNOWN FACTS` | `_db_actionable_facts` | **12** |
| `DO NOT RE-PROPOSE` | permanent rejections + `_db_recent_abandoned` | permanents unbounded (guard 100); recents **8** |

Two bounds are deliberate and easy to misread as bugs:

- The **24h anomaly window** exists so a since-fixed infrastructure bug stops generating the same
  investigation goal forever. Anomalies also only surface a source that is down *right now* —
  a flap that recovered is intentionally invisible.
- `NEXUS_CANNOT` is the "it will fail identically regardless of wording" list: Unraid
  alerts/notifications, per-device UniFi client metrics, Proxmox package installs, HA automation
  or scene configuration, router/firewall rules. A goal in one of these areas is *supposed* not to
  appear.

## The suppression mechanisms, and how each fails

### 1. Permanent rejection — `Goal.permanently_rejected` (2026-08-22)

A permanently-rejected goal is injected into `DO NOT RE-PROPOSE` (prefixed `(NEVER)`) **regardless
of the 8-row recency window**, via `goals._db_permanently_rejected()`.

This exists because plain rejection only ever reached the prompt through `_db_recent_abandoned(8)`.
The 9th rejection silently pushed the 1st out, and a goal Brian had already said no to became
proposable again — purely because he'd rejected eight other things since. Nothing about the
rejection had changed.

Set it explicitly: the Telegram **🚫 Never** button (`goal:reject_forever:<id>`) or
`POST /api/goals/{id}/reject {"permanent": true}`. Plain **✗ Reject** deliberately does *not* set
it — most rejections mean "not now" ("the garage door is open" is a fine goal tomorrow), and
making every rejection permanent would starve the proposer.

*Failure mode:* nobody ever presses Never, so a recurring nuisance goal keeps aging out of the
8-row window and returning. If a goal has come back three times, that's the fix — not a prompt
tweak.

### 2. Fuzzy re-proposal matching (2026-08-22)

Before `goals.propose()` is called, each proposal's title is compared against every
permanently-rejected and recently-abandoned title with `difflib.SequenceMatcher` over the
*normalised* text (`goals.similar_to_rejected`). A match drops it with
`reason: "similar_to_rejected"` in the tick's `filtered` list.

**Threshold: `goals.REJECTED_SIMILARITY_THRESHOLD = 0.85`.** Know this number before concluding a
rewording "slipped past" — it has to be a real paraphrase, not a synonym swap:

- *caught* (~0.87): "Investigate high temperature on switch-1" vs "Investigate the high
  temperature on switch-1"
- *not caught* (~0.62): "Investigate high temperature on switch-1" vs "Update firmware on
  switch-1" — and that's correct; a threshold low enough to catch it would swallow every distinct
  goal about the same device.

Normalisation (`goals._normalise`) lowercases and **strips embedded sensor readings** (`111°F`,
`-89dBm`, `42%`), so a fluctuating metric doesn't defeat the match.

*Failure mode:* a genuinely different goal about a device with a long shared title prefix gets
swallowed. Check the tick's `filtered` list before assuming the model didn't propose it.

### 3. Fingerprint debounce — the mechanism this backstops

`goals.propose()` has three debounce guards keyed on `_fingerprint(title, description)`, a
SHA-256 prefix: duplicate-active, backoff-after-failure, and cooldown-since-last-proposal
(`monitoring`-category goals get a longer forced cooldown). A partial unique index
`ux_goal_fingerprint_active` makes the DB itself reject a concurrent duplicate.

**A fingerprint is an exact hash.** One reworded word produces a completely different one, so it
has never caught a reworded re-proposal even once — which is exactly why mechanism 2 exists. Don't
expect the fingerprint to do that job. It is still the right tool for its actual job (identical
proposals racing in the same tick).

### 4. `expired:` is not a rejection

`_db_recent_abandoned` **excludes** rows whose `rejection_reason` starts with `expired:`. Nobody
rejected those — they were TTL-swept unanswered, and treating that as Brian's judgment starves the
proposer. This is a real past incident: goal #66 (2026-07-14) was auto-swept and then suppressed
forever as if it had been rejected. A `rejection_reason` of `None` **is** treated as a real
rejection (someone called `reject()` without a reason).

### 5. Night gate

`is_night` prefers the **actual `sun.sun` entity** (real dawn/dusk); only if that's unavailable
does it fall back to the fixed clock window `NIGHT_START_HOUR=20` / `NIGHT_END_HOUR=7`. If even
that fails (bad timezone), it defaults to **daytime** — a missed exemption is safer than an
incorrectly forced one; worst case is a redundant notification rather than lights being cut on
Brian while he's relying on them.

At night the prompt tells the model porch and garage lights are deliberate security lighting, and
a deterministic backstop drops any proposal whose title+description mentions
`NIGHT_EXEMPT_ENTITY_IDS | NIGHT_EXEMPT_LABELS` (`filtered` reason `night_exempt`). Never rely on
the prompt alone here.

### 6. Hardcoded porch-light exclusion — unconditional

`KNOWN_HARDWARE_ISSUE_LABELS = {"porch_light_left", "porch_light_right"}` (`proposer.py:143`).
Water damage; operable only from the physical wall switch. **Any** proposal mentioning these two —
turning off, investigating, anything — is dropped regardless of time of day (`filtered` reason
`hardware_issue`). This is temporary until Brian confirms the repair; if you're wondering why
NEXUS ignores an obviously-on porch light, this is why, and it is not a bug.

## Reading a tick's own record

`propose_goals_tick` returns `count_proposed`, `count_auto_approved`, `count_filtered`,
`filtered` (each `{title, reason}`), and `results`. The last tick's stats are persisted via
`governor.record_proposer_tick_stats` into `SystemState.proposer_tick_stats_json`:

```bash
ssh -i ~/.ssh/id_ed25519 root@100.84.21.43 'cd /var/lib/nexus && PYTHONPATH=/opt/nexus /opt/nexus/venv/bin/python -' <<'PY'
import json
from sqlmodel import Session, select
from backend.database import SystemState, engine
with Session(engine) as s:
    row = s.exec(select(SystemState)).first()
    print(json.dumps(json.loads(row.proposer_tick_stats_json or "{}"), indent=2)[:3000])
PY
```

The `filtered` reasons map one-to-one onto the mechanisms above: `no_success_criteria`,
`night_exempt`, `hardware_issue`, `similar_to_rejected`. **If the goal you're asking about appears
there, the model did propose it and a deterministic filter dropped it — that is a completely
different investigation from a model miss**, and it's why step 0 comes first.

`no_success_criteria` is worth calling out: a goal with no checkable done-condition is dropped
outright, because the executor can never honestly complete it. Criteria must also be checkable by
the executor's own remote read-only tools — anything requiring physical presence fails
verification every time no matter how well the work goes.

## Auto-approve, briefly

A newly-proposed goal is auto-approved only when **all four** hold: actor `autonomous`, risk
`low`, reversible, and `auto_approve_low_risk` on (`goals.is_auto_approvable`). Everything else
waits for a human tap. If a goal was proposed but never ran, check those four before suspecting
the dispatcher.
