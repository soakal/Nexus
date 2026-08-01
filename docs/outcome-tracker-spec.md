# NEXUS Outcome Tracker — Implementation Spec v1

**Origin:** NEXUS's own feature proposal (2026-07-30), asked directly via its chat API by a Claude Code session. **Role:** Opus planner. **Next role:** Sonnet writer (Council-loop or direct).
**Repo:** `C:\Users\Brian\Documents\Agentic os\nexus`

---

## 0. Problem restated against what actually exists

NEXUS surfaces three categories of thing and gets a signal back on exactly one:

| Surface | Where it originates | Existing structured hook | Loop closed? |
|---|---|---|---|
| Broker-dispatched writes | `backend/safety/broker.py::execute_action` | `ActionLog` row, `safety:confirm`/`safety:reject` buttons | **Yes** |
| Homelab edge alerts | `backend/agents/homelab_watch.py` (6 checks) | `events.notify_phone` only; in-memory `_active_alerts` latch | No |
| Watchdog pages | `backend/agents/watchdog.py` (5 checks) | `events.notify_phone` only | No |
| Briefing observations | `backend/agents/briefing.py` — LLM prose from the `context` dict | **None at all** | No |

The three concrete examples NEXUS gave map exactly to this table: "back door unlocked" is `homeassistant.fetch().alerts` → briefing prose (no hook); "only 3 Docker containers" is `unraid.fetch().docker_containers` → briefing prose (no hook); "stale PRs" is `github.fetch().stale_prs` → briefing prose (no hook). None of the three goes through the broker. **This is the load-bearing fact for the data-model decision in §1.**

---

## 1. Data model

### 1.1 Decision: new table, NOT an `ActionLog` extension

Justified from the code, not by preference:

1. **`ActionLog` has no rows to attach to.** Every `ActionLog` row is written by `broker._insert_action_log` (`broker.py:527`), which requires `actor`, `kind`, `target`, `payload_json`, `risk`, `reversibility`, `decision`. A briefing observation has none of these — there is no actor, no dispatch, no target. Recording one would mean inventing six fake fields per row.
2. **`decision` is a live state machine, not a free field.** `_TERMINAL_DECISIONS` (`broker.py:78`) gates idempotency replay in `_find_completed_action`; `_claim_action_log_for_confirm` (`broker.py:695`) does a raw `UPDATE actionlog SET decision=... WHERE decision='needs_confirm'`. Adding `resolved`/`deferred`/`false_positive` into this column would silently interact with the double-confirm race guard and the replay filter. Adding a *parallel* status column instead leaves every one of the existing rows permanently NULL on it.
3. **`ActionLog` is immutable-by-convention and audit-shaped.** `database.py:199-202` and `GET /api/safety/actions` both treat it as "what NEXUS did." Mixing in "what NEXUS noticed" changes the meaning of the Safety page's Recent Actions view and of every existing test that filters it.

The counter-case (a `needs_confirm` action that expired unanswered *is* a flag-shaped thing) is handled by a nullable FK, not by merging the tables — see `action_log_id` below.

### 1.2 New table `OutcomeFlag` — `backend/database.py`

Add after `SecretFallback` (~line 630). Created by `create_all` (new table), plus one index shim (§7).

```python
class OutcomeFlag(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    source: str = Field(index=True)      # homelab_watch | watchdog | briefing | contracts | manual
    check: str                            # garage_open | stale_prs | ha_alerts | vm:{vmid} | ...
    fingerprint: str = Field(default="", index=True)   # f"{source}:{check}" — dedup key
    summary: str                          # one-line human-readable, <=300 chars, plain text
    detail: str | None = None             # optional longer body / JSON blob
    severity: str = "medium"              # low | medium | high  (matches ActionLog.risk / Goal.risk)
    status: str = "open"                  # open | resolved | deferred | false_positive | needs_follow_up
    resolved_at: datetime | None = None
    resolved_by: str | None = None        # "telegram" | "api" | "auto:condition_cleared" | "auto:expired"
    resolution_note: str | None = None
    deferred_until: datetime | None = None
    action_log_id: int | None = Field(default=None, index=True)  # set only when a broker action accompanied this flag
    surfaced_count: int = 1               # incremented each time the source re-observes while still open
    last_surfaced_at: datetime = Field(default_factory=datetime.utcnow)
    created_at: datetime = Field(default_factory=datetime.utcnow, index=True)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
```

