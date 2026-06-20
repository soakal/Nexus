# NEXUS Integration Guide — Brain Organizer

All Brain Organizer code lives in `modules/brain-organizer/`. The NEXUS core files
(`backend/main.py`, `backend/scheduler.py`, `start.ps1`) are untouched — you wire
them in by adding the snippets below when ready.

---

## 1 — Secrets vault

Brain Organizer reads three env vars. Add them to `nexus.vault` via the NEXUS
secrets UI or `backend/secrets/vault.py`:

| Key | Value |
|---|---|
| `ANTHROPIC_API_KEY` | Your Anthropic API key |
| `OPENROUTER_API_KEY` | OpenRouter key (fallback, optional) |
| `HERMES_HOST` | e.g. `http://192.168.1.55:5000` |

NEXUS must export these as environment variables before invoking either PS1 script.

---

## 2 — Daily scheduler job

Add one job to `backend/scheduler.py` (inside the `_setup_jobs` function, after
the existing jobs):

```python
import subprocess, os

def _run_brain_organizer():
    script = Path(__file__).parent.parent / "modules" / "brain-organizer" / "Start-BrainOrganizer.ps1"
    env = {**os.environ, "ANTHROPIC_API_KEY": settings.anthropic_api_key}
    if getattr(settings, "hermes_host", None):
        env["HERMES_HOST"] = settings.hermes_host
    result = subprocess.run(
        ["powershell", "-File", str(script)],
        env=env, capture_output=True, text=True
    )
    if result.returncode != 0:
        logger.error("Brain Organizer failed: %s", result.stderr)

scheduler.add_job(
    _run_brain_organizer,
    trigger="cron",
    hour=2, minute=0,          # 2:00 AM daily — adjust as needed
    id="brain_organizer_daily",
    replace_existing=True,
    misfire_grace_time=3600,
)
```

---

## 3 — MCP server startup

Add to `backend/main.py` lifespan (after existing startup code):

```python
import subprocess, os

@asynccontextmanager
async def lifespan(app):
    # ... existing startup ...
    _start_brain_mcp_server()
    yield
    # ... existing shutdown ...

def _start_brain_mcp_server():
    script = Path(__file__).parent.parent / "modules" / "brain-organizer" / "Start-MCPServer.ps1"
    proc = subprocess.Popen(
        ["powershell", "-File", str(script)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    pid_line = proc.stdout.readline().strip()
    if pid_line.isdigit():
        logger.info("Brain Organizer MCP server started (PID %s)", pid_line)
    else:
        logger.warning("Brain Organizer MCP server may not have started")
```

---

## 4 — Health polling

Add to the `/api/health` endpoint or the uptime scheduler job:

```python
async def _check_brain_mcp() -> dict:
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            r = await client.get("http://localhost:8765/health")
            return {"status": "ok" if r.status_code == 200 else "degraded"}
    except Exception:
        return {"status": "offline"}
```

---

## 5 — One-time setup

```powershell
cd "C:\Users\Brian\Documents\Agentic os\nexus\modules\brain-organizer"
python -m venv venv
.\venv\Scripts\pip install -r requirements.txt
```

Create the vault folders if they don't exist:
```powershell
$vault = "C:\Users\Brian\iCloudDrive\iCloud~md~obsidian\Brain"
New-Item -ItemType Directory -Force "$vault\raw\backups", "$vault\wiki", "$vault\_meta"
```

---

## 6 — Windows Firewall for Tailscale

Allow port 8765 on the Tailscale interface:

```powershell
New-NetFirewallRule -DisplayName "Brain Organizer MCP (Tailscale)" `
    -Direction Inbound -Protocol TCP -LocalPort 8765 `
    -InterfaceAlias "Tailscale" -Action Allow
```

---

## 7 — Claude Desktop MCP config

Add to `%APPDATA%\Claude\claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "brain": {
      "url": "http://localhost:8765",
      "name": "Brain — Personal Knowledge Base"
    }
  }
}
```

For remote (Tailscale) access, replace `localhost` with your Windows machine's
Tailscale IP (find it in the Tailscale app, typically `100.x.x.x`).
