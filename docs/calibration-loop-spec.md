I have everything I need. Here is the spec.

---

# NEXUS Calibration Loop — Implementation Spec v1

**Origin:** NEXUS's second self-proposal (2026-08-01), asked via its own chat API. **Role:** Opus planner. **Next role:** Council-loop (writer/verifier).
**Repo:** `C:\Users\Brian\Documents\Agentic os\nexus`
**Builds on:** `docs/outcome-tracker-spec.md` (shipped, live). Do not re-read that spec as aspirational — every line of it is running code.

---

## 0. Verification of NEXUS's self-report — what is already live vs. what is actually missing

I grepped `calibration_hints`, `calibration_summary`, and every per-rule threshold/suppression path before planning. **NEXUS's self-report is substantially accurate but understates what already exists in two places and overstates the usability of the data in one.**

### 0.1 Already live (NEXUS did not claim these, but they exist and constrain the design)

| Thing | Location | What it actually does |
|---|---|---|
| `calibration_summary(days=30)` | `backend/agents/outcomes.py:434` → `_db_calibration:256` | Groups **rows created since cutoff** by `f"{source}:{check}"`, counts by `status`. Nothing else. No rate, no threshold, no time bucket, no `resolved_by`, no `severity`, no `surfaced_count`. |
| Briefing advisory line | `backend/agents/briefing.py:152` `_format_calibration_line`, injected at `BRIEFING_PROMPT:328` | One prose line between the RECENTLY CLOSED block and `Produce a morning brief`. Advisory to the LLM's tone only. **Zero behavior change.** Capped at `outcome_flag_briefing_max`. |
| Digest line | `backend/agents/digest.py:187-198` | The same line, **uncapped**, appended to `build_autonomy_digest()`. |
| REST | `backend/api/safety.py:118` `GET /api/safety/flags/calibration?days=` | Returns the raw dict. |
| Automatic suppression | `outcomes.record_flag` branches 2 & 3 | The *only* auto-suppression that exists: a `deferred` window, and a fixed 30-day post-`false_positive` cooldown, both **per exact fingerprint**. |

**No `/calibration` Telegram command exists.** `COMMANDS` (`backend/agents/telegram_commands.py:453`) has `/flags`, `/resolve`, `/defer`, `/flag` and nothing else flag-related. NEXUS's "optionally a `/calibration` command" is genuinely unbuilt.

**No `calibration_hints` table, no FP-rate computation, no threshold, no persisted suppression state, no source-level gate.** NEXUS is correct that the read-back loop does not exist.

### 0.2 Where NEXUS's proposal is wrong or incomplete — three findings a competent plan must correct

**Finding A — `calibration_summary`'s denominator is not usable as an FP rate, and naively dividing it would auto-suppress on a sample size of one.**
`record_flag`'s dedup (branch 1) collapses N re-observations of a live condition into **one row** with `surfaced_count` incremented. And branch 3 means that *after* Brian marks one flag `false_positive`, `record_flag` returns `None` for 30 days for that fingerprint — **no rows are written at all**. So over a 30-day window, a rule Brian false-positived exactly once shows `{"false_positive": 1}` → `sum(counts.values()) == 1` → a 100% "FP rate" on N=1. `>60% over 30 days` applied to this data suppresses on a single tap. §2.3 defines a different, honest denominator.

**Finding B — NEXUS asked for the wrong suppression point, and gating `record_flag` alone would make things strictly worse.**
NEXUS says "suppress at the SOURCE rather than after-the-fact." But look at `homelab_watch._edge_alert:81-89`: `record_flag` is called, and then `events.notify_phone` is called **unconditionally** — a `None` return only omits the two inline buttons. The same is true today of branches 2 and 3: **the existing 30-day false-positive cooldown suppresses the database row but does not suppress the Telegram page.** Brian is still paged for 30 days about a flag he explicitly called a false alarm. If the new gate only sits inside `record_flag`, the outcome is the worst of both worlds: the page still fires, and the data that could ever un-suppress the rule stops being written. §3 splits "write the row" from "page the human" and treats them as separate decisions. **Fixing the existing cooldown's page-through is arguably the single highest-value item in this build and NEXUS did not mention it.**

**Finding C — the time-of-day dimension NEXUS led with is not affordable at this sample size.**
`created_at` is naive UTC (`briefing_timezone` is not applied). Bucketing by hour multiplies cells by up to 24 against a denominator that is already going to be single-digit. §1.4 keeps a `context_bucket` column (populated `"all"` in v1) and computes time-bucket rates **for display in `/calibration` only, never for suppression** — so Brian can see whether the pattern NEXUS hypothesised is real before anything acts on it. This is a deliberate, explicit departure from NEXUS's stated design.

**Finding D — self-terminating feedback.** Once a rule is suppressed, Brian never sees its flags, therefore never taps ✓/✗, therefore the FP rate freezes and the rule can **never** un-suppress on data. NEXUS's proposal has no answer. §2.5 solves it with a mandatory re-probation expiry, not with cleverness.

---

## 1. Data model

### 1.1 Decision: a new nightly-computed `CalibrationHint` table, not a live query — but not for the reason you'd expect

The performance argument is **weak here and I will not lean on it**. Ground it in the real call rate: `record_flag` is *not* called on every tick. `_edge_alert` (`homelab_watch.py:77-81`) returns early when the key is already in `_active_alerts`, so it calls `record_flag` only on a **rising edge**. `check_proxmox_vms`/`check_docker` call it only on a transition. `watchdog.py`'s four call sites are behind `_should_alert`/`_should_alert_dead_letters_db`/`_contract_fail_streak` debounces. `briefing.py` runs once a day. Real-world rate is a handful of calls per day. A live `GROUP BY` over an indexed `fingerprint` would cost nothing.

The table earns its place on four grounds that a live recompute cannot satisfy:

1. **Auditability.** NEXUS's own explicit requirement is "keeps it auditable, not a black box." A hint row freezes *the numbers that justified suppression at the moment it was decided* (`fp_rate`, `verdict_count`, `computed_at`). A live recompute can only ever show today's numbers, so `/calibration` could never answer "why did you start suppressing this on the 14th."
2. **Stability.** A live recompute flaps: a rule sits at 0.61 in the morning and 0.59 in the evening as rows age out of the trailing window, silently toggling the page path mid-day with no record. A nightly snapshot changes at most once per day and logs the transition.
3. **The override needs a durable home** (§5). Brian un-suppressing a rule must survive restarts and must be sticky against the next nightly recompute.
4. **Hysteresis and probation** (§2.5) are *state machines*, not functions of the current window. They require `first_active_at` and `override_until` persisted.

Staleness tolerance is high by construction: a noise pattern that took 30 days of evidence to establish does not need sub-24h reaction time.

### 1.2 Rejected alternatives, each grounded in existing code