**Vocabulary choices, each grounded in existing code:**
- `severity` reuses `low|medium|high` from `ActionLog.risk`/`Goal.risk` rather than inventing `info|warn|critical` — one less vocabulary in the codebase.
- `status` uses NEXUS's own four words verbatim plus `open`.
- `resolved_by="auto:condition_cleared"` mirrors the existing `Goal.approved_by == "auto:low_risk_reversible"` convention that `digest._db_recent_autonomous_goals` already keys off.
- `fingerprint` + a partial unique index mirrors `Goal.fingerprint` + `ux_goal_fingerprint_active` (`database.py:483-487`) exactly — the established prior art in this repo for "at most one active row per logical thing."

### 1.3 Explicitly NOT on `SystemState`

`governor.py`'s CSV-on-singleton idiom (`policy_auto_allow_kinds`, `muted_notify_kinds`, `auth_burst_alert_sources`) is right for "a handful of bare strings, no per-item metadata." A flag carries eight mutable per-item fields and unbounded row count. Confirmed by reading `_add_csv_kind`/`_remove_csv_kind` — there is nowhere to put `resolution_note`. Own table.

---

## 2. Write path

### 2.1 New module `backend/agents/outcomes.py`

Follows the established shape: sync `_db_*` helpers that open their own `Session`, async wrappers that call them via `asyncio.to_thread`, best-effort throughout (`digest.py` and `facts.py` are the reference implementations).

**Public async API:**

| Function | Signature | Contract |
|---|---|---|
| `record_flag` | `(source, check, summary, *, detail=None, severity="medium", action_log_id=None) -> int \| None` | Returns the row id (new or existing-open), or `None` if suppressed/disabled/errored. **NEVER raises** — same contract as `events.notify_phone`. |
| `resolve_flag` | `(flag_id, status, *, note=None, by="api", defer_days=None) -> str` | Returns `"resolved"` \| `"not_found"` \| `"already_closed"` \| `"invalid_status"`. Mirrors `broker.reject_action`'s (status-string) return convention so Telegram/REST/tests share one mapping. |
| `clear_flag` | `(source, check, *, by="auto:condition_cleared") -> int` | Auto-resolves any open flag matching the fingerprint. Returns count. |
| `open_flags` | `(limit=50) -> list[dict]` | Open + `needs_follow_up` + `deferred`-past-due, newest first. |
| `recently_closed` | `(hours=48, limit=30) -> list[dict]` | For the "do not re-raise" block. |
| `calibration_summary` | `(days=30) -> dict` | Per-`source`/`check` counts by status. |
| `sweep_deferred` | `() -> list[int]` | Flips `deferred` rows past `deferred_until` back to `needs_follow_up`. |

**Suppression logic inside `record_flag` (this is the actual feature):**

Given `fingerprint = f"{source}:{check}"`, in one transaction:
1. Existing row with `status="open"` or `"needs_follow_up"` → increment `surfaced_count`, bump `last_surfaced_at`, return its id. No new row. *(This is "stop re-surfacing things already raised".)*
2. Existing row with `status="deferred"` and `deferred_until > now` → return `None`. *(Deferred means shut up until then.)*
3. Existing row with `status="false_positive"` and `resolved_at` within `outcome_flag_false_positive_cooldown_days` (default 30) → return `None`. *(This is "stop crying wolf on the same pattern".)*
4. Otherwise INSERT with `status="open"`. Catch `IntegrityError` from the partial unique index → re-SELECT and return the winner's id (same insert-then-catch pattern `goals.propose()` uses against `ux_goal_fingerprint_active`).

Gated by a new `Settings.outcome_flags_enabled: bool = True` (`backend/config.py`), read with `getattr(s, ..., True)` — same defensive read every other check uses.

### 2.2 Concrete call sites

**A. `backend/agents/homelab_watch.py` — 6 flags, mostly through one choke point**

