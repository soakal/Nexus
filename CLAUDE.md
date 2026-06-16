# NEXUS — Agentic OS · Claude Code Context

Production-grade personal AI OS for Windows 11. FastAPI backend + React/Vite frontend, a system-tray launcher, and a multi-agent layer that talks to a homelab.

> Also read the user's master map at `C:\Users\Brian\CLAUDE.md` for global rules (model pipeline, secrets, deploy confirmations). This file is the project-local detail.

## Run / build / test
- **Start:** `.\start.ps1`  ·  **Stop:** `.\stop.ps1`  ·  **Setup:** `.\setup.ps1`
- Backend: FastAPI + uvicorn on **:8000**, venv at `.\venv` (`.\venv\Scripts\python.exe`).
- Frontend: React + Vite + Tailwind on **:3000**. Build with `cd frontend && npm run build`; `start.ps1` serves the build via `npx vite preview --host 0.0.0.0`.
- **After any frontend change you must `npm run build`** — preview serves `dist/`, not live source.
- Tests: `pytest` (in `tests/`). Backend changes need a restart (`stop.ps1` then `start.ps1`) to take effect.
- LAN access (phone): `http://192.168.1.119:3000`. Firewall private profile is disabled.

## Layout
- `backend/main.py` — FastAPI app + lifespan (scheduler start, memo watcher thread). Routers registered under `/api/*`.
- `backend/api/` — routers: tasks, briefing, voice, sources, agents, channels, adguard, trends, secrets, unraid_api, homeassistant, uptime, chat, today, trigger.
- `backend/integrations/` — one module per system: homeassistant, unifi, unraid, obsidian, github, openrouter, weather, channels_dvr, adguard, hermes, speedtest. Each exposes async `fetch()` and `health_check()`.
- `backend/agents/` — `router.py` (opus/sonnet/haiku + Anthropic web search), `orchestrator.py` (Opus plans → Sonnet executes, with WEB_SEARCH/VAULT_SEARCH directives), `worker_pool.py` (durable task execution pool), `chat.py`, `voice.py`, `briefing.py`, `memo_watcher.py`.
- `backend/scheduler.py` — APScheduler jobs: morning_briefing, trend_snapshots (15m), retry_deliveries (60s), record_uptime (2m), record_speedtest (30m).
- `backend/cache.py` — `async_ttl_cache` (see below).
- `backend/database.py` — SQLModel tables in `nexus.db` (WAL mode, busy_timeout 30s). `create_db_and_tables()` runs an idempotent `_ensure_task_columns()` shim that ALTERs in `Task.cancel_requested` on old DBs.

## Durable task execution (resumable / cancellable / resume-on-restart)
- **`TaskStep` table is the source of truth for task progress.** Planning writes one `TaskStep` row per step (status `pending|running|done|failed`, `output_json`, `idempotency_key`). The orchestrator loop **skips `done` steps** (resume), commits each step's output the instant it finishes (the checkpoint), and rebuilds context from completed `TaskStep.output_json` — context is NEVER reset to `[]` on retry.
- **`TaskWorkerPool` (`worker_pool.py`) is the single owner of orchestration concurrency.** A bounded pool of N workers (`NEXUS_TASK_WORKERS`, default 2) drains an `asyncio.Queue` of Task ids. `create_task` inserts a `pending` Task and enqueues it; the pool runs it. There is no in-memory `_running` dict and no bare `asyncio.create_task` in `api/tasks.py`.
- **Resume on boot:** `main.py` lifespan calls `get_pool().start()`, which calls `requeue_unfinished()` — every Task left `running`/`pending` is re-enqueued (NOT force-failed). A `TaskStep` stuck in `running` (process died mid-step) is reset to `pending` by `_load_steps`. A Task whose planning died (no `TaskStep` rows) re-plans from scratch.
- **Cancellation:** `DELETE /api/tasks/{id}` sets `Task.cancel_requested` (cooperative, checked between steps → Task status `stopped`, done steps preserved) and hard-cancels the in-flight coroutine as a backstop, then deletes the row. `stopped` is a real status used for programmatic/boot cancellation.
- **`Task.status` is free-text:** `pending | running | success | failed | stopped`.
- **Orchestrator legacy path:** `run_task(prompt, task_id=None)` runs the old in-memory loop (used by `tests/test_orchestrator.py`); `task_id` set always uses the durable path. All durable DB helpers are sync and invoked via `asyncio.to_thread` — no Session/ORM crosses an `await`.
- `nexus.db-wal` / `nexus.db-shm` are gitignored SQLite WAL sidecars.
- `frontend/src/pages/` — 11 pages (Dashboard, Briefing, Tasks, Chat, Voice, Media, HomeAssistant, Trends, Uptime, Agents, Settings); `Today` page for calendar/email. `App.jsx` holds the `NAV` array + routes; `components/MobileNav.jsx` is the mobile bottom bar.
- `tray.py` + `launch_tray.vbs` — system tray launcher, auto-starts at login via a Registry Run key.

