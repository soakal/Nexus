import asyncio
import os

import logging
import pathlib
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

# Module-level (not inside lifespan()): uvicorn imports this module during
# config.load(), which runs BEFORE startup() binds the listening socket -- so
# this import pays the shared TLS context's ~1.3s cost (see that module's
# docstring) before anything is served, not during a live request. run.py
# already imports it earlier still (before uvicorn itself); this import is
# what covers every OTHER entrypoint (pytest, `-m uvicorn`).
import backend.http_client  # noqa: F401

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
# httpx logs the full request URL at INFO -- several integrations carry their
# credential IN the URL (Telegram bot token, OpenWeather API key, the
# Google/Apple calendar ICS tokens), which would otherwise write cleartext
# secrets to the (persistent) journal on every single call.
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


def _brain_mcp_spawn_env(token: str | None) -> dict | None:
    """Build the env for the Brain Organizer MCP server subprocess.

    Returns None (inherit the parent env unchanged, today's behavior) when no
    token is set. Otherwise returns a FULL copy of the parent env with
    MCP_WRITE_TOKEN added — never a minimal env dict, which would strip the
    parent environment the child would otherwise inherit (PATH for anything
    it shells out to, HOME, and any other env-provided config the server or
    its imported modules read via os.environ).
    """
    if not token:
        return None
    return {**os.environ, "MCP_WRITE_TOKEN": token}


@asynccontextmanager
async def lifespan(app: FastAPI):
    _bo_proc: list = [None]  # mutable slot for the Brain Organizer MCP server subprocess
    _activity_broadcaster_task: list = [None]  # mutable slot for the Pulse broadcast loop

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
            # Best-effort warm-up of the secret-store cache (no-op on the vault
            # backend) so the first Settings property access is a cache hit,
            # not a loop-blocking network call. Never fatal.
            try:
                from backend.secrets import manager as _secrets_manager
                await asyncio.to_thread(_secrets_manager.warm_up)
            except Exception as e:
                logger.warning(f"Secret store warm-up skipped: {e}")

            # Capture the running commit SHA once, at boot (backend/version.py) —
            # the deploy-drift watchdog check compares this against the repo's
            # live HEAD every 5 minutes to catch a stale process serving old code
            # after a git pull with no restart. Best-effort: a failure here just
            # degrades that check to a permanent no-op, never blocks boot.
            try:
                from backend import version
                boot_sha = await asyncio.to_thread(version.capture_running_sha)
                logger.info(f"Running SHA at boot: {boot_sha or 'unknown (no .git resolution)'}")
            except Exception as e:
                logger.warning(f"Could not capture running SHA (deploy-drift check degrades to no-op): {e}")

            from backend.scheduler import scheduler, setup_scheduler
            setup_scheduler(settings.briefing_time, settings.briefing_timezone)
            scheduler.start()

            # Eagerly populate dashboard state once at boot, fire-and-forget,
            # so the very first request after a restart has real data instead
            # of waiting on the scheduler's own first (one-interval-away) run.
            # Never blocks boot: a slow/failed collector is caught inside
            # refresh_collector itself, not here.
            # Reference kept in this local var (not discarded) so the task
            # isn't eligible for GC mid-run: asyncio.create_task() only holds
            # a weak reference internally, and this generator frame stays
            # suspended at `yield` below for the app's whole lifetime, which
            # keeps this local alive for exactly as long as it needs to be.
            from backend.state_workers import prime_state_workers
            prime_task = asyncio.create_task(prime_state_workers())

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

            # Telegram inbound poller (goal/safety button callbacks) — a pure
            # async task, not a thread (see telegram_poller's module docstring).
            # Returns None (no-op) when disabled or TELEGRAM_BOT_TOKEN is unset,
            # so its absence never blocks boot.
            from backend.agents import telegram_poller
            telegram_poller.start()

            # Pulse activity broadcaster (backend/activity.py) — coalescing
            # 250ms loop pushing live agent/worker/job status over
            # /ws/agent-activity. Reference kept in the mutable slot (same
            # weak-ref-avoidance reasoning as prime_task above) so it survives
            # to the shutdown block for a clean cancel.
            from backend import activity
            _activity_broadcaster_task[0] = asyncio.create_task(activity.run_activity_broadcaster())

            # Brain Organizer MCP server — optional, only starts if the module is installed
            try:
                import subprocess
                from pathlib import Path
                _bo_dir = Path(__file__).parent.parent / "modules" / "brain-organizer"
                _bo_py = brain_organizer.venv_python_path(_bo_dir)
                _bo_srv = _bo_dir / "mcp_server.py"
                if _bo_py.exists() and _bo_srv.exists():
                    try:
                        _bo_token = settings.brain_mcp_write_token
                    except Exception:
                        _bo_token = None  # vault hiccup -> spawn token-less (remote writes stay disabled)
                    _bo_proc[0] = subprocess.Popen(
                        [str(_bo_py), str(_bo_srv)],
                        cwd=str(_bo_dir),
                        env=_brain_mcp_spawn_env(_bo_token),
                    )
                    logger.info(f"Brain Organizer MCP server started (PID {_bo_proc[0].pid})")
            except Exception as e:
                logger.warning(f"Brain Organizer MCP server not started: {e}")

            logger.info("NEXUS backend started")
        except Exception as e:
            logger.warning(f"Startup partial: {e}")
    else:
        logger.warning("Vault not configured — running in limited mode")

    try:
        from backend.api.setup import ensure_bootstrap_token
        ensure_bootstrap_token()
    except Exception as e:
        logger.warning(f"Bootstrap token init skipped: {e}")

    yield

    # Shutdown
    # Flush any buffered secret-fallback events before we lose the process —
    # a normal `systemctl restart nexus-backend` must not discard up to 300s of audit signal.
    try:
        from backend.secrets import fallback_log
        await asyncio.to_thread(fallback_log.drain)
    except Exception:
        pass
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
    try:
        from backend.agents import telegram_poller
        await telegram_poller.stop()
    except Exception:
        pass
    try:
        if _activity_broadcaster_task[0] is not None:
            _activity_broadcaster_task[0].cancel()
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
    dashboard_state,
    facts,
    goals,
    homeassistant,
    protonmail,
    proxmox_api,
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
app.include_router(dashboard_state.router, prefix="/api/dashboard", tags=["dashboard"])
app.include_router(briefing.router, prefix="/api/briefing", tags=["briefing"])
app.include_router(voice.router, prefix="/api/voice", tags=["voice"])
app.include_router(sources.router, prefix="/api/sources", tags=["sources"])
app.include_router(agents.router, prefix="/api/agents", tags=["agents"])
app.include_router(channels.router, prefix="/api/channels", tags=["channels"])
app.include_router(adguard.router, prefix="/api/adguard", tags=["adguard"])
app.include_router(uptime.router, prefix="/api/uptime", tags=["uptime"])
app.include_router(secrets.router, prefix="/api/secrets", tags=["secrets"])
app.include_router(unraid_api.router, prefix="/api/unraid", tags=["unraid"])
app.include_router(proxmox_api.router, prefix="/api/proxmox", tags=["proxmox"])
app.include_router(homeassistant.router, prefix="/api/ha", tags=["homeassistant"])
app.include_router(protonmail.router, prefix="/api/protonmail", tags=["protonmail"])
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