`_edge_alert` (line 50) already carries a `key` that is precisely the fingerprint we want (`"unraid_array"`, `"unraid_temp"`, `"garage_open"`, `"vzdump_failed"`). Restructure it to:
- On `active=False`: call `outcomes.clear_flag("homelab_watch", key)` **before** the existing `_active_alerts.discard(key)`. This is the free auto-resolve — a garage that got closed resolves itself.
- On `active=True` and not already latched: call `flag_id = await outcomes.record_flag("homelab_watch", key, message, severity=...)` **first**, then pass the returned id into the `notify_phone(buttons=...)` call (§3.1).

**Leave `_active_alerts` in place, unchanged.** Do not replace it with the DB. Reason: the module docstring (lines 14-24) documents the in-memory latch as a deliberate, drilled choice, and its restart-re-fire behavior is explicitly accepted ("safe direction, a duplicate not a miss"). `record_flag` is independently idempotent by fingerprint, so on a restart-re-fire the alert re-sends (unchanged behavior) but no duplicate flag row appears. This makes the change strictly additive with zero regression surface on a proven alert path.

The two transition-triggered checks call `notify_phone` directly and need explicit calls:
- `check_proxmox_vms` (line 82) → `record_flag("homelab_watch", f"vm:{vmid}", ...)`, severity `high`.
- `check_docker` (line 111) → `record_flag("homelab_watch", f"docker:{name}", ...)`, severity `medium`.
- Both also need a `clear_flag` when the object transitions back to running (the `prev != "running" and status == "running"` case, which currently has no branch — add it).

**B. `backend/agents/watchdog.py` — 4 flags**

Add `record_flag` immediately before each `notify_phone`, reusing the existing debounce so no extra alerting fires:
- `check_scheduler_stalls` (line 119) → `("watchdog", f"stall:{job.id}", ...)`, `high`.
- `check_dead_letters` (line 172) → `("watchdog", "dead_letters", ...)`, `high`.
- `check_auth_failure_burst` (line 255) → `("watchdog", f"auth_burst:{src}", ...)`, `high`.
- `check_integration_contracts` (line 327) → `("contracts", f"breach:{name}", ...)`, `high`.
- `check_budget_warning` (line 205) → **do NOT flag.** It is self-clearing on a calendar boundary and already once-per-day; a flag adds nothing. Note this in the code comment so the writer doesn't "helpfully" add it.

**C. `backend/agents/briefing.py` — the important one, deterministic only**

This is where NEXUS's three examples actually live, and the write must be **structured, not prose-derived**. Add `_record_briefing_flags(context: dict) -> None` called **after** the `sonnet()` call, alongside `_build_protonmail_section` (line 294) — same "assembled in Python, never in the prompt" discipline that section's docstring establishes.

Flags derived from the already-built `context` dict (lines 230-268):

| Flag `check` | Source field | Fire when |
|---|---|---|
| `ha_unavailable_entities` | `context["home_assistant"]["alerts"]` | non-empty |
| `unraid_array` | `context["unraid"]["array_status"]` | not `"started"` and not `"unknown"` |
| `unraid_parity` | `context["unraid"]["parity_status"]` | indicates a running/failed parity check |
| `github_stale_prs` | `context["github"]["stale_prs"]` | non-empty |
| `unifi_new_devices` | `context["unifi"]["new_devices"]` | non-empty |
| `adguard_filtering_off` | `context["adguard"]["filtering_enabled"]` | is `False` (**not** `None` — `None` means the read failed, per the 2026-07-26 companion fix) |

And symmetric `clear_flag` calls when each condition is absent, so yesterday's stale-PR flag auto-resolves when the PR merges.

**Explicitly NOT parsed: the `## Priority Actions` LLM prose.** Porting `frontend/src/lib/priorityActions.js`'s parser to Python was considered and rejected: LLM prose has no stable fingerprint across days, so dedup — the entire point of the feature — would fail on day two. Structured-source-only in v1.

**D. `backend/agents/telegram_commands.py` — `/flag`**

New handler `_cmd_flag(args, msg)` → `record_flag("manual", <slugified first words>, args, severity="medium")`. Lets Brian log his own item into the same store. One line in the `COMMANDS` dict.

---

## 3. Close-the-loop path