## Action broker (Tier 1.3 — policy-gated writes + immutable audit)
- **`backend/safety/broker.py::execute_action(actor, kind, target, payload, idempotency_key=None, *, confirmed=False)` is the ONE chokepoint every side-effecting write passes through.** It classifies risk/reversibility, decides allow/needs_confirm/forbid by actor, writes an immutable `ActionLog` row BEFORE and AFTER the attempt, dispatches only when allowed, and is idempotent by key. It NEVER re-raises a dispatch error.
- **Two outcome axes, never conflated:** GATE outcome = {allowed, needs_confirm, forbidden}; DISPATCH outcome = {executed, failed}. `ActionLog.decision` holds the FINAL state.
- **Actors:** `user` is always allowed (preserves chat UX, still logged); `agent`/`autonomous` go through the policy — IRREVERSIBLE→forbidden unless confirmed; HIGH/UNCLASSIFIABLE→needs_confirm unless confirmed; LOW/MEDIUM→allowed. An UNKNOWN actor string degrades to `autonomous` (most restrictive), never `user`.
- **Dispatchers live only in the broker** (`_DISPATCHERS`): `ha_service`→`homeassistant.call_service`, `hermes_relay`→`hermes.relay`. `chat.py` has NO raw `call_service`/`relay(...)` dispatch — it calls `execute_action`. Add new write paths as a new dispatcher, never a direct integration call.
- **`ActionLog` table** (`backend/database.py`): immutable by convention — app code only INSERTs then UPDATEs a row, never deletes. Created by `create_all` (new table, no migration shim).
- **API:** `GET /api/safety/actions` (auth, `?limit=`≤200, `?decision=`, `?actor=`) lists the audit trail newest-first. `POST /api/safety/actions/{id}/confirm` is a documented inert placeholder (real 404/409; confirm-and-dispatch lands with Tier 1.5 autonomy).
- All broker DB helpers are sync + `asyncio.to_thread`-only — no Session/ORM crosses an `await`.

## Secrets — never commit
`config.py`, `nexus.vault`, `.vault.key`, `.env` are gitignored and MUST stay that way. Secrets live encrypted in `nexus.vault` (Fernet, key in `.vault.key`); non-secret config in `.env`. `backend/secrets/vault.py` reads them; `Settings` (`backend/config.py`) exposes secret properties lazily. `nexus.vault.meta` (names + timestamps only, no values) is safe to track.

## Non-obvious rules (hard-won)
- **Never block the asyncio event loop.** Windows ProactorEventLoop + a blocked loop = `WinError 64`, dropped connections, "everything offline". All sync DB work inside `async` funcs goes through `asyncio.to_thread`. The memo watcher starts on a daemon `threading.Thread`, not the loop.
- **`async_ttl_cache` is load-bearing.** Every integration `fetch()`/`health_check()` is cached (success ~10-60s, failures ~3s via `falsy_ttl`). This is what keeps `/api/health` fast when many tabs/devices poll. Don't add per-request outbound calls without caching.
- **Auth:** all `/api/*` need a Bearer key (`NEXUS_API_KEY` from the vault); `/api/health`, `/api/briefing/latest`, `/api/trigger` are unauthenticated. Each browser stores the key in `localStorage`; a `?key=...` link (Settings → "Copy device setup link") onboards new devices.
- **Uptime job runs checks sequentially**, not concurrently — firing all 10 health checks at once false-fails on cold TLS.

## Model pipeline
Opus 4.8 (`claude-opus-4-8`) plans/verifies · Sonnet 4.6 (`claude-sonnet-4-6`) writes/answers · Haiku 4.5 (`claude-haiku-4-5`) routes/classifies. Chat uses Anthropic's hosted web search tool. Calls bill the `ANTHROPIC_API_KEY`.

## Hermes link (LXC 200, 192.168.1.55)
NEXUS reads calendar/email + relays actions through Hermes's REST API (`/hermes/gmail`, `/hermes/calendar`, `/hermes/action`, auth = `HERMES_WEBHOOK_SECRET`). Hermes is a **live production bot** — confirm before any SFTP deploy or `systemctl restart`. Deploy pattern: paramiko, password read from a temp file that the script deletes; back up the remote file first; restart only the needed service.
