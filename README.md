# NEXUS — Personal Agentic OS

A production-grade personal AI operating system. FastAPI backend + React/Tailwind frontend + multi-agent orchestration.

> Runs on an Ubuntu LXC (Proxmox CT 207, `192.168.1.62`, `/opt/nexus`) under systemd. The
> project previously ran on Windows 11 — that history is archived on the frozen `windows-archive`
> branch, no longer deployed anywhere. See this repo's own `CLAUDE.md` for the full migration
> history.

## Quick Start

1. **Setup (first time only)** — `python3.13 -m venv venv && venv/bin/pip install -r requirements.txt`, `cd frontend && npm ci && npm run build`, then seed `/var/lib/nexus/.env` + `nexus.vault`/`.vault.key` (see `docs/lxc-migration-spec.md` Phase 1.1-1.4 for the full sequence).
2. **Start** — `systemctl start nexus-backend nexus-frontend` (both enabled at boot, `Restart=on-failure`).
3. **Stop** — `systemctl stop nexus-backend nexus-frontend`.
4. **Logs** — `journalctl -u nexus-backend -f`.

## Architecture

```
NEXUS
├── Frontend (React + Tailwind) → http://localhost:3000 (PWA-installable over HTTPS)
├── Backend (FastAPI)           → http://localhost:8000
│   ├── Agents (durable task orchestrator, read/write tool loop, goal proposer, learning loop)
│   ├── Safety (action broker + audit log, cost governor + kill switch)
│   ├── Integrations (HA, UniFi, Unraid, Proxmox, Obsidian, GitHub, Channels, AdGuard, Weather, Speedtest, Telegram, Calendar)
│   └── Scheduler (briefing, trends, uptime, backups + integrity, watchdogs, spend ingest, goal ticks)
└── Secrets Vault (AES-256 Fernet, nexus.vault + .vault.key)
```

## Autonomy & Safety

NEXUS runs a closed loop: it senses homelab state, proposes goals, auto-approves only
low-risk reversible ones, executes through durable resumable tasks, verifies outcomes,
and reports what it did in a daily digest. Everything side-effecting passes through a
single policy broker (`allow / needs_confirm / forbid` + immutable audit log). HIGH-risk
actions always wait for a human — approve or reject straight from Telegram buttons.
A kill switch (`POST /api/safety/pause`) halts all autonomy; daily + per-task USD caps
brake every LLM call, and every call is labeled in the spend report
(`GET /api/safety/spend-report`).

## Backups & Restore

Hourly WAL checkpoint, daily local backup (integrity-checked against the **copy**),
daily off-box bundle (vault + nexus.db) to the Unraid share, phone alert on failure.
**There is no restore script yet** — restore is manual: `rclone copy
nexus-unraid:<share>/<path> <dest>` (see `backend/backup.py::restore_vault`'s own
guard/docstring for the exact remote layout).

## Remote Access

`tailscale serve` fronts the app over HTTPS on the LXC's own Tailscale identity
(`nexus-lxc.tailfa52c.ts.net`, one HTTPS origin: `/` frontend, `/api` + `/ws` backend).
LAN clients use plain HTTP (`http://192.168.1.62:8000`/`:3000`). Device onboarding:
open Settings and paste the API key — key-in-URL links are retired.

## Secrets Management

Secrets are stored encrypted in `nexus.vault`. The master key is in `.vault.key`.

**IMPORTANT — Back up both files separately:**
- `nexus.vault` → encrypted secrets (safe to back up to cloud)
- `.vault.key` → master key (store separately, e.g. password manager)

Losing `.vault.key` makes all vault secrets permanently unrecoverable.

### CLI Tools

```bash
# Add/update a secret
python tools/encrypt_secret.py

# View a secret (auto-clears after 30s)
python tools/decrypt_secret.py --key ANTHROPIC_API_KEY --confirm

# Rotate a secret (logs rotation timestamp)
python tools/rotate_secret.py --key ANTHROPIC_API_KEY

# Import existing .env into vault (one-time migration)
python tools/import_env.py --env-file .env

# Audit all secrets with timestamps
python tools/audit_secrets.py
```

## Integrations

| Integration | Purpose |
|-------------|---------|
| Home Assistant | Smart home entity states, alerts, broker-gated control |
| Proxmox | Node CPU/mem, VM/LXC inventory, storage (direct PVE API) |
| UniFi | Network clients, bandwidth, new device detection |
| Unraid | Array status, disk health, docker containers |
| Obsidian | Daily notes, task sync, briefing storage |
| GitHub | Open PRs, assigned issues, stale PR detection |
| Channels DVR | Recording status, library, storage |
| AdGuard Home | DNS stats, filtering toggle, timed disable |
| OpenWeatherMap | Current conditions, forecast, high/low |
| Telegram | Own bot — outbound phone alerts + inbound goal/safety button taps |
| Calendar | Google/Apple iCal feeds — agenda for the briefing and Today page |
| OpenRouter | Fallback model gateway |

If a Telegram send fails, payloads are queued in SQLite and retried every 60 seconds.

## API Authentication

All API endpoints except `/api/health` require:
```
Authorization: Bearer <NEXUS_API_KEY>
```

The `NEXUS_API_KEY` is stored in the vault and generated during setup.

## Model Routing

| Task | Model |
|------|-------|
| Chat replies, briefing | Claude Sonnet |
| Intent classify, goal proposer, facts/wiki extraction, summaries | Claude Haiku |
| Orchestrator plan / execute / verify | Configurable per role (`.env`), defaults Sonnet/Sonnet/Haiku |

Every call is metered into `SpendLog` with a label; daily + per-task USD caps are
enforced before each billed call. Hosted web-search requests bill at $10/1k.

## Development

```bash
# Run tests
venv/bin/pytest tests/ -v --cov=backend --cov-report=term-missing

# Lint
venv/bin/ruff check backend/

# Type check
venv/bin/mypy backend/ --ignore-missing-imports
```