@app.websocket("/ws/state")
async def websocket_state(websocket: WebSocket):
    # Same handshake auth as /ws/logs (see its comments above), but connects
    # to state_ws_manager — a SEPARATE broadcaster, deliberately not shared
    # with /ws/logs (see backend/state_workers.py's module docstring for why).
    import hmac
    provided = ""
    accept_subprotocol = None
    subprotocols = websocket.scope.get("subprotocols", []) or []
    if len(subprotocols) >= 2 and subprotocols[0] == "nexus-api-key":
        provided = subprotocols[1]
        accept_subprotocol = "nexus-api-key"
    if not provided:
        provided = websocket.query_params.get("key", "")
    try:
        from backend.config import get_settings
        expected = get_settings().nexus_api_key
    except Exception:
        expected = ""
    if not provided or not expected or not hmac.compare_digest(provided, expected):
        await websocket.close(code=1008)
        return
    from backend.api.agents import state_ws_manager
    await state_ws_manager.connect(websocket, subprotocol=accept_subprotocol)
    try:
        while True:
            await websocket.receive_text()
    except Exception:
        state_ws_manager.disconnect(websocket)


@app.websocket("/ws/agent-activity")
async def websocket_agent_activity(websocket: WebSocket):
    # Same handshake auth as /ws/logs/ws/state above, own broadcaster (see
    # backend/activity.py's coalescing broadcast loop + api/agents.py's
    # activity_ws_manager). Sends a full snapshot immediately on connect so
    # the Pulse page paints without waiting for the next 250ms broadcast tick.
    import hmac
    provided = ""
    accept_subprotocol = None
    subprotocols = websocket.scope.get("subprotocols", []) or []
    if len(subprotocols) >= 2 and subprotocols[0] == "nexus-api-key":
        provided = subprotocols[1]
        accept_subprotocol = "nexus-api-key"
    if not provided:
        provided = websocket.query_params.get("key", "")
    try:
        from backend.config import get_settings
        expected = get_settings().nexus_api_key
    except Exception:
        expected = ""
    if not provided or not expected or not hmac.compare_digest(provided, expected):
        await websocket.close(code=1008)
        return
    from backend.api.agents import activity_ws_manager
    await activity_ws_manager.connect(websocket, subprotocol=accept_subprotocol)
    try:
        import json
        from backend import activity
        await websocket.send_text(json.dumps({"type": "activity.snapshot", **activity.snapshot()}))
        while True:
            await websocket.receive_text()
    except Exception:
        activity_ws_manager.disconnect(websocket)


@app.get("/api/activity")
async def get_activity(_=Depends(require_api_key)):
    # REST fallback for the Pulse page's initial paint (before the socket
    # opens) and as a poll fallback. Reads the in-memory registry only — no
    # DB, no LLM. `entries`/`events` are the same shape as the websocket's
    # "activity.snapshot" message; `jobs` is the scheduler's own live job
    # list, which the registry cannot know about until a job actually fires.
    from backend import activity
    from backend.scheduler import registered_jobs
    return {**activity.snapshot(), "jobs": registered_jobs()}