- **A `SystemState` CSV column** (the `policy_auto_allow_kinds` / `muted_notify_kinds` idiom, `governor.py:153` `_add_csv_kind`): rejected for the identical reason `docs/outcome-tracker-spec.md` §1.3 rejected it for flags — there is nowhere to put `fp_rate`, `verdict_count`, `first_active_at`, `override_until`, or `reason`. `_add_csv_kind` stores bare strings.
- **Extending `OutcomeFlag` with the hint state**: rejected. A hint is per-*fingerprint*, one row; `OutcomeFlag` is per-*occurrence*, many rows. Storing hint state on flags means either duplication across rows or an arbitrary "latest row is authoritative" rule.
- **An in-process cached dict with a TTL**: rejected. It would defeat the override's immediacy (Brian types `/calibration unsuppress` and expects the next page to fire), and it buys nothing given the call rate above.

### 1.3 New table `CalibrationHint` — `backend/database.py`

Add immediately after `OutcomeFlag` (which ends at line 660, before `_ensure_processedmail_columns`). Created by `SQLModel.metadata.create_all(engine)` — a genuinely new table, so **no migration shim**, matching `OutcomeFlag`/`SecretFallback`/`TaskOutcome`. The uniqueness constraint is declared on the model (`unique=True`), not bolted on via a `CREATE UNIQUE INDEX` shim, because unlike `ux_outcomeflag_open` it is unconditional — no partial-index carve-out is needed.

```python
class CalibrationHint(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    fingerprint: str = Field(unique=True, index=True)   # f"{source}:{check}", same key OutcomeFlag uses
    context_bucket: str = "all"        # v1 always "all" — see §1.4
    status: str = "active"             # active | expired | overridden_off
    # --- the frozen evidence that justified the current status ---
    verdict_count: int = 0             # denominator: human-verdicted rows in window (§2.3)
    false_positive_count: int = 0      # numerator
    fp_rate: float = 0.0
    auto_cleared_count: int = 0        # resolved_by LIKE 'auto:%' — excluded from both, shown in /calibration
    suppressed_surfacings: int = 0     # sum(surfaced_count) over suppressed rows — "how loud it still is"
    max_severity: str = "medium"       # highest severity seen in window; gates §3.4
    window_days: int = 30
    reason: str = ""                   # human-readable, rendered verbatim by /calibration
    # --- state machine ---
    first_active_at: datetime | None = None   # when suppression STARTED (never reset by a recompute)
    expires_at: datetime | None = None        # mandatory re-probation, §2.5
    override_by: str | None = None            # "telegram" | "api"
    override_at: datetime | None = None
    override_until: datetime | None = None    # nightly job refuses to re-activate before this
    override_note: str | None = None
    computed_at: datetime = Field(default_factory=datetime.utcnow)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
```

**Vocabulary grounded in existing code:** `fingerprint` is byte-identical to `OutcomeFlag.fingerprint` (`f"{source}:{check}"`, built in `outcomes.record_flag:331`) so the join is trivial and no new key format enters the codebase. `max_severity` reuses `low|medium|high` from `ActionLog.risk` / `Goal.risk` / `OutcomeFlag.severity`. `override_until` mirrors `OutcomeFlag.deferred_until`'s semantics exactly.

### 1.4 `context_bucket` — present, unused for suppression in v1

Column exists and is always `"all"` in v1. The nightly job **also** computes per-time-bucket rates (4 coarse buckets in `briefing_timezone`: `night` 00-06, `morning` 06-12, `afternoon` 12-18, `evening` 18-24) and stores them **only** in the `reason` string for `/calibration` to render. No hint row is ever written with `context_bucket != "all"` in v1, and the gate in §3 only ever reads `context_bucket == "all"`.

Rationale: NEXUS's motivating example ("garage open at a time of day when it's always intentionally open") is a hypothesis about Brian's data that nobody has tested. Displaying the buckets tests it for free. Acting on them before it's tested multiplies an already-thin denominator by 24 (or 4). Adding time-bucketed suppression later is a data-only change — the column, the gate's lookup key, and the unique index are already shaped for it.

### 1.5 Two new columns on the existing live `OutcomeFlag` table

`OutcomeFlag` exists in Brian's live `nexus.db`, so these need the idempotent ALTER-shim pattern (§9.2):

```python
suppressed: bool = False              # written by record_flag when a hint gated it
suppressed_reason: str | None = None  # "calibration:homelab_watch:garage_open fp_rate=0.71 n=7"
```

**`status` is deliberately NOT given a sixth value.** Adding `"suppressed"` to `status` would touch `_VALID_TARGET_STATUSES`, `_ACTIVE_STATUSES`, `_CLOSED_STATUSES` (`outcomes.py:34-43`), the partial unique index `ux_outcomeflag_open` (`database.py:671`, predicated on `status = 'open'`), `_db_open_flags`, `_db_recently_closed`, `_db_sweep_deferred`, `resolve_flag`'s state machine, and `backup.prune_old_outcome_flags`. A separate boolean touches none of them.

**This gives the design a property worth naming:** a suppressed flag is still `status="open"`, so it still holds the `ux_outcomeflag_open` slot, so **`record_flag`'s dedup branch 1 keeps absorbing every subsequent observation into `surfaced_count` on that one row.** The ongoing volume of a suppressed rule is measured for free, in a single row, with no new write path — and it is exactly what `/calibration` must display to prove the suppression is doing something.

---

## 2. The nightly aggregation job

### 2.1 New module vs. extending `outcomes.py`

New module **`backend/agents/calibration.py`**. Justification: `outcomes.py` is a write/read API for a single table with a stated, tested invariant set (no broker import — AC32; no Session across `await` — AC33; zero LLM calls — AC34). The calibration job is a *scheduled aggregator* that reads `OutcomeFlag` and writes `CalibrationHint` — a different table, a different lifecycle, and it belongs next to `digest.py`/`facts_digest.py`/`brain_spend.py`, which are all exactly this shape.

The **read-side accessors that `record_flag` needs** (§3) go in `outcomes.py`, because that is where the hot path lives and a circular import between `outcomes` and `calibration` must not exist. Split:

| Function | Module | Kind |
|---|---|---|
| `recompute_hints() -> dict` | `calibration.py` | nightly job entry point |
| `hint_report(days) -> dict` | `calibration.py` | for `/calibration` + REST |
| `set_override(fingerprint, active, *, by, note) -> str` | `calibration.py` | §5 |
| `active_hint(fingerprint) -> dict \| None` | `outcomes.py` | hot-path read |
| `should_page(source, check, severity) -> tuple[bool, str \| None]` | `outcomes.py` | the gate, §3 |
| `record_flag_ex(...) -> dict` | `outcomes.py` | the richer write, §3.2 |

`calibration.py` imports `outcomes`; `outcomes` never imports `calibration`. Same sync-`_db_*` + `asyncio.to_thread` discipline, same never-raises contract.

### 2.2 Registration — `backend/scheduler.py`

Follows `_prune_retention`'s exact shape (`scheduler.py:263`) and `retention_prune`'s registration block (`scheduler.py:512`):

```python
async def _calibration_recompute():
    try:
        from backend.agents.calibration import recompute_hints
        result = await recompute_hints()
        logger.info(f"Calibration recompute: {result}")
    except Exception as e:
        logger.error(f"Calibration recompute job error: {e}")
```