### 3.1 Telegram inline buttons — the primary UX

Extend the existing callback pattern in `backend/agents/telegram_poller.py`. New namespace `flag`:

- `callback_data` format `flag:<verb>:<id>` where verb ∈ `{resolved, false_positive, deferred}`. Longest is `flag:false_positive:99999` = 25 bytes, comfortably under Telegram's 64-byte limit (the constraint `homelab_watch.py:109` already checks for container names).
- Add `"flag"` to `_INT_ID_NAMESPACES` (line 58) — the id is a DB primary key, so int-coercion applies, same as `goal`/`safety`.
- Add `"resolved"` to `_AFFIRMATIVE_VERBS` (line 59) so the edit-message icon renders `✓` not `✗`.
- New branch in `_dispatch` (after the `safety` branch, ~line 104) calling `outcomes.resolve_flag`, with a status→message mapping identical in shape to the `safety` branch's:
  ```
  {"not_found": "Flag not found.", "already_closed": "Already closed.",
   "resolved": "Marked resolved.", ...}
  ```
  Return `definitive=True` for all mapped outcomes (a flag resolve never dispatches anything, so there is no transport-error case to keep buttons for — unlike `docker:restart`).

**Two buttons on the alert, not four.** Attach only `✓ Resolved` and `✗ False alarm`. Justification: this matches the established 2-button precedent everywhere else (`goal:approve`/`goal:reject`, `safety:confirm`/`safety:reject`), and `deferred`/`needs_follow_up` are lower-frequency and better served by the text command below. A 4-button keyboard on every homelab alert is the kind of friction that gets a feature muted.

### 3.2 Telegram text commands — `backend/agents/telegram_commands.py`

Three new handlers + `COMMANDS` entries:
- `/flags` → `outcomes.open_flags()`, rendered `#{id} [{severity}] {source}:{check} — {summary} ({age})`. Same shape as `_cmd_goals`/`_cmd_tasks`.
- `/resolve <id> [status] [note]` → `outcomes.resolve_flag(id, status or "resolved", note=..., by="telegram")`. Default status `resolved` so the common case is `/resolve 12`.
- `/defer <id> <days> [note]` → `resolve_flag(id, "deferred", defer_days=days)`.

