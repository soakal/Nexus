import asyncio
import sys

# Windows: force the Selector event loop BEFORE uvicorn creates its loop (this
# block runs at app-import time, which is before the server starts). The default
# Windows ProactorEventLoop raises OSError [WinError 64] "The specified network
# name is no longer available" under CONCURRENT outbound httpx connections — e.g.
# /api/health fanning out to ~10 integrations at once — which surfaces to the user
# as "app not loading data" (sequential calls work, concurrent ones don't). The
# SelectorEventLoop handles that concurrency cleanly. NEXUS spawns no in-loop
# subprocesses (the memo watcher is a daemon thread), so Selector's limitations
# (no subprocess transport, ~512 sockets) do not apply here.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import logging
import pathlib
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    _bo_proc: list = [None]  # mutable slot for the Brain Organizer MCP server subprocess

    # Startup
    from backend.database import create_db_and_tables
    create_db_and_tables()

    # Warn once at startup if spend-metering prices have not been field-verified.
    try:
        from backend.config import get_settings as _gs
        if not _gs().prices_verified:
            logger.warning(
                "Spend metering prices are UNVERIFIED (config prices_verified=False). "
                "Cost caps may be inaccurate until rates are field-validated against "
                "Anthropic billing."
            )
    except Exception:
        pass  # vault not ready yet — warning will appear once vault is unlocked

    # Tasks left "running"/"pending" from a dead process are NOT force-failed —
    # the worker pool re-enqueues them on start() so durable execution resumes.

    vault_ok = pathlib.Path(".vault.key").exists() and pathlib.Path("nexus.vault").exists()

    # Lock down the key file's permissions on every boot (best-effort, never fatal).
    try:
        from backend.secrets.vault import secure_key_file
        secure_key_file()
    except Exception as e:
        logger.warning(f"Key file hardening skipped: {e}")

    if vault_ok:
        from backend.config import get_settings
        settings = get_settings()
        # Validate config/secrets before anything depends on them. A failure here is
        # fatal — log at ERROR and re-raise so uvicorn fails fast rather than running
        # half-configured. This is deliberately OUTSIDE the broad try below so it is
        # not demoted to a "Startup partial" warning.
        try:
            settings.validate()
        except Exception as e:
            logger.error(f"Startup aborted — invalid configuration: {e}")
            raise
        try:
            from backend.scheduler import scheduler, setup_scheduler
            setup_scheduler(settings.briefing_time, settings.briefing_timezone)
            scheduler.start()

            import threading
            from backend.agents.memo_watcher import start_watcher_blocking
            loop = asyncio.get_running_loop()
            threading.Thread(
                target=start_watcher_blocking,
                args=(settings.memo_watch_folder, loop),
                name="memo-watcher-start",
                daemon=True,
            ).start()

            # Durable task worker pool — start() re-enqueues any unfinished tasks
            # so execution resumes after a restart instead of being force-failed.
            from backend.agents.worker_pool import get_pool
            await get_pool().start()

            # Brain Organizer MCP server — optional, only starts if the module is installed
            try:
                import subprocess
                from pathlib import Path
                _bo_dir = Path(__file__).parent.parent / "modules" / "brain-organizer"
                _bo_py = _bo_dir / "venv" / "Scripts" / "python.exe"
                _bo_srv = _bo_dir / "mcp_server.py"
                if _bo_py.exists() and _bo_srv.exists():
                    _bo_proc[0] = subprocess.Popen(
                        [str(_bo_py), str(_bo_srv)],
                        cwd=str(_bo_dir),
                    )
                    logger.info(f"Brain Organizer MCP server started (PID {_bo_proc[0].pid})")
            except Exception as e:
                logger.warning(f"Brain Organizer MCP server not started: {e}")

            logger.info("NEXUS backend started")
        except Exception as e:
            logger.warning(f"Startup partial: {e}")
    else:
        logger.warning("Vault not configured — running in limited mode")

    yield

    # Shutdown
    try:
        if _bo_proc[0] is not None:
            _bo_proc[0].terminate()
            _bo_proc[0].wait(timeout=5)
    except Exception:
        pass
    try:
        from backend.scheduler import scheduler
        if scheduler.running:
            scheduler.shutdown()
    except Exception:
        pass
    try:
        from backend.agents.memo_watcher import stop_watcher
        await stop_watcher()
    except Exception:
        pass
    try:
        from backend.agents.worker_pool import get_pool
        await get_pool().stop()
    except Exception:
        pass


