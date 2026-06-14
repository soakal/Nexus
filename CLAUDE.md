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
- `backend/agents/` — `router.py` (opus/sonnet/haiku + Anthropic web search), `orchestrator.py` (Opus plans → Sonnet executes, with WEB_SEARCH/VAULT_SEARCH directives), `chat.py`, `voice.py`, `briefing.py`, `memo_watcher.py`.
- `backend/scheduler.py` — APScheduler jobs: morning_briefing, trend_snapshots (15m), retry_deliveries (60s), record_uptime (2m), record_speedtest (30m).
- `backend/cache.py` — `async_ttl_cache` (see below).
- `backend/database.py` — SQLModel tables in `nexus.db`.
- `frontend/src/pages/` — 11 pages (Dashboard, Briefing, Tasks, Chat, Voice, Media, HomeAssistant, Trends, Uptime, Agents, Settings); `Today` page for calendar/email. `App.jsx` holds the `NAV` array + routes; `components/MobileNav.jsx` is the mobile bottom bar.
- `tray.py` + `launch_tray.vbs` — system tray launcher, auto-starts at login via a Registry Run key.

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