Registered at **03:50** — five minutes after `retention_prune` (03:45), inside the existing nightly-hygiene block, conditional on `getattr(s, "calibration_enabled", True)` (matching the `facts_digest_enabled` / `homelab_watch_enabled` registration idiom). `id="calibration_recompute"`, `replace_existing=True`, `CronTrigger(hour=3, minute=50, timezone=timezone)`.

**Nightly, not weekly** (NEXUS offered both): the metric is a 30-day trailing window, so the marginal cost of running it daily is one query, and daily gives `/calibration` and the digest fresh numbers every morning. A weekly cadence would mean up to 7 days between Brian tapping ✗ and the system reacting — the exact latency this build exists to remove.

**Its own job id, not folded into `_prune_retention`** — so `watchdog.check_scheduler_stalls` (`watchdog.py:107`, which iterates `sched.get_jobs()`) can see it stall independently, and so `calibration_enabled=False` cleanly de-registers it.

### 2.3 What `recompute_hints()` computes — the honest denominator

For each distinct `fingerprint` in `OutcomeFlag` with `created_at >= now - calibration_window_days`:

- **`verdict_count` (denominator)** = rows whose `status` ∈ {`resolved`, `false_positive`} **AND** `resolved_by` does **not** start with `"auto:"`. That is: rows Brian actually judged, via the Telegram ✓/✗ buttons (`by="telegram"`), the REST resolve (`by="api"`), or `/resolve`.
- **`false_positive_count` (numerator)** = of those, `status == "false_positive"`.
- **`auto_cleared_count`** = rows with `resolved_by` starting `"auto:"` (i.e. `outcomes.clear_flag`'s `"auto:condition_cleared"`, `_db_clear_by_fingerprint:212`). **Excluded from both numerator and denominator**, displayed separately.
- **`suppressed_surfacings`** = `SUM(surfaced_count)` over rows with `suppressed = True`.
- **`max_severity`** = highest of `low < medium < high` across all rows in the window.
- **`fp_rate`** = `false_positive_count / verdict_count` (0.0 when `verdict_count == 0`).

**Why `auto:condition_cleared` is excluded from both, not counted as a true positive:** a garage door that Brian eventually closed auto-clears, and so does a garage door that was intentionally open and closed on its own schedule. The row carries no information about which. Counting it as a true positive would systematically *hide* exactly the noise pattern NEXUS is trying to find; counting it as a false positive would suppress genuinely useful rules. Excluding it is the only defensible choice, and it is precisely the distinction `calibration_summary`'s `sum(counts.values())` throws away (Finding A).

**Rows excluded from the computation entirely:**
- `source == "manual"` **and** `check` starting `"missed:"` (§6) — these are Brian's retroactive missed-detection logs and have no rule to calibrate.
- Rows with `suppressed = True` — a suppressed row never reached Brian, so it carries no verdict. (It contributes to `suppressed_surfacings` only.)

### 2.4 Threshold crossing → persisted state

A fingerprint becomes an **active** hint when **all** of:

1. `calibration_enabled` is true;
2. `verdict_count >= calibration_min_verdicts` (default **5**);
3. `fp_rate >= calibration_fp_threshold` (default **0.60**);
4. no existing row for that fingerprint has `override_until > now` (§5's stickiness).

On activation: `status="active"`, `first_active_at=now` (only if not already set — a re-activation after expiry sets it fresh), `expires_at = now + calibration_hint_max_days`, `reason` built as a plain sentence including the per-time-bucket breakdown from §1.4, e.g.:

> `7 of 9 judged flags were false alarms (78%) in the last 30 days. By time of day: night 0/1, morning 1/1, afternoon 5/6, evening 1/1. 41 further occurrences suppressed since 2026-08-14.`

**Severity is recorded but not enforced here.** `max_severity` is written onto the hint row, and the *gate* (§3.4) is what refuses to act on a `high` hint. Deliberate: the hint still gets computed and displayed for a high-severity rule (Brian can see "you've called this noise 8 of 9 times"), it just doesn't silence anything. That keeps `/calibration` informative rather than mysteriously empty.

### 2.5 Un-suppression — three independent paths, all required

**(a) Hysteresis on the rate.** An `active` hint drops back to `status="expired"` when `fp_rate < calibration_clear_threshold` (default **0.40**). The gap between 0.60 (suppress) and 0.40 (clear) prevents daily flapping around a single boundary. Same shape as the `contract_canary_consecutive_ticks` debounce.

**(b) Mandatory re-probation — the answer to Finding D.** Every active hint carries `expires_at = first_active_at + calibration_hint_max_days` (default **30**). When the job runs past that date, the hint flips to `expired` **unconditionally, regardless of rate**. The rule then pages normally, re-accumulates human verdicts, and must re-earn suppression from fresh data.

This is not optional garnish — it is the only mechanism that makes the loop terminate correctly. Once suppressed, a rule generates zero new human verdicts, so (a) alone can never fire: `verdict_count` stays frozen at whatever it was, and after 30 days those rows age out of the window entirely, dropping `verdict_count` to 0 — at which point condition (2) fails anyway. Without (b) the system would silently oscillate between "suppressed forever on stale evidence" and "un-suppressed by data starvation" with no legible reason. (b) makes the cycle explicit, dated, and visible in `/calibration`.

**(c) Brian's explicit override** (§5).

**Side effect on transition to non-active (all three paths):** the job must also **clear `suppressed`/`suppressed_reason` on any currently-`open` `OutcomeFlag` row for that fingerprint.** Without this, a rule that un-suppresses while its condition is still live would stay invisible in `/flags`, `## Open Items`, chat, and the `open_flags` agent tool until the condition cleared and re-fired — potentially days. One `UPDATE` in the same job, one AC.

### 2.6 Return shape and logging

`recompute_hints() -> {"scanned": int, "activated": [fp], "expired": [fp], "unchanged": int, "skipped_override": [fp]}`. Logged at INFO by `_calibration_recompute`, matching `_run_facts_digest`/`_goal_recurrence`. **Never raises** (outer try/except, returns a zeroed dict) — a calibration bug must never take out the 03:45-03:50 nightly block.

**Notification on activation:** the job fires **one** `events.notify_phone(kind="calibration_suppress")` per newly-activated hint, naming the rule and the rate. Justification: auto-suppression of alerts is a false-negative risk (§5's premise). Brian finding out only when he happens to type `/calibration` is the black-box failure NEXUS explicitly asked to avoid. One page per activation, at most a handful ever, `/mute`-able like every other kind. Expiry is *not* paged (going back to normal alerting is self-announcing).

---

## 3. The suppression gate — the actual behavior change

### 3.1 The write/page split

This section implements Finding B. Two separate decisions, never conflated:

- **Write the row: always.** `record_flag` continues to write an `OutcomeFlag` row on every rising edge, suppressed or not, stamped with `suppressed=True` / `suppressed_reason`. The ledger never stops. This is what keeps `suppressed_surfacings` measurable and what makes §2.5(b)'s re-probation meaningful.
- **Page the human: gated.** The Telegram alert is what gets suppressed.

### 3.2 New public API in `backend/agents/outcomes.py`

```python
async def should_page(source: str, check: str, severity: str = "medium") -> tuple[bool, str | None]
```
Returns `(True, None)` to page, or `(False, reason)` to stay quiet. **Fails open on absolutely everything**: any exception, `calibration_enabled=False`, `calibration_suppression_enabled=False`, no hint row, `status != "active"`, `override_until > now`, or a severity the guardrail protects → `(True, None)`. Wrapped in one outer try/except exactly like `record_flag`.

```python
async def record_flag_ex(source, check, summary, *, detail=None, severity="medium",
                         action_log_id=None) -> dict
```
Returns `{"id": int | None, "surface": bool, "reason": str | None}`. Runs `record_flag`'s existing four-branch logic unchanged, plus:
- Branch 0 (new, first): consult `should_page`. If `False`, still execute the normal insert/dedup, but stamp `suppressed=True` + `suppressed_reason` on the row and return `surface=False`.
- Branches 2 and 3 (deferred window / FP cooldown) now return `surface=False` with reason `"deferred"` / `"false_positive_cooldown"` **in addition to** `id=None`. *This is the fix for the existing page-through wart in Finding B.*
- Branch 1 (already-open row) returns `surface` per the hint only — it does **not** return `surface=False` merely because the row already existed. The callers' own latches (`_active_alerts`, `_should_alert`, `_contract_fail_streak`) already own re-alert suppression, and moving that responsibility would change proven alerting semantics well outside this scope.

```python
async def record_flag(...) -> int | None   # UNCHANGED SIGNATURE AND SEMANTICS
```
Becomes a thin wrapper over `record_flag_ex` returning `d["id"] if d["surface"] or d["id"] else None`, preserving byte-identical behavior for the eight call sites that are **not** being gated (§3.3). Every existing test in `tests/test_outcome_flags.py` (AC4-AC10) must stay green untouched — that is an acceptance criterion, not a hope.

### 3.3 Exactly which call sites get gated

There are **15 `record_flag` call sites**. Not all of them page, and the ones that don't need no change.

**GATED — rewire to `record_flag_ex` and honor `surface` (7 call sites, covering 10 checks):**

| File:line | Fingerprint | Severity | Change |
|---|---|---|---|
| `homelab_watch.py:81` (`_edge_alert`, serves 4 checks: `unraid_array`, `unraid_temp`, `garage_open`, `vzdump_failed`) | `homelab_watch:{key}` | medium (default) | Replace `record_flag` with `record_flag_ex`; skip `events.notify_phone` when `surface=False`. **`_active_alerts.add(key)` stays before the call, unmoved** — the latch must still latch on a suppressed edge, otherwise every 60s tick re-enters and re-writes. |
| `homelab_watch.py:111` (`check_proxmox_vms`) | `homelab_watch:vm:{vmid}` | **high** | Same; protected by §3.4 by default. |
| `homelab_watch.py:146` (`check_docker`) | `homelab_watch:docker:{name}` | medium | Same. |
| `watchdog.py:120` (`check_scheduler_stalls`) | `watchdog:stall:{job.id}` | **high** | Same; protected by default. |
| `watchdog.py:180` (`check_dead_letters`) | `watchdog:dead_letters` | **high** | Same; protected by default. |
| `watchdog.py:274` (`check_auth_failure_burst`) | `watchdog:auth_burst:{src}` | **high** | Same; protected by default. |
| `watchdog.py:352` (`check_integration_contracts`) | `contracts:breach:{name}` | **high** | Same; protected by default. |

**NOT GATED — no code change (8 call sites):**

- `briefing.py:268, 275, 282, 289, 296, 309` (the six `_record_briefing_flags` writes). These **do not page** — they feed the briefing read path only. Suppression is handled entirely by `_db_open_flags` filtering `suppressed=True` (§3.5); the row still gets stamped by `record_flag_ex` internally via the wrapper. Adding a gate at these call sites would be redundant code with no behavioral effect.
- `telegram_commands.py:447` (`/flag`) and `api/safety.py:187` (`POST /api/safety/flags`). **A human-entered flag must never be auto-suppressed.** Hard rule with its own AC. `should_page` returns `(True, None)` for `source == "manual"` unconditionally, before any hint lookup.

**Which checks can actually be silenced under default config:** with `calibration_suppress_high_severity=False`, the auto-suppressible set is exactly **the four `_edge_alert` level checks, docker-stopped, and the six briefing flags**. Every watchdog page and every VM-stopped page is structurally immune. That set maps precisely onto NEXUS's own motivating example (the garage door) and excludes every alert whose silence would be dangerous.

### 3.4 The high-severity guardrail — NEXUS did not ask for this, and it is the most important line in the spec

`should_page` returns `(True, None)` whenever `severity == "high"` unless `calibration_suppress_high_severity` is explicitly `True` (default **False**).

Rationale, stated plainly because Council-loop needs to not "helpfully" relax it: the failure mode of this entire feature is **going silently blind on a real problem**. A false positive costs Brian an unnecessary glance at his phone. A suppressed true positive on `watchdog:dead_letters` means the notification pipeline is broken and nothing tells him — including this feature, which depends on that same pipeline. The asymmetry is enormous and the guardrail is one comparison.

**Two medium-severity flags that arguably should be high, flagged for Brian's decision (§9.5 step 6, optional):** `_edge_alert` never passes `severity`, so `homelab_watch:unraid_array` (array not started) and `homelab_watch:vzdump_failed` (backup failed) are `medium` today and therefore **in the auto-suppressible set**. Both are genuinely serious. Recommendation: pass `severity="high"` for those two keys in `_edge_alert`. This is a one-line change with its own AC, but it does alter the severity recorded on future rows, so it is called out separately rather than smuggled in.

### 3.5 Read-path filtering

`outcomes._db_open_flags` (`outcomes.py:221`) gains `and not row.suppressed` to its existing in-Python filter, plus an `include_suppressed: bool = False` parameter threaded through the async `open_flags(limit=50, *, include_suppressed=False)`.

One line, five consumers, all correct by default:
- `briefing.py:416` KNOWN OPEN ITEMS block and `briefing.py:564` the deterministic `## Open Items` section
- `chat.py:498` the `[OPEN ITEMS]` memory block
- `telegram_commands.py:368` `/flags`
- `tools.py:339` the `open_flags` agent tool

`/calibration` is the sole caller passing `include_suppressed=True`.

**One edge case to handle, not to worry about:** `watchdog.check_deferred_flags:405` calls `open_flags` to look up summaries for just-swept ids. A swept row is `needs_follow_up` and could in principle be `suppressed` (only if Brian deferred an id he read out of `/calibration`). `by_id.get(flag_id)` returns `None` and `_format_flag_followup` already degrades to `"flag #N"` (`watchdog.py:370`). No change needed; note it so nobody "fixes" it.

**Retention interaction:** a suppressed row stays `status="open"` forever and `backup.prune_old_outcome_flags` (`backup.py:346`) correctly never deletes it. Unbounded? No — the `ux_outcomeflag_open` partial unique index guarantees **at most one open row per fingerprint**, and there are ~15 distinct fingerprints in the entire system. Bounded by construction. No prune change.

### 3.6 The briefing prompt line stays, and is extended — not replaced

NEXUS asked for "an actual read-before-decide gate, not just an FYI line." It gets one (§3.3). The existing `_format_calibration_line` advisory (`briefing.py:152`) **stays exactly as it is** and gains one appended sentence when active hints exist:

> `Flag calibration (30d): homelab_watch:garage_open — 8 raised, 6 false_positive. Currently auto-suppressed: homelab_watch:garage_open (78% false alarm, until 2026-09-13).`

**Append as a separate sentence, never modify the existing prefix.** Three live tests assert on the exact existing substring (`tests/test_briefing.py:678`, `:712`, `:763`) using `in`; appending keeps them green. Same discipline for `digest.py:187-198`'s line — append `" | Auto-suppressed: N rule(s)"` only when N > 0.

---

## 4. Visibility — the `/calibration` command

Genuinely new; nothing like it exists. One entry in `COMMANDS` (`telegram_commands.py:453`), one handler `_cmd_calibration`, following `_cmd_resolve`'s arg-parsing style exactly. It picks up Telegram's `/` autocomplete for free via `command_menu()` (`:481`) → `telegram.set_my_commands`, and `_match_voice_command` picks up spoken "calibration" for free because it reads `COMMANDS` dynamically.

**Bare `/calibration`** renders, per rule with any activity in the window:

```
Flag calibration (30 days)

SUPPRESSED (2)
homelab_watch:garage_open — 78% false alarm (7/9 judged)
  since 2026-08-14, re-tests 2026-09-13 · 41 occurrences silenced
briefing:unifi_new_devices — 100% false alarm (6/6 judged)
  since 2026-08-20, re-tests 2026-09-19 · 3 occurrences silenced

WATCHING (below threshold)
homelab_watch:unraid_temp — 33% false alarm (1/3 judged)
watchdog:dead_letters — 80% false alarm (4/5 judged) · HIGH, never auto-suppressed
briefing:github_stale_prs — 0% false alarm (0/4 judged) · 12 auto-cleared

OVERRIDDEN BY YOU (1)
homelab_watch:vzdump_failed — un-suppressed 2026-08-22, protected until 2026-11-20

Suppression: ON (>=60% of >=5 judged flags)
/calibration unsuppress <rule>  ·  /calibration suppress <rule>
```

**Hard requirement, per NEXUS's own "never a black box":** the SUPPRESSED section renders **first and is never truncated**, even when the WATCHING list is capped (cap at 15 there, matching `_cmd_flags`'s unbounded-but-practically-small precedent). Its own AC.

`watchdog:dead_letters` in the example illustrates the guardrail doing its job visibly: the rate is computed and shown, the `HIGH, never auto-suppressed` annotation explains why nothing happened. Silence with an explanation, not silence.

**Also extended (not new):**
- `digest.build_autonomy_digest()` (`digest.py:196`) — append the suppressed count so it lands in the 20:00 digest.
- `briefing.py` advisory line — §3.6.
- **REST**, mirroring `api/safety.py`'s existing flag routes and Bearer-gated with `Depends(require_api_key)`:
  - `GET /api/safety/flags/calibration/hints?days=` → `calibration.hint_report(days)` (the full structure `/calibration` renders). **Declared before** the existing `GET /flags/calibration` is unnecessary — `/flags/calibration/hints` is strictly longer and cannot be shadowed by a literal segment — but keep both literal routes above `POST /flags/{flag_id}/resolve`, preserving the ordering comment already at `api/safety.py:116`.
  - `POST /api/safety/flags/calibration/{fingerprint}/override` — body `{active: bool, note?: str}`. `200` applied / `404` no such hint / `400` malformed fingerprint.
  - The pre-existing `GET /api/safety/flags/calibration` is **unchanged**, returning `calibration_summary`'s dict, because `tests/test_api_endpoints.py:518,572` locks it.

---

## 5. Override path

### 5.1 No broker mediation. Justified from the existing precedent, not from convenience.

The confirm-policy layer (CLAUDE.md, 2026-07-26) established the asymmetry this repo already uses: **granting more autonomy needs a human tap (`policy_promote`, HIGH risk); revoking it needs no gate at all, because tightening only removes capability.**

Map it here: `unsuppress` = more alerts = tightening = **no gate**. `suppress` = fewer alerts = loosening = the risky direction. But the actor is Brian typing into an authorized Telegram chat, which every existing path treats as `actor="user"` and always-allows (the same trust as a valid Bearer key — see `telegram_poller._authorized`). A broker kind here would gate Brian against himself while adding an `ActionLog` row for a change already fully audited in `CalibrationHint`. **No new broker kind.**

The one thing manual suppression *does* need is a TTL, and getting that right closes a wart CLAUDE.md already documents: `/mute` has no TTL, so "a typo'd kind mutes nothing, silently, permanently." A manual `/calibration suppress` therefore writes the same `expires_at = now + calibration_hint_max_days` as an auto-hint. **A manual suppression cannot outlive its re-probation window.**

### 5.2 Mechanics

`calibration.set_override(fingerprint, active: bool, *, by: str, note: str | None) -> str` returns an outcome string (`"applied"` / `"not_found"` / `"invalid"`), mirroring `outcomes.resolve_flag`'s status-string convention (`outcomes.py:374`) so Telegram/REST/tests share one mapping.

- **`active=False` (un-suppress).** Sets `status="overridden_off"`, `override_by`, `override_at`, `override_note`, and `override_until = now + calibration_override_days` (default **90**). Clears `suppressed` on the open row (§2.5 side effect). **The nightly job checks `override_until > now` and refuses to re-activate** — otherwise the job would silently undo Brian's decision the same night, which is the single most infuriating possible bug in this feature.
- **`active=True` (manual suppress).** Creates or updates the row to `status="active"`, `first_active_at=now`, `expires_at=now + calibration_hint_max_days`, `reason="manually suppressed by <by>: <note>"`, and `override_until=None`. **Still subject to §3.4's high-severity guardrail** — Brian manually suppressing a `high` rule requires flipping `calibration_suppress_high_severity`, same as the automatic path. Deliberate: the guardrail protects against a bad late-night decision as much as a bad computation.
- **Global escape hatch:** `calibration_suppression_enabled=False` in `.env` + restart makes every `should_page` return `(True, None)`. Hints keep computing and `/calibration` keeps rendering — the observability survives the rollback, unlike `outcome_flags_enabled=False` which takes everything down. §9.7.

---

## 6. The "missed detection" extension — capture only, explicitly not consumed

**Decision: include the capture, exclude the consumption. ~4 lines and one test.**

`_cmd_flag` (`telegram_commands.py:436`) already slugifies free text into `check` and writes `source="manual"`. Add one branch: if the text starts with `missed ` (case-insensitive), write `check=f"missed:{slug}"` and `severity="high"`. That is the whole change. `/flag missed the water heater was leaking and you never told me` becomes a queryable, dated, structured row today.

**Explicitly NOT consumed by the calibration loop, and this is a correctness requirement, not a scoping preference:** a missed detection has no fingerprint corresponding to any existing rule. Nothing in the data connects "the water heater leaked" to `homelab_watch:unraid_temp` or to any other rule whose threshold might have caught it. Making that connection requires LLM classification of free text into a rule namespace — which `docs/outcome-tracker-spec.md` §5.1 explicitly forbids and which would be a fabricated inference driving an automated behavior change.

Concretely: `missed:*` rows are **excluded from `recompute_hints`'s scan** (§2.3) and from `/calibration`'s display. Without that exclusion they would land in the `manual:` namespace and pollute a denominator, and — since they are `severity="high"` and never false-positived — would look like a perfect-precision rule. Its own AC.

NEXUS called this "an extension, not a new build." It is correct. This ships the data collection so a future Phase 2 has something real to look at — the exact discipline the confirm-policy work used when it found only 2 `ActionLog` rows in 40 days and deferred the learner.

---

## 7. Scope boundaries — explicitly OUT of v1

Mirroring `docs/outcome-tracker-spec.md` §5's discipline. Council-loop must not "helpfully" add any of these.

1. **No ML, no statistical model, no confidence intervals.** A ratio and a constant. `calibration_fp_threshold` is a config float, not a learned parameter.
2. **No automatic tuning of the underlying detection thresholds.** NEXUS said "adjust my own thresholds *or* suppress rules." Suppressing a rule is a reversible, auditable list entry. Mutating `homelab_garage_open_minutes` or `homelab_disk_temp_warn_c` at runtime means a running system rewriting its own safety configuration, with no rollback story and no relationship to the flag data (the flag records *that* it fired, not what value would have prevented it). Out.
3. **No time-of-day-based suppression.** Computed and displayed only (§1.4).
4. **No cross-rule or cross-source generalization.** Per-fingerprint only. "This class of thing" (NEXUS's phrasing) requires a taxonomy that does not exist.
5. **No LLM anywhere in `calibration.py`.** Zero billed calls; `tests/test_spend_report.py::test_no_unlabeled_llm_calls_in_agents` passes trivially. In the writer's checklist explicitly.
6. **No agent-facing write tool for hints.** `tools.py` may gain a read-only hint tool at most. A `set_override` tool would let NEXUS un-suppress or suppress its own rules — the exact self-closing-loop failure `docs/outcome-tracker-spec.md` §3.4 forbids for `resolve_flag`, only worse, because here the agent would be editing the mechanism that judges it.
7. **No high-severity auto-suppression by default** (§3.4).
8. **No change to `calibration_summary`'s signature or return shape.** Three live consumers plus AC28. `calibration_rates`/`hint_report` are new functions, not a rewrite.
9. **No sixth `OutcomeFlag.status` value** (§1.5).
10. **No frontend page.** REST + Telegram + briefing + digest cover v1. Follows `docs/outcome-tracker-spec.md` §5.5 (and would require `npm run build`).
11. **No `ActionLog` change, no new broker kind, no new dispatcher** (§5.1).
12. **No retroactive backfill.** Hints compute from whatever `OutcomeFlag` rows exist. On day one there will be almost none, and §9.6 says so out loud.

---

## 8. Acceptance criteria

New file **`tests/test_calibration_loop.py`** (pytest only, no new framework), plus additions to `tests/test_outcome_flags.py`, `tests/test_homelab_watch.py`, `tests/test_watchdog.py`, `tests/test_briefing.py`, `tests/test_telegram_commands.py`, `tests/test_api_endpoints.py`, `tests/test_backup.py`.

### 8.1 Data model / migration
- **CAL1** `create_db_and_tables()` on a fresh `:memory:` engine creates `calibrationhint` with all declared columns, and the `fingerprint` unique constraint rejects a second row with the same fingerprint.
- **CAL2** `create_db_and_tables()` is idempotent — calling it twice raises nothing.
- **CAL3** `_ensure_outcomeflag_columns()` adds `suppressed`/`suppressed_reason` to a table created **without** them, is idempotent on a second call, and leaves existing rows readable with `suppressed` falsy.
- **CAL4** `OutcomeFlag.status`'s five values are unchanged; `outcomes._VALID_TARGET_STATUSES`, `_ACTIVE_STATUSES`, `_CLOSED_STATUSES` are byte-identical to their pre-change contents (guards §7.9).

### 8.2 The aggregation — the honest denominator
- **CAL5** Given 6 rows `false_positive` (`resolved_by="telegram"`) and 3 `resolved` (`resolved_by="telegram"`) for one fingerprint, `recompute_hints()` writes `verdict_count=9`, `false_positive_count=6`, `fp_rate==pytest.approx(0.667)`.
- **CAL6** Rows with `resolved_by="auto:condition_cleared"` are counted in `auto_cleared_count` and **excluded from both** numerator and denominator — 6 FP + 3 auto-cleared yields `verdict_count=6`, `fp_rate==1.0`, `auto_cleared_count=3`.
- **CAL7** Rows still `open`, `deferred`, or `needs_follow_up` contribute to neither.
- **CAL8** Rows with `suppressed=True` are excluded from `verdict_count` and summed into `suppressed_surfacings` via their `surfaced_count`.
- **CAL9** Rows older than `calibration_window_days` are excluded.
- **CAL10** `source="manual"`, `check="missed:foo"` rows are excluded entirely — no hint row is created for them (guards §6).
- **CAL11** `recompute_hints()` on an empty table returns `{"scanned": 0, ...}` and creates no rows.
- **CAL12** `recompute_hints()` never raises: with `backend.database.engine` patched to raise, it returns a zeroed dict and logs (mirrors AC9's never-raises assertion).

### 8.3 Threshold state machine
- **CAL13** `fp_rate=0.70`, `verdict_count=9` → hint `status="active"`, `first_active_at` set, `expires_at == first_active_at + calibration_hint_max_days`, `reason` non-empty.
- **CAL14** `fp_rate=0.70` but `verdict_count=3` (< `calibration_min_verdicts=5`) → **no active hint**. The sample-size floor is the guard against Finding A and must be tested directly.
- **CAL15** Hysteresis: an active hint whose rate falls to 0.50 (between clear=0.40 and suppress=0.60) stays `active`; at 0.35 it flips to `expired`.
- **CAL16** Re-probation: an active hint whose `expires_at` has passed flips to `expired` on the next recompute **even at `fp_rate=1.0`** — the Finding D guard.
- **CAL17** A hint with `override_until` in the future is **not** re-activated by a recompute even when every threshold condition is met; it appears in the return dict's `skipped_override`.
- **CAL18** On any transition out of `active`, an existing `status="open"` `OutcomeFlag` row for that fingerprint has `suppressed` set back to `False` and `suppressed_reason` to `None` (§2.5's side effect).
- **CAL19** Newly-activated hints fire exactly one `events.notify_phone(kind="calibration_suppress")` each; an unchanged or expiring hint fires zero.

### 8.4 The gate — safety properties
- **CAL20 (the critical one)** With an active hint at `fp_rate=1.0`, `verdict_count=20`, and `calibration_suppress_high_severity=False`, `should_page(..., severity="high")` returns `(True, None)`. **A high-severity flag is never auto-suppressed without explicit opt-in.** Assert for all four watchdog fingerprints and `homelab_watch:vm:{vmid}`.
- **CAL21** The same hint with `severity="medium"` returns `(False, reason)` with a non-empty reason.
- **CAL22** `should_page` fails open on every degraded path: hint table missing, engine raising, `calibration_enabled=False`, `calibration_suppression_enabled=False`, `status="expired"`, `status="overridden_off"`, and `override_until` in the future — all `(True, None)`.
- **CAL23** `should_page("manual", ...)` returns `(True, None)` regardless of any hint — a human-entered flag is never auto-suppressed (§3.3).
- **CAL24** With `calibration_suppression_enabled=False` (the shipped default), **no** call site's behavior differs from pre-build behavior in any test.

### 8.5 Call-site wiring
- **CAL25** (`tests/test_homelab_watch.py`) With an active medium hint for `homelab_watch:garage_open`, `check_garage` past threshold calls `record_flag_ex`, writes a row with `suppressed=True` and a non-empty `suppressed_reason`, and calls `events.notify_phone` **zero** times.
- **CAL26** Same scenario: `_active_alerts` **still contains** `garage_open` afterward, and a second tick writes no second row and sends no second page (the latch must not be bypassed by suppression).
- **CAL27** Pre-existing `tests/test_homelab_watch.py` behavior with no hint present is byte-identical: `record_flag` id threaded into `flag:resolved:{id}` buttons, `notify_phone` called once. AC18/AC19/AC20 from the prior spec stay green.
- **CAL28** (`tests/test_watchdog.py`) With `calibration_suppress_high_severity=False` and an active hint on `watchdog:dead_letters` at 100% FP, `check_dead_letters` **still pages**. All of AC21's assertions stay green.
- **CAL29** With `calibration_suppress_high_severity=True` and the same hint, it does not page — proving the opt-in is real and the default is the only thing protecting it.
- **CAL30** The existing FP-cooldown branch now suppresses the **page**, not just the row: after `resolve_flag(id, "false_positive")`, the next `check_garage` rising edge calls `notify_phone` zero times while inside `outcome_flag_false_positive_cooldown_days` (the Finding B fix), and pages again after it.
- **CAL31** `check_budget_warning` still calls `record_flag`/`record_flag_ex` **zero** times (the prior spec's AC21 second half, re-asserted so nobody "helpfully" wires it up).

### 8.6 Read-path filtering
- **CAL32** `open_flags()` omits `suppressed=True` rows; `open_flags(include_suppressed=True)` includes them.
- **CAL33** (`tests/test_briefing.py`) A suppressed open flag appears in neither the KNOWN OPEN ITEMS prompt block nor the `## Open Items` section; an unsuppressed one appears in both. Existing AC24/AC25 stay green.
- **CAL34** (`tests/test_chat_memory.py`) A suppressed flag does not reach the `[OPEN ITEMS]` memory block.
- **CAL35** (`tests/test_briefing.py`) `_format_calibration_line`'s existing output is **unchanged** when no hints exist; with hints, the pre-existing asserted substring at `tests/test_briefing.py:678` is still present and a suppression sentence follows it. All three existing calibration-line tests pass unmodified.

### 8.7 Visibility and override
- **CAL36** (`tests/test_telegram_commands.py`) `/calibration` with no data replies with a "no calibration data yet" message, not an error or an empty string.
- **CAL37 (NEXUS's explicit no-black-box requirement)** `/calibration` with 2 active hints and 40 watching rules renders **both** suppressed rules in full, before any truncation, including rate, since-date, re-test date, and silenced count. Assert each suppressed fingerprint appears in the output.
- **CAL38** `/calibration` renders a high-severity rule that crossed the threshold with an explicit never-auto-suppressed annotation — proving the guardrail is explained, not merely silent.
- **CAL39** `/calibration unsuppress homelab_watch:garage_open` calls `set_override(..., active=False, by="telegram")`; the hint becomes `overridden_off` with `override_until` ≈ now + `calibration_override_days`; the next `recompute_hints()` leaves it alone; and `should_page` for that fingerprint returns `(True, None)`.
- **CAL40** `/calibration suppress <fp>` on a high-severity rule with the default config sets the hint but `should_page` **still** returns `(True, None)` — the guardrail outranks a manual override (§5.2).
- **CAL41** A manual suppression always carries `expires_at` set to `now + calibration_hint_max_days` — it can never be permanent (the `/mute`-has-no-TTL wart, closed).
- **CAL42** `/calibration unsuppress <unknown>` replies "not found" and changes nothing.
- **CAL43** (`tests/test_api_endpoints.py`) `GET /api/safety/flags/calibration/hints` → 401 without a Bearer key, 200 with one. `POST .../calibration/{fp}/override` → 200 / 404 / 400 per §4. **The pre-existing `GET /api/safety/flags/calibration` response is unchanged** — `tests/test_api_endpoints.py:518,572` pass untouched.
- **CAL44** (`tests/test_telegram_commands.py`) `"calibration"` is in `COMMANDS` and therefore in `command_menu()`.

### 8.8 Scheduler and invariants
- **CAL45** `setup_scheduler` registers job id `calibration_recompute` when `calibration_enabled=True` and does not when `False`; the trigger is 03:50 in the configured timezone.
- **CAL46** `_calibration_recompute` swallows a raising `recompute_hints` and logs (mirrors every other job wrapper in `scheduler.py`).
- **CAL47** `backend/agents/calibration.py` does not import `backend.safety.broker` (grep-style assertion, matching `tests/test_tools.py`'s existing no-broker-import guard and the prior spec's AC32).
- **CAL48** No `Session` or ORM object crosses an `await` in `calibration.py` — every DB helper is sync, every async wrapper uses `asyncio.to_thread`, and every `_db_*` returns plain dicts (AC33's discipline).
- **CAL49** `tests/test_spend_report.py::test_no_unlabeled_llm_calls_in_agents` still passes; `calibration.py` contains zero `haiku`/`sonnet`/`opus` calls.
- **CAL50** (`tests/test_backup.py`) `prune_old_outcome_flags()` still never deletes a `suppressed=True, status="open"` row regardless of age (existing AC31 extended).
- **CAL51** All of `tests/test_outcome_flags.py` (AC1-AC34) passes **unmodified** — `record_flag`'s signature, return semantics, and four-branch behavior are unchanged for un-gated callers.

---

## 9. Migration / rollout

### 9.1 Schema — new table
`CalibrationHint` is brand new → created by `SQLModel.metadata.create_all(engine)` with **no shim**, matching `OutcomeFlag`, `SecretFallback`, `TaskOutcome`, `Fact`. The `unique=True` on `fingerprint` is declared on the model and handled by `create_all`; unlike `ux_outcomeflag_open` it is unconditional, so it needs no `CREATE UNIQUE INDEX ... WHERE` shim.

### 9.2 Schema — existing table, needs a shim
`OutcomeFlag` is live in Brian's `nexus.db`, so `suppressed`/`suppressed_reason` need the established idempotent ALTER pattern. Add to `backend/database.py`, modeled verbatim on `_ensure_processedmail_columns` (`database.py:663`) and using the same `_safe_add_column` helper:

```python
def _ensure_outcomeflag_columns():
    """Idempotently add the calibration-loop columns to an OutcomeFlag table
    that predates them. Best-effort — never fatal to startup."""
    _safe_add_column("outcomeflag", "suppressed", "BOOLEAN DEFAULT 0")
    _safe_add_column("outcomeflag", "suppressed_reason", "VARCHAR")
```

Registered in `create_db_and_tables()` **before** `_ensure_outcomeflag_index()` (the index predicate reads `status`, not `suppressed`, so ordering is not load-bearing — but keeping the two `outcomeflag` shims adjacent matters for the next reader).

### 9.3 Config — `backend/config.py`
Nine new fields in `Settings`, placed immediately after the existing `outcome_flag_*` block (`config.py:340-352`), with a comment block in that block's style:

```python
calibration_enabled: bool = True              # compute hints + /calibration; harmless
calibration_suppression_enabled: bool = False # THE behavior change — off for the soak
calibration_window_days: int = 30
calibration_min_verdicts: int = 5
calibration_fp_threshold: float = 0.60
calibration_clear_threshold: float = 0.40     # hysteresis floor
calibration_hint_max_days: int = 30           # mandatory re-probation
calibration_override_days: int = 90           # how long Brian's un-suppress is sticky
calibration_suppress_high_severity: bool = False  # THE guardrail — do not flip lightly
```

**Two master switches on purpose.** `calibration_enabled=True` ships the measurement and `/calibration` immediately (zero behavior change, immediate value). `calibration_suppression_enabled=False` keeps the gate inert until Brian has read real numbers. This is the same "Phase 1 data-capture ships on its own merits, Phase 2 waits for real data" discipline the confirm-policy layer used after finding 2 `ActionLog` rows in 40 days — and it is the correct posture given §9.6.

### 9.4 Notify kind
One new kind `calibration_suppress` (§2.6). `/mute`-able like every other homelab-convenience kind; **not** added to `_NEVER_MUTABLE_NOTIFY_KINDS` — it is an informational notice, not safety machinery.

### 9.5 Rollout order — each step independently shippable and testable
1. `CalibrationHint` table + `_ensure_outcomeflag_columns` + the nine config fields + `outcomes.active_hint`/`should_page`/`record_flag_ex` (with `record_flag` as the back-compat wrapper) + `tests/test_calibration_loop.py` §8.1-8.2 + CAL51. **Nothing computes, nothing suppresses.**
2. `backend/agents/calibration.py::recompute_hints` + the scheduler job. Hints now compute and persist nightly. **Still nothing suppresses** (`calibration_suppression_enabled=False`).
3. `/calibration` command + the two REST routes + `set_override` + the digest/briefing line extensions. **Fully observable, still zero behavior change.**
4. **SOAK HERE — at least 14 days.** Brian reads `/calibration` and decides whether any rule genuinely deserves suppression. Matches the 14-day Infisical/Hermes soak convention this repo already uses for irreversible-feeling changes.
5. Gate wiring: `homelab_watch.py`'s 3 call sites, then `watchdog.py`'s 4, plus `_db_open_flags`'s `suppressed` filter. Still inert behind the flag.
6. Optional, Brian's call: bump `homelab_watch:unraid_array` and `homelab_watch:vzdump_failed` to `severity="high"` in `_edge_alert` (§3.4), and add `/flag missed …` capture (§6).
7. **Ops step, not a code step:** flip `CALIBRATION_SUPPRESSION_ENABLED=true` in `.env` and restart. This is the moment the feature actually changes behavior, and it should be a deliberate, dated decision Brian makes after reading step 4's data.

### 9.6 Expected day-one behavior — state this plainly so nobody "fixes" it
With `calibration_min_verdicts=5`, a rule needs **five human ✓/✗ taps within 30 days** before it can be suppressed at all. Brian's actual tap volume is unknown and the Outcome Tracker only shipped days ago. **The most likely outcome for the first month is that zero rules get suppressed.** That is correct behavior, not a bug, and the writer must not "helpfully" lower `calibration_min_verdicts` to make the demo work. The near-term value of this build is `/calibration` making the noise visible; the suppression is the payoff once the evidence exists.

### 9.7 Rollback, three levels of severity
- `CALIBRATION_SUPPRESSION_ENABLED=false` → every `should_page` returns `(True, None)`; alerting is byte-identical to today. Hints keep computing, `/calibration` keeps working. **This is the intended lever.**
- `CALIBRATION_ENABLED=false` → the nightly job de-registers, no hints compute, `/calibration` reports disabled, gate inert. The `CalibrationHint` table stays and stops growing (same posture as `contract_canary_enabled`).
- `OUTCOME_FLAGS_ENABLED=false` → the prior spec's §7.7 rollback, which takes the whole tracker down. Not the right lever for a calibration problem; noted so nobody reaches for it.

### 9.8 Ops
Backend-only through step 7. `.\stop.ps1` then `.\start.ps1` after each step. **No `npm run build`** — zero frontend files touched. No new dependencies → `requirements.txt` untouched, and critically the `starlette==0.38.6` re-pin after `mcp` stays as-is. No new secrets. No Hermes-side change. No Council-loop-side change.

---

## 10. Summary of where this spec contradicts NEXUS's own proposal

Handed forward explicitly so Council-loop does not silently "correct" back toward the agent's self-report:

1. **NEXUS's `>60% FP rate` would fire on a sample of one.** Dedup + the existing 30-day FP cooldown mean `calibration_summary`'s counts are not observation counts. §2.3 replaces the denominator with human-verdicted rows only, and §8.14 tests the floor directly.
2. **"Suppress at the source" is the wrong target.** Gating `record_flag` alone would keep paging Brian while destroying the data. §3.1 splits row-write from human-page.
3. **The existing FP cooldown already pages through** — the prior build's suppression is row-only. Fixing that (CAL30) is likely the highest-value single item here, and NEXUS did not notice it.
4. **Time-of-day suppression is deferred** to display-only, against NEXUS's leading example, on sample-size grounds (§1.4).
5. **NEXUS's proposal has no un-suppression story**, and the loop is self-terminating without one (Finding D). §2.5(b)'s mandatory re-probation is load-bearing, not garnish.
6. **NEXUS's proposal has no severity guardrail.** §3.4 adds one and defaults it to protective. The danger of this feature is going blind, not staying noisy.
7. **The briefing advisory line NEXUS dismissed as insufficient stays** — it is extended, not replaced (§3.6), and three live tests depend on its current output.

---

### Critical Files for Implementation
- `C:\Users\Brian\Documents\Agentic os\nexus\backend\agents\outcomes.py`
- `C:\Users\Brian\Documents\Agentic os\nexus\backend\agents\calibration.py` (new)
- `C:\Users\Brian\Documents\Agentic os\nexus\backend\database.py`
- `C:\Users\Brian\Documents\Agentic os\nexus\backend\agents\homelab_watch.py`
- `C:\Users\Brian\Documents\Agentic os\nexus\backend\agents\watchdog.py`
- `C:\Users\Brian\Documents\Agentic os\nexus\backend\agents\telegram_commands.py`
- `C:\Users\Brian\Documents\Agentic os\nexus\backend\scheduler.py`
