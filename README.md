# NEXUS — Personal Agentic OS

A production-grade personal AI operating system for Windows 11. FastAPI backend + React/Tailwind frontend + multi-agent orchestration.

## Quick Start

1. **Setup (first time only)**
   ```powershell
   .\setup.ps1
   ```
   The wizard configures all integrations and stores secrets in an encrypted vault.

2. **Start**
   ```powershell
   .\start.ps1
   ```
   Opens `http://localhost:3000` automatically.

3. **Stop**
   ```powershell
   .\stop.ps1
   ```

## Architecture

```
NEXUS
├── Frontend (React + Tailwind) → http://localhost:3000
├── Backend (FastAPI)           → http://localhost:8000
│   ├── Agents (Opus plan / Sonnet execute / Haiku validate)
│   ├── Integrations (HA, UniFi, Unraid, Obsidian, GitHub, Channels, AdGuard, Weather, Hermes)
│   └── Scheduler (briefing cron, 15-min trend snapshots, delivery retry)
└── Secrets Vault (AES-256 Fernet, nexus.vault + .vault.key)
```

## Secrets Management

Secrets are stored encrypted in `nexus.vault`. The master key is in `.vault.key`.

**IMPORTANT — Back up both files separately:**
- `nexus.vault` → encrypted secrets (safe to back up to cloud)
- `.vault.key` → master key (store separately, e.g. password manager)

Losing `.vault.key` makes all vault secrets permanently unrecoverable.

### CLI Tools

```powershell
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
| Home Assistant | Smart home entity states, alerts |
| UniFi | Network clients, bandwidth, new device detection |
| Unraid | Array status, disk health, docker containers |
| Obsidian | Daily notes, task sync, briefing storage |
| GitHub | Open PRs, assigned issues, stale PR detection |
| Channels DVR | Recording status, library, storage |
| AdGuard Home | DNS stats, filtering toggle, timed disable |
| OpenWeatherMap | Current conditions, forecast, high/low |
| Hermes | Outbound Telegram delivery, HA action bridge |
| OpenRouter | Fallback model gateway |

## Hermes Bridge

NEXUS is the intelligence layer. Hermes is the delivery layer.

- NEXUS → Hermes: POST `/hermes/notify` (Telegram), POST `/hermes/action` (Home Assistant)
- Hermes → NEXUS: POST `/api/trigger` (kick off tasks), GET `/api/briefing/latest`

If Hermes is unreachable, payloads are queued in SQLite and retried every 60 seconds.

## API Authentication

All API endpoints except `/api/health` and `/api/briefing/latest` require:
```
Authorization: Bearer <NEXUS_API_KEY>
```

The `NEXUS_API_KEY` is stored in the vault and generated during setup.

## Model Routing

| Task | Model |
|------|-------|
| Architecture, planning, briefing, debug | Claude Opus |
| Code generation, execution, responses | Claude Sonnet |
| Validation, schema checks | Claude Haiku |

## Development

```powershell
# Dev mode (hot reload)
.\start.ps1 -dev

# Run tests
.\venv\Scripts\pytest tests/ -v --cov=backend --cov-report=term-missing

# Lint
.\venv\Scripts\ruff check backend/

# Type check
.\venv\Scripts\mypy backend/ --ignore-missing-imports
```
