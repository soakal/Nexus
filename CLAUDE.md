# NEXUS — Agentic OS · Claude Code Context

Production-grade personal AI OS for Windows 11. FastAPI backend + React/Vite frontend, a system-tray launcher, and a multi-agent layer that talks to a homelab.

> Also read the user's master map at `C:\Users\Brian\CLAUDE.md` for global rules (model pipeline, secrets, deploy confirmations). This file is the project-local detail.

**Test-isolation fix cherry-picked from the `linux-lxc` branch (2026-08-14, `master`, `tests/`
only)** — the Linux port (`nexus-linux-lxc` worktree, branch `linux-lxc`, real host: Proxmox LXC
207) live-reproduced a critical bug twice on 2026-08-14: `backend/secrets/vault.py::set_secret()`
calls `backup_vault()` on every write, best-effort, with no test isolation — a bare `pytest`
invocation with no `.env` in cwd resolves `unraid_backup_path` to `backend/config.py`'s real
hardcoded default (`\\192.168.1.50\Computer Backup\Nexus_backup`) and writes/mirror-deletes for
real. This host's own `backend/backup.py` is the pre-port Windows-only version (`_mount_unc` +
`shutil.copy2`, no rclone) but carries the exact same exposure — a test resolving the real UNC
path could `shutil.copy2` straight onto it with no explicit mount step needed, since this host
already has share access. `tests/conftest.py` gained the same two-layer defense as the Linux
branch, adapted to this file's own seam: `os.environ["UNRAID_BACKUP_PATH"] = ""` (assignment, not
setdefault — must beat both a real shell value and the cwd `.env`) forced before any backend
import, plus a new autouse `_isolate_backup_targets` fixture patching `backend.backup._mount_unc`
to hard-fail (the analogous guard to the Linux branch's `_run_rclone` patch — `_mount_unc` is this
codebase's actual network-authenticating seam; note it does NOT fully close the gap on its own,
since `shutil.copy2` can still reach an already-accessible real UNC path independent of whether
`_mount_unc` succeeds — the env override is what actually prevents that). Two scheduler job-count
tests in `tests/test_coverage_boost.py` (`test_setup_scheduler_adds_jobs`,
`test_auth_burst_check_adds_no_scheduler_job`) regressed from this change (vault_backup's job
registration is gated behind `if unraid_backup_path:`, now empty by default) — fixed the same way
the Linux branch fixed the identical regression: both tests explicitly set a fake non-empty
`UNRAID_BACKUP_PATH` + reset `config._settings_instance` to `None`, scoped to themselves. Full
suite re-run clean: 1965 passed, 1 skipped, 1 failed (the pre-existing time-of-day-flaky proposer
test, unrelated — see below). No production code touched, tests-only change. The real data-loss
incident itself happened entirely on the Linux branch/host, not here — nothing on this Windows
instance's actual backup data was affected; this cherry-pick is pure prevention.

**Instance-ownership split, Windows vs LXC — Track A (2026-08-15, `master`)** — same night
as the Brain Organizer fix below, Brian asked point-blank "what system will run my brain
organizer," and the honest answer (both would) led to a bigger decision: **"I want the LXC
handling everything from this point, turn off the features in the Windows version."** That's
effectively Phase 6 (cutover) of the migration spec, which the spec itself gates behind a
14-day burn-in — the LXC was one day old and had already produced two real incidents that
same day (the Unraid backup deletion, a live mail-autodraft duplication bug). Fable-planned a
split: **Track A** (this entry) stops every currently-active duplication tonight, config-only,
zero risk; **Track B** (the genuinely high-stakes cutover decisions — Telegram bot ownership,
the autonomy/kill-switch flip, Uptime Kuma repointing, the HTTPS origin move, vault topology)
is deliberately deferred to its own confirmed sitting, not bundled into tonight's fix. A real
bug surfaced during planning: an LXC-sent `goal:approve:<id>` Telegram button would be consumed
by Windows's poller (the only `getUpdates` consumer) and could approve the WRONG goal on
Windows, since the two instances' goal ids overlap (the LXC's DB is a stale seed of Windows's)
— one more reason ownership-flipping needs to be deliberate, not assumed from "turn off
Windows's duplicates."
- **Ownership choices**: mail autodraft → **LXC** (Brian's explicit word; drafts are
  reversible; content derives from the shared mailbox, not a local DB). Everything
  Telegram-interactive or local-DB-derived (`goal_proposer`, `goal_recurrence`,
  `autonomy_digest`, `homelab_watch`, `homelab_digest`, `spend_report`, `facts_digest`,
  `anthropic_balance_watch`, `morning_briefing`) → **stays on Windows**, since it's still the
  only `getUpdates` consumer (buttons/commands act on ITS db) and its Fact/Goal DBs are the
  live ones — those eight-plus-one get disabled on the LXC's `.env` instead.
- **`MAIL_AUTODRAFT_ENABLED=false`** added to this instance's `.env` (flag already existed,
  `config.py:39`, gate at `scheduler.py:642-649` — unchanged; also stops this instance's
  nested autotrash, which runs inside `autodraft_tick`). **New `wiki_fragmentation_report_enabled`
  flag** (default True) added, same narrow-gate pattern as `brain_organizer_nightly_enabled`
  below — gates only the Sunday 02:30 job, not `wiki_ingest.py`'s module import. Set False on
  this instance's `.env`; pairs with `brain_organizer` as one 02:00→02:30 LXC-owned pipeline.
- **Consequence stated explicitly, not left implicit**: mail autotrash now runs on NEITHER
  instance (Windows's just stopped; the LXC's was already `false` per its own shadow-mode
  `.env`). Junk accumulates in the inbox until a Track B decision assigns ownership. Deliberate,
  safe direction (no false positives, just no auto-cleanup for now).
- **Tests**: the two job-count tests in `tests/test_coverage_boost.py` gained
  `monkeypatch.setenv("MAIL_AUTODRAFT_ENABLED", "true")` +
  `monkeypatch.setenv("WIKI_FRAGMENTATION_REPORT_ENABLED", "true")` (same "this host's real
  `.env` doesn't match what the test wants to count" pattern hit twice already tonight for
  `UNRAID_BACKUP_PATH`/`BRAIN_ORGANIZER_NIGHTLY_ENABLED`). Also fixed
  `tests/test_config_validation.py::test_mail_autodraft_settings_defaults`, which constructs a
  bare `Settings()` reading the real `.env` directly — added the same protective monkeypatch.
  New `test_wiki_fragmentation_report_disabled_skips_job` /
  `test_wiki_fragmentation_report_enabled_default_is_true`, mirroring the brain-organizer pair.
  Full suite green after (1967 passed / 1 skipped / 1 known-flake, unchanged).
- **Verified post-restart**: startup log shows `Wiki fragmentation report DISABLED` and no
  `Mail autodraft enabled:` line (that flag's gate has no `else`-branch log — silent skip by
  original design, confirmed correct via absence rather than a positive message); `:8765` MCP
  server still spawned; `/api/health` ok.