Note `_match_voice_command` (`telegram_poller.py:216`) will now match spoken "flags"/"resolve"/"defer" — a strict improvement (these are new command names that would otherwise fall through to `chat()`'s HOME_CONTROL branch, the exact 2026-07-27 incident class). No change needed there; it reads `COMMANDS` dynamically.

### 3.3 REST — `backend/api/safety.py`

This is the Claude-Code-facing path. Bearer-gated via `Depends(require_api_key)` like every other route in the file — the same key this session already used to hit the chat API ad hoc.

- `GET /api/safety/flags?status=&source=&limit=` — mirrors `list_actions` (lines 33-75) exactly: `Depends(get_session)`, capped limit, filters, newest-first, ISO-formatted timestamps.
- `POST /api/safety/flags/{flag_id}/resolve` — body `{status, note?, defer_days?}`. Status codes mirroring `confirm_action` (lines 155-196): `200` ok, `404` not found, `409` already closed, `400` invalid status.
- `POST /api/safety/flags` — manual create (`source="manual"`), for Claude Code sessions logging their own observations.
- `GET /api/safety/flags/calibration?days=` → `calibration_summary`.

### 3.4 Agent-facing tool — `backend/agents/tools.py`

Add one `ReadTool` named `open_flags` (no-args schema) whose dispatcher calls `outcomes.open_flags()` and formats a compact string. This is read-only and does not import the broker, so it satisfies that module's stated invariant (docstring lines 6-9).

**No write tool in v1.** A `resolve_flag` write tool would belong in `write_tools.py` behind a new broker kind — and letting NEXUS close its own loop destroys the signal the feature exists to capture. Explicitly out of scope; note it in the module docstring.

### 3.5 Deferred sweeper

`outcomes.sweep_deferred()` called as a sixth check inside the existing `watchdog.run_watchdog()` 5-minute job. Flips `deferred` rows whose `deferred_until` has passed to `needs_follow_up` and pages once per flag via `notify_phone(kind="flag_followup", buttons=[...])`.

**Deliberately inside `run_watchdog()` and NOT a new scheduler job** — with one caveat the writer must handle: `run_watchdog` is gated by `watchdog_enabled`, and the 2026-07-28 `SecretFallback` work explicitly called out that coupling a durability guarantee to that flag repeats the budget-warn wart. Here the coupling is acceptable because a missed sweep is a *late reminder*, not lost data (the row persists). Give it its own `outcome_flag_sweep_enabled` sub-flag anyway, matching `budget_warn_enabled`'s precedent.

---

## 4. Read path

### 4.1 Briefing — suppression (pre-LLM)

In `run_briefing()` (`briefing.py:192`), add `outcomes.open_flags()` and `outcomes.recently_closed(hours=48)` to the existing `asyncio.gather` block. Inject into the prompt as a new `BRIEFING_PROMPT` section placed **before** `## Priority Actions`:

```
KNOWN OPEN ITEMS (already raised with the user — reference them if still relevant,
but do NOT present them as new findings):
{open_items_block}

RECENTLY CLOSED (last 48h — the user already handled these; do NOT re-raise):
{closed_items_block}
```

Both blocks capped at `outcome_flag_briefing_max` (default 10) items to bound prompt growth, degrading to `"(none)"` on empty or on any fetch exception (the `safe()`/`isinstance(..., Exception)` pattern already used throughout that function).

### 4.2 Briefing — deterministic Open Items section (post-LLM)

Append a `## Open Items` section built in Python **after** the `sonnet()` call, exactly alongside `proton_section` (line 303). Lists open flags with `id`, age, and summary. This is the trustworthy surface — no LLM in the path, so it can't hallucinate a resolved item back into existence.

**Add `"Open Items"` to `_UNVERIFIED_FACT_SECTIONS`** (`briefing.py:23`). This is not optional: without it, `extract_and_store` would turn "garage door has been open for over 30 minutes" into a durable 0.9-confidence `Fact`, which the goal proposer then treats as grounds for an autonomous investigation — the precise incident that list's comment documents.

### 4.3 Chat

In `chat.py`'s `CHAT` branch (line 471), add `outcomes.open_flags(limit=10)` to the existing `asyncio.gather`, coerce exceptions to `[]`, and thread it into `memory.assemble()` as a fourth optional block:

```
[OPEN ITEMS] (flagged to you, not yet closed — reference with the id if the user asks)
```

`memory.assemble` (`memory.py:86`) is a pure function with an existing optional-parameter pattern (`facts_str`); add `flags_str: str = ""` the same way, keeping the "" → skip-section behavior. Its existing tests stay green because the parameter defaults to empty.

### 4.4 Calibration

`calibration_summary(days=30)` returns per-`source:check` counts by status. Two consumers in v1:
- Injected into the briefing prompt as one advisory line: `"Flag calibration (30d): homelab_watch:garage_open — 8 raised, 6 false_positive."` Advisory only; the LLM may soften its tone.
- Surfaced in `digest.build_autonomy_digest()` as one line, matching the existing `Completed (24h)` line style.

**No automatic suppression by false-positive rate in v1.** The `false_positive` cooldown in §2.1 step 3 is the only automatic behavior change; a rate-based auto-mute is a Phase 2 decision that needs real data first — the same reasoning that deferred the confirm-policy learner (`ActionLog` had 2 rows in 40 days).

---

## 5. Scope boundaries — explicitly OUT of v1

1. **No LLM classification of outcomes.** No "did Brian probably resolve this?" inference. A human tap or an observable condition-clear only. This matches the `safety:confirm`/`safety:reject` precedent and NEXUS's own framing.
2. **No auto-suppression by false-positive rate.** Only the fixed 30-day post-false-positive cooldown.
3. **No parsing of `## Priority Actions` LLM prose.** Structured `context` fields only (§2.2-C).
4. **No agent-facing write tool.** NEXUS must not close its own loop (§3.4).
5. **No frontend page.** REST + Telegram + briefing cover v1. A `Flags.jsx` page is Phase 2. (Note: adding one requires `npm run build` — see CLAUDE.md.)
6. **No Obsidian/Vault write path.** NEXUS proposed "probably the Vault," but the Vault is a nightly-organized markdown store with no query surface — you cannot ask it "what's open." SQLite is the store; an `obsidian.emit_event("flag.resolved", ...)` emitter is a cheap Phase 2 addition that fits the existing v1 event-type list (`goal.approved`/`goal.rejected`/`goal.completed`/`goal.failed`).
7. **Retiring the soak-reminder jobs is NOT in scope.** NEXUS claimed the Aug 7 audit-soak hack "becomes unnecessary." Partially true — `deferred_until` + `sweep_deferred` does replace the *scheduling* mechanism (`INFISICAL_SOAK_REMINDER_AT` / `HERMES_SOAK_REMINDER_AT` constants + one-off `DateTrigger` job + a dedicated test file each). But `_infisical_soak_reminder` also *drains and quotes real `SecretFallback` data*, which a generic deferred flag cannot do. And moot for these two: they fire 2026-08-03 and 2026-08-05, before this could ship. Use the pattern for the *next* soak, don't retrofit these.
8. **`ActionLog` is not modified.** No new columns, no new decision values, no changes to `_TERMINAL_DECISIONS` or `_claim_action_log_for_confirm`.

---

## 6. Acceptance criteria

New file `tests/test_outcome_flags.py` (pytest only, no new framework — matches every existing test file).

**6.1 Data model / migration**
- `AC1` `create_db_and_tables()` on a fresh `:memory:` engine creates `outcomeflag` with all declared columns.
- `AC2` `create_db_and_tables()` is idempotent — calling it twice raises nothing and creates no duplicate index.
- `AC3` The partial unique index rejects a second `status="open"` row with the same `fingerprint`, and permits one when the first is `resolved`.

**6.2 Write path / dedup — the core of the feature**
- `AC4` `record_flag` on an empty DB inserts one row, `status="open"`, `surfaced_count=1`, and returns its id.
- `AC5` A second `record_flag` with the same `(source, check)` returns the **same id**, creates **no** second row, and increments `surfaced_count` to 2 and advances `last_surfaced_at`.
- `AC6` After `resolve_flag(id, "resolved")`, a subsequent `record_flag` with the same fingerprint creates a **new** row (a genuinely recurring condition re-raises).
- `AC7` After `resolve_flag(id, "false_positive")`, a `record_flag` with the same fingerprint within the cooldown returns `None` and creates no row; past the cooldown it creates one.
- `AC8` After `resolve_flag(id, "deferred", defer_days=7)`, `record_flag` returns `None` while `deferred_until` is future.
- `AC9` `record_flag` never raises: with the DB engine patched to raise, it returns `None` and logs. (Mirrors `test_notify_loudness.py`'s never-raises assertions.)
- `AC10` `record_flag` is a no-op returning `None` when `outcome_flags_enabled=False`.

**6.3 Close-the-loop**
- `AC11` `resolve_flag` returns `"not_found"` for an unknown id, `"already_closed"` for a non-open row, `"invalid_status"` for a status outside the five.
- `AC12` A successful `resolve_flag` sets `resolved_at`, `resolved_by`, `resolution_note`, and `status`, and leaves `created_at`/`summary` untouched.
- `AC13` (`tests/test_telegram_poller.py`) `handle_callback` with `data="flag:resolved:1"` from the authorized chat calls `outcomes.resolve_flag(1, "resolved", by="telegram")`, answers the callback exactly once, and edits the message with a `✓` prefix.
- `AC14` (`tests/test_telegram_poller.py`) The same callback from an **unauthorized** `chat.id` calls `resolve_flag` zero times and answers with "Not authorized" — the fail-closed regression guard that file already enforces for `goal:`/`safety:`.
- `AC15` (`tests/test_telegram_poller.py`) `"flag"` is in `_INT_ID_NAMESPACES`; `data="flag:resolved:abc"` answers "Invalid id." and dispatches nothing.
- `AC16` (`tests/test_telegram_commands.py`) `/flags` with no open flags replies "No open flags."; with flags, the reply contains each id. `/resolve 3 false_positive typo` calls `resolve_flag(3, "false_positive", note="typo", by="telegram")`.
- `AC17` (`tests/test_api_endpoints.py`) `GET /api/safety/flags` without a Bearer key returns 401; with one, returns a JSON list. `POST /api/safety/flags/{id}/resolve` returns 200 / 404 / 409 / 400 per §3.3.

**6.4 Source wiring**
- `AC18` (`tests/test_homelab_watch.py`) `check_garage` with the door open past threshold calls `record_flag("homelab_watch", "garage_open", ...)` **before** `notify_phone`, and the `notify_phone` call carries `buttons` containing `flag:resolved:{returned_id}`.
- `AC19` (`tests/test_homelab_watch.py`) `check_garage` on the next tick with the door **closed** calls `clear_flag("homelab_watch", "garage_open")`, and the flag's `status` becomes `resolved` with `resolved_by="auto:condition_cleared"`.
- `AC20` (`tests/test_homelab_watch.py`) With `homelab_watch.reset()` simulating a restart while the condition persists, the alert re-fires (unchanged existing behavior) but `record_flag` returns the existing id and the row count stays 1. **This is the no-regression guard for leaving `_active_alerts` alone.**
- `AC21` (`tests/test_watchdog.py`) Each of the four flagging checks calls `record_flag` with its documented `(source, check)`; `check_budget_warning` calls it **zero** times.
- `AC22` (`tests/test_briefing.py`) A `context` with non-empty `github.stale_prs` records `("briefing", "github_stale_prs")`; a subsequent run with an empty `stale_prs` clears it.
- `AC23` (`tests/test_briefing.py`) `adguard.filtering_enabled is None` records **no** flag (only `False` does) — guards the 2026-07-26 unknown-vs-off distinction.

**6.5 Read path**
- `AC24` (`tests/test_briefing.py`) With one open flag, the briefing prompt passed to the mocked `sonnet` contains its summary under the KNOWN OPEN ITEMS header; with none, it contains `(none)` and the briefing still succeeds.
- `AC25` (`tests/test_briefing.py`) The returned briefing text contains a `## Open Items` section, and `_strip_unverified_sections` removes it — so the text passed to `extract_and_store` contains none of the flag summaries.
- `AC26` (`tests/test_briefing.py`) An exception from `open_flags` degrades to `(none)` and the briefing still completes (the `return_exceptions=True` discipline).
- `AC27` (`tests/test_chat_memory.py`) `memory.assemble("", "", "", "")` returns `""`; with only `flags_str` it returns a block containing the `[OPEN ITEMS]` header. Existing 3-arg calls still behave identically.
- `AC28` `calibration_summary(days=30)` groups by `source:check` with correct per-status counts and returns `{}` on an empty table.

**6.6 Sweeper & retention**
- `AC29` `sweep_deferred()` flips a row whose `deferred_until` is past to `needs_follow_up` and leaves a future-dated one alone.
- `AC30` (`tests/test_watchdog.py`) `run_watchdog()`'s return dict gains a `deferred_swept` key, and every pre-existing key is still present with unchanged semantics.
- `AC31` (`tests/test_backup.py`) `prune_old_outcome_flags()` deletes closed rows older than `outcome_flag_retention_days` and **never** deletes an `open` or `needs_follow_up` row regardless of age.

**6.7 Invariants**
- `AC32` `backend/agents/outcomes.py` does not import `backend.safety.broker` (grep-style assertion, matching `test_tools.py`'s existing no-broker-import guard).
- `AC33` No `Session` or ORM object crosses an `await` in `outcomes.py` — every DB helper is sync and every async wrapper calls it via `asyncio.to_thread`.
- `AC34` `tests/test_spend_report.py::test_no_unlabeled_llm_calls_in_agents` still passes — trivially, since v1 adds **zero** LLM calls. Make this explicit in the writer's checklist: any `haiku`/`sonnet`/`opus` call added to `outcomes.py` must carry `label=`, and v1 should add none.

---

## 7. Migration / rollout

**7.1 Schema.** `OutcomeFlag` is a brand-new table → created by `SQLModel.metadata.create_all(engine)` with **no `_ensure_*_columns` shim**, matching `TaskOutcome`, `Fact`, `SecretFallback`, `MailVoiceProfile`. No Alembic.

**7.2 Index shim.** The partial unique index needs one addition to `backend/database.py`, following `_ensure_goal_columns`'s inline pattern (lines 470-490) verbatim:

```python
def _ensure_outcomeflag_index():
    """Partial unique index: at most one OPEN flag per fingerprint. Hard backstop
    against record_flag()'s check-then-insert TOCTOU, exactly as
    ux_goal_fingerprint_active backstops goals.propose(). fingerprint != ''
    excludes directly-constructed test rows, same carve-out."""
    try:
        with engine.connect() as conn:
            conn.execute(text(
                "CREATE UNIQUE INDEX IF NOT EXISTS ux_outcomeflag_open "
                "ON outcomeflag(fingerprint) WHERE status = 'open' AND fingerprint != ''"
            ))
            conn.commit()
    except Exception as e:
        logger.warning(f"_ensure_outcomeflag_index create failed: {e}")
```

Registered in `create_db_and_tables()` (line 640) after `_ensure_processedmail_columns()`. Best-effort, never fatal to startup — the same contract every other shim in that file carries.

**7.3 Config.** New fields in `backend/config.py`'s `Settings` (plain `.env`-overridable, not secrets), with a comment block matching the `homelab_watch_enabled` / `contract_canary_enabled` style:

```python
outcome_flags_enabled: bool = True
outcome_flag_sweep_enabled: bool = True
outcome_flag_false_positive_cooldown_days: int = 30
outcome_flag_retention_days: int = 180
outcome_flag_briefing_max: int = 10
```

**7.4 Retention.** New `prune_old_outcome_flags()` in `backend/agents/backup.py`, registered in the nightly 03:45 `retention_prune` job alongside `prune_old_uptime_samples`/`prune_old_traces`. It cannot reuse `_prune_table_by_cutoff` (which takes only table + column + cutoff, with no extra `WHERE`) — write a sibling with the same `_PRUNE_BATCH_SIZE` batching and commit discipline plus `AND status != 'open' AND status != 'needs_follow_up'`.

**7.5 Rollout order (each step independently shippable and testable):**

1. Table + index shim + config + `outcomes.py` + `tests/test_outcome_flags.py`. Nothing writes yet.
2. REST endpoints in `api/safety.py`. Now Claude Code can create and resolve manually — validates the loop end-to-end with zero risk to any alerting path.
3. Telegram callbacks + `/flags` `/resolve` `/defer` `/flag`. Now Brian can close the loop from his phone.
4. Write-path wiring: `homelab_watch.py` first (highest volume, cleanest fingerprints), then `watchdog.py`.
5. Briefing write path (`_record_briefing_flags`) — this is where the three examples NEXUS actually cited get captured.
6. Read path: briefing prompt injection + `## Open Items` + `_UNVERIFIED_FACT_SECTIONS` + chat memory block.
7. Sweeper + retention + calibration in the digest.

**7.6 Ops.** Backend-only through step 6 → `stop.ps1` then `start.ps1` after each step. No `npm run build` needed (no frontend change in v1). No new dependencies, so `requirements.txt` is untouched — importantly, the `starlette==0.38.6` re-pin after `mcp` stays as-is.

**7.7 Rollback.** Set `outcome_flags_enabled=false` in `.env` and restart. Every `record_flag` becomes a `None`-returning no-op; every read path already degrades to `(none)`/`[]`; alerts and briefings behave exactly as they do today. The table stays and simply stops growing — the same reversibility posture as `contract_canary_enabled`.

---

### Critical Files for Implementation
- `C:\Users\Brian\Documents\Agentic os\nexus\backend\database.py`
- `C:\Users\Brian\Documents\Agentic os\nexus\backend\agents\outcomes.py` (new)
- `C:\Users\Brian\Documents\Agentic os\nexus\backend\agents\homelab_watch.py`
- `C:\Users\Brian\Documents\Agentic os\nexus\backend\agents\briefing.py`
- `C:\Users\Brian\Documents\Agentic os\nexus\backend\agents\telegram_poller.py`
- `C:\Users\Brian\Documents\Agentic os\nexus\backend\api\safety.py`