app = FastAPI(title="NEXUS Agentic OS", version="1.0.0", lifespan=lifespan)

try:
    from backend.config import get_settings as _gs_cors
    _cors_origin_regex = _gs_cors().cors_allow_origin_regex
except Exception:
    # Vault not ready at build time — fall back to the hard-coded default so the app always starts.
    _cors_origin_regex = r"^https?://(localhost|127\.0\.0\.1|0\.0\.0\.0|10\.\d{1,3}\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3}|172\.(1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})(:\d+)?$"

app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=_cors_origin_regex,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health")
async def health():
    vault_key_exists = pathlib.Path(".vault.key").exists()
    vault_exists = pathlib.Path("nexus.vault").exists()
    if not vault_key_exists:
        return JSONResponse({"status": "vault_missing"})
    if not vault_exists:
        return JSONResponse({"status": "vault_empty"})
    return {"status": "ok"}


# Register all routers
from backend.api import (
    adguard,
    agents,
    brain_organizer,
    briefing,
    channels,
    chat,
    facts,
    goals,
    homeassistant,
    safety,
    secrets,
    sources,
    tasks,
    today,
    traces,
    unraid_api,
    uptime,
    voice,
)
from backend.api.trigger import router as trigger_router
from backend.api.setup import router as setup_router

app.include_router(setup_router, prefix="/api/setup", tags=["setup"])
app.include_router(brain_organizer.router, prefix="/api/brain-organizer", tags=["brain-organizer"])
app.include_router(tasks.router, prefix="/api/tasks", tags=["tasks"])
app.include_router(facts.router, prefix="/api/facts", tags=["facts"])
app.include_router(goals.router, prefix="/api/goals", tags=["goals"])
app.include_router(chat.router, prefix="/api/chat", tags=["chat"])
app.include_router(briefing.router, prefix="/api/briefing", tags=["briefing"])
app.include_router(voice.router, prefix="/api/voice", tags=["voice"])
app.include_router(sources.router, prefix="/api/sources", tags=["sources"])
app.include_router(agents.router, prefix="/api/agents", tags=["agents"])
app.include_router(channels.router, prefix="/api/channels", tags=["channels"])
app.include_router(adguard.router, prefix="/api/adguard", tags=["adguard"])
app.include_router(uptime.router, prefix="/api/uptime", tags=["uptime"])
app.include_router(secrets.router, prefix="/api/secrets", tags=["secrets"])
app.include_router(unraid_api.router, prefix="/api/unraid", tags=["unraid"])
app.include_router(homeassistant.router, prefix="/api/ha", tags=["homeassistant"])
app.include_router(today.router, prefix="/api/today", tags=["today"])
app.include_router(safety.router, prefix="/api/safety", tags=["safety"])
app.include_router(traces.router, prefix="/api/traces", tags=["traces"])
app.include_router(trigger_router, tags=["trigger"])

from backend.auth import require_api_key  # noqa: E402


@app.get("/api/weather")
async def get_weather(_=Depends(require_api_key)):
    from backend.integrations.weather import fetch
    try:
        return await fetch()
    except Exception as e:
        return JSONResponse(status_code=502, content={"error": str(e)})


@app.websocket("/ws/logs")
async def websocket_logs(websocket: WebSocket):
    # Authenticate on the handshake. Preferred: the key is offered as the second
    # WebSocket subprotocol after the "nexus-api-key" sentinel — this keeps the
    # secret OUT of the URL, so it never lands in uvicorn's access log. Legacy
    # fallback: the ?key= query param (still works, but logs the key — avoid).
    # Reject (close 1008) before accepting if it doesn't match.
    import hmac
    provided = ""
    accept_subprotocol = None
    subprotocols = websocket.scope.get("subprotocols", []) or []
    if len(subprotocols) >= 2 and subprotocols[0] == "nexus-api-key":
        provided = subprotocols[1]
        accept_subprotocol = "nexus-api-key"  # echo the sentinel, NOT the key
    if not provided:
        provided = websocket.query_params.get("key", "")
    try:
        from backend.config import get_settings
        expected = get_settings().nexus_api_key
    except Exception:
        expected = ""
    if not provided or not expected or not hmac.compare_digest(provided, expected):
        await websocket.close(code=1008)  # policy violation
        return
    from backend.api.agents import ws_manager
    await ws_manager.connect(websocket, subprotocol=accept_subprotocol)
    try:
        while True:
            await websocket.receive_text()
    except Exception:
        ws_manager.disconnect(websocket)