- **LXC-side companion change** (own repo, `linux-lxc` branch — see that repo's own CLAUDE.md):
  eight `_ENABLED=false` lines appended to `/var/lib/nexus/.env` for the Windows-owned jobs
  above, plus a new `morning_briefing_enabled` flag (code) + `MORNING_BRIEFING_ENABLED=false`
  (env) so Windows keeps the daily briefing. **A gap was caught and fixed the same sitting**:
  the `.env` edit for `morning_briefing` was initially forgotten (only the eight other flags
  were appended) — caught by directly querying the live scheduler's registered job ids after
  restart rather than trusting the log lines alone, since not every flag's gate logs a disabled
  message. Final verified job list on the LXC confirmed all nine Windows-owned jobs absent,
  `brain_organizer`/`wiki_fragmentation_report`/`mail_autodraft` present (LXC-owned), every
  per-instance self-monitoring job (watchdog, backups, uptime, state refresh, etc.) present on
  both — deliberately never touched, since the LXC has no Uptime Kuma monitor yet and its own
  watchdog pages are its only failure signal for now.
- **Track B — deliberately NOT done tonight, each needs its own explicit confirmation later**:
  Telegram bot ownership (flipping `telegram_poll_enabled` makes the LXC the one Brian actually
  interacts with — user-facing, not background housekeeping), autonomy/kill-switch
  (`SystemState.autonomy_enabled` — the actual safety dimension shadow mode exists to gate, not
  implied by turning off duplicate scheduled jobs), Uptime Kuma monitors for the LXC (currently
  zero — it can die silently right now), `app_base_url`/Tailscale serve HTTPS origin for the LXC
  (confirmed not built — LXC-sent alerts still deep-link to Windows's URL), vault topology
  (the LXC now owns nightly digestion, so its wiki copy is the one accumulating canonical
  content — the original spec's Phase 6.1 plan to overwrite it from Windows at cutover may no
  longer be correct as written), mail autotrash ownership (currently owned by nobody).

**LXC now owns nightly Brain Organizer digestion (2026-08-14, `master`)** — both this
instance and the LXC migration's `linux-lxc` instance had a working `modules/brain-organizer/venv`
and, since shadow mode only gates the action broker (not scheduled jobs), both would have
independently run the 02:00 `brain_organizer` digestion job against their own Syncthing-synced
copy of the vault tonight — a duplicate-digestion race, not data loss, but one that would have
produced divergent/near-duplicate wiki content needing manual reconciliation. Fable-planned
(explicitly asked, given the time pressure — ~2h to the next 02:00 fire — and to avoid repeating
a mistake already caught once: the first instinct, renaming the shared venv folder so its
existence guard fails, was tried and reverted before deploying, since that SAME guard also gates
`main.py`'s `:8765` Brain MCP write-server spawn and `POST /api/brain-organizer/run` — renaming it
would have collateral-disabled both, not just the nightly job). Fixed properly with a new,
narrowly-named settings flag, `brain_organizer_nightly_enabled: bool = True` (`backend/config.py`)
— gates ONLY the `scheduler.add_job(_run_brain_organizer, ...)` call in `setup_scheduler()`, wrapped
around the pre-existing venv-presence check rather than replacing it. Deliberately not a bare
`brain_organizer_enabled` — that name would invite a future reader to gate the MCP spawn with it
too, exactly the trap already caught. Set `BRAIN_ORGANIZER_NIGHTLY_ENABLED=false` in this instance's
own `.env`; the LXC needs no change at all (defaults `True`, no flag set there). The manual "Run
Now" API route is deliberately left ungated — an explicit human click is `actor="user"`, same
always-allowed precedent as every other broker-gated action in this codebase, and it's the one
remaining way to debug the Windows module during the migration's burn-in window; it already
self-guards against concurrency with a 409 while a run is in flight. Verified post-restart:
startup log shows `Brain Organizer nightly job DISABLED (brain_organizer_nightly_enabled=False)`
immediately followed by `Brain Organizer MCP server started (PID ...)` — proving the collateral
concern is fully avoided — plus `GET /api/brain-organizer/status` → 200 and `/api/health` → ok.
`tests/test_coverage_boost.py`'s two job-count tests (which already had to fight this exact
"this host's own `.env` doesn't match what the test wants to count" problem once before, for
`UNRAID_BACKUP_PATH`) gained the same treatment: `monkeypatch.setenv("BRAIN_ORGANIZER_NIGHTLY_ENABLED",
"true")` to force full-configuration counting regardless of this host's real `.env` — counts stay
unchanged at 29(nt)/30(POSIX), `brain_organizer` stays in `expected_ids`. Two new tests added:
`test_brain_organizer_nightly_disabled_skips_job` (only that one job drops, count −1, everything
else unaffected) and `test_brain_organizer_nightly_enabled_default_is_true` (class-default pin,
checked via `Settings.model_fields`, not a live instance — this host's own `.env` sets it false).
81/81 targeted tests (`test_coverage_boost.py` + `test_brain_organizer_capture.py` +
`test_facts_digest.py`) green both before and after the `.env` edit, confirming the monkeypatches
are genuinely env-independent. **Broader, deliberately out of scope tonight** (Fable flagged this
while planning, for a future session): every other job that touches state shared between the two
instances — `telegram_poll` (409-collision risk if both instances are actually polling; check
`telegram_poll_enabled` on both `.env`s), mail autodraft/autotrash, `facts_digest` (each instance
has its own local Fact DB, so duplicates would *diverge*, not just repeat), `morning_briefing`/
`wiki_fragmentation_report` (no flag exists for either yet), and every duplicate-pager job
(`homelab_watch`, `spend_report`, watchdog alerts, etc.) — needs its own explicit
job-to-owner-to-mechanism decision, not an ad-hoc flip each time one bites. A follow-up session
should produce that ownership matrix rather than repeating tonight's one-off fix per job.

**Post-start tray flap fixed — `start.ps1` now waits for the frontend port (2026-08-12, `master`)** —
every cold start of NEXUS produced a spurious "Backend went unhealthy while running — auto-restart
attempt 1/3" ~17-32s later, followed by a full stop/start cycle that self-healed. Root cause was NOT
the backend: `start.ps1` launched the frontend, slept a blind `Start-Sleep -Seconds 2`, and printed
"Frontend ready" unconditionally — but `tray.py`'s `_backend_healthy()` requires a TCP connect to
`127.0.0.1:3000` **in addition to** `/api/health`. So start.ps1 exited 0 while `npx vite preview` was
still coming up, the tray flipped to "running", and the monitor's next two 15s ticks both failed →
auto-restart. `npx vite preview` is a 4-process chain (`cmd.exe` → npx `node` → `cmd.exe` → vite
`node`); measured **3.8s warm** on this host on a spare port, **~9s** on a real restart, and the log
evidence shows **>34s cold after boot**. Timing arithmetic confirms it was never a stall: with the
monitor thread's t0 known from the "Tray started" line, both flap events fit two health checks
~15.0s apart costing ~1.05s each — a 12s `urllib` timeout (the known one-time watchdog GIL spike)
would have put the ticks ~27s apart and the "stopped" line ~12s later than observed. `/api/health`
itself is two `pathlib.exists()` calls (`backend/main.py:261-269`) and cannot return non-ok while
`.vault.key`/`nexus.vault` are on disk, so the failing leg was the `:3000` connect, by elimination.
Fix (`start.ps1:139-169`): the blind sleep is replaced by a bounded ~60s poll of
`Get-NetTCPConnection -LocalPort $port -State Listen`, mirroring the existing backend-wait loop, with
fail-fast on `$frontend.HasExited`. **It deliberately still exits 0 if the port never binds** — a
non-zero exit parks the tray at "stopped" with no auto-recovery (`tray.py`'s monitor only
auto-restarts a backend whose status was `running`), which is strictly worse than letting the tray
watchdog handle a genuinely dead frontend. No Python touched, no test covers `start.ps1`; verified by
actually running it (frontend wait printed, `:3000` listening + HTTP 200 and `/api/health` = `ok` at
exit, no flap in `logs/tray.log`). Two side observations logged but NOT fixed here (out of scope):
`.nexus.pids` records the `cmd.exe` wrapper PID, not the real vite `node` PID, so `stop.ps1`'s
PID-file kill relies on its kill-by-port fallback; and `modules/brain-organizer/mcp_server.py`
children were seen surviving a backend restart as orphans.

**Deploy-drift check — watchdog's 7th check (2026-08-11, branch `feat/deploy-drift-gitleaks-cleanup`,
worktree `nexus-deploy-drift-gitleaks-cleanup`)** — closes the exact failure mode this repo's
"Restart After Council Build" feedback note already names: a `git pull` lands but nobody restarts
the running process, which then keeps serving old code indefinitely with no signal anywhere that
it's happened. New leaf module `backend/version.py` (pure stdlib, sync, no subprocess/asyncio, no
imports from the rest of `backend/`) reads the repo's git HEAD straight off disk — loose ref,
packed-refs (skipping `#`/`^` lines), or detached HEAD — always returning either a 40-char
lowercase hex SHA or `None`, never raising. `main.py`'s lifespan calls
`version.capture_running_sha()` once at boot, inside the existing `if vault_ok:` block right before
`setup_scheduler(...)`; `watchdog.check_deploy_drift()` — the new 7th check inside the same 5-min
`run_watchdog()` job, gated by a new `deploy_drift_check_enabled` flag sharing `watchdog_enabled`'s
gate and reusing `watchdog_alert_cooldown_s` (no new cooldown setting) — re-reads HEAD fresh on
every tick and compares it to the boot-captured SHA, paging (kind `deploy_drift`, individually
`/mute`-able, not on `_NEVER_MUTABLE_NOTIFY_KINDS`) via the plain in-memory `_should_alert` debounce
(not the DB-backed dead-letter one — a drift condition only matters while this specific process is
still running the old code, so it doesn't need to survive a restart the way the dead-letter cooldown
does). Restart-safe by construction: a fresh boot always re-captures a matching SHA, so the alert
self-clears the moment someone actually restarts. `GET /api/safety/status` gained a `running_sha`
field (`str | None`, never coerced to `""`).
- **Deliberately NOT done (scope decisions, not oversights):** no Pulse/activity surfacing —
  `ActivityEntry.actor_type` (`job | worker | loop | trace | task`) has no actor representing "the
  app" as a whole, and inventing a synthetic one solely to carry a static boot-time SHA would be the
  first non-lifecycle entry in that registry and would mislead more than it'd inform; surfacing is
  `running_sha` on `/api/safety/status` + the watchdog phone alert + the `OutcomeFlag` row, full
  stop. No frontend changes. **Worktree checkouts are explicitly out of scope**: a real git worktree
  has a `.git` FILE (a `gitdir:` pointer), not a directory — `get_git_head()` returns `None` for one
  by construction (checked and documented in the module's own docstring), and the deploy-drift check
  silently no-ops there rather than raising or misreporting. Production runs from a normal checkout,
  not a worktree, so this isn't a real gap — just a documented boundary.
- New `tests/test_version.py` (pure-filesystem, `tmp_path`-based, no DB/app) plus 8 new cases
  appended to `tests/test_watchdog.py` covering no-drift, drift-detected (asserting the message
  carries both 12-char SHA prefixes), calibration-suppressed-still-returns-True, cooldown-debounce,
  both unknown-SHA no-op cases, disabled-flag no-op, and the `run_watchdog()` result-dict key.
  `tests/test_governor.py::test_safety_pause_resume_status` extended to assert `running_sha` is one
  of the now-six documented `/api/safety/status` keys. Full suite: 1941 passed, 1 skipped, 3 failed —
  all 3 confirmed pre-existing/unrelated (the two hardcoded scheduler-job-count-29-vs-28 tests in
  `tests/test_coverage_boost.py`, and `tests/test_proposer.py::test_known_hardware_issue_light_goal_dropped`,
  which is the documented time-of-day-flaky proposer test — it ran at night this pass, tripping the
  night-exemption filter before the hardware-issue filter ever got a chance to fire).

**Gitleaks CI on the public repo (2026-08-11, branch `feat/deploy-drift-gitleaks-cleanup`,
worktree `nexus-deploy-drift-gitleaks-cleanup`)** — new `.github/workflows/gitleaks.yml` runs
`gitleaks/gitleaks-action@v2` on every push to `master` and every pull request (no branch
protection exists, and feature work merges to master locally then pushes, so a PR-only trigger
would miss the real merge path). New `.gitleaks.toml` at repo root extends gitleaks' default rule
set and allowlists only what's deliberately public: the `tailfa52c.ts.net` tailnet suffix, LAN
(`192.168.1.x`) and Tailscale CGNAT (`100.x.x.x`) addresses, and a path allowlist for
`CLAUDE-SECURITY-*/` exercise-fixture directories — see 33e5bdd for why those infra values are
public on purpose and must not be scrubbed. `CLAUDE.md` and `README.md` are deliberately NOT
path-allowlisted wholesale, so a real secret pasted into either still gets caught. No
`.gitleaksignore`, no pre-commit hook, no `GITLEAKS_LICENSE`. Local validation this session was
config-syntax-only (`yaml.safe_load` + `tomllib` both passed) — no `gitleaks` binary was present
on this host, so the first real scan of current history happens as the first GitHub Actions run
after this merges and pushes. Future sessions: read `.gitleaks.toml`'s own header comment before
touching the allowlist.

**Cleanup of contaminated production calibration data (2026-08-11, branch
`feat/deploy-drift-gitleaks-cleanup`, worktree `nexus-deploy-drift-gitleaks-cleanup`)** — the
contamination: `pytest` runs from the repo root were writing real rows into the live `nexus.db`
because `backend/database.py`'s `DB_PATH` was cwd-relative, so a bare `pytest` invocation from
the repo root pointed straight at production — synthetic `homelab_watch:docker:plex`/`plex;evil`/
`plex<b>`/`plex`+80-`a` calibration-loop test fingerprints (the 4th fingerprint's length is
`tests/test_homelab_watch.py`'s own `long_name = "a" * 80` fixture — "pushes `docker:restart:<name>`
past Telegram's 64-byte limit") leaked into the real `OutcomeFlag` and `CalibrationHint` tables.
**Prevention already shipped 2026-08-09**, before this task: this
worktree's own `tests/conftest.py::_isolate_test_database` (session-scoped autouse fixture)
repoints `backend.database`'s engine/`DB_PATH` at a throwaway temp-file DB before any test runs,
so no future `pytest` invocation can write into the live DB again — that part is done and live,
not pending. This task is the cleanup of the contamination that already happened before that fix
existed. New `tools/cleanup_calibration_contamination.py` (plain stdlib `sqlite3`, zero imports
from `backend/` — deliberately not `backend.database`, since importing the backend would
reconstruct the exact same cwd-relative-`DB_PATH` bug class that caused this contamination in the
first place) is dry-run by default, requires an explicit `--confirm` to delete anything, and
refuses to touch the DB at all (exit 2, before either the dry-run or confirm branch) unless a
hard count-verification gate confirms the live DB still matches the 2026-08-11 investigation
exactly (88 `OutcomeFlag` rows, 22 per fingerprint, source `homelab_watch`, created in a specific
2026-08-02 date window; 4 `CalibrationHint` rows with ids exactly `{3,4,5,6}`). Never wired into
anything — no scheduler job, no migration shim, no conftest hook, confirmed by a regression test
that greps the script's own source for `import backend`/`from backend`. New
`tests/test_cleanup_calibration_contamination.py` (6 cases, all against a `tmp_path` SQLite file,
never the real DB) covers dry-run-deletes-nothing, confirm-deletes-exactly-the-matching-rows-and-
decoys-survive, a stale-count mismatch refusing before any delete, a hint-id mismatch refusing
before any delete, a missing `--db` path refusing, and the no-backend-import guard. Full suite:
1947 passed, 1 skipped, 3 failed — same 3 pre-existing/unrelated failures Task 1 already
documented above (unchanged by this task). **The first dry run against the real live `nexus.db`
(pointed at explicitly via `--db`, since this worktree has no `nexus.db` of its own) found a count
mismatch — 66/88 `OutcomeFlag` rows and 3/4 `CalibrationHint` ids (`{3,4,6}`, missing `5`) — but the
live DB had NOT actually drifted.** Read-only verification against the live DB (`SELECT id,
fingerprint, length(fingerprint) FROM outcomeflag WHERE fingerprint LIKE 'homelab_watch:docker:a%'`)
found the real contaminated fingerprint is `"homelab_watch:docker:" + "a"*80` (total length 101,
confirmed on rows id 10/28/43/58/73, 22 total) — matching `tests/test_homelab_watch.py:252`'s real
fixture, `long_name = "a" * 80`, exactly. The script's own `FINGERPRINTS` constant had a transcription
typo (`84` instead of `80`) from the original 2026-08-11 investigation/spec, not a real DB drift.
`CalibrationHint id=5` was independently confirmed to hold that same 80-`a` fingerprint with
`status='expired'`, `created_at`/`updated_at` timestamps identical (to the second) to ids 3/4/6 —
proving it belongs to the same contamination batch, so `EXPECTED_HINT_IDS={3,4,5,6}` was already
correct and needed no change. Fixed by correcting `FINGERPRINTS`' 4th entry from `"a"*84` to `"a"*80`
in `tools/cleanup_calibration_contamination.py` (the sibling test file imports `FINGERPRINTS` from
the script directly rather than hardcoding its own count, so no separate test fix was needed). Full
`tests/test_cleanup_calibration_contamination.py` suite re-run green (6/6) after the fix. **Re-run
dry run against the live `nexus.db` post-fix: exit 0, reports the full 88 `OutcomeFlag` / 4
`CalibrationHint` rows with zero `MISMATCH:` lines** — "DRY RUN — no changes made." confirmed, zero
writes. **The live `--confirm` run against `nexus.db` is still PENDING BRIAN'S OWN EXECUTION** — only
`--db`-pointed dry-run reads have happened in this repo across both passes (confirmed zero writes
each time); the fingerprint/id set is now verified correct, so whoever runs `--confirm` next does
not need to re-derive it, just run it.

**Traces discoverability + Pulse detail wiring (2026-08-10, branch `feat/traces-pulse-detail`,
worktree `nexus-traces-pulse-detail`)** — Fable-planned, Sonnet-built, following a read-only
investigation (querying the live `nexus.db` + reading the real code, not guessing) into why Brian
didn't see "detailed information" in Traces or Pulse. Two different root causes:
- **Traces was a discoverability problem, not a bug.** All 686 real spans in the live DB carried
  input/output text, and the UI already rendered it — but detail was two clicks deep (expand trace
  row → click the small `›` chevron on a span row). Fixed with a one-line, CSS-truncated preview
  (`input_summary`/`output_summary`, whitespace-collapsed, sliced to 160 chars) shown directly on
  every collapsed span row that `hasDetail` — so scanning needs zero clicks, full text still needs
  one. Considered and rejected auto-expanding every span on trace-open: would unroll up to 1000
  chars × N spans per trace, turning a 10-span orchestrator trace into a wall of text.
- **Pulse's `detail` field was a genuine, unwired gap.** `backend/activity.py`'s `ActivityEntry.detail`
  was written by exactly ONE call site in the whole codebase (`orchestrator.py`'s task step loop) —
  every other actor type (`worker:{id}`, `job:{id}`, `trace:{kind}:{id}`) never had it populated,
  and `Pulse.jsx` had exactly one render slot for it (the task progress bar). Extended per actor
  type, with an explicit reasoned skip for one: `worker:{id}` now calls
  `activity.update_detail(f"worker:{worker_id}", {"task_id": task_id})` right after `begin()` (own
  try/except, NOT chained into begin's — see bug below) so a running/idle worker card can show
  which task it picked up (the prompt was already in `label`, just never rendered on board cards —
  half the gap was frontend-only); `router._record_trace_span` now also calls `update_detail` on
  `trace:{kind}:{trace_id}` with `{last_span, span_type, duration_ms}` for every completed span,
  keyed off the existing `_open_trace_kinds` map (populated only by `router.open_trace`, so
  orchestrator `task:{id}` entries — which don't go through that map — can never be clobbered);
  `job:{id}` (scheduled jobs) deliberately got NO new detail — APScheduler's event only carries
  `job_id`, runs are millisecond-scale, and "last ran Xm ago · Yms · OK" is already the complete
  story; wiring detail would mean touching ~29 job functions and breaking the one-listener
  choke-point design B9/B10 established. `Pulse.jsx` gained two render slots: the Actors board's
  worker cards show `· task #N` in the running-status line plus a label line (prompt text) when the
  entry's `label` differs from its stripped actor id (so job cards, whose `label` IS the job id,
  stay byte-identical); the Now Running strip shows a `last: {span_name} · {duration}` line for
  `trace:*` entries. No `backend/activity.py` changes, no DB changes, no wire-format changes — its
  `snapshot()`/`drain_dirty()` already `asdict()`'d `detail` for the one working case, confirmed
  before writing any code so nobody re-investigates that question later.
- **One real bug self-caught while writing the worker_pool.py change, before it ever reached
  Opus:** the initial edit called `activity.update_detail(...)` INSIDE the same try/except as
  `activity.begin(...)`, with `_worker_began = True` placed after both calls — so a poisoned
  `update_detail` (or any future exception between them) would skip `_worker_began = True`
  entirely, and since `_worker_loop`'s `finally` only calls `activity.end()` when `_worker_began`
  is true, the worker's Pulse entry would get stuck "running" forever. Fixed by splitting into two
  independent try/excepts — `_worker_began = True` now only depends on `begin()` succeeding, never
  on `update_detail`. Regression-tested (`test_worker_loop_poisoned_update_detail_does_not_block_task`).
- 25 tests added/extended in `tests/test_activity_wiring.py` (worker detail + label assertions on
  the existing busy/idle test, a new deleted-task-never-begins test, the poisoned-update_detail
  constraint test above, and 4 new router-side tests: detail populated correctly, 200-char
  truncation, no-op + no entry created for an unknown/orchestrator trace_id, and a
  poisoned-update_detail-doesn't-break-pulse-or-DB-write constraint test matching the file's
  existing pattern for `activity.pulse`).
- **Opus verify caught a real gap in the worker_pool.py regression test above — proven, not just
  argued.** `test_worker_loop_poisoned_update_detail_does_not_block_task` only asserted
  `mock_run.assert_awaited_once()`, which is true under BOTH the fixed code and the original buggy
  ordering (the task runs either way — the exception is swallowed before `run_task` is ever
  reached, not after). The verifier reintroduced the original shared-try/except ordering and ran
  the test suite: all 3 worker tests still passed. Fixed by adding the actual discriminating
  assertion — `worker:0`'s entry must read `status == "ok"` after the run (fails with `"stuck as
  'running'"` on the buggy ordering, passes on the fix). Also fixed a minor inconsistency the same
  pass surfaced: the Actors board's card-title regex (`Pulse.jsx`, stripping `job|worker|loop:`
  prefixes) didn't strip `task:`, so a `task:{id}` card's title read literally `"task:42"` while
  the new label-line comparison (which DOES strip `task:`) would then always find a mismatch and
  show a label line the title regex never anticipated — aligned both regexes to strip the same four
  prefixes. Two other verifier notes accepted as-is, not fixed: idle worker cards show the last
  picked-up task's prompt indefinitely (matches Fable's own flagged open question, no complaint to
  resolve); Now Running re-sorts on every span since `update_detail` bumps `seq` (inherent to the
  existing seq-ordering design, not a regression this batch introduced).

**Agents page removed, Traces gained search + richer detail, mobile pass (2026-08-09, branch
`feat/traces-detail-mobile`, worktree `nexus-traces-mobile`)** — Fable-planned, Sonnet-built,
Opus-verified. Follows directly from the frontend-dedup batch below: with `Pulse.jsx`'s ticker
already covering "Live Feed" and `Traces.jsx` already showing more per-run detail than "Run
History" (cost/tokens/model name/expandable input-output), the Agents page had nothing left that
wasn't duplicated elsewhere — confirmed by checking what `AgentRun` (the table Run History reads)
is even still written by: only `orchestrator.py`/`worker_pool.py`, a strict subset of what
`AgentTrace`/`TraceSpan` already capture for those same runs. The one capability Run History had
that Traces lacked — free-text search — was folded into Traces instead of lost.
- **Removed:** `frontend/src/pages/Agents.jsx` + its two now-orphaned children
  (`components/AgentLog.jsx`, `components/RunHistory.jsx`, confirmed zero other importers), all
  `App.jsx` references (import, NAV entry, route, the now-unused `Bot` icon import), the dead
  `api.agents.runs` client in `lib/api.js`, and a `/agents` entry in `CommandPalette.jsx` that
  Fable's original audit missed (caught by the Writer's own grep pass). `GET /api/agents/runs`
  and the `AgentRun` table themselves were deliberately left in place — backend removal was out of
  scope, they're just UI-orphaned now (still exercised by their own tests).
- **`GET /api/traces` (`backend/api/traces.py`) gained `?q=`** — case-insensitive, matches the
  trace's own `label` OR any of its spans' `input_summary`/`output_summary` (an uncorrelated
  subquery over `TraceSpan`, evaluated before pagination/limit — has to be, since "does any span
  match" can't be known from the trace row alone), AND-combined with the existing `?kind=` filter.
  `%`/`_` are escaped as literals and — the one place a wrong order would silently corrupt
  results — **backslash is escaped BEFORE percent/underscore**, so a literal backslash in the
  search text can't accidentally un-escape a percent the escaping itself just inserted. Verified
  by Opus running real escape-discrimination cases against a live SQLite DB, not just reasoning
  about it, then pinned as regression tests in `tests/test_traces_api.py`
  (`test_list_traces_q_underscore_is_literal`/`_percent_is_literal`/`_backslash_escaped_before_percent`).
  Each list row also gained `span_count`/`total_cost_usd`/`total_tokens_in`/`total_tokens_out`,
  aggregated via one grouped `SUM`/`COUNT` query over the returned trace ids — the three totals
  are `None` (not `0`) when a trace has no spans or every span's value was NULL, per this repo's
  established `None`="unknown" vs `0`="confirmed zero" convention (see UniFi `alerts=None`).
- **`Traces.jsx`** gained a 300ms-debounced search box (reusing the existing `?kind=` select's
  header row) and a restructured two-line collapsed row (status/kind/span-count/cost/duration/age
  on line 1, full-width label on line 2 — the old single-line layout couldn't fit all of that at
  375px) plus an expanded-view summary strip (span count + summed tokens/cost) above the existing
  per-span list. One real bug caught and fixed mid-implementation (not by a separate review pass):
  the client-side token-sum `reduce` seeded its accumulator with `null` and added directly
  (`null + number = NaN` in JS on the first hit) — fixed to `(a ?? 0) + val`, matching the cost
  reduce's already-correct pattern.
- **Mobile-friendliness pass across 8 pages**, each getting concrete `flexWrap`/`overflowWrap:
  'anywhere'`/tap-target-`padding` fixes found by actually reading each page against a ~375px
  viewport budget, not generic advice: `Pulse.jsx` (was the only page missing the standard
  `maxWidth:1100px` page-container wrapper — cards sat flush against the screen edge), `Chat.jsx`
  (header wrap, message/list overflow, `minHeight:'calc(100vh - 4px)'` → `'100%'` so the composer
  isn't below the fold on mobile Safari), `HomeAssistant.jsx` (the thermostat dial was a genuinely
  broken control on narrow screens — fixed `width:240px;height:240px` on a flex item next to two
  `flexShrink:0` buttons meant ONLY the dial could shrink, stretching the circular SVG into an
  ellipse; fixed with `width:'min(240px,100%)', height:'auto', aspectRatio:'1'`, confirmed by Opus
  to resolve to a definite height since every child is `position:absolute` and contributes no
  content height — plus VM-row/Proxmox-header wrap and button tap-target sizing), `Facts.jsx`
  (subject/value + recall-result overflow, Dismiss button tap-target), `Mail.jsx` (inbox row
  wrap), `Settings.jsx`+`SecretField.jsx` (backup-status overflow, Edit/Test/Remove tap-targets),
  `Dashboard.jsx` (VM-action select + "+N more" button tap-targets), `Safety.jsx` (two
  `overflowWrap` instances the original Safety pass missed, secret-rotation row wrap),
  `BriefingPanel.jsx` (list/paragraph overflow), and a global `index.css` mobile media rule
  forcing 16px on all `input`/`select`/`textarea` (prevents iOS Safari's focus-zoom on every
  <16px form field in the app — Chat, Tasks, Facts, Mail, Settings, Safety, Flags, and the new
  Traces search all benefit).
- Full pytest suite: 1904 passed, 1 skipped, 3 failed — all 3 confirmed pre-existing/unrelated
  (two assert a hardcoded scheduler job count of 29 that's actually 28, one is a proposer test
  that's time-of-day-flaky and happened to run at night); `git diff --stat master -- backend/`
  shows only `traces.py` changed, confirming neither `scheduler.py` nor `proposer.py` were touched.
  `npm run build` clean. Opus verify: PASS on all 5 reviewed areas (backend SQL correctness
  verified by executing real escape/aggregate cases against a live DB rather than just reading the
  code, frontend NaN-fix correctness, mobile CSS reasoning, exhaustive stale-reference grep, and a
  general diff read) — recommended adding the `?q=`/aggregate regression tests (done, see above)
  and fixing 4 stale doc comments still naming the deleted `AgentLog.jsx` (done: `state_workers.py`,
  `tests/test_state_workers.py`, two spots in this file).

**Frontend duplicate-info cleanup (2026-08-09, branch `feat/frontend-dedup`, worktree
`nexus-frontend-dedup`)** — Fable audit of all 16 frontend pages found 4 redundancies, Sonnet-built,
Opus-verified. `Pulse.jsx`'s header autonomy chip now polls `api.safety.status()` every 30s instead
of once on mount (was a stale-forever value). `Safety.jsx`'s "Live Activity" card — a `/ws/logs`
feed duplicating the new Pulse page's own ticker — removed outright along with its websocket
plumbing (`wsLogsUrl`/`wsLogsProtocols` import, connect/reconnect effect, backfill effect, refs);
`/ws/logs` itself is untouched (at the time, still served `AgentLog.jsx`/`TaskCard.jsx` — `AgentLog.jsx`
was removed in the Agents-page-removal batch further down; `/ws/logs` now serves only `TaskCard.jsx`).
`Dashboard.jsx`'s
"Sources" KPI card removed — duplicated the "System Sources" section already on the same page.
`App.jsx`'s sidebar-footer and mobile-top-bar `StatusDot`s were hardcoded green regardless of real
health; both now reflect the page's existing `apiOk` state (green+pulsing/red), and the footer text
changed from "All systems online"/"Systems degraded" to "NEXUS connected"/"NEXUS unreachable". Opus
verify passed all 4 as specced and caught two minor follow-on gaps in the same pass, both fixed:
Dashboard's header `StatusPill` was hardcoded `tone="green"` (harmless while the Sources card also
showed live red/offline state below it, but became the page's only top-of-page health signal once
that card was removed) — now `amber` when `online !== total`; and `wsLogsUrl` in `lib/api.js` was
left dead (Safety.jsx was its last consumer) — removed. Full public-repo infra-leak check clean,
merged to `master`, pushed, frontend rebuilt, NEXUS restarted and confirmed healthy.

**Housekeeping (2026-08-05):** the parent `Agentic os\` folder (which holds this repo) had two
stale founding docs from before NEXUS existed — `AGENTIC_OS_PROMPT.md` (the original bootstrap
build prompt, referenced an old model and a since-retired Hermes-webhook briefing path) and
`Brain-Organizer-Build-Spec.md` (the original from-scratch spec; that build is long complete, and
its live follow-on specs are the ones under `docs/brain-organizer-*-spec.md` in this repo, not
that loose copy) — both deleted, plus an empty leftover `.claude\` dir. Separately, a
`feat/agentic-os-state-foundation` branch + `nexus-agentic-os-v2` worktree (Codex's own
independent attempt at the same dashboard-cold-cache/state-cache fix — durable SQLite snapshots +
staggered APScheduler collectors + `/api/dashboard/state` + `/ws/state` push, no Redis, verified
working: 113/113 targeted tests passed, clean merge-base with `master`) was deleted at Brian's
explicit call rather than landed. Kodak's Redis/multi-process "Phase 1 Architecture Refactor" plan
(and its later multi-agent phases) was independently reviewed and rejected the same session — its
diagnosis didn't match this codebase's actual behavior, and its specialist-agent stack turned out
to already exist, functionally, in-process (orchestrator, Telegram bot triage, homelab watchdog,
outcome tracker) — see `feat/dashboard-state-cache`'s commit history for the full audit trail.

**The dashboard-state-cache fix WAS rebuilt** (2026-08-05, branch `feat/dashboard-state-cache`,
worktree `nexus-dashboard-cache`, 8 commits) — same non-Redis design, this time with the eager
prime actually wired in, `/ws/state` on its own broadcaster (not shared with `/ws/logs`), and a
real boot-time race condition found and fixed during live testing. Independently reviewed twice
(Fable audit against Kodak's original plan: no gaps; Opus verification against the approved build:
confirmed, 3 minor findings fixed). While rebuilding it, the same live-fanout pattern was also
found and fixed on `Uptime.jsx`/`Media.jsx`/`Today.jsx`/`Mail.jsx` (each had its own leftover live
call Kodak's original complaint never covered), plus a real, separately-diagnosed bug in
`backend/integrations/speedtest.py` (`ping_ms` was measuring a cold TLS handshake, not RTT, because
httpx builds an SSL context synchronously inside `AsyncClient()` — ~1.3-1.45s on this host,
suspected Defender scanning the certifi bundle, blocking the event loop each time. Fixed here via a
module-level reused SSL context + one shared client instead of three. This turned out to be a
**repo-wide pattern — fixed the same session, commit `a307446`**: `backend/http_client.py`'s
`SSL_CONTEXT` (built once at import, before uvicorn's loop exists) is now passed as
`verify=SSL_CONTEXT` at 34 `AsyncClient()` sites across 18 `backend/integrations/`/`agents/`
modules. Two categories deliberately left untouched: 6 sites using `verify=False` (Proxmox,
scheduler.py — no CA bundle to reuse) and 6 using a custom `transport=` (UniFi/Unraid TLS pinning —
bypasses SSL context construction entirely). Guarded by `tests/test_httpx_ssl_context.py`, which
scans all of `backend/`+`tools/` (not just speedtest.py) and asserts the shared context is imported
before uvicorn's loop exists. Merged to `master`.

**Claude Code + OpenRouter usage tracker (2026-08-05, branch `feat/claude-usage-tracker`, worktree
`nexus-claude-usage`)** — two analogous dashboard cards + briefing sections, both deterministic
(never LLM-narrated, never fact-extracted — see `_UNVERIFIED_FACT_SECTIONS` in `briefing.py`).
**Claude usage**: `backend/integrations/claude_usage.py` reads `~/.claude/rate-limits.json`, a file
`~/.claude/statusline-command.sh` now writes on every statusline render (edited outside this repo)
— there is no Anthropic API for personal subscription usage, this is the only legitimate source.
Deliberately `dashboard.*` only in `state_workers.py`, no `source.*` entry — the file is
legitimately hours old whenever no Claude Code session is running, which is normal, not an outage.
**OpenRouter**: `backend/integrations/openrouter.py`'s `fetch()` extended to also hit the real
`GET /api/v1/key` endpoint (live-verified against Brian's actual key before implementing) for
credit/usage data; gained a `@async_ttl_cache(30)` in the same pass, since it moved out of
`backend/safety/contracts.py`'s `EXCLUDED` list (now a real `CONTRACTS` entry) and picked up a
`dashboard.openrouter` collector on top of its pre-existing `source.openrouter` health check —
uncached, that would have silently turned ~1 request/5min into ~6. Independently Opus-verified
against the approved plan; caught and fixed a real crash bug (`_build_openrouter_section` raised
`TypeError` when `credit_limit` was set but `credit_remaining` was null — reachable, would have
silently killed the entire daily briefing after the paid Sonnet call already ran), a wrong-signal
staleness bug (the Claude Usage card was dimming off poll freshness instead of the capture's own
age), a "resets in due now" phrasing bug plus a missing day-tier on both the backend and frontend
countdown helpers, and a null-coerced-to-$0.00 frontend bug on `credit_remaining`. Full pytest
suite: 1870 passed, 1 skipped, 0 failed.

**OpenRouter real account balance fix (2026-08-05, branch `fix/openrouter-account-balance`, worktree
`nexus-openrouter-balance`)** — the usage tracker above showed `/api/v1/key`'s `limit`/
`limit_remaining` as "the balance," which is wrong: those are a per-KEY spending cap, not the real
account credit balance. Live-verified: Brian's key had `limit=5, limit_remaining=0` (exhausted)
while the real account (`GET /api/v1/credits`, `{"total_credits": 30, "total_usage": 17.35}`) held
$12.65 of real spendable balance. `openrouter.py`'s `fetch()` now calls all THREE endpoints
concurrently (models/key/credits); `OpenRouterData` gained `account_total_credits`/
`account_total_usage`/`account_balance` (`total_credits - total_usage`, `None` if either isn't
numeric — never coerced to 0). `_build_openrouter_section`/Dashboard.jsx's OpenRouter card now
lead with the real balance; the old per-key `credit_limit`/`credit_remaining` fields are demoted
to a secondary "Key limit:" line (omitted entirely for an unlimited key). A quick Opus pass caught
4 minor findings (a `0/0 → NaN` bar-width edge case for a never-topped-up account, a missing
`account_total_credits` contract entry for a field Dashboard.jsx dereferences unguarded, a stale
`usage` contract with zero remaining consumers after the rewrite, one inaccurate comment) — all
fixed. Full pytest suite: 1874 passed, 1 skipped, 0 failed.

**Monthly Anthropic balance-feature watch (2026-08-05, branch `feat/anthropic-balance-watch`,
worktree `nexus-anthropic-balance-watch`)** — Brian asked NEXUS to check monthly whether Anthropic
has shipped a public API credit-balance endpoint yet (there is none today; the Claude usage tracker
above only covers subscription rate limits, not API dollar balance). New
`backend/agents/anthropic_balance_watch.py`, a read-only, no-LLM, deterministic-trigger monthly job
(1st of the month, 09:30, `anthropic_balance_watch` scheduler job) combining two independent
signals: the public GitHub issue tracker for `anthropics/claude-code#47574` (state/state_reason/
comment count, no auth needed) and a live probe of `GET /v1/organizations/balance` (the one
concrete candidate endpoint the community has proposed; live-verified 404 today). Persists the last
seen signal on `SystemState` (three new nullable columns, ALTER-shimmed) and notifies via
`events.notify_phone` only on a genuine CHANGE from last month's baseline — never on the first
check unless the current read is already resolved-looking, so this doesn't page about a gap that's
already known. A transient network failure of either sub-check degrades to silence and preserves
the prior persisted baseline rather than overwriting it with `None`. Caught and fixed one real bug
during the build (not by a separate review pass — self-caught while wiring the scheduler job): the
new `if getattr(s, "anthropic_balance_watch_enabled", True):` block was inserted in the MIDDLE of
the pre-existing `spend_report_enabled` block, splitting its own closing log statement off into
unconditionally-executed code that referenced locals (`report_day`/`rh`/`rm`) only defined inside
the `if` — a real `UnboundLocalError` whenever `spend_report_enabled` was `False`, caught by 5
existing tests that assert on scheduler job registration around that region. Full pytest suite:
1882 passed, 1 skipped, 0 failed.

**UniFi alarms endpoint broken, whole integration was dying over it (2026-08-06, branch
`fix/unifi-alarms-invalid-object`, worktree `nexus-unifi-alarms-fix`)** — found via Telegram's own
`/status` command showing "UniFi: unavailable" while every other source read fine. Live-verified
root cause: `unifi.py::fetch()`'s alarms sub-call (`GET .../list/alarm`) returns a real HTTP 400
`{"meta":{"rc":"error","msg":"api.err.InvalidObject"}}` on the real controller (UniFi Network
10.5.67 / UDM Pro Max) — confirmed `"alarm"` is no longer (or never was, on this firmware) a valid
record type by testing `list/wlanconf`/`list/networkconf`/`list/user` on the same controller (all
200 OK, proving the legacy `list/*` REST pattern itself still works) and trying several plausible
replacements live (`list/event`, `list/alert`, `stat/alarm`, `stat/event`, the v2
`/proxy/network/v2/api/site/default/alarm` path) — none worked. Since `fetch()` raised on ANY
sub-call failure, this ONE broken call was killing the entire UniFi read — client count, uplink
status, and bandwidth, all independently confirmed working, went dark too. Fixed by isolating the
alarms call in its own try/except, degrading `UniFiData.alerts` to `None` (never `[]` — `None`
means "couldn't read this cycle," `[]` means "confirmed no active alarms"; conflating the two would
be exactly the false-all-clear the rest of this integration's raise-on-failure design exists to
prevent) while clients/uplink/bandwidth keep raising as before, since those are still genuinely
working. `tools.py::_unifi_status` and `contracts.py`'s `alerts` FieldContract both updated to treat
`None` as a distinct, valid "unknown" state rather than crashing or silently reading as zero.
Live-verified against the real controller post-fix: `client_count=52, uplink_status=ok,
bandwidth_mbps=0.16, alerts=None`. Full pytest suite: 1920 passed, 1 skipped, 0 failed.

## Run / build / test
- **Start:** `.\start.ps1`  ·  **Stop:** `.\stop.ps1`  ·  **Setup:** `.\setup.ps1`  ·  **Restore db:** `.\restore.ps1 [-From <dir>]` (validates the backup BEFORE stopping NEXUS; logic lives in `backend/agents/backup.py::restore_from` — tested by `tests/test_restore_drill.py`)
- Backend: FastAPI + uvicorn on **:8000**, venv at `.\venv` (`.\venv\Scripts\python.exe`). `start.ps1` launches it via **`run.py`** (NOT `-m uvicorn`) — run.py pins the Windows **SelectorEventLoop** before uvicorn builds its loop (must be set there: uvicorn creates the loop before importing the app, so a policy in `main.py` is too late). The default ProactorEventLoop throws `WinError 64` under concurrent integration fetches → "app not loading data". See Non-obvious rules.
- Frontend: React + Vite + Tailwind on **:3000**. Build with `cd frontend && npm run build`; `start.ps1` serves the build via `npx vite preview --host 0.0.0.0`.
- **After any frontend change you must `npm run build`** — preview serves `dist/`, not live source.
- Tests: `pytest` (in `tests/`). Backend changes need a restart (`stop.ps1` then `start.ps1`) to take effect. `start.ps1` is **race-safe**: a single-instance mutex (`Global\NEXUS_START_LOCK`) makes a concurrent invocation abort cleanly instead of killing the other's fresh backend, and it waits for ports to free before launching.
- LAN access (phone): `http://192.168.1.119:3000`. Firewall private profile is disabled.

## Layout
- `backend/main.py` — FastAPI app + lifespan (scheduler start, memo watcher thread). Routers registered under `/api/*`.
- `backend/api/` — one FastAPI router module per feature area (see the directory for the list).
- `backend/integrations/` — one module per system (see the directory for the list). Each exposes async `fetch()` and `health_check()` (protonmail has no `fetch()` — its reads are parameterized, see below).
- **Proton Mail (2026-07-23) — direct MCP client, not a REST shim.** `backend/integrations/protonmail.py` talks real MCP protocol (JSON-RPC over streamable-HTTP) straight to a self-hosted `ai-zerolab/mcp-email-server` on Brian's private network (`protonmail_mcp_url`/`protonmail_account` — both lazy secret-store-backed `Settings` properties (Infisical keys `PROTONMAIL_MCP_URL`/`PROTONMAIL_ACCOUNT`, promoted from plain `.env` fields 2026-07-24: the MCP server itself still has no auth token, but a real tailnet IP + personal account name are worth protecting like every other secret). A missing key raises `KeyError` — every consumer degrades loudly (health_check → False/OFFLINE, agent tools → "unavailable", API routes → 502) rather than silently limping on an empty value). `_call_tool()` opens a fresh `streamablehttp_client`+`ClientSession` PER CALL (no persistent/lifespan-held session — a shared session isn't concurrency-safe and would need reconnect logic on every tailnet blip; streamable-HTTP is pure async httpx/SSE, spawns no subprocess, so a per-call session is cheap and compatible with the forced Windows SelectorEventLoop). Only `health_check()` is `@async_ttl_cache`'d (30s, matches the dashboard poll pattern) — `list_recent()`/`read_email()` are parameterized reads and are deliberately NOT cached (a shared cache would return one filter's result for another, and "any *new* email" needs to actually be fresh). Read/status tools (`protonmail_inbox`, `protonmail_read_email`, `protonmail_status`) live in `backend/agents/tools.py`'s read-only registry. **Send is user-only, not an executor write tool:** `classify("protonmail_send", ...)` is `HIGH`/`IRREVERSIBLE` (can't unsend an email) — `decide()` therefore hard-FORBIDs an unconfirmed agent/autonomous actor outright (never a phone-tap needs_confirm) and only a `confirmed=True` or `actor="user"` call executes. `POST /api/protonmail/send` (`backend/api/protonmail.py`) is the only send path, actor=`user`, broker-gated like `/api/ha/service`. **`mcp` pulls a newer `starlette` that breaks FastAPI's `<0.39.0` pin** — `requirements.txt` explicitly re-pins `starlette==0.38.6` after `mcp`; don't drop that pin when touching dependencies. **Delete moves to Trash, not a hard remove:** verified 2026-07-23 that the MCP hard-remove tool permanently expunges (a test email never reached the real Trash folder, unlike Brian's years of normal deletes) — `protonmail.trash_email()` uses the MCP move-between-folders tool instead (`destination_mailbox="Trash"`), and the hard-remove path is banned from the codebase (invariant-tested). `classify("protonmail_delete", ...)` is now `LOW`/`REVERSIBLE_BY_INVERSE` (same band as `protonmail_archive`, kind name kept for continuity) — an unconfirmed agent/autonomous actor now executes, which is the point: the auto-draft scheduler's `autodraft_tick()` also auto-trashes obvious junk (a `MailJunkProfile` singleton row, distilled via one Sonnet call from Brian's real Trash-folder sender+subject history, refreshed every 30 days) — but ONLY for senders that already match the automated-sender pre-filter, never a human-looking sender, so trashing genuine correspondence is impossible by construction. One Haiku call (`_is_junk`) judges each automated candidate against the profile, default-deny on any ambiguity; `mail_autotrash_enabled` (default True) and a `MAX_TRASHED_PER_TICK=5` cap (chosen to match the broker's per-kind throttle) bound it; no profile yet ⇒ no auto-trashing that tick (fail-safe, unlike the voice profile's fabricated default). **Today page + briefing Inbox now source from Proton, not Gmail (2026-07-24):** `protonmail.inbox_summary()` (`@async_ttl_cache(120)`, matching the dashboard poll pattern — unlike `list_recent()` above, this one IS cached since every caller passes the same fixed `limit=7`) formats `list_recent(unread_only=True, mailbox="inbox")` into a Gmail-shaped plain-text summary for `GET /api/today`'s `email` field; sender/subject are trimmed (`_clean_sender`/`_clean_subject`) since raw `"Name" <addr>` From headers are unreadable in the Today card. The briefing's old LLM-narrated `## Inbox` section (Gmail-sourced) was retired outright rather than fed Proton data — see `backend/agents/briefing.py`'s `_UNVERIFIED_FACT_SECTIONS` comment; the pre-existing deterministic `## Proton Mail` section (`_build_protonmail_section`, appended AFTER the LLM call) is now the sole mail section in the briefing.
- **Obsidian writes go through the Brain MCP server — NOT direct file I/O and NOT the old REST API.** `backend/integrations/obsidian.py` POSTs all writes to `http://localhost:8765/raw` (the Brain Organizer MCP server). Settings: `brain_mcp_url` (default `http://localhost:8765`, `.env`-overridable) in `config.py`. `brain_mcp_token` is now a lazy property that delegates to `brain_mcp_write_token` (secret-store-backed, `BRAIN_MCP_WRITE_TOKEN`) — one value, two names, since the token NEXUS *sends* and the token it expects the spawned server to *enforce* are supposed to match. From work, set `BRAIN_MCP_URL=http://win11-vm-proxmox.tailfa52c.ts.net:8765` in `.env`. `OBSIDIAN_TOKEN`, `obsidian_host`, and direct `pathlib` writes to the vault are gone — do not re-add them.
- **Brain event emitters:** `backend/integrations/obsidian.py::emit_event(event_type, title, body)` fires a short "this happened" markdown note through the same `POST /raw` path (never a new/direct write path) so the nightly Brain Organizer folds it into wiki memory. It's best-effort and fire-and-forget — one outermost try/except swallows every failure (timeout, connection error, non-2xx) and logs at warning level at most; a failed emit never disrupts the calling operation. Emitted event types (v1, complete list): `goal.approved`, `goal.rejected`, `goal.completed`, `goal.failed` — wired into `backend/agents/goals.py`'s `approve`/`reject`/`reconcile_running` transitions. **Not emitted, on purpose:** `goal.proposed` (a pending proposal is noise, not yet a decision — the proposer can raise several per tick), and briefing/wiki_ingest/facts/durable-task completions (all already vault-captured by their own existing write paths, so an event would just duplicate them). Auth on `:8765/raw` is loopback-exempt: calls from `127.0.0.1`/`::1` (NEXUS's own emits, always loopback) never need a token; non-loopback (LAN/Tailscale) callers must send `Authorization: Bearer <token>`, and if no token is configured at all, remote writes are rejected outright. The token itself is the optional vault secret **`BRAIN_MCP_WRITE_TOKEN`**, plumbed into the spawned MCP server's environment at startup — name only, the value is never written to any file in this repo.
- `backend/agents/` — `router.py` (opus/sonnet/haiku + Anthropic web search + `run_with_tools` tool-use loop), `tools.py` (read-only native tool registry), `orchestrator.py` (Opus plans → Sonnet executes via the read-only tool-use loop), `worker_pool.py` (durable task execution pool), `chat.py`, `voice.py`, `briefing.py`, `memo_watcher.py`, `telegram_poller.py` (long-poll button/command dispatch), `telegram_commands.py` (Telegram slash-command/chat handlers).
- `backend/scheduler.py` — APScheduler jobs: morning_briefing, retention_prune (nightly 03:45), retry_deliveries (60s), record_uptime (2m), record_speedtest (30m). **`brain_organizer` (02:00) is the SOLE raw→wiki pipeline** as of 2026-07-14 — it and `wiki_ingest` (formerly 01:55) both routed `Brain/raw/` into wiki pages 5 minutes apart with a real collision risk over date-named pages (two independent builds 3 days apart in June, never reconciled). The daily `wiki_ingest` cron job was removed; the module/functions stay wired for `wiki_fragmentation_report` (Sundays 02:30), which still imports from it. Don't re-add a `wiki_ingest` cron job without resolving that ownership conflict first. `_run_brain_organizer`'s subprocess capture is `encoding="utf-8", errors="replace"` because the child (`modules/brain-organizer/brain_organizer.py`) reconfigures its own stdout to UTF-8 and routes ALL diagnostics through it (never stderr), so the failure-path log reads stdout, not stderr, and tolerates any byte the child ever emits. **`facts_digest` (Sundays 01:30, 30 min before `brain_organizer`) is ENABLED as of 2026-07-29** (`facts_digest_enabled=True` in `backend/config.py`) — previously disabled because the Fact table had pre-fix duplicate-subject noise (e.g. "Charlie"/"Charlee", multiple "Unraid" spellings) that a NULL watermark would have digested wholesale into one note `brain_organizer` permanently bakes into `Brain/wiki/` the same night. Cleaned up via `backend/agents/facts_cleanup.py` (one-time, safe-to-rerun subject-rename + predicate-merge pass, never deletes) — applied to the live `nexus.db` 2026-07-29 (backed up first to `nexus.db.bak-2026-07-29`), 125→116 active facts, 40→33 distinct active subjects, verified idempotent and non-destructive (total row count unchanged at 188). See `backend/agents/facts_digest.py` and the regression-guard test `tests/test_facts_digest.py::test_facts_digest_enabled_by_default`.
- `backend/cache.py` — `async_ttl_cache` (see below).
- `backend/database.py` — SQLModel tables in `nexus.db` (WAL mode, busy_timeout 30s). `create_db_and_tables()` runs an idempotent `_ensure_task_columns()` shim that ALTERs in `Task.cancel_requested` on old DBs.

## Read-only tool-use loop (Tier 2.1 — native executor tools)
- **`backend/agents/tools.py` is a READ-ONLY native tool registry.** Twelve side-effect-free tools (`homeassistant_status`, `homeassistant_temperatures`, `unraid_status`, `unifi_status`, `adguard_status`, `channels_status`, `weather`, `github_status`, `proxmox_updates`, `proxmox_backups`, `vault_search(query)`, `ddg_search(query)`). `homeassistant_temperatures` reads every `sensor.*temperature*` HA entity dynamically (shared logic with the goal proposer's room-temp discovery, `chat.py::extract_temperature_sensors`) — a newly-added sensor is queryable with zero per-sensor wiring. `proxmox_updates`/`proxmox_backups` call `backend/integrations/proxmox.py`'s `fetch_updates()`/`fetch_backups()` directly (PVE apt/tasks API) — read-only. (`channels_status` now also surfaces a `failed/skipped(24h)=N` count — always emitted incl. zero, so a verifier can confirm the zero case.) NOTE (Tier 1.6): the local DuckDuckGo tool is named `ddg_search` (renamed from `web_search`) so it never collides with Anthropic's HOSTED `web_search` tool when both are sent in the same `tools=` list — distinct names let them coexist; the loop only dispatches client-side `tool_use` blocks, never the hosted server-result block. Each `ReadTool` wraps an integration `fetch()`/search in try/except, returns a compact string truncated to `MAX_TOOL_RESULT_CHARS` (1500), and NEVER raises. There are ZERO write tools here and the module never imports `backend.safety.broker` — all writes are deferred behind the broker for a later tier. `anthropic_spec()` omits the `"type"` key (custom tools, not hosted ones).
- **`router.run_with_tools(model, max_tokens, prompt, system, tool_specs, dispatch, *, web_search=False, label="", max_rounds=5)`** runs the multi-round tool loop: Claude calls tools, we dispatch + feed results back, until a final text answer or `max_rounds` (default 5). Metering parity with `_run` is preserved via the shared `_budget_brake()` (daily `check_budget` BEFORE each round; `BudgetExceeded` propagates, other governor errors swallowed) and `_create_sync_raw` (records spend AFTER each `messages.create`, in-thread). When `web_search=True` the hosted `_WEB_SEARCH_TOOL` is prepended to the local custom tools.
- **The orchestrator executor (`_sonnet_execute`) calls `run_with_tools`** with the full read-only tool set + hosted web search (label `orchestrator_execute`) — the old `WEB_SEARCH:`/`VAULT_SEARCH:` regex directives are GONE. `_opus_plan` advertises the tools via `planner_tool_block()` (it tells the planner the executor calls them natively, no step prefixes). A `BudgetExceeded` bubbling out of `run_with_tools` mid-loop still finalizes a durable task `failed`/`budget_exceeded`. Tests that drive the executor patch `backend.agents.router.run_with_tools` (not `router.sonnet`).

## Durable task execution (resumable / cancellable / resume-on-restart)
- **`TaskStep` table is the source of truth for task progress.** Planning writes one `TaskStep` row per step (status `pending|running|done|failed`, `output_json`, `idempotency_key`). The orchestrator loop **skips `done` steps** (resume), commits each step's output the instant it finishes (the checkpoint), and rebuilds context from completed `TaskStep.output_json` — context is NEVER reset to `[]` on retry.
- **`TaskWorkerPool` (`worker_pool.py`) is the single owner of orchestration concurrency.** A bounded pool of N workers (`NEXUS_TASK_WORKERS`, default 2) drains an `asyncio.Queue` of Task ids. `create_task` inserts a `pending` Task and enqueues it; the pool runs it. There is no in-memory `_running` dict and no bare `asyncio.create_task` in `api/tasks.py`.
- **Resume on boot:** `main.py` lifespan calls `get_pool().start()`, which calls `requeue_unfinished()` — every Task left `running`/`pending` is re-enqueued (NOT force-failed). A `TaskStep` stuck in `running` (process died mid-step) is reset to `pending` by `_load_steps`. A Task whose planning died (no `TaskStep` rows) re-plans from scratch.
- **Cancellation:** `DELETE /api/tasks/{id}` sets `Task.cancel_requested` (cooperative, checked between steps → Task status `stopped`, done steps preserved) and hard-cancels the in-flight coroutine as a backstop, then deletes the row. `stopped` is a real status used for programmatic/boot cancellation.
- **`Task.status` is free-text:** `pending | running | success | failed | stopped`.
- **Orchestrator legacy path:** `run_task(prompt, task_id=None)` runs the old in-memory loop (used by `tests/test_orchestrator.py`); `task_id` set always uses the durable path. All durable DB helpers are sync and invoked via `asyncio.to_thread` — no Session/ORM crosses an `await`.
- **Completion push for standalone tasks (2026-08-05):** `worker_pool._worker_loop` is the single choke point for every `enqueue()` call site, so it's also where a finished standalone `Task` (from `POST /api/tasks/` or Telegram `/task`) gets a phone push — two new notify kinds, `task_completed`/`task_failed`, both individually `/mute`-able (deliberately NOT on `governor._NEVER_MUTABLE_NOTIFY_KINDS` — a task-finished ping is a convenience notification, same class as the `homelab_*` kinds). `stopped` (user cancel, autonomy-disabled, `TaskAborted`) never notifies — status is checked against an ALLOW-list (`success`/`failed` only), not a deny-list, which is also what silently excludes a task left `running` (hard-cancel mid-await never reaches a finalizer) and a task row deleted mid-run (`DELETE /api/tasks/{id}` cancels then deletes — `_load_task_notify_info` returns `None`). A **Goal-backed task never notifies here** — `goals.py`'s `reconcile_running` already owns that task's whole notification lifecycle (`obsidian.emit_event` + its own `notify_phone`), so double-paging is prevented by a `Goal.task_id` lookup before ever building a message. No `OutcomeFlag` is written for a failed standalone task — a per-task fingerprint (`f"task:{task_id}"`) would be unique-by-construction, permanently disabling `record_flag_ex`'s false-positive-cooldown/defer machinery and poisoning `calibration_summary`'s bucket aggregation; a failed Task already has a permanent `Task`+`AgentRun`(+usually `TaskOutcome`) row, it was only missing a push, which this adds.
- `nexus.db-wal` / `nexus.db-shm` are gitignored SQLite WAL sidecars.
- `frontend/src/pages/` — 14 pages (see the directory for the list). Mail (Proton inbox/send) and Traces (agent/task execution spans) were added alongside their respective features above but never listed here until 2026-07-29. Trends was removed 2026-07-07 (Grafana/UptimeKuma cover it externally). `App.jsx` holds the `NAV` array + routes + the full sidebar/off-canvas drawer (236px desktop, transforms to a hamburger-triggered off-canvas drawer at ≤880px). `components/MobileNav.jsx` has been removed — mobile nav is now the drawer in App.jsx. **HomeAssistant.jsx shows ONLY the curated `CONTROLS` list** (12 devices, each `{id, name, group}` — groups: Lights / Doors & Garage / Thermostat / Fans; add a row at the top of the file to surface more). UI: iOS-style toggles for lights/fan, OPEN-CLOSED/LOCKED-UNLOCKED status+button for garage/lock, Ecobee-style thermostat panel. After a service call the page waits 1.8s before reloading state — HA lags polled devices (TP-Link/ESPHome) and an instant reload reverts the optimistic button state (looks like dead controls); setpoint writes skip the reload entirely (Ecobee cloud lag). **`ECOBEE_SET_OFFSET = 0` — do not compensate:** during 3-7pm the Ecobee holds setpoints 3°F above what's sent; that is Brian's electric-utility peak-savings program working as intended, NOT a device bug. Sending lower values to cancel it forfeits his peak-rate savings. `POST /api/ha/service` accepts optional `service_data` (entity_id merged server-side).
- `frontend/src/components/` — shared UI kit added in June 2026 redesign: Card, Eyebrow, StatusDot, StatusPill, ScreenHeader, PrimaryButton, GhostButton, TextInput, AreaChart (inline SVG — replaces recharts TrendChart). Design tokens (Space Grotesk font, --accent/#2fd4ee, --ac-dim, --ac-line, --gap, --pad, nx-pulse/nx-ring keyframes) live in `frontend/src/index.css`. (`SegmentedControl` deleted 2026-07-14 — zero importers. `StatChip` was already stale doc text, never existed as a file.)
- `tray.py` + `launch_tray.vbs` — system tray launcher, auto-starts at login via a Registry Run key. Everything in `main()` before `NexusTray().run()` must be non-fatal + logged: under `pythonw.exe` an uncaught startup exception is 100% invisible (no console, no event-log entry). The orphan-sweep PowerShell/WMI call takes ~4-10s warm and longer at logon — it is try/except-wrapped and best-effort (2026-07-14 fix: its old unguarded 10s timeout silently killed the tray at every reboot). **Auto-recovery (2026-07-15):** `_monitor()` bounces the backend via `_maybe_auto_restart()` on a running→unhealthy transition instead of only relabeling the tray icon — a genuine hang self-heals within ~30-45s instead of sitting broken until someone notices. Bounded (300s cooldown, 3 attempts/rolling hour) so a truly broken build can't restart-storm; a manual Stop is untouched (only a *running→unhealthy* transition triggers it). `_do_start`/`_do_stop` also guard `_run_ps`'s `TimeoutExpired` re-raise, which used to wedge the tray at `"starting"` forever.

## Action broker (Tier 1.3 — policy-gated writes + immutable audit)
- **`backend/safety/broker.py::execute_action(actor, kind, target, payload, idempotency_key=None, *, confirmed=False)` is the ONE chokepoint every side-effecting write passes through.** It classifies risk/reversibility, decides allow/needs_confirm/forbid by actor, writes an immutable `ActionLog` row BEFORE and AFTER the attempt, dispatches only when allowed, and is idempotent by key. It NEVER re-raises a dispatch error.
- **Two outcome axes, never conflated:** GATE outcome = {allowed, needs_confirm, forbidden}; DISPATCH outcome = {executed, failed}. `ActionLog.decision` holds the FINAL state.
- **Actors:** `user` is always allowed (preserves chat UX, still logged); `agent`/`autonomous` go through the policy — IRREVERSIBLE→forbidden unless confirmed; HIGH/UNCLASSIFIABLE→needs_confirm unless confirmed; LOW/MEDIUM→allowed. An UNKNOWN actor string degrades to `autonomous` (most restrictive), never `user`.
- **Dispatchers live only in the broker** (`_DISPATCHERS`): `ha_service`→`homeassistant.call_service`, `vm_power`→`proxmox.set_vm_power`. `chat.py` has NO raw `call_service`/`relay(...)` dispatch — it calls `execute_action`. Add new write paths as a new dispatcher, never a direct integration call.
- **`ActionLog` table** (`backend/database.py`): immutable by convention — app code only INSERTs then UPDATEs a row, never deletes. Created by `create_all` (new table, no migration shim).
- **API:** `GET /api/safety/actions` (auth, `?limit=`≤200, `?decision=`, `?actor=`) lists the audit trail newest-first. `POST /api/safety/actions/{id}/confirm` is LIVE (Tier 1.5, `backend/api/safety.py::confirm_action`) — confirms and dispatches a `needs_confirm` action, re-checking the kill switch and confirmation TTL at dispatch time. `200` (dispatch outcome `executed`/`failed` in the body) / `403` (kill switch on) / `404` (not found) / `409` (not awaiting confirmation) / `410` (TTL expired). Wired up in the frontend at `Safety.jsx::handleConfirm`.
- All broker DB helpers are sync + `asyncio.to_thread`-only — no Session/ORM crosses an `await`.

## Cost governor / kill switch (Tier 1.5 — spending caps + global autonomy switch)
- **Two new DB tables (created by `create_all`, no migration shim):** `SpendLog` (one best-effort row per billed LLM call — model, token counts, `cost_usd`, `label`, indexed `created_at`) and `SystemState` (single row `id=1`: `autonomy_enabled`, `daily_budget_usd`, `per_task_budget_usd`). `create_db_and_tables()` calls `_ensure_system_state()` which idempotently seeds row 1 from `Settings` defaults (`daily_budget_usd=25.0`, `per_task_budget_usd=5.0`, `autonomy_enabled_default=True`; all `.env`-overridable).
- **Metering lives in `backend/agents/router.py`.** `_PRICE_PER_MTOK` holds `$/1M-token` rates per model — **verified 2026-07-20 against current Anthropic pricing** (Opus 4.8 $5/$25, Sonnet 4.6 $3/$15, Haiku 4.5 $1/$5; `claude-sonnet-5`'s $2/$10 entry is the 2026-07-18 promo rate and expires 2026-08-31 — re-verify that one entry after the promo ends, non-promo is $3/$15). `_compute_cost()` prices cache tokens as FRACTIONS of the input rate (`cache_creation` at 1.25x input, `cache_read` at 0.1x input) — also verified. `_record_spend()` is **best-effort**: wrapped in try/except, an absent/odd `usage` field or a DB failure NEVER crashes the LLM response. usage-None is silent (legit, no row); usage-present-but-unparseable (e.g. a `MagicMock` in tests) logs a one-line `could not meter…` warning and writes NO row. The spend write happens synchronously inside `_create_sync`/`_create_sync_raw` — that runs in a `run_in_executor` worker thread (NOT the event loop), so a sync `Session` there is correct; do NOT "fix" it into `to_thread`.
- **Per-task spend attribution (Tier 1.6):** `SpendLog.task_id` (nullable, indexed; added to old DBs by the idempotent `_ensure_spendlog_columns()` ALTER-shim). `router._current_task_id` is a `contextvars.ContextVar` set by the orchestrator on the durable path (`set_task_context`/`reset_task_context`, reset in a `finally`). **The contextvar does NOT survive the `run_in_executor` hop** — verified by test — so `_run`/`run_with_tools` capture `_current_task_id.get()` ON THE LOOP and thread the value down to `_record_spend(..., task_id)` via `functools.partial` (the documented fallback, not the contextvar-in-thread path). `governor.task_spend_since(start, task_id=None)` and `check_budget(task_id, task_start)` scope per-task spend by `task_id` so one task's overspend can never trip another's cap.
- **`backend/safety/governor.py`** is all-sync (call via `asyncio.to_thread` from coroutines). `today_spend_usd()` sums `SpendLog.cost_usd` since the most recent local midnight (in `briefing_timezone`, converted to a NAIVE UTC instant to match `created_at`). `task_spend_since(start)` sums since a task's start. `check_budget(task_id=None, task_start=None)` raises `BudgetExceeded(scope, spend, cap, task_id)` when the daily (always) or per-task (if `task_start` given) cap is reached. `get_system_state` / `set_autonomy` / `set_budgets` read/write row 1.
- **Daily brake (universal):** `router._run` calls `check_budget()` before EVERY billed call. Only `BudgetExceeded` propagates; any other governor error is swallowed so a governor bug can't DOS the assistant. **Per-task brake:** `orchestrator.run_task` (durable path only) stamps `task_start` at entry and calls `check_budget(task_id, task_start)` before each non-done step; a `BudgetExceeded` (raised here or bubbling from `_run`) finalizes the task `failed` with `result_json {"error":"budget_exceeded","scope","spend","cap"}` (done steps preserved) and returns `TaskResult(success=False, reason="budget_exceeded")`.
- **Budget early-warning (2026-07-20):** `governor.budget_warning_due(threshold_pct)` fires a single Telegram warning (kind `budget_warn`, never suppressed by `phone_suppressed_kinds`) the first time spend crosses `budget_warn_pct` (default 0.80) of the daily cap, then stays silent until the local date changes — edge state persisted in `SystemState.last_budget_warn_day` (a date string, not a timestamp, so rollover reset is free and restarts don't re-fire). Runs as a third check inside the existing 5-min `watchdog.run_watchdog()` job, NOT a new scheduler job or per-call check — this is purely an early-warning UX nicety, `check_budget()`'s hard 100% enforcement above is completely untouched and unaffected either way. **Coupling to know about:** disabling `watchdog_enabled` also disables the budget warning (shares its gate); a separate `budget_warn_enabled` flag exists for disabling just the warning while keeping stall/dead-letter checks on.
- **401-burst watchdog (2026-07-25):** `backend/auth.py::require_api_key`'s existing 401 branch now also feeds `backend/safety/authfail.py` — a bounded, process-local, in-memory counter (`MAX_SOURCES=64` distinct client identities, `MAX_EVENTS_PER_SOURCE=512` ring buffer each; deliberately volatile, resets on restart) keyed by client identity. `_client_source` only trusts `X-Forwarded-For`'s first hop when the direct peer IS loopback (i.e. genuinely arrived via `tailscale serve`, which is why uvicorn — run without `proxy_headers` — would otherwise see every proxied request as `127.0.0.1`); trusting XFF unconditionally was a real bug caught in verification — any caller could spoof it to evade detection, evict a real offender out of the bounded table, or misattribute the alert. Both `_client_source` and `_client_path` are sanitized (charset-restricted+truncated / HTML-escaped respectively) since both are attacker-controlled and end up in an HTML-parse-mode Telegram message — an unescaped path was a second real bug caught in verification (a crafted path could render a live link in a trusted alert, or malformed HTML could fail the send and suppress the alert, since `claim_auth_burst_alert` commits the claim before `notify_phone` is even attempted). `watchdog.check_auth_failure_burst()` — the fourth check inside the same 5-min `watchdog.run_watchdog()` job — pages once (kind `auth_burst`, not suppressed) when one source crosses `auth_burst_threshold` (default 25) failures within `auth_burst_window_minutes` (default 30), via `governor.claim_auth_burst_alert()` (same edge-trigger-and-persist pattern as `budget_warning_due`, but persisted PER SOURCE in `SystemState.auth_burst_alert_json` — a JSON map of client identity → last-active UTC timestamp — rather than a per-day flag: each claimed source re-arms independently once IT has been quiet for the full window, not on a calendar boundary). **Fixed 2026-08-05 (was a known limitation):** the original design persisted the tracked sources as a CSV (`auth_burst_alert_sources`) sharing ONE timestamp (`auth_burst_alert_at`) for the whole set — a perpetually-storming source kept refreshing that shared timestamp, so a different, already-quiet source sharing the set never re-armed until the noisy one also went fully quiet, and the set could only ever be cleared all-or-nothing, growing without bound (and across restarts) for as long as any one source kept storming. The per-source JSON map (`governor._load_auth_burst_tracked`/`claim_auth_burst_alert`) fixes both defects — each source re-arms on its own schedule — and the persisted map is hard-capped at `governor._MAX_AUTH_BURST_TRACKED = 64`, mirroring `authfail.MAX_SOURCES`, since this path is pre-auth reachable and, unlike authfail's in-memory table, this one survives restarts. The two old columns are left orphaned (nullable, unread) on existing DBs rather than dropped — SQLite `DROP COLUMN` is a table rebuild and two dead columns cost nothing — with no CSV-to-JSON migration (a storm in flight at upgrade time gets one duplicate page, cheaper than migration code that can only ever run once). Gated by both `auth_burst_enabled` and the shared `watchdog_enabled`. Deliberately does NOT block/rate-limit the offending source, parse `logs/backend.log` (no timestamps in that format, and the file is truncated fresh per run — see the option-(b)-over-(a) reasoning in `authfail.py`'s docstring), or persist individual failure events to the DB (write-amplification risk on a pre-auth-reachable path — only the alert *edge* is durable).
- **Integration contract canary (2026-07-26):** `backend/safety/contracts.py` is a pure registry (`CONTRACTS`/`EXCLUDED` — no I/O, no await, no DB) of `FieldContract`s asserting each integration's **cached** `fetch()` return still has the shape its real consumers actually index into — not a generic schema, the specific field a specific `briefing.py`/`tools.py`/JSX line reads. Catches the failure mode neither the 2-min uptime job nor a `health_check()` can: a 200-but-silently-blanked response (e.g. an expired token returning `{}`/an empty list instead of raising), which every downstream consumer would otherwise render as a confident fact. `watchdog.check_integration_contracts()` — the fifth check inside the same 5-min job — reads the ALREADY-cached `fetch()` (never `fetch.__wrapped__`: the cache is what consumers actually read, and bypassing it would re-trigger real side effects — `homeassistant.fetch()` can POST `reload_config_entry`, `unifi.fetch()` writes `KnownDevice` rows and does a full login). An integration whose `fetch()` **raises** is not a breach (that's an outage, already the uptime job's job) and resets that integration's streak; only a successful-but-wrong-shaped return counts, and it takes 3 consecutive 5-min ticks (`contract_canary_consecutive_ticks`, ~15 min — long enough that no two ticks can share one integration's ~30-60s cache window) before paging (kind `contract_breach`, not suppressed). Deliberately **hand-maintained, not auto-generated from consumer code** — an auto-introspector would assert on fields an integration never even assigns; `unraid.mover_running` is exactly that case (its real reader is `briefing.py`, not `tools.py`) — so its contract is inverted: "assert it's STILL the dead default", which fires exactly once if anyone ever wires it up — and would silently regenerate itself at the exact moment a review is most needed. **`unraid.cpu_pct`/`ram_pct` and `unifi.bandwidth_mbps`/`alerts` wired up 2026-07-29** (previously the same dead-default case as `mover_running` — `tools.py` read them but nothing ever assigned them): live GraphQL `__type(name: "Metrics")` introspection against the real Unraid server (`__schema` is disabled, named-type queries aren't) found `metrics.cpu.percentTotal`/`metrics.memory.percentTotal`, added to `unraid.py`'s `_GQL_QUERY`; live-verified `memory.percentTotal` tracks `active/total`, NOT `used/total` (which reads ~95% on a healthy box thanks to Linux's reclaimable disk cache) — same "actual usage, excluding cache" convention Unraid's own webGUI uses. `unifi.py`'s `fetch()` now derives `bandwidth_mbps` from the already-fetched `stat/health` response's `wan` subsystem `tx_bytes-r`/`rx_bytes-r` (bytes/sec, converted to Mbps — verified live it moves between polls like a real gauge; the site-wide `stat/report/5minutes.site` endpoint was tried live too but only returns stale historical rollups, not a current rate) and `alerts` from a live GET `.../list/alarm` call (verified 200 + real `{"data": [...]}` shape against the real controller; an empty result is a legitimate "no active alarms" state, not a failure — raises on non-200 same as the pre-existing clients/health calls in that function). UniFi's `list/alarm` returns ALL alarms by convention, not just active ones — no alarm existed on the real controller during verification to confirm the exact field name, so `fetch()` defensively filters on `archived` (a no-op if that key is absent, correct if present) rather than trusting an unverified "no `archived` param = active-only" assumption. All four GraphQL/JSON leaf reads use `.get(key) or default`, not `.get(key, default)` — a leaf can be explicitly `null` (not just missing), and the dict-default form doesn't catch that; an unguarded `round(None, 1)` or `None + None` would raise and get misreported as a full integration outage by the outer exception handler. Contracts: `cpu_pct`/`bandwidth_mbps`/`alerts` flipped to `"type"` (a genuinely idle/quiet reading can legitimately be near-zero/empty, so only a real-shape check applies) — but `ram_pct` was set to `"positive"` (not `"type"`), since a running server's memory usage is never legitimately 0.0 and losing that check would blind the canary to `metrics` silently going null again, the same class of regression that left `parity_status` stuck at its default for months before the canary caught it. A drift test (`test_every_integration_is_covered`) enforces every integration in `backend/api/sources.py`'s registry has a `CONTRACTS` or an explicit `EXCLUDED` (with reason) entry, so a newly-added integration without a contract fails the suite rather than going silently unwatched. Companion fixes shipped alongside (both intentional behavior changes, approved 2026-07-26): `github.py` now raises on any non-200 instead of returning an empty list (matches every other integration's already-adopted convention — a dead token used to read as "0 open PRs"); `adguard.py`'s `filtering_enabled` is `bool | None` (`None` = "status read failed", not a silently-wrong `True` default that could report protection on when it's actually off) — `briefing.py`/`chat.py`/`tools.py`/`Dashboard.jsx` all updated to render "unknown" for the `None` case.
- **Council-loop post-mortem (2026-07-26):** `backend/agents/council_postmortem.py` independently re-verifies a Council-loop session's Realist claims against the real commit range in whatever `target_repo` it just built (Council-loop is a separate repo, `C:\Users\Brian\Documents\Council-loop` — its three checks: scope drift via `git diff --name-only` against an allowlist Haiku-extracts from `goal.md`'s prose Objective, placeholder code via `ast`-parsing changed `.py` files for a `pass`/`...`/`raise NotImplementedError`-only body plus a cruft-marker grep restricted to added diff lines, and test-citation existence via `git cat-file -e` against path-like tokens regexed out of the Realist's transcript prose and `history.jsonl` notes). **Deterministic, not model-based independence** — Council-loop's own `.council/config.local.json` currently assigns Realist=opus (same as Arbiter), so there's no smarter model to delegate to; the one LLM call (the allowlist extraction) uses Haiku purely as the only role-free model, for cost, not independence — the real independence is that checks 1-3 contain no LLM at all. Triggered by Council-loop's `run-loop.ps1` POSTing `{"task_name": "council_postmortem"}` to the existing `/api/trigger` endpoint at driver exit (giving that endpoint its first real caller — previously provisioned but unused) rather than a NEXUS scheduler job, because `/goal` **truncates** `.council/state/history.jsonl` on every new session, so a poller that missed the window would lose that session's history permanently and silently. Read-only against the target repo (git log/diff/cat-file/rev-parse only — never checkout/revert/stash); pages once via `notify_phone(kind="council_postmortem")` only when findings are non-empty (a clean post-mortem logs and stays silent). **If a recorded commit SHA no longer resolves in the target repo** (rebase/amend/force-push, or `target_repo` repointed since history.jsonl was written) **that itself pages** with `ok: False` — verification caught that the original design let this fall through to silent `ok=False`/no-notify, indistinguishable from a clean run on Brian's phone, the worst failure mode for a feature whose entire premise is "nobody re-reads this otherwise." Does NOT run the target repo's configured `test_commands` by default (`council_postmortem_run_tests`, opt-in, unimplemented in v1 — executing a foreign repo's arbitrary configured command inside the NEXUS process is a much bigger trust step than reading its git history). Does NOT cover a manual `/council-cycle` run or a hard driver crash — only the autonomous `run-loop.ps1` exit path, which is the one nobody re-reads. The Council-loop-side change (the one POST added to `run-loop.ps1`) is a separate repo and a separate confirmation, not part of this NEXUS-side build.
- **Chat degrades gracefully:** `chat.py` catches `BudgetExceeded` across the classify + routing branches and replies "I've hit the configured spending limit for now…" (persisted normally; no exception reaches FastAPI).
- **Kill switch:** `SystemState.autonomy_enabled`. The broker (`execute_action`) checks it AFTER the idempotency replay but BEFORE classify/decide — when OFF, `agent`/`autonomous` actors get `FORBIDDEN` (logged with reason `autonomy_disabled`, no dispatch); `user` actions are unaffected.
- **Confirm-policy override layer (2026-07-26, Feature 3 Phase 1):** `decide()` gained an optional keyword-only `policy: dict | None = None` (`{"auto_allow": set[str], "forbid": set[str]}`) — `policy=None` reproduces pre-existing behavior byte for byte, every positional-arg caller/test is unaffected. `execute_action` builds `policy` from `governor.get_system_state()`'s two new CSV-on-`SystemState` fields (`policy_auto_allow_kinds`/`policy_forbid_kinds`, same singleton-CSV idiom as `auth_burst_alert_sources`) and reuses the SAME `get_system_state()` call the kill-switch check already makes — zero extra DB round trips. Evaluation order IS the safety property: a `forbid` entry is checked FIRST (before irreversibility/risk), so a kind in both lists is always `FORBIDDEN`, fail-safe on contradictory state; `auto_allow` is checked AFTER irreversibility (an irreversible kind like `protonmail_send` stays unpromotable structurally, not by policy) and excludes `UNCLASSIFIABLE` risk (an unknown kind has no real policy behind it — promoting it would be a blank cheque) and a hardcoded `_NEVER_PROMOTABLE` floor (`policy_promote` itself — the promotion mechanism can never promote itself). Granting a promotion is a new broker kind `policy_promote` (HIGH/REVERSIBLE_BY_INVERSE — reused `safety:confirm`/`safety:reject` Telegram buttons, full ActionLog audit in the same table a future learner reads from) so promoting toward MORE autonomy always needs a human tap; revoking one (`DELETE /api/safety/policy/auto-allow/{kind}`) or demoting toward LESS autonomy (`DELETE /api/safety/policy/forbid/{kind}`, or a future learner calling `governor.add_forbidden_kind` directly) needs no gate at all — tightening only removes capability. `ActionLog.confirmed_at` (stamped by `confirm_action` immediately after the needs_confirm guard, before TTL/kill-switch/dispatch, so it measures human reaction time not dispatch latency — 19-30s for `protonmail_delete` alone) makes `needs_confirm→confirmed→executed` finally distinguishable from `allowed→executed` in the audit trail, which were previously identical rows. **The actual promotion learner (the thing that reads confirm/reject history and proposes changes) is deliberately NOT built yet** — a live query of `ActionLog` before this shipped found only 2 `needs_confirm` rows in 40 days, both expired unanswered, zero real confirms ever recorded; Phase 1 (this) ships the data-capture + override layer + manual `/api/safety/policy` endpoints on their own merits, Phase 2 (the learner) waits until real confirm data exists to learn from. `_dispatch_policy_promote` validates `payload["kind"] == target` (single kind, no comma, not in `_NEVER_PROMOTABLE`) before writing anything — verification found that without this, a payload naming multiple comma-joined kinds would promote all of them while the Telegram confirm alert (built from `target`) only ever named one, so the human would be tapping confirm on a description that doesn't match what actually happens. Not reachable in Phase 1 (no caller of this kind exists yet), but the exact trap Phase 2's learner must not fall into.
- **Kill/budget/cancel enforced INSIDE the loop (Tier 1.6):** `POST /api/safety/pause` now halts an in-flight user task. The orchestrator durable loop runs a per-step gate in the order **BUDGET → KILL → CANCEL** (budget brake, then `get_system_state().autonomy_enabled` → finalize `stopped`/`autonomy_disabled`, then cooperative cancel → `stopped`/`cancelled`). `router.run_with_tools(..., task_id=, task_start=)` runs the SAME guard (`_loop_guard`) BETWEEN tool rounds: `BudgetExceeded` propagates, a new `router.TaskAborted(reason)` (`stopped`|`cancelled`) propagates; the orchestrator's outer try catches `TaskAborted` and finalizes `stopped` (done steps preserved). With `task_id=None` (chat/briefing single-shot via `_run`) the guard is the daily-cap brake only — kill switch + cancel are NOT consulted. Order is documented in both `_loop_guard` and the orchestrator.
- **Poison-step ceiling (Tier 1.6):** `orchestrator.MAX_STEP_ATTEMPTS=5`. Before marking a non-done step running (so an exhausted step trips on resume too), if `attempts >= MAX_STEP_ATTEMPTS` the task finalizes `failed`/`step_exhausted` (with `step_index`/`attempts`). `attempts` accumulates across retry+replan (`_mark_step_running` increments; `_patch_step_durably` preserves). `worker_pool._load_unfinished_task_ids` filters to `(pending, running)` so a terminal task is never re-enqueued — that terminal status is what breaks the poison loop. `worker_pool._finalize_failed` also writes a minimal best-effort `AgentRun` row.
- **API (`backend/api/safety.py`, all auth-gated):** `POST /pause` (autonomy off + `scheduler.pause()`), `POST /resume` (autonomy on + `scheduler.resume()`), `GET /status` (autonomy + today's spend + caps + scheduler-running), `POST /budget` (`{daily_usd?, per_task_usd?}` runtime cap-setter → new state). All scheduler access is guarded with `getattr`/try-except.

## Secrets — never commit
`config.py`, `nexus.vault`, `.vault.key`, `.env` are gitignored and MUST stay that way. Secrets live encrypted in `nexus.vault` (Fernet, key in `.vault.key`); non-secret config in `.env`. `backend/secrets/vault.py` reads them; `Settings` (`backend/config.py`) exposes secret properties lazily. `nexus.vault.meta` (names + timestamps only, no values) is safe to track.

**Durable infisical→legacy-vault fallback signal (2026-07-28):** the `infisical → legacy vault` fallback in `backend/secrets/manager.py::get_secret` now writes a durable `SecretFallback` row (`backend/database.py`) — one AGGREGATE row per secret key (`event_count`/`first_at`/`last_at`), the key NAME only, never the value. The write path is a bounded in-memory coalescing buffer in `backend/secrets/fallback_log.py` (`record()` is called synchronously from `get_secret()`, which is loop-thread-reachable — no DB write happens there, since `engine`'s `busy_timeout=30000` could block the loop up to 30s, `get_secret()` can be called from inside code already holding an open `Session`, and it runs during startup before `create_db_and_tables()`). The buffer is drained by the unconditional 300s `secret_fallback_drain` scheduler job (`backend/scheduler.py`) plus a lifespan-shutdown flush (`backend/main.py`) — deliberately **not** a `run_watchdog()` check, since that would couple a durability guarantee to `watchdog_enabled` (see the existing "disabling `watchdog_enabled` also disables the budget warning" wart above — this fix exists specifically not to repeat that pattern). Surfaced via `GET /api/safety/status`'s `secret_fallback` field and the rewritten `_infisical_soak_reminder` (now quotes real DB data instead of telling Brian to grep a log that truncates on every restart). Residual gap: a hard process kill (`taskkill`) still loses at most one 300s window of buffered-but-undrained events.

## Tier B layer (council round 3, 2026-07-02 — all live)
- **Tool loop hardening (`router.py`):** every client-side tool_result is wrapped in `<tool_output>…</tool_output>` sentinels and `run_with_tools` auto-appends `TOOL_OUTPUT_RULE` (data-not-instructions) to EVERY caller's system prompt — don't add per-caller copies. A moving `cache_control` breakpoint sits on the newest tool_result each round (system+tools blocks were already cached). Tests assert the wrapped shape.
- **Spend labels:** every `haiku/sonnet/opus` call in `backend/agents/` MUST pass `label=` — `tests/test_spend_report.py::test_no_unlabeled_llm_calls_in_agents` is a paren-scanning regression guard that fails the suite otherwise. `governor.spend_report` groups `by_label`; `GET /api/safety/spend-report?days=` (auth) serves it.
- **Goal outcomes:** completing a goal writes `Goal.outcome_summary` (no-LLM distill from Task.result_json, `goals._summarize_outcome`); the daily digest has a "Completed (24h)" block. Optional Haiku facts extraction behind `goal_outcome_distill_llm` (default ON since 2026-07-07 — best-effort, never blocks completion; set `GOAL_OUTCOME_DISTILL_LLM=false` to disable).
- **Goal Approve/Reject from Telegram:** proposer's `goal_proposed` notify passes `buttons` (`goal:approve:{id}`) through `events.notify_phone(buttons=)` → NEXUS's own bot (`backend/integrations/telegram.py`, since the 2026-07-26 Hermes decoupling — see below) renders inline buttons → `backend/agents/telegram_poller.py`'s long-poll loop calls `goals.approve`/`goals.reject` directly (no HTTP round-trip through Hermes anymore). Single-use is enforced BOTH sides (the poller edits the message and drops the keyboard; `goals.py` itself 409s a re-approve, 410s expiry).
- **Safety Confirm/Reject from Telegram (2026-07-20, moved onto NEXUS's own bot 2026-07-26):** the same button pattern, for the action broker's `needs_confirm` alerts instead of goals. Both `broker.py` notify sites (the gate path ~611 and the judge-veto path ~697) pass `buttons=[{"safety:confirm:{log_id}"}, {"safety:reject:{log_id}"}]`. `telegram_poller.handle_callback` (pattern `^safety:`) calls `broker.confirm_action`/`broker.reject_action` directly — `reject_action` closes a needs_confirm row to `forbidden`/`rejected_by_user`, no dispatch, no TTL/kill-switch check needed since rejection never dispatches. Same single-use-both-sides discipline as goals. This was the single biggest daily-friction gap identified in a 2026-07-20 audit — previously a HIGH-risk action just texted "open the Safety page," now it's one tap.
- **Uptime HTTP targets:** `UPTIME_HTTP_TARGETS="name|url|expect,..."` in .env adds plain-HTTP services (GLP app, Open WebUI…) to the 2-min uptime job as first-class sources.
- **PWA:** `frontend/public/manifest.webmanifest` + `icon.svg` (NO service worker, no new deps). Served from dist/ after `npm run build`.

## Tier C (batch 1) — NEXUS-only additions
- **Direct Proxmox read-only integration (`backend/integrations/proxmox.py`):** modeled line-for-line on `unraid.py`. Config keys: `proxmox_host` (default `https://192.168.1.60:8006`, plain `.env` field) and the `proxmox_token` secret property → vault `PROXMOX_TOKEN` (the FULL PVE header string `PVEAPIToken=user@realm!tokenid=uuid`, sent verbatim as the `Authorization` header). `fetch()` does ONE GET to `/api2/json/cluster/resources` and partitions rows by `type`: node → cpu (0-1 fraction ×100) + mem (bytes→GiB), qemu/lxc → `vms`, storage → summed disk/maxdisk. **Raises `RuntimeError` on non-200 / missing data / transport error — NEVER zero-defaults** (the Unraid lesson: zeros look like a dead node to briefing/trends/proposer). `health_check()` GETs `/api2/json/version`; returns False on any exception AND returns False (no HTTP call) when the token is empty/unset (unconfigured shows OFFLINE, never crashes). Registered in BOTH registries: `backend/api/sources.py` and `scheduler.py::_record_uptime`. No frontend/tools changes.
- **Dashboard VM/LXC card + reboot controls (2026-07-20, later repointed to the native `vm_power` broker kind — see "Write-actions brought in-house" below):** `GET /api/proxmox/` (`backend/api/proxmox_api.py`) surfaces `fetch()`'s data on the Dashboard — node status, VM/LXC count, a chip list with a start/stop/reboot dropdown per chip, actor=`user` (a click IS the human decision, so it executes immediately, same as chat — fully audit-logged either way).
- **Maintenance badges (2026-07-20):** `GET /api/proxmox/maintenance` adds two more cached fetchers, `fetch_updates()`/`fetch_backups()` (TTL 900s/falsy 60s — these change ~daily, not worth polling harder), calling the PVE apt/tasks endpoints directly — a chat-prose relay would be brittle to parse and coupled to another bot's uptime. Deliberately a SEPARATE endpoint from `fetch()`, not folded into it — `fetch()` raises-on-any-failure by design (feeds sources/uptime), so coupling would blank the whole VM/LXC card whenever the apt endpoint hiccups. `/maintenance` degrades each part independently to `null` instead, never 5xx's. Card shows "N updates pending" (only when N>0 — a 0 badge is noise) and a Backup OK/FAILED/running pill with a timestamp.
- **Today page checkable Priority Actions (frontend-only):** `frontend/src/lib/priorityActions.js::parsePriorityActions(content)` is a pure parser (finds `## Priority Actions`, tolerates `(max 3)` suffix, collects until next `## `, returns `{items, note}`). `Today.jsx` also fetches `api.briefing.latest()` (catch → hides the card; it 404s when no briefing exists) and renders a Priority Actions card ABOVE Agenda: each item is a checkbox row (checked = strikethrough/dimmed); empty items + note → renders the note as text. Persistence: **localStorage key `nexus_today_done:${briefing.id}`** = JSON array of checked indexes; on load it sweep-deletes any `nexus_today_done:*` keys with a different id. All storage access is try/catch-wrapped (disabled storage → in-memory only, no crash).
- **Brain Organizer spend under the governor (usage-file handoff):** the organizer subprocess can't write `nexus.db`, so `_call_api` in `modules/brain-organizer/brain_organizer.py` appends one JSON line per successful provider response to `modules/brain-organizer/logs/usage.jsonl` (`{ts, model, input_tokens, output_tokens, provider}`; Anthropic `response.usage.*_tokens`, OpenRouter `usage.prompt/completion_tokens`; whole thing best-effort try/except, stdlib-only, open-append-close per write). NEXUS-side ingestor `backend/agents/brain_spend.py::ingest_brain_spend()` (sync, NEVER raises) reuses `backend/api/brain_organizer.py::_MODULE_DIR`, `os.replace`s `usage.jsonl → usage.jsonl.ingest` as an atomic claim (FileNotFoundError → no-op; PermissionError → skip cycle), ingests any leftover `.ingest` FIRST (crash recovery), and writes one **`SpendLog` row per line with `label="brain_organizer"`, `task_id=None`**, cost priced via `router._PRICE_PER_MTOK` (**unknown model → cost 0.0 but tokens still recorded**; OpenRouter's `anthropic/…` prefix is stripped to match the table; malformed line → skipped with a warning). Scheduler runs it every **300s** (`id="brain_spend_ingest"`, best-effort via `asyncio.to_thread`). PRICE-table caveat: same `# VERIFY`-once `_PRICE_PER_MTOK` placeholders the router uses — dollar figures are only as good as that table.

## Tier C (batch 3) — HTTPS via tailscale serve
- **Frontend is HTTPS-ready:** when the page loads over `https:` the app uses SAME-ORIGIN `/api` + `/ws` (tailscale serve path mounts) instead of `http://host:8000` — avoids mixed-content blocking. Plain-HTTP LAN clients keep hitting `:8000` directly from the SAME build (runtime branch in `frontend/src/lib/api.js` + `ws.js`; `VITE_API_BASE`/`VITE_WS_BASE` stay top-priority overrides).
- **One-time operational setup** (Tailscale serve path mounts, WS verification, `APP_BASE_URL`) — see the `nexus-https-setup` skill (`.claude/skills/nexus-https-setup/SKILL.md`).

## Non-obvious rules (hard-won)
- **Never block the asyncio event loop.** Windows ProactorEventLoop + a blocked loop = `WinError 64`, dropped connections, "everything offline". All sync DB work inside `async` funcs goes through `asyncio.to_thread`. The memo watcher starts on a daemon `threading.Thread`, not the loop. **The backend now runs on the SelectorEventLoop (forced in `run.py`)** — ProactorEventLoop also throws `WinError 64` purely from CONCURRENT outbound httpx (e.g. `/api/health` fanning out to ~10 integrations) and on the accept path, independent of loop-blocking. Selector handles it; NEXUS spawns no in-loop subprocesses so Selector's limits don't apply. Don't revert to Proactor.
- **`async_ttl_cache` is load-bearing.** Every integration `fetch()`/`health_check()` is cached (success ~10-60s, failures ~3s via `falsy_ttl`). This is what keeps `/api/health` fast when many tabs/devices poll. Don't add per-request outbound calls without caching.
- **Auth:** all `/api/*` need a Bearer key (`NEXUS_API_KEY` from the vault); only `/api/health` is unauthenticated (`/api/briefing/latest` was retired from that exemption 2026-07-28 — see the security-fix note below). `/api/trigger` is Bearer-required (Tier 1.6) and additionally rate-limited (5 calls / 60s, process-local) — live caller is Council-loop's `run-loop.ps1` (`council_postmortem`). An earlier optional HMAC-signature layer on top of Bearer was removed 2026-08-09 (Hermes decommissioned, zero callers had ever signed a request). Each browser stores the key in `localStorage`; onboarding = open Settings on the new device and paste the key (the old `?key=...` setup links were RETIRED in Tier C — keys don't belong in URLs). `/api/setup/complete` is gated by a per-boot first-run bootstrap token (`backend/api/setup.py::require_setup_token`), generated while `_needs_setup()` is true, published to `.nexus-setup-token` (ACL-hardened like `.vault.key`) + the startup log (stderr → `logs\backend.err.log`), destroyed on successful completion; `/api/setup/status` stays public.
- **Uptime job runs checks sequentially**, not concurrently — firing all 10 health checks at once false-fails on cold TLS.
- **Goal proposer entity_id + nighttime lighting exemption (`backend/agents/proposer.py`, 2026-07-05):** `_ha_entity_summary`'s WATCH labels (e.g. `garage_light_left`) now carry the real `entity_id` alongside them in the prompt — the executor only ever sees the goal's title/description text (no live HA lookup), so it was previously guessing entity IDs back from friendly labels and hitting nonexistent entities (`light.garage_light_left` instead of the real `light.left_garage_light`). `homeassistant.call_service` now validates `entity_id` against the cached `fetch()` entity list BEFORE calling HA and raises `IntegrationError` on an unknown id — HA returns 200 with an empty changed-list both for a bogus entity AND a valid entity already in the target state, so the response alone can't distinguish them; existence must be checked up front, not inferred after the call. Separately, Brian leaves the porch/garage lights on overnight on purpose (security lighting) — `NIGHT_EXEMPT_LABELS`/`NIGHT_EXEMPT_ENTITY_IDS` + `_is_night(ha)` read HA's live `sun.sun` entity (`below_horizon`/`above_horizon`) as the primary night signal (tracks actual dawn, not a guessed clock hour — Detroit sunrise swings ~6am–8am seasonally), falling back to a fixed 20:00–07:00 window only if `sun.sun` is unavailable, and defaulting to daytime (no exemption) if that also fails. The exemption is enforced BOTH in the Haiku prompt AND as a deterministic post-filter in `propose_goals_tick` that drops any proposal referencing an exempt light while `is_night` — never rely on the LLM alone to honor an instruction like this.

## Model pipeline
Chat/classify: Sonnet 4.6 (`claude-sonnet-4-6`) answers · Haiku 4.5 (`claude-haiku-4-5`) routes/classifies. Chat uses Anthropic's hosted web search tool. Calls bill the `ANTHROPIC_API_KEY`.

**Orchestrator model tiers are CONFIGURABLE** (`backend/config.py`, `.env`-overridable) via `orchestrator_planner_model` / `orchestrator_executor_model` / `orchestrator_verifier_model`. The plan/debug roles route through `router.run_model(<configured model>)`; executor + verifier pass the configured model to `run_with_tools`. **Defaults are the cheaper "balanced" profile** (was Opus plan + Sonnet exec + Opus verify): **Sonnet plans + executes, Haiku verifies** — ~40–60% cheaper/task, no Opus in the loop. Restore max quality by setting planner/verifier back to `claude-opus-4-8` in `.env`. Tests drive the planner via `router.run_model` (not `router.opus`). The cost governor (daily/per-task USD caps) + kill switch still apply on top.

## Telegram bot + calendar (Phase 1-2c of the original Hermes decoupling, 2026-07-26/27)
**Phase 1 moved Telegram delivery and calendar reads directly into NEXUS**, off the Hermes bot NEXUS used to relay through. Hermes itself was **fully decommissioned 2026-08-09** — see that dated section further down for the full retirement record; nothing in this section describes anything still live on Hermes's side, since Hermes no longer exists.

**New: NEXUS owns its own Telegram bot** (its own bot/token — a separate Telegram chat, not a shared relay):
- `backend/integrations/telegram.py` — direct Bot API transport (`send_message`/`answer_callback_query`/`edit_message_text`/`get_updates`/`health_check`, plus Phase 2a's `send_reply`/`send_chat_action`/`set_my_commands`), `notify(payload)` (drop-in replacement for the old `hermes.notify()`, same payload shape). Failure classification: 400/401/403/404 (or a missing `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID`) are TERMINAL — logged at ERROR, never queued, since a byte-identical retry can't succeed; 429/5xx/transport errors queue for `deliver_pending()`. Also owns the relocated `PendingDelivery` retry queue (see above).
- `backend/agents/telegram_poller.py` — long-polls `getUpdates` as an asyncio task on the lifespan loop (NOT a thread — pure async I/O, unlike `memo_watcher` which needs a thread because `Observer.join()` blocks). Dispatches `goal:*`/`safety:*` callback_data straight to `goals.approve/reject`/`broker.confirm_action/reject_action` — no HTTP hop, no Hermes involvement. Rejects callbacks from an unrecognized `chat.id`. A Telegram `409` (another poller already consuming `getUpdates`) gets its own distinct log line + 30s backoff rather than being treated as a generic error. Authorization is fail-CLOSED and asymmetric: an unauthorized *callback* gets a "Not authorized" popup; an unauthorized *message* (Phase 2a — see below) gets NO reply at all, only a log line, so Brian can self-serve the one-time `TELEGRAM_CHAT_ID` discovery step without the bot confirming to a stranger that it's live.
- **Phase 2a (2026-07-26) — text commands/chat from the bot itself.** `backend/agents/telegram_commands.py`: `dispatch(text, msg)` parses `/cmd@botname args` or treats bare text as chat (matches Hermes's `/nx` muscle memory — no new habit to learn), looks up a `COMMANDS` table, wraps every handler in try/except (a handler exception becomes an error reply, never kills the poll loop), replies via `telegram.send_reply` (chunked, but deliberately does NOT queue into `PendingDelivery` on failure — a command reply redelivered an hour later is noise, not resilience). Commands: bare text/`/nx` → `chat.chat(conversation_id, text)` in-process (conversation persisted across restarts in `SystemState.telegram_conversation_id`, via new `governor.get_telegram_conversation_id`/`set_telegram_conversation_id`; `/clear` nulls it); `/status` is a NATIVE no-LLM snapshot (Proxmox/Unraid/UniFi/AdGuard/HA/Calendar/spend/autonomy, each degrading independently) — deliberately not routed through `chat()`'s STATUS branch, which only covers 4 of those and would cost nothing but still be a worse answer; `/calendar`, `/mail`, `/spend`, `/vms`, `/briefing`, `/help` are thin reads. **Deliberately NOT built**: `/model` (no NEXUS equivalent — its model tiers are `.env`-configured, not chat-switchable; the governor's spend caps + `/spend` already cover the cost-control need `/model` served) and the background alert watcher (VM/docker/garage/doorbell — the thing that actually breaks if Hermes's own bot ever stops), deferred to a later phase, not yet scoped.
- **Phase 2b (2026-07-26) — memory, goals/tasks, digest, mute, voice.** More `telegram_commands.py` handlers: `/remember <text>` runs the SAME Haiku extractor `chat()`/briefing already use (`facts.extract_and_store`, source="telegram") — non-deterministic (can extract 0/1/several facts) but consistent with the rest of NEXUS, not a separate strict-format parser; `/facts` / `/forget <id>` read/soft-dismiss via the existing `facts.py` audit-list/dismiss functions; `/goals` lists via `goals._db_list_goals`; `/task <prompt>` creates a `Task` row + `worker_pool.get_pool().enqueue()` in-process — the exact same effect as the authed REST `POST /api/tasks/`, just reached through the Telegram auth gate instead of a Bearer key (the orchestrator's read-only executor has no actor-based broker exposure at all, so this is strictly LESS privileged than Phase 2a's chat path, not an escalation); `/tasks` lists recent `Task` rows; `/digest` calls `digest.build_autonomy_digest()`. `/research` was deliberately NOT ported (Hermes's version was unused — 5 commits, one week, nothing since); `/task`+`/tasks` is its replacement, with no auto-emailed report (would need its own design decision — `protonmail_send` is IRREVERSIBLE and hard-forbidden to non-user actors).
  - **Runtime per-kind notify mute**: `/mute <kind>` / `/unmute <kind>` / `/muted`, backed by `SystemState.muted_notify_kinds` (CSV, same singleton-row idiom as `policy_auto_allow_kinds`) via new `governor.get_muted_notify_kinds`/`add_muted_notify_kind`/`remove_muted_notify_kind`. Distinct from the static `.env`-configured `phone_suppressed_kinds` — this is Brian's own on-the-fly control, checked in `events.notify_phone` right after the static check (both cheap short-circuits run before this DB read). That check has its OWN `try/except` defaulting to "not muted" — a DB hiccup here must degrade to sending the alert, never to silently dropping it, since this gates `auth_burst`/`contract_breach`/`budget_warn`/`needs_confirm` pages that are documented elsewhere as un-suppressible. **`kind` is now validated (2026-08-05)** against `backend/events.py::NOTIFY_KINDS`, a hand-maintained frozenset of every kind any `notify_phone`/`_edge_alert` call site actually passes — kept honest by an AST-scanning drift test (`tests/test_autonomy_notify.py::test_notify_kinds_registry_covers_every_call_site`, same discipline as `contracts.py`'s `test_every_integration_is_covered`) that fails the suite if a real call site's kind is missing from the registry or is ever built dynamically. `add_muted_notify_kind` rejects (never warns-and-mutes) a comma, a `_NEVER_MUTABLE_NOTIFY_KINDS` entry, or an unregistered kind, in that order — an unknown kind gets a `difflib`-based "did you mean" suggestion on a near-miss typo, and `/mute` with no args lists every valid kind. `remove_muted_notify_kind` deliberately stays unvalidated (must always be able to clear a stale pre-validation value) and now returns whether the kind was actually muted, so `/unmute` can say "wasn't muted" instead of falsely claiming success. Still no TTL — an accepted, still-open gap, but the free-form/unvalidated-typo gap above is now closed.
  - **Voice messages**: `telegram_poller._transcribe_voice` downloads via new `telegram.get_file_bytes` (Telegram file downloads use a DIFFERENT base path, `/file/bot<token>/<path>`, not `/bot<token>/<method>` like every other call) into a temp file, transcribes via the pre-existing `backend/agents/voice.py::transcribe()`, then dispatches the transcript through the same `telegram_commands.dispatch()` bare text uses. The temp-file cleanup is a GUARDED `os.unlink` (Windows can transiently hold a handle on a just-written file — an unguarded unlink failure in a bare `finally` would silently discard an already-successful transcript; matches the existing guarded pattern in `backend/api/voice.py`). A failed transcription still gets a reply ("couldn't transcribe that") — silence here would be indistinguishable from the bot being down, unlike every other inbound path. Environment: `openai`/`openai-whisper`/`torch` installed (bumped past the pinned `requirements.txt` version, which fails to build on this Python 3.13 setup — see that file's comment) + `ffmpeg` via winget; `voice.py::_ensure_ffmpeg_on_path()` works around `start.ps1`'s `Start-Process` not reliably inheriting a freshly-updated PATH on Windows.
  - **`_match_voice_command` (real incident, 2026-07-27):** voice can never produce a literal `/`, so a spoken command name — even a bare word like "mute" — would otherwise ALWAYS fall through to `chat()`. Live-proven consequence: Brian said "mute" meaning the `/mute` notification command; the transcript was heard closely enough to a real HA entity name that `chat()`'s HOME_CONTROL branch (actor="user", always-allowed, no confirm gate) turned on an actual smart switch instead. Fix: if a voice transcript's first word exactly matches a known `COMMANDS` name (case-insensitive, trailing punctuation stripped), it's rewritten to `/name <rest>` before dispatch — same as typing the slash would produce. Only an EXACT leading command-name match rewrites; ordinary conversational speech is untouched. This does not fully close the gap (any other single/short word could still coincidentally match a real HOME_CONTROL entity name) — it specifically closes the "a real command name spoken aloud" case, which was the actual incident.
  - **Why this needed real security scrutiny, not just "it's a Telegram bot":** `chat()` takes no actor parameter — every branch it dispatches through the broker hardcodes `actor="user"`, which the broker always-allows with no confirm gate and no kill-switch check. That's the accepted model (a `chat_id` that passes the auth check IS Brian, same trust as a valid Bearer key) — but it means the `_authorized` check is the ONLY gate in front of `HOME_CONTROL` and `TASK` intent's full orchestrator plan+execute loop. A hole here is full control, not an inconvenience — see `_authorized`'s fail-closed design and its regression tests in `tests/test_telegram_poller.py`.
  - Replay guard: `poll_once` drops any `message` update older than `telegram_command_max_age_s` (default 300s) using the update's `date` field — fails CLOSED on a missing/malformed date (dropped, not treated as fresh) since Telegram replays up to 24h of un-acked updates after a restart and a queued command re-executing on boot is a real correctness issue, unlike single-use buttons (already idempotent). Never applied to `callback_query` updates. A malformed single update can't wedge the batch — the offset advances before per-update processing, and a per-update exception is caught and logged rather than propagating.
  - Concurrency: message handling is fire-and-forget (`asyncio.create_task`) gated by a module-level `asyncio.Semaphore(1)` so a slow `chat()` call (20-40s with web search) never blocks `getUpdates` or a button tap, while two messages still serialize relative to each other. Every spawned task's reference is held in a module-level `_message_tasks` set (removed via `add_done_callback`) — `asyncio.create_task()` only holds a *weak* internal reference, so a dropped reference risks silent mid-run garbage collection; `stop()` cancels any still-running message tasks rather than leaving them orphaned past shutdown.
- `backend/integrations/calendar.py` — port of Hermes's `tools/gcal.py` (pure iCal-over-HTTPS + RRULE expansion, no OAuth), fixed during the port rather than copied: the `%`+no-pad-flag strftime directives are glibc-only and raise on Windows (this host), and `date.today()` read the *process* timezone instead of `briefing_timezone`. Raises `RuntimeError` when no feed is configured or every configured feed fails — never silently returns an empty-looking calendar (a genuinely empty calendar is `events=[]` with `feeds_ok>0`, a real, legitimate state).
- `calendar` (not `telegram` — it isn't a "source" the way integrations are) is registered as a 13th entry in `api/sources.py` and `scheduler.py::_record_uptime`, plus a `backend/safety/contracts.py` entry — which asserts `summary` stays str-typed, NOT `events`: briefing.py/today.py only ever consume `get_today_events()` (a string), never touch `.events` directly, so `summary` is the field a real shape change would actually break. The `feeds_ok`/`feeds_total` cross-field "one feed silently dead" case was considered and deliberately left unasserted, since the contract framework only compares one field to a fixed default/positive/nonempty rule, not two fields to each other.
- 4 new vault secrets: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `GOOGLE_CALENDAR_ICAL_URL`, `APPLE_CALENDAR_ICAL_URL` (last one optional — a second feed, exactly as Hermes treated it).
- **Phase 2c (2026-07-27) — background homelab alert watcher.** `backend/agents/homelab_watch.py`, a new 60s scheduler job (`_homelab_watch`), ports Hermes's `watcher.py` edge-alert loop onto NEXUS's own integrations so these pages keep firing even if Hermes's bot process is ever stopped: VM/LXC stopped (Proxmox), Docker container stopped (Unraid), array unhealthy, a disk over `homelab_disk_temp_warn_c` (default 45C), garage door open past `homelab_garage_open_minutes` (default 30), and a failed vzdump backup. Six new notify kinds (`homelab_vm_stopped`/`homelab_docker_stopped`/`homelab_array`/`homelab_disk_temp`/`homelab_garage`/`homelab_backup_failed`), all individually `/mute`-able (deliberately NOT on `_NEVER_MUTABLE_NOTIFY_KINDS` — these are homelab conveniences, not safety-machinery pages; a mute has no TTL, same known gap as every other kind).
  - **State is in-memory, not DB-persisted** (`_vm_states`/`_docker_states`/`_active_alerts`/`_garage_open_since`) — its entire useful lifetime is the 60s until the next tick. Consequence accepted knowingly: after a NEXUS restart, the four level-triggered checks (array/disk/garage/backup) re-derive from live data and may re-fire once more (safe direction, a duplicate not a miss); the two transition-triggered checks (VM/docker) lose any transition that happened while NEXUS was down, same as Hermes's own watcher today.
  - **Latches on ATTEMPT, not confirmed delivery** — deliberate deviation from Hermes's watcher, which blocked on delivery confirmation because it had no retry path. NEXUS's `notify_phone` already hands off to the `PendingDelivery` retry queue + dead-letter watchdog, so blocking a scheduler tick on Telegram delivery would be strictly worse.
  - **Two new Telegram callback namespaces**, `docker:restart:<name>` and `vm:start:<vmid>`, added to `telegram_poller.py`. Docker restart dispatches `unraid_docker` directly (native) — note `_dispatch_unraid_docker` does NOT raise on a failed restart, it returns `{"success": False}` and the broker still records `EXECUTED`, so success must be read off the result, not the decision (the exact gap `test_docker_restart_failure_alerts_and_keeps_buttons` guards). VM start dispatches the native `vm_power` kind (see "Write-actions brought in-house" below — that phase landed after this one, repointing both `telegram_poller.py` and `Dashboard.jsx` off their original bridge onto the native path). Both use `actor="user"` (a button tap IS the human decision, same precedent as the Safety page's confirm/reject buttons) and route through `broker.execute_action`, never a direct integration call. The int-coercion on `handle_callback`'s third callback_data segment is now namespace-scoped (`_INT_ID_NAMESPACES = {"goal", "safety"}`) since a container name/vmid isn't a DB primary key — `goal:*`/`safety:*` behavior is unchanged.
  - **KNOWN ISSUE, unverified (found by Opus verify, 2026-07-27): the ↺ Restart button may not actually work.** `unraid.py`'s `fetch()` truncates every container's GraphQL id to `[:12]` — live schema introspection against the real server showed Unraid's real container ids are 129-char `<server-hash>:<container-hash>` PrefixedIDs, and the `[:12]` truncation collapses EVERY container to the identical shared server-hash prefix. `restart_docker`'s own `_SAFE_CONTAINER_ID` guard also rejects the `:` in a real id outright. This is a PRE-EXISTING bug (Dashboard.jsx's restart button, which passes `c.id`, inherits the exact same breakage — not introduced by Phase 2c), not something safe to blind-fix overnight against a shared, live integration without empirical confirmation. It degrades gracefully — a failed restart shows "Restart failed." and keeps the button for a retry, never crashes or silently no-ops. **Before trusting this button: tap ↺ Restart once on a container you don't mind bouncing and read the popup.** If it fails, the fix is in `unraid.py` (stop truncating `id`, relax `_SAFE_CONTAINER_ID` to allow `:`) plus resolving the Telegram-carried container NAME to its real id before the mutation (keep the name in `callback_data` — the real id is 144 bytes, well past Telegram's 64-byte limit).
  - A container-restart button is only attached when the name passes `unraid._SAFE_CONTAINER_ID` AND the full `callback_data` fits Telegram's 64-byte limit — otherwise the alert still sends, just with no button, rather than risking a truncated `callback_data` restarting the wrong container. Every interpolated name (container, VM, disk) is `html.escape()`d before going into the alert text — `notify_phone` sends `parse_mode="HTML"`, and an unescaped `<`/`&` would make Telegram reject the whole message as a 400 (terminal, never queued — the alert would simply vanish).
  - **Explicitly NOT built**: doorbell/camera-snapshot alerts (Brian declined — needs a separate 5s poll + `send_photo`, not worth it yet). NEXUS's own liveness check (a process cannot monitor its own death) was solved externally on 2026-07-27 — see "NEXUS liveness monitoring (Phase 5)" below.
## Write-actions brought in-house (Phase 7, 2026-07-27)
Three of the 19 verbs NEXUS used to relay through Hermes now dispatch natively
— `backend/safety/broker.py`'s dispatch table already treated a direct-integration call and a
relay call as fully interchangeable (confirm/audit/idempotency/kill-switch operate purely
on the `kind` string), so no broker redesign was needed. The relay verbs stayed available in
parallel for a rollback window, matching the Infisical migration pattern — Hermes retired
entirely 2026-08-09 (see that dated section further down), so that window is long closed and
the old relay path (`hermes_relay`/`hermes_action`) has itself since been deleted from the
broker (2026-08-09).

- **7a — UniFi block/unblock.** `backend/integrations/unifi.py`: `_login()` extracted from
  `fetch()`'s inline login block, now also captures the `X-CSRF-Token` UniFi OS requires for
  mutating POSTs (header first, falls back to decoding it out of the `TOKEN` cookie's JWT payload).
  `_normalize_mac()` accepts colon/hyphen/dot/bare-hex forms, raises on anything else — a
  malformed value never reaches the `stamgr` POST body. `block_client(mac)`/`unblock_client(mac)`
  raise on any failure (non-2xx or a 200 with `rc != "ok"`) rather than returning a swallowed
  `False`. Broker: `unifi_block`/`unifi_unblock`, HIGH risk (same band Hermes's own verbs already
  carried) — the inverse is clean, but a wrong MAC risks locking out a real device, so an agent
  always needs a human tap. **Drilled live** against the Ecobee thermostat (`44:61:32:bb:53:fd`):
  blocked, confirmed offline both via a fresh UniFi API query and visually on the device, unblocked,
  confirmed reconnected.
- **7b — Proxmox VM power (start/stop/reboot).** Checked the existing `PROXMOX_TOKEN`'s actual
  Proxmox-side permissions directly (`pveum user token list root@pam` → `"privsep": 0`) before
  writing any code — privilege separation is disabled, so the token already has full `root@pam`
  admin rights; no permission grant was needed. `backend/integrations/proxmox.py::set_vm_power`
  resolves the vmid against `fetch()` to get both `node` and `type` (never assumes qemu vs lxc —
  the API path differs: `/nodes/{node}/qemu/{vmid}/...` vs `.../lxc/{vmid}/...`). `action="stop"`
  deliberately maps to Proxmox's graceful `shutdown` op, not the raw power-pull `stop` — that
  harsher operation isn't exposed. Broker: `vm_power`, HIGH risk (matches Hermes's `vm_action`
  exactly). New broker-gated route `POST /api/proxmox/vm/{vmid}/power`; `telegram_poller.py`'s
  `vm:start:<vmid>` and `Dashboard.jsx`'s VM action dropdown both repointed from the old
  `hermes_action`/`vm_action` bridge to this native path (Dashboard now sends `vmid`, not `name`).
  **Drilled live** against LXC 201 (already stopped since Phase 6, so free to bounce): start → real
  Proxmox UPID + `pct status` confirms running; stop → real UPID + confirms stopped; start again to
  leave it in a known state.
- **7c — Unraid docker restart, actually fixed.** Live GraphQL introspection (schema-level
  `__schema` is disabled server-side, but named-type queries like `__type(name: "Mutation")` still
  work) confirmed two things the original plan didn't know going in: **no `restartContainer`
  mutation exists anywhere in the schema** — the OLD `restart_docker()` had been silently calling a
  mutation that doesn't exist, failing gracefully (caught by its own error handling) but for a more
  fundamental reason than the previously-documented id-truncation bug — and **no prune mutation
  exists at all**, root or nested (Hermes's `docker_prune` uses raw SSH, not GraphQL — confirming
  7d's original scoping was correct). Fixed both real bugs found in this pass:
  1. **Id truncation removed.** `fetch()` no longer truncates container ids to `[:12]` (real ids
     are 129-char `<hash>:<hash>` PrefixedIDs); `_SAFE_CONTAINER_ID` relaxed to allow `:`.
  2. **`restart_docker(name_or_id)` is now genuinely two calls: `stop` then `start`**, via a new
     `resolve_container_id()` (passes through a real id as-is, else resolves an exact name match,
     raising on unknown/ambiguous — never guesses) and `_docker_mutation()`. Polls up to ~5s for
     the container to actually report non-RUNNING before firing `start` (starting mid-stop was the
     likely failure mode of firing immediately). Returns a 3-state result — `{"success": True}`,
     `{"success": False, "error": ...}` (stop itself failed, container presumed untouched), or
     `{"success": False, "stopped": True, "error": ...}` (stop succeeded, start failed — **the
     container is confirmed DOWN**, must never read as a routine failure). Every caller
     (`broker._dispatch_unraid_docker`, the REST route, `telegram_poller.py`, and
     `write_tools._unraid_docker_restart`'s agent-facing string) updated to surface the `stopped`
     state distinctly — the generic `_decision_to_str` helper would otherwise have rendered a
     failed/half-restart as `"OK — performed. {...}"` for an agent, a real gap found while fixing
     this (predates Phase 7c, now closed for this dispatcher specifically).
  3. Callback data / Dashboard both keep passing the container **name** (never the 129-byte real
     id — past Telegram's 64-byte `callback_data` limit); the backend resolves name → id
     server-side. This is what the Phase 2c note's "known issue, unverified" restart button was
     actually blocked on — resolved now, not just theorized.
  **Drilled live** against `glp-app` (the public GLP calculator container) via the real broker
  path: `{"success": True}`, independently confirmed via a fresh `fetch()` (state `RUNNING`,
  `"Up 19 seconds"` — genuinely fresh, not stale) and a live `HTTP 200` from the actual public
  site.
- **7d — native docker prune via restricted SSH (2026-08-08).** `docker_prune` (dangling images
  only, `backend/integrations/unraid.py::prune_docker_images`/`_ssh_prune_sync`) now dispatches
  natively over SSH instead of Hermes's relay, via a new vault secret
  (`UNRAID_SSH_PRIVATE_KEY`, an Ed25519 key loaded from an in-memory string — never a file path)
  plus four plain `.env`-overridable settings (`unraid_ssh_host`/`unraid_ssh_user`/
  `unraid_ssh_port`/`unraid_ssh_prune_timeout_s`). The credential is scoped server-side by an
  SSH **forced-command restriction** in Unraid's `authorized_keys`
  (`restrict,from="<nexus-ip>",command="..."` pattern — the key can run exactly one server-side
  command and nothing else, regardless of what this code sends). This code deliberately sends a
  fixed sentinel string (`_PRUNE_SENTINEL = "nexus-docker-prune"`), NOT the real `docker image
  prune -f` command — the forced-command mapping on the Unraid side is what turns that sentinel
  into the real prune. If that server-side restriction is ever weakened or removed, this fails
  LOUDLY ("command not found") instead of silently becoming an unrestricted arbitrary-command
  channel. Host-key verification uses TOFU (trust-on-first-use) via a new
  `.unraid_ssh_known_hosts` file (paramiko-native known_hosts format, gitignored, host-specific —
  same pattern as `.unraid_known_hosts.json`/`.unifi_known_hosts.json`): the first connection
  learns and persists the host key, every later connection is checked against it, and a changed
  key still raises — this is NOT the same as disabling host-key checking
  (`StrictHostKeyChecking=no`), which the code's own comments explicitly warn future readers not
  to "simplify" it into. Wired through the broker (`unraid_docker_prune`, HIGH risk — an agent
  always needs a human tap), the executor write-tool registry, `POST /api/unraid/docker/prune`,
  and Telegram's `/prune` command. Dangling images only, deliberately NOT a full system-wide
  prune — Brian keeps some Unraid containers intentionally stopped, and a system-wide prune would
  delete those containers plus unused volumes/networks along with the images; this scope decision
  is inherited from the original Hermes implementation (`hermes-agent/tools/unraid.py`, a sibling
  repo, not part of this codebase), not invented here. **Shipped but not yet live**: the SSH
  credential itself has not been installed on Unraid — that's a separate, human-gated step,
  tracked outside this codebase. `restart_service`/`service_logs` remain out of scope, but for a
  different reason than before: those are local-to-Hermes'-own-LXC systemd operations (restarting
  a service ON Hermes's box, reading ITS logs), not a remote-Unraid capability at all — they
  become moot once Hermes retires, not something NEXUS needs to gain.

## Open WebUI dependency retired (Phase 6, 2026-07-27)
Open WebUI (LXC 201 `hermes-webui`, `192.168.1.56`) ran a Pipelines container calling Hermes's
`/ask` REST endpoint for browser-based chat — a consumer entirely separate from NEXUS. Brian
confirmed he rarely/never uses it anymore. Verified empirically before touching anything: grepped
Hermes's `hermes-api` journal for all `/ask` hits — real traffic (from `192.168.1.56` and, oddly,
an old pre-Phase-1 NEXUS integration from `192.168.1.119`) stopped **3+ weeks ago** (last real hit
2026-07-02); the only hits since were two isolated `127.0.0.1` 401s (manual smoke-tests, matching
Hermes's own documented `curl .../ask` example), not real usage. Stopped both Docker containers
(`open-webui`, `pipelines`) on LXC 201 via `docker stop` — confirmed functionally down (both ports
refuse connections, no process running), though `docker ps`/`docker inspect` on that container
unreliably still reported them as "Up" due to a **pre-existing, unrelated Docker data corruption**
on that LXC (`dockerd` logs show `local-kv.db`/overlay2 layer files missing under
`/opt/openwebui-data/docker/` — a libnetwork/container-state bookkeeping issue, not a live-service
problem). Not investigated further since this whole container stack is heading toward eventual
retirement anyway. Initially left LXC 201 itself running (only the two Docker containers
stopped) — Brian then asked for the whole container stopped too (`pct stop 201`), since Proxmox's
UI only shows LXC-level status and doesn't reflect what's stopped inside it. LXC 201 is now fully
stopped. No other consumer depends on it. This dependency is now fully cleared; no replacement
was needed. To bring it back if ever needed: `pct start 201` (Docker containers have no
restart-on-boot issue — `docker` itself starts with the container; `open-webui`/`pipelines` would
need `docker start open-webui pipelines` afterward since they were stopped, not just left to
auto-restart).

## NEXUS liveness monitoring (Phase 5, 2026-07-27)
External alerting + OS-level recovery, closing the gap left by every prior phase (nothing watched
NEXUS's own liveness except Hermes's `watcher.py:_check_nexus`, which won't exist once Hermes
retires). Two independent layers:

- **Alerting: self-hosted Uptime Kuma on new Proxmox LXC 206** (`uptime-kuma`, `192.168.1.61`,
  Debian 13, unprivileged, Docker via `nesting=1,keyctl=1` features + a manually-mapped
  `/dev/net/tun` for Tailscale — unprivileged CTs don't get this by default, added via
  `lxc.cgroup2.devices.allow`/`lxc.mount.entry` in `/etc/pve/lxc/206.conf`). Also joined to the
  tailnet (`uptime-kuma.tailfa52c.ts.net`, `100.99.177.105`) — LAN + tailnet only, no Funnel, same
  private-by-default pattern as Proton Bridge (CT 204). Two monitors: `NEXUS backend`
  (`http://192.168.1.119:8000/api/health`, **HTTP(s)-Keyword** type checking `"status":"ok"`, NOT
  plain status-code — the endpoint always returns HTTP 200 even when `vault_missing`, so a
  status-only check would report healthy against a broken vault) and `NEXUS frontend`
  (`http://192.168.1.119:3000`, plain HTTP(s)). Both: 60s interval, 3 retries (~3min to alarm,
  chosen so the tray's own ~30-90s self-heal gets a chance to fix things first — Kuma should only
  page on failures recovery *didn't* already handle). **Notification deliberately reuses NEXUS's
  own `TELEGRAM_BOT_TOKEN`/chat** (not a dedicated bot) — Kuma's Telegram integration calls the Bot
  API directly from inside the Kuma container, so NEXUS's own backend is never in the alert path
  either way; the choice of bot token doesn't reintroduce the single-point-of-failure this phase
  exists to remove, it's purely a "which chat do alerts land in" question, and reusing the existing
  one was simpler.
- **Recovery: Windows Task Scheduler task "NEXUS Tray"**, replacing the old `HKCU...Run` autostart
  entry entirely (backed up to `NEXUS_Tray_autostart_backup.reg` in the repo root first — not
  deleted, just superseded). **Real mechanism-design bug found and fixed during this build, twice:**
  1. `start.ps1` isn't a persistent process (it launches things and exits), so Task Scheduler can't
     wrap it directly — the task action targets `tray.py` (via a wrapper, see below), which is the
     actual long-lived process.
  2. The venv's `pythonw.exe` is a launcher STUB for windowless apps — it starts the real
     interpreter as a detached child and exits in under a second without waiting on it, so Task
     Scheduler tracking `pythonw.exe` directly loses the real process's lifetime almost
     immediately. Fixed with `tray_supervisor.ps1`, a wrapper that Task Scheduler actually tracks —
     it launches tray.py and polls (CIM, matched on command line) until that specific process is
     gone.
  3. **The bigger bug, confirmed via the TaskScheduler operational event log and corroborated by
     Microsoft's own forum guidance: `RestartOnFailure` does NOT fire on a non-zero exit code from
     a successfully-launched action — only if the scheduler fails to START the action at all.**
     Event 201 logged "successfully completed" despite the wrapper's deliberate `exit 1`, and no
     retry was ever queued, with `RestartCount=3`/`RestartInterval=PT1M` configured. This makes the
     entire "exit code → Task Scheduler restarts it" design a dead end regardless of wrapper
     script — confirmed, not guessed, before changing approach (per the standing "loop 3x → ask
     Opus" rule). **Fix: `tray_supervisor.ps1` no longer exits and relies on Task Scheduler's
     restart feature at all — it self-loops forever** (launch tray.py → poll until it's gone →
     relaunch, with a fast-fail backoff: 5 consecutive sub-60s crashes trigger a 10-minute pause
     rather than spinning hot). Task Scheduler's job is reduced to starting this once at logon,
     plus a **15-minute indefinite repetition on the logon trigger** as a backstop in case the
     supervisor process itself dies (a no-op most of the time, since `MultipleInstances=IgnoreNew`
     + the wrapper's own "attach if already running" check make a redundant fire harmless).
  4. Also fixed while here: `tray.py`'s `_kill_other_tray_instances()` runs before
     `acquire_single_instance()`, so a naive relaunch that doesn't check for an already-running
     tray first would kill a healthy one and steal its place — the wrapper checks for a live
     `tray.py` process before ever launching a new one.
- **Drilled live, twice, both passed**: (1) killed just the real tray process — supervisor detected
  it in ~31s and relaunched cleanly (`logs/tray_supervisor.log` shows the exact detect→relaunch
  sequence), new tray correctly saw NEXUS's backend/frontend still healthy and did NOT
  unnecessarily restart them. (2) Full failure — `stop.ps1` plus killing the supervisor and tray
  entirely, backend/frontend down for ~4 minutes (confirmed via repeated `/api/health` connection
  refusals) — manually fired the task to simulate the 15-min backstop, full chain recovered within
  ~15s (supervisor → tray → `start.ps1` → healthy). Confirm with Brian whether the real Telegram
  DOWN/UP alerts landed during this drill — that part can't be verified from this session alone.
- **Residual gap CLOSED (2026-08-12)**: a full reboot with nobody logged in used to leave NEXUS
  down until someone logged in (the tray needs an interactive desktop session — it's a systray
  icon, Windows services can't own one, Session 0 is isolated from the desktop; the 15-min
  repetition backstop only helps if a session is already active). Root-caused live the same day:
  Windows Update force-rebooted the unattended machine twice around 17:30 (`MoUsoCoreWorker.exe`/
  `TrustedInstaller.exe`, run by `NT AUTHORITY\SYSTEM`), which logged out the interactive session
  and killed NEXUS — the logon-triggered "NEXUS Tray" task never fired since nobody was logged in,
  and NEXUS stayed down ~2h until Brian remoted in and logged in manually (`backend/logs/backend.err.log`
  showed a fresh process start at 19:27:45, matching the login). Fable-planned, closed same session
  via two layers:
  1. **Windows auto-logon** via Sysinternals `Autologon64.exe` (Microsoft-signed, verified before
     running) — Brian entered credentials directly into the tool's own GUI, never through Claude
     Code. Stores the password as an LSA secret (`HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon`:
     `AutoAdminLogon=1`, `DefaultUserName=Brian` — no `DefaultPassword` value, confirming it did NOT
     land in the plaintext registry field netplwiz would have used). Accepted tradeoff (Brian's
     explicit call): the credential is recoverable by a local admin or anyone with offline disk
     access to this machine — obscured, not strongly encrypted at rest. No BitLocker on this host,
     so no pre-boot PIN interaction to worry about. This makes the EXISTING "NEXUS Tray" logon
     trigger fire on every reboot, not just ones a human is present for — the task itself was
     verified byte-identical (`Export-ScheduledTask`) before and after, untouched by this change.
  2. **Windows Update harm reduction** — `HKLM:\SOFTWARE\Microsoft\WindowsUpdate\UX\Settings`:
     `SmartActiveHoursState=0`, `ActiveHoursStart=7`, `ActiveHoursEnd=1` (fixed 07:00–01:00 window,
     the 18h max); `HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU`:
     `NoAutoRebootWithLoggedOnUsers=1` — now meaningful since auto-logon guarantees a user is always
     "logged on," so update-driven reboots wait for Brian to initiate one rather than forcing it.
     Known caveat: this combination can defer a pending-reboot update indefinitely if never
     manually triggered — accepted, since a nagged reboot is now low-cost (NEXUS self-recovers in
     minutes either way).
  - **Password-change runbook**: if Brian's Windows password ever changes, auto-logon silently
    breaks — symptom is the machine sitting at the login screen after the next unattended reboot,
    caught within ~3 min by the Uptime Kuma NEXUS-down alert (fails loud, not silent). Fix: re-run
    `Autologon64.exe` (`https://live.sysinternals.com/Autologon64.exe`), re-enter the new password
    in its GUI, click Enable, then re-verify with a reboot drill.
  - **End-to-end no-touch reboot drill: not yet run** (Brian deferred it to a separate session,
    since it kills whatever session triggers it, including a live Claude Code session). Until that
    drill runs, this fix is verified at the configuration level (registry values correct, LSA
    secret confirmed, task untouched) but not yet empirically proven against a real unattended
    reboot. Run it via `shutdown /r /t 20` with nobody touching the machine afterward, then confirm
    `http://192.168.1.119:8000/api/health` returns healthy within 5 minutes from a second device,
    and `logs/tray_supervisor.log` + `backend/logs/backend.err.log` show fresh starts in that
    window.
  - A second scheduled task to auto-lock the session shortly after an unattended auto-logon
    (closing the "unlocked desktop at the console" exposure) was proposed and **declined** by
    Brian — not built.
- **Also confirmed while building this**: Proxmox host access still works via the existing
  `processforge_proxmox_ed25519` keypair (no new credential handoff needed for the LXC itself).

- **Council-loop post-mortem trigger** (2026-07-27): `Council-loop/run-loop.ps1` now POSTs `{"task_name": "council_postmortem", "parameters": {"since": <run start>}}` to `/api/trigger` at driver exit (best-effort, own `try/catch`, separate from the Brain-event block) — the NEXUS-side `council_postmortem.py` has existed since earlier this session but had no live caller until this hookup. Auth is `$env:NEXUS_API_KEY` (a one-time manual `[Environment]::SetEnvironmentVariable`, not stored in any Council-loop file); absent, it logs a skip line and does nothing else. See that repo's own `CLAUDE.md` for the full design.
- **Brain Organizer moved off the old Hermes relay too** (2026-07-27): `modules/brain-organizer/brain_organizer.py` is a standalone script/venv run as a subprocess by `scheduler.py::_run_brain_organizer` — it never called `backend/events.py`'s `notify_phone`, so Phase 1's Telegram migration missed it entirely (found live: its "Run complete" summary kept arriving on the OLD Hermes-relayed bot). `send_hermes_notification` (POSTed to `{HERMES_HOST}/hermes/notify`) is now `send_telegram_notification` (POSTs straight to `https://api.telegram.org/bot<TOKEN>/sendMessage`). `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` are injected into the subprocess's env by `_run_brain_organizer` from the NEXUS vault (same pattern as `ANTHROPIC_API_KEY`/`OPENROUTER_API_KEY` — replaces the old `HERMES_HOST` injection), so the bot token never needs a second on-disk copy in the module's own `.env`. The module's test suite has its own `_no_real_secrets_in_tests` autouse fixture (`tests/conftest.py`) that strips these env vars for every test — this is the SAME guard that already exists for the retired Hermes secrets, put there after a real incident where a leaked `HERMES_HOST`/`HERMES_WEBHOOK_SECRET` fired real Telegram spam from a failure-path test; treat `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` with the identical suspicion in any future test work on this module.

## Image generation (Phase 8, 2026-07-27)
`/image <prompt>` ported from Hermes: `backend/integrations/image_gen.py::generate_image(prompt)` is an
unauthenticated GET to Pollinations.ai (no secret, no cache — modeled on `sports.py`), returning raw
image bytes or `None` on any failure. New `telegram.send_photo()` is the one Bot API call that needs
multipart upload instead of `_call()`'s JSON POST. `_cmd_image` (`telegram_commands.py`) sends the photo
directly and returns `None` — reuses `dispatch()`'s existing "handler already replied" convention rather
than changing the `Handler` type/`dispatch()` body for one command.

## Outcome Tracker (2026-08-01)
NEXUS's own feature proposal (`docs/outcome-tracker-spec.md`, Opus-planned, Council-loop-built across
7 rollout steps + 2 gap-closure cycles) closes the loop on three previously-silent signal classes —
homelab edge alerts, watchdog pages, and deterministic briefing observations — none of which flowed
through the broker's `ActionLog`/confirm-reject audit trail the way broker-dispatched writes already do.

- **Data model:** new `OutcomeFlag` table (`backend/database.py`, created by `create_all`, no migration
  shim) — `source`/`check`/`fingerprint` (`f"{source}:{check}"`, the dedup key), `summary`/`detail`,
  `severity` (reuses `ActionLog.risk`/`Goal.risk`'s `low|medium|high` vocabulary), `status`
  (`open|resolved|deferred|false_positive|needs_follow_up`), resolution/defer/surfaced-count
  bookkeeping, and an optional `action_log_id` FK for the rare flag that accompanied a broker action.
  Deliberately NOT an `ActionLog` extension (a briefing observation has no actor/target/decision to
  attach to) and NOT a `SystemState` CSV field (too many mutable per-item fields, unlike
  `policy_auto_allow_kinds`/`muted_notify_kinds`). `_ensure_outcomeflag_index()` adds
  `ux_outcomeflag_open`, a partial unique index (`WHERE status='open' AND fingerprint != ''`) mirroring
  `ux_goal_fingerprint_active` — the hard backstop against `record_flag()`'s check-then-insert TOCTOU.
- **Write path (`backend/agents/outcomes.py`):** `record_flag(source, check, summary, ...)` never raises
  (same contract as `notify_phone`) and applies suppression rules in order: (1) an existing
  `open`/`needs_follow_up` row with the same fingerprint just bumps `surfaced_count`/`last_surfaced_at`,
  no new row — "stop re-surfacing things already raised"; (2) an existing `deferred` row with a future
  `deferred_until` returns `None` — "deferred means shut up until then"; (3) an existing `false_positive`
  row within `outcome_flag_false_positive_cooldown_days` (default 30) returns `None` — "stop crying wolf
  on the same pattern"; (4) otherwise inserts (catching the partial-unique-index race the same way
  `goals.propose()` catches its own). `resolve_flag`/`clear_flag`/`open_flags`/`recently_closed`/
  `calibration_summary`/`sweep_deferred` round out the public API. No `backend.safety.broker` import, no
  `Session`/ORM object crosses an `await`, zero LLM calls anywhere in the module.
- **Five config flags (`backend/config.py`):** `outcome_flags_enabled` (default True — the global
  rollback lever; `False` makes `record_flag` a no-op and every read path degrade to `(none)`/`[]`),
  `outcome_flag_sweep_enabled`, `outcome_flag_false_positive_cooldown_days` (30),
  `outcome_flag_retention_days` (180), `outcome_flag_briefing_max` (10).
- **REST (`backend/api/safety.py`, Bearer-gated via `Depends(require_api_key)` like every other route in
  the file):** `GET /api/safety/flags/calibration` (declared FIRST so path matching can't shadow it
  behind the parameterized `/flags/{flag_id}/resolve` route), `GET /api/safety/flags`
  (`?status=&source=&limit=`), `POST /api/safety/flags` (manual create, `source="manual"` — this is the
  Claude-Code-facing path), `POST /api/safety/flags/{flag_id}/resolve`.
- **Telegram:** new `flag` namespace in `telegram_poller.py`'s `_INT_ID_NAMESPACES` (int-coerced id, same
  as `goal`/`safety`) — `callback_data` `flag:<resolved|false_positive|deferred>:<id>`, a two-button
  keyboard (`✓ Resolved`/`✗ False alarm`) matching the `goal:approve/reject` and `safety:confirm/reject`
  precedent rather than a busier four-button layout. `telegram_commands.py` adds `/flags` (list open,
  same shape as `/goals`/`/tasks`), `/resolve <id> [status] [note]` (default status `resolved`),
  `/defer <id> <days> [note]`, `/flag <text>` (manual log into the same store, `source="manual"`).
- **6th watchdog check:** `watchdog.run_watchdog()`'s 5-minute job gained `check_deferred_flags()` →
  `outcomes.sweep_deferred()`, flipping past-`deferred_until` rows to `needs_follow_up` and paging once
  per flag; the job's return dict gained a `deferred_swept` key. Gated by both `watchdog_enabled` and its
  own `outcome_flag_sweep_enabled` sub-flag — the same "don't couple a durability guarantee to
  `watchdog_enabled`" concern the `SecretFallback` drain called out, but accepted here since a missed
  sweep is a late reminder, not lost data (the row persists either way).
- **Retention:** `backend/agents/backup.py::prune_old_outcome_flags()` deletes closed rows older than
  `outcome_flag_retention_days`, and NEVER deletes an `open`/`needs_follow_up` row regardless of age —
  wired into the nightly 03:45 `retention_prune` job alongside `prune_old_uptime_samples`/
  `prune_old_traces`.
- **Read paths:** `briefing.py` injects `open_flags()`/`recently_closed(hours=48)` into the prompt as
  KNOWN OPEN ITEMS / RECENTLY CLOSED blocks (pre-LLM, capped at `outcome_flag_briefing_max`, degrading to
  `(none)` on any fetch exception) AND appends a deterministic post-LLM `## Open Items` section — added
  to `_UNVERIFIED_FACT_SECTIONS` so `extract_and_store` never turns a flag summary into a durable, LLM-
  proposer-visible `Fact`. `chat.py`'s CHAT branch threads `open_flags(limit=10)` into
  `memory.assemble()`'s new optional `flags_str` param as an `[OPEN ITEMS]` block (existing 3-arg callers
  unaffected). Write-path call sites: `homelab_watch.py` (6 checks routed through `_edge_alert`'s
  existing `key` as the fingerprint, `_active_alerts`' in-memory latch deliberately left untouched),
  `watchdog.py` (4 of its checks flag; `check_budget_warning` deliberately does NOT — it's already
  self-clearing on a calendar boundary), and `briefing.py::_record_briefing_flags` (structured `context`
  fields only — `ha_unavailable_entities`/`unraid_array`/`unraid_parity`/`github_stale_prs`/
  `unifi_new_devices`/`adguard_filtering_off` — never the `## Priority Actions` LLM prose).
- **§5 scope boundaries, held to exactly:** no LLM classification of outcomes (a human tap or an
  observable condition-clear only), no auto-suppression beyond the fixed 30-day false-positive cooldown,
  no parsing of the briefing's `## Priority Actions` LLM prose, **no agent-facing write tool** —
  `tools.py` gained a read-only `open_flags` `ReadTool` only; a `resolve_flag` write tool would let
  NEXUS close its own loop and destroy the signal the feature exists to capture, so it stays explicitly
  out of v1 (noted in `outcomes.py`'s own module docstring, not just this file), no `Flags.jsx`/frontend
  page (REST + Telegram + briefing/chat cover v1), no Obsidian/Vault write path (SQLite is the query-able
  store the Vault isn't), the existing Infisical soak-reminder job is untouched, and
  `ActionLog` gets zero new columns/decision values.

## Hermes fully decommissioned (2026-08-09)
Hermes (the separate homelab bot on Proxmox LXC 200 `hermes-agent` + LXC 201 `hermes-webui`) is
gone entirely — `pct destroy --purge`'d, all `HERMES_*`/`cred:hermes:*` Infisical secrets deleted
(the local `nexus.vault` Fernet fallback's leftover `HERMES_*`/`cred:hermes:*` entries — plus
`LXC201_SSH_PASSWORD`/`cred:lxc201:*`, dead for the same reason, LXC 201 was `hermes-webui` — were
cleaned up 2026-08-14, commit `20f1f05`; see that commit message for the full list and for a
`nexus.vault.meta`/`nexus.vault` drift bug found during the cleanup), the SSH key access removed.
This was the last step of a roadmap
that ran across several sessions (2026-07-21 through 2026-08-09): every real Hermes capability was
ported natively first — see the still-live "Telegram bot + calendar", "Write-actions brought
in-house (Phase 7)", "Open WebUI dependency retired (Phase 6)", "NEXUS liveness monitoring
(Phase 5)", and "Image generation (Phase 8)" sections above/below for that history, which stays
accurate as a record of *why* the current native code looks the way it does.

**Same session, all Hermes-tied code removed from this repo too** (not just the infra): the
action-relay bridge (`backend/integrations/hermes.py`, `backend/safety/hermes_actions.py` —
both deleted outright), the broker's `hermes_relay`/`hermes_action` kinds and dispatchers, the
`hermes_status`/`hermes_command` agent tools (the two `proxmox_updates`/`proxmox_backups` read
tools that used to relay through Hermes now call `backend/integrations/proxmox.py` directly),
`chat.py`'s HERMES intent branch, the `/api/safety/hermes-actions` routes, `hermes` as a monitored
source (`api/sources.py`/`state_workers.py`/`scheduler.py::_record_uptime`/`contracts.py`), the
`hermes_soak_reminder` scheduler job (had already fired, no longer needed), `hermes_host`/
`hermes_webhook_secret` config + the `/api/trigger` HMAC-signing layer built for Hermes to use
(zero callers had ever signed a request — deleted rather than kept as an unused capability;
`/api/trigger` itself stays, Bearer-only, live caller is Council-loop), and `Briefing`'s
`delivered_to_hermes` column (renamed to `delivered` via an idempotent migration shim,
`_ensure_briefing_columns()` in `database.py` — a pure rename, the column was write-only). Full
pytest suite green after every step. `ActionLog` rows with `kind='hermes_relay'`/`'hermes_action'`
from before this change stay in the DB untouched — immutable audit trail by convention, rendered
fine as free text by `GET /api/safety/actions`. Historical `UptimeSample` rows with
`source='hermes'` are left to age out via the normal nightly prune, same as any other integration
that stops being polled.

## Legibility batch — proposer drop visibility + homelab recovery notices (2026-08-09)
Fable-planned, Sonnet-built (branch `feat/proposer-recovery-notices`, worktree `nexus-b9-b10`) —
two of the "make NEXUS easier to understand" items from a legibility review: the goal proposer's
silent drops, and homelab alerts that never say when they've cleared.

- **Proposer drop visibility (B9).** `propose_goals_tick` (`backend/agents/proposer.py`) already
  computed candidates it filtered out (no `success_criteria`, night-exempt lights, known
  hardware-issue lights) but only logged them at debug — Brian had no way to see "the proposer ran
  and considered N things" versus "the proposer did nothing." Each drop site now appends
  `{"title", "reason"}` to a `filtered` list, returned as `count_filtered`/`filtered` on the tick's
  result dict. A new `SystemState.proposer_tick_stats_json` column (idempotent ALTER shim, same
  `_ensure_system_state_columns()` pattern as every other `*_json` singleton-row field) holds a
  rolling window of the last 16 ticks (`governor.record_proposer_tick_stats`, called best-effort
  from the tick's success path only — never on the `skipped`/`budget`/`error` early returns, since
  nothing was evaluated there). `governor.get_proposer_tick_stats(hours=24)` aggregates that window
  (ticks/proposed/auto_approved/filtered_total/filtered_by_reason/filtered_items, capped at 5 most
  recent), returning `None` on no data so the digest can render a clean "no ticks" line instead of
  a zeroed-out block. `digest.build_autonomy_digest` gained a `Proposer (24h)` line between the
  `proposed:` block and `Spend today:`, showing proposed/auto-approved/filtered counts plus up to 5
  recent filtered titles — titles are LLM-generated free text and are `html.escape()`d, since
  `notify_phone` sends `parse_mode="HTML"` (the digest's OTHER goal-title interpolations are NOT
  escaped, a separate pre-existing gap this line doesn't rely on or inherit). **auto_approved is
  rendered as its own count, not folded into `proposed`**: `proposer.py` reassigns an
  auto-approved goal's status away from `"proposed"`, so a tick that auto-approved everything has
  `proposed=0` — an Opus verify pass caught that without a separate auto-approved figure, a fully
  autonomous tick would misread as "the proposer did nothing" (regression-tested:
  `test_build_autonomy_digest_proposer_stats_all_auto_approved`). The same pass also caught a
  reproducible ~1-in-6 flaky digest test — `tests/test_autonomy_notify.py`'s shared `eng` fixture
  uses `StaticPool` (one shared SQLite connection across threads), and this build's 9th concurrent
  `asyncio.to_thread` DB read was enough to occasionally interleave two Sessions' transaction state
  on that one connection, making `get_proposer_tick_stats` spuriously read an existing row as
  absent. An initial fix serializing that connection's pool checkout/checkin behind an `RLock`
  deadlocked outright under real test load and was reverted; the actual fix backs the test fixture
  with a real tmp-file SQLite engine instead of a single shared in-memory `StaticPool` connection
  (`make_engine(db_path)`, same pattern `tests/test_db_pragma.py`'s `file_db` fixture already
  used) — each thread gets its own pooled connection to the same file, matching how production's
  engine actually behaves, so there's nothing left to race on. Verified clean across 8+ consecutive
  full runs of `test_autonomy_notify.py` post-fix. `build_autonomy_digest`'s `gather(...,
  return_exceptions=True)` fan-out
  now also `logger.warning`s any task that came back as an `Exception`, so a degraded digest field
  leaves a trace instead of silently reading identically to "no data yet".
- **Homelab recovery notices (B10).** `homelab_watch.py`'s edge-alert checks (VM/LXC stopped,
  Docker container stopped, Unraid array unhealthy, disk temp, garage open, backup failed) already
  auto-cleared their `OutcomeFlag` on recovery but never told Brian the thing came back — a stopped
  VM paged once, then silence forever whether it was fixed in 30 seconds or never. New opt-in flag
  `homelab_recovery_notify_enabled` (default `False` — this is a NEW notification class, off by
  default so nobody gets an unasked-for second page per incident) gates a new `homelab_recovered`
  notify kind (registered in `events.NOTIFY_KINDS`, individually `/mute`-able like every other
  `homelab_*` kind, deliberately not on `_NEVER_MUTABLE_NOTIFY_KINDS`). A module-level `_paged_alerts`
  set tracks which alert keys actually fired `notify_phone` (not ones suppressed by calibration/
  dedup) — `_maybe_notify_recovery(key, message)` only sends when `key` is in that set, so a
  suppressed alert's eventual recovery stays silent too (an unprompted "all clear" for something
  that was never announced as broken would be confusing, not helpful), and always clears the
  tracking entry regardless of the flag so a disabled flag can't leak entries forever. Wired into
  the shared `_edge_alert` helper (covers array/disk-temp/garage/backup) and individually into
  `check_proxmox_vms`/`check_docker` (their own transition-triggered logic, not `_edge_alert`-based).
  Recovery text is a distinct, less alarming message ("... is running again" / "... is back to
  normal"), not a repeat of the original alert. `_paged_alerts` is in-memory, same restart-loses-it
  tradeoff already documented for the rest of this module's state.
- Full pytest suite green throughout (backend-only change, no frontend build needed).

## Legibility batches 1+2 — making NEXUS's own behavior easier to read (2026-08-09)
Two Fable-planned, Sonnet-built batches from the same legibility review that also produced the B9/B10
proposer/homelab-recovery batch above — batch 1 shipped first but never got a CLAUDE.md entry at the
time (found as drift while planning batch 2); documented together here since they're one continuous
theme: NEXUS explaining more of what it's actually doing, with no behavior change to what it does.

**Batch 1 (branch `feat/legibility-batch1`, worktree `nexus-legibility-batch1`, merged before B9/B10):**
- **B1 — expandable trace spans.** `frontend/src/pages/Traces.jsx` span rows with an `input_summary`/
  `output_summary` become clickable, expanding a monospace pre-wrap block inline — previously that data
  existed in the DB (`TraceSpan.input_summary`/`output_summary`) but the page never rendered it.
- **B2 — span names show the actual model.** `router.py::_record_trace_span` gained an optional `model`
  kwarg; `_create_sync`/`_create_sync_raw` build `span_name = f"{label} ({model})"` and pass it through,
  and cost computation now keys off `model or name` — a span used to just say "chat_classify", now says
  "chat_classify (claude-haiku-4-5)".
- **B4 — Telegram `/tasks`/`/goals` show WHY something failed.** `telegram_commands.py::_cmd_tasks`
  imports `worker_pool._summarize_task_result` and appends a failure-reason line only for
  `status == "failed"`; `_cmd_goals` appends `rejection_reason`/`outcome_summary` via `.get()`.
- **B8 — completed goals show their outcome in the Safety UI.** `goals.py::_goal_to_dict` gained
  `"outcome_summary"`; `Safety.jsx` renders it under a completed goal in the page's existing success
  color (`#5fe0b4`, verified as the real pre-existing token, not invented).
- Opus verify caught two cosmetic-only findings, both fixed before merge: a dead `stopPropagation()`
  call in `Traces.jsx` and a truncation-length mismatch (200 vs 300 chars) between the two Telegram
  commands.

**Batch 2 (branch `feat/legibility-batch2`, worktree `nexus-legibility-batch2`):**
- **B3 — Telegram goal messages show what's actually being approved.** `proposer.py`'s
  `goal_proposed`/`auto_approved` notify messages used to be just a title (+ risk, for the approval
  one) — now both include description, "Done when:" (success_criteria), and confidence (approval
  message only), each truncated to 200 chars THEN escaped via a new `_esc()` helper — truncating
  after escaping can cut an HTML entity in half, which Telegram's `parse_mode="HTML"` parser rejects
  as a 400, silently dropping the whole message (this was a real latent bug: a title containing a
  literal `&`/`<` already silently killed the notification before this batch, since `events.notify_phone`
  switches to HTML parse mode whenever `app_base_url` is set — fixed as a side effect of adding the
  escaping this batch needed anyway).
- **B5 — chat routing decisions are now traced, not just logged.** `chat.py`'s intent-classify log line
  gained the Haiku-supplied `reason` field (previously discarded); a new `chat_route_decision`
  `TraceSpan` (`span_type="routing"`) records `intent`+`reason` for every turn, and a second
  `ha_entity_pick` span records which HA entity/service a HOME_CONTROL turn resolved to (or "no entity
  matched") — the parsed decision the code actually acts on, not just the raw LLM prompt/response the
  existing `chat_lane_pick` `llm_call` span already stored. Both spans are pure observability, written
  in their own `try/except` + `asyncio.to_thread(_record_trace_span, ...)`, placed strictly AFTER the
  intent-parse block completes (never inside it — that block coerces any exception to `intent="CHAT"`,
  so a bug in span code placed there could silently reroute a real HOME_CONTROL command). `router.py`'s
  `close_trace` gained an optional keyword-only `label` param (backward compatible, every existing
  positional caller unaffected) so the trace's label can fold in the resolved intent at close time
  (`"conv:42 intent=CHAT"`) — the intent isn't known yet when `open_trace` runs at turn start.
- **B6 — the daily digest shows failures, not just successes/suppressions.** New `Failed (24h)` block
  in `digest.py::build_autonomy_digest` (positioned right after `Completed (24h)`), sourced from a new
  `_db_recent_failed_goals` helper mirroring the existing completed-goals one; each entry shows
  `rejection_reason` (or "no failure reason recorded"). The pre-existing `Auto-suppressed: N rule(s)`
  calibration suffix now NAMES the fingerprints (up to 3, `"(+N more)"` beyond that) with their false-
  positive rate — previously just a bare count with no way to tell which rules without opening the
  Safety page. Both additions are `html.escape()`d (free-text rejection reasons, `parse_mode="HTML"`
  delivery) — the digest's OTHER, pre-existing goal-title interpolations are still NOT escaped, a
  separate known gap this batch doesn't touch or rely on.
- **B7 — the orchestrator's retry/replan decisions are traced.** A new `debug_decision` `TraceSpan`
  records each `_opus_debug` verdict (RETRY_STEP/REPLAN/ABORT + reason) on the durable task loop, plus
  a second span for the specific case where a REPLAN comes back with zero new steps and the code
  overrides it to ABORT (`"ABORT: replan_empty"`) — makes the EFFECTIVE terminal decision legible, not
  just Opus's raw one. Durable path only (`run_task(task_id=...)`); the legacy in-memory loop
  (`test_orchestrator.py`) is untouched. Deliberately no `try/except` around the `_opus_debug` call
  itself — its `json.loads` parse failure must keep propagating to the existing generic exception
  handler exactly as before this batch; only the span WRITE (after the call returns) is best-effort.
- All four items are pure additive observability — no dispatch/routing/retry logic changed, confirmed
  by dedicated "span write fails" constraint tests (`test_span_failure_never_breaks_chat`,
  `test_debug_span_failure_does_not_break_retry`) asserting the real behavior is byte-identical whether
  or not the new span code succeeds.
- Full pytest suite green (backend-only change, no frontend build needed for batch 2's items — B1/B8
  above were the frontend-touching half, already built/merged in batch 1).

## Pulse — real-time agent/worker activity page (2026-08-09)
Fable-planned, Sonnet-built (branch `feat/pulse-activity`, worktree `nexus-pulse`) — the last of the
legibility-review recommendations: a live "what is NEXUS doing right now" view. Fable explicitly
argued against a node graph (the call graph is shallow/static — nothing to watch change) and a live
timeline/Gantt (that's what Traces.jsx already is, post-hoc; real-time the axis is 99% empty since
most of NEXUS's ~29 scheduled jobs run for milliseconds every few minutes) in favor of a **status
board + coalesced event ticker**, matching how the system actually behaves: mostly idle, with an
honest "last ran 90s ago, 840ms, OK" answer that also makes a stalled job visible at a glance — the
one thing no log stream shows.

- **`backend/activity.py`** — a new in-memory, thread-safe, process-local registry. Zero DB writes,
  zero LLM calls, zero new tables — everything durable already exists (`AgentTrace`/`TraceSpan`/
  `TaskStep`/`SpendLog`); this is purely a live snapshot, lost on restart, same accepted tradeoff as
  `homelab_watch.py`'s in-memory state. `ActivityEntry` dataclass (`actor_id`, `actor_type` —
  `job|worker|loop|trace|task` — `label`, `status` — `running|idle|ok|error` — `started_at`,
  `last_run_at`, `last_duration_ms`, `last_error`, `detail`, `seq`) plus a 200-row `deque` ticker
  ring. Three mutators — `begin`/`end`/`pulse` — plus `update_detail`/`remove`/`sweep_stale` (a
  24h-idle backstop, piggybacked every ~60 broadcaster ticks, not a liveness mechanism), all
  lock-guarded, sync, and **never raise** — same best-effort contract as `events.publish`. This
  matters concretely: `router._record_trace_span` runs inside a `run_in_executor` worker thread (the
  same reason `SpendLog`/`TraceSpan` writes are sync-not-`to_thread` there), so every mutator had to
  be safe to call from a non-loop thread with no `await` anywhere in the module.
- **Coalescing broadcaster** (`activity.run_activity_broadcaster()`, started/cancelled in `main.py`'s
  lifespan next to the worker pool): wakes every 250ms, broadcasts ONE delta message only if
  something changed AND at least one client is connected — an unwatched Pulse page costs a 250ms
  timer wake and nothing else. Caps outbound traffic at ≤4 msg/s regardless of internal event rate,
  and is what lets a `run_in_executor`-thread mutation reach a browser without any
  `call_soon_threadsafe` plumbing (the poll picks up thread-side writes on its own next tick).
- **Third `WebSocketManager`** (`activity_ws_manager` in `backend/api/agents.py`, alongside
  `ws_manager`/`state_ws_manager` — same "must not share a connection list" reasoning as
  `/ws/state`'s own module docstring) backing a new `/ws/agent-activity` route (`main.py`, cloned
  auth handshake from `/ws/state`) that sends a full `activity.snapshot` immediately on connect, then
  `activity.delta` messages from the broadcaster. `GET /api/activity` (Bearer-gated) is the REST
  fallback for the page's first paint and a poll-fallback path — reads memory only, no DB.
  Deliberately NOT reusing `/ws/logs`: that feed is nearly dead today (its only two publishers are
  the broker's terminal-action broadcast and the autonomy on/off toggle, and at the time its one
  consumer, `AgentLog.jsx` — since removed in the Agents-page-removal batch further down, leaving
  `TaskCard.jsx` as the sole consumer — appended every raw message unfiltered) — Pulse's ~4/s
  coalesced deltas would have spammed it exactly the way `state_ws_manager` already exists to avoid
  for `/ws/state`.
- **Backend wiring — 6 choke points instead of ~28 per-module edits.** Because NEXUS already funnels
  almost everything through a handful of shared functions, only 2 agent files needed direct
  touching beyond the choke points themselves:
  1. **`backend/scheduler.py`** — one `APScheduler` event listener
     (`_register_activity_listener()`, called from `setup_scheduler()`, guarded by a module flag
     since `setup_scheduler()` runs once per test file against the SAME module-level `scheduler`
     singleton — without the guard a full pytest run would stack up dozens of duplicate listeners)
     covers **every** registered job — present and future, zero per-job code — via
     `EVENT_JOB_SUBMITTED/EXECUTED/ERROR/MISSED`. A `_TICKER_QUIET_JOBS` frozenset (the four
     `state_refresh_*s` jobs, `retry_deliveries`, `secret_fallback_drain`) keeps high-frequency
     housekeeping off the ticker while their board status still updates — otherwise a 30s job would
     dominate the 200-row ring.
  2. **`router.open_trace`/`close_trace`** and **`orchestrator._open_trace`/`_close_trace`** —
     `begin()`/`end()` a `trace:{kind}` entry (one of five: chat/briefing/orchestrator/proposer/
     voice) around every traced entry point. The orchestrator's durable path additionally
     `begin()`s/`remove()`s a per-task `task:{id}` entry (removed on finalize at the `run_task()`
     call site, not inside `_close_trace`, since only that call site still has `task_id` in scope).
  3. **`router._record_trace_span`** — a `pulse()` call was moved to fire **unconditionally**, even
     when `trace_id is None` (the common case for calls outside a traced entry point) — this is how
     `mail_drafts`/`facts`/`wiki_ingest`/`telegram_commands` LLM activity shows up in the ticker with
     zero changes to those modules, since every LLM call already carries a mandatory `label=`
     (`test_spend_report.py::test_no_unlabeled_llm_calls_in_agents`). Only the DB `TraceSpan` write
     stays gated on `trace_id`.
  4. **`worker_pool._worker_loop`** — `begin()`/`end()` a `worker:{id}` entry around task pickup
     (2 workers by default); **`orchestrator`'s durable step loop** calls `update_detail()` on the
     step-running transition with `{step_index, total_steps, description}` for the Now-Running strip's
     progress bar.
  5. **`telegram_poller.poll_once`** pulses once per non-empty `getUpdates` batch (never per empty
     poll — that's a heartbeat, not activity); **`memo_watcher._MemoHandler.dispatch`** pulses on each
     detected memo file (runs on the watchdog `Observer`'s own OS thread, not the loop — safe, since
     `activity.pulse` is a plain thread-safe sync call).
  6. **`broker._publish_action`** (the existing terminal-outcome broadcast helper) pulses
     `kind decision · target` alongside its pre-existing `events.publish` call.
- **`frontend/src/pages/Pulse.jsx`** (new page, NAV entry under SYSTEMS, `Radar` icon — `Activity` was
  already taken by Uptime): three stacked zones —
  1. **Now Running** — one row per `running` task/trace/worker entry: pulsing dot, label, a
     client-ticked elapsed timer, and for `task:*` entries a step progress bar off `detail`.
  2. **Actors** — a responsive grid grouped by type (Workers / Loops / Scheduled jobs / Tasks,
     `trace:*` entries deliberately have no dedicated board card — they're fully covered by the Now
     Running strip already): "last ran 2m ago · 840ms · OK", red + error snippet on the last failure.
  3. **Live Ticker** — the event ring, newest first, pause-on-hover (buffers incoming deltas in a
     ref while the mouse is over it, flushes on mouse-leave — the board above keeps updating live
     regardless, only the ticker list itself pins in place).
  A third `wsActivityUrl()`/`wsActivityProtocols()` pair in `api.js` (same authenticated-subprotocol
  pattern as `wsStateUrl`/`wsStateProtocols` — **not** the older keyless `wsLogsUrl`/`ws.js` pattern,
  which Fable's original spec flagged as having a live pre-existing auth bug of its own, left
  untouched here as a separate follow-up). Header chip reuses `GET /api/safety/status` for an
  "autonomy ON/PAUSED · $X today" readout — zero new backend for that one line.
- **Cost/perf discipline**: no `Session`/DB usage anywhere in `backend/activity.py` (grepped and
  confirmed — the point of the whole design), no new LLM calls, mutators are microsecond dict/deque
  ops under a lock, the broadcaster is pure-async and skips all work when nobody's connected, every
  string field is truncated (200-char labels/errors/summaries, 80-char event names — same discipline
  as `open_trace`'s existing `label[:200]`), and the ring is hard-capped at 200 by construction
  (`deque(maxlen=200)`).
- **Test coverage**: `test_activity_registry.py` (registry unit tests incl. a `ThreadPoolExecutor`
  concurrency test mutating from 6 threads while repeatedly snapshotting, and a "poisoned dict"
  injection test pinning that every mutator degrades silently rather than raising),
  `test_activity_ws.py` (snapshot-on-connect shape, auth reject/accept parity with `/ws/logs`/
  `/ws/state`, REST fallback), `test_activity_wiring.py` (the scheduler listener's idempotent
  registration guard + all four event codes + the quiet-jobs ticker exclusion, the worker loop's
  busy/idle transition observed mid-run via a stubbed `run_task`, `open_trace`/`close_trace` +
  orchestrator's `_open_trace`/`_close_trace` entry lifecycle, `_record_trace_span`'s unconditional
  pulse — including a constraint test proving a poisoned `activity.pulse` can't block the underlying
  `TraceSpan` DB write — and the broker's terminal-outcome pulse, again with its own
  pulse-failure-doesn't-break-the-real-broadcast constraint test). Full pytest suite green;
  `npm run build` clean.
- **Opus verify pass — 1 blocking + 3 should-fix, all fixed before merge:**
  1. **BLOCKING — `_pending_events` grew unbounded whenever nobody was watching Pulse** (the normal
     state ~100% of the time, since the broadcaster's `if not activity_ws_manager.active: continue`
     guard skips `drain_dirty()` entirely with no client connected). Measured ~2MB per 5000 pulses,
     forever, for the life of the process — then the first connecting client would trigger one
     `json.dumps`/`send_text` of the ENTIRE backlog on the event loop, exactly the stall class this
     repo's CLAUDE.md calls its #1 hard-won rule. Fixed by making it a `deque(maxlen=200)`, same cap
     as `_ring`. Root cause of the miss: `run_activity_broadcaster()` itself had zero real test
     execution (both "broadcaster" tests reimplemented its logic inline instead of calling it) —
     fixed with two tests that actually run the coroutine, plus a direct unit regression test
     (`test_pending_events_bounded_even_when_never_drained`).
  2. **Removals were never broadcast to a connected client.** `remove()`/`sweep_stale()` drop an
     entry from the registry immediately, but `drain_dirty()` only ever emitted entries still
     present — so a finished `task:{id}` showed as permanently "running" in any Pulse tab left open
     (self-healing only on reload/reconnect, via the full-replace `activity.snapshot`). Fixed with a
     third delta channel, `removed: [actor_id, ...]` (a `_removed` set, drained/cleared alongside
     `_dirty`/`_pending_events`; `begin()` discards a pending removal if an actor is re-begun before
     the removal was ever drained). `Pulse.jsx` deletes those ids from its `entries` state on each
     `activity.delta`.
  3. **`close_trace`'s activity cleanup was gated on the DB read succeeding at CLOSE time** — `kind`
     was read from the `AgentTrace` row inside the same session that persists status/error, so a
     transient DB failure (SQLite lock contention, not exotic — `busy_timeout` is 30s, not never-
     blocks) skipped `activity.end()` entirely, leaving a phantom "running" entry for up to
     `sweep_stale`'s 24h backstop. Fixed with a small `_open_trace_kinds: dict[int, str]` cache in
     `router.py`, populated at open time and `.pop()`'d at close — the activity cleanup no longer
     needs the DB read to succeed at all (own regression test:
     `test_close_trace_activity_cleanup_independent_of_db_read_failure`, patches `sqlmodel.Session`
     to raise and confirms the board entry is still removed).
  4. **Shared actor ids raced across concurrent runs.** `trace:{kind}` (no trace_id) meant two
     overlapping traces of the same kind — two concurrent chat turns (web + Telegram), or two
     concurrent durable orchestrator runs (`NEXUS_TASK_WORKERS` defaults to **2**) — clobbered one
     shared board entry: whichever finished first would `end()`/mark it idle while the other was
     still genuinely running, and duration would be measured off the wrong start time. Fixed two
     ways: `router.py`'s generic `open_trace`/`close_trace` now key on `trace:{kind}:{trace_id}`
     (unique per instance, `remove()`'d on close rather than `end()`'d — a one-shot per-call entry
     left as "ok" forever would grow `_entries` by one row per chat/briefing/proposer/voice call,
     unboundedly, for the process lifetime) instead of the shared `trace:{kind}`; orchestrator's
     `_open_trace` DROPPED its own separate `trace:orchestrator` entry entirely rather than
     rekeying it — `task:{task_id}` was already a unique per-run identity carrying strictly more
     information (step progress), so the shared key was pure redundant race surface, not a needed
     feature. Own regression test:
     `test_open_trace_two_concurrent_same_kind_traces_dont_collide`.
  - Two nitpicks folded in: `worker_pool._worker_loop` no longer calls `activity.end()` for a
    picked-up task_id whose row was already deleted (never `begin()`'d), which used to stamp a
    fresh "ok" run on the pre-existing `worker:{id}` entry for a task that never ran; the frontend's
    ticker pause-buffer is now capped at 200 (`.slice(-200)`) to match the server-side ring, so
    parking the cursor over it for a long time can't grow it unbounded.
  - Second verify-adjacent pass re-ran the full suite green (1899+ tests) and rebuilt the frontend
    clean after all of the above.
- **A targeted follow-up verify pass on the four fixes above confirmed all four correct — and found
  ONE more real bug, an interaction between fixes #2 and #4.** `_removed` (the new removal channel
  from fix #2) has no cap of its own, and fix #4 turned trace actor ids into unique-per-instance
  strings (`trace:{kind}:{trace_id}`, `remove()`'d rather than `end()`'d) — so every completed chat/
  briefing/proposer/voice trace and durable task now feeds a NEW string into `_removed` forever,
  reintroducing the exact fix-#1 defect (unbounded growth while nobody has Pulse open, a first-
  connect burst dump) through a different channel. Measured empirically (not just reasoned about):
  5000 begin/remove cycles with no client connected left `_removed` at 5000 entries (~800KB), and the
  first connecting client would have received one 94KB `json.dumps`/`send_text` burst on the event
  loop. Fixed with `activity.discard_undelivered()` — called from the broadcaster's existing
  no-clients branch (`if not activity_ws_manager.active: discard_undelivered(); continue`) — which
  clears `_pending_events` and `_removed` without touching `_entries`/`_dirty` (a later connecting
  client gets a full authoritative `snapshot()` on connect regardless, making anything accumulated
  while nobody was watching redundant by construction; `_dirty` isn't a leak vector on its own since
  `remove()` already discards from it). Three new regression tests:
  `test_removed_leaks_unbounded_without_discard_undelivered` (proves the raw bug empirically, same
  discipline as the fix-#1 regression test), `test_discard_undelivered_clears_pending_and_removed_not_entries`,
  and a strengthened `test_broadcaster_real_loop_skips_send_when_no_clients_connected` that runs the
  REAL broadcast loop (not a reimplementation) and asserts `_removed`/`_pending_events` are actually
  empty afterward — this is exactly the kind of interaction-between-fixes bug a narrower "did each
  fix work in isolation" check can miss, which is why this follow-up pass was scoped to specifically
  scrutinize fix #2 × fix #4's interaction rather than re-deriving the whole feature. Full suite
  re-run green a third time after this fix; frontend unaffected (backend-only change).
