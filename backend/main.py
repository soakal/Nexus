import logging
import pathlib
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    from backend.database import create_db_and_tables
    create_db_and_tables()

    # Mark any tasks left in "running" state as failed (they died with the previous process)
    try:
        from sqlmodel import Session, select
        from backend.database import Task, engine
        with Session(engine) as s:
            stuck = s.exec(select(Task).where(Task.status == "running")).all()
            for t in stuck:
                t.status = "failed"
                t.result_json = '{"error": "interrupted by backend restart"}'
            if stuck:
                s.commit()
                logger.info(f"Cleared {len(stuck)} interrupted task(s)")
    except Exception as e:
        logger.warning(f"Startup task cleanup failed: {e}")

    vault_ok = pathlib.Path(".vault.key").exists() and pathlib.Path("nexus.vault").exists()

    if vault_ok:
        try:
            from backend.config import get_settings
            settings = get_settings()
            from backend.scheduler import scheduler, setup_scheduler
            setup_scheduler(settings.briefing_time, settings.briefing_timezone)
            scheduler.start()

            from backend.agents.memo_watcher import start_watcher
            await start_watcher(settings.memo_watch_folder)

            logger.info("NEXUS backend started")
        except Exception as e:
            logger.warning(f"Startup partial: {e}")
    else:
        logger.warning("Vault not configured — running in limited mode")

    yield

    # Shutdown
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


app = FastAPI(title="NEXUS Agentic OS", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
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
    briefing,
    channels,
    homeassistant,
    secrets,
    sources,
    tasks,
    trends,
    unraid_api,
    voice,
)
from backend.api.trigger import router as trigger_router

app.include_router(tasks.router, prefix="/api/tasks", tags=["tasks"])
app.include_router(briefing.router, prefix="/api/briefing", tags=["briefing"])
app.include_router(voice.router, prefix="/api/voice", tags=["voice"])
app.include_router(sources.router, prefix="/api/sources", tags=["sources"])
app.include_router(agents.router, prefix="/api/agents", tags=["agents"])
app.include_router(channels.router, prefix="/api/channels", tags=["channels"])
app.include_router(adguard.router, prefix="/api/adguard", tags=["adguard"])
app.include_router(trends.router, prefix="/api/trends", tags=["trends"])
app.include_router(secrets.router, prefix="/api/secrets", tags=["secrets"])
app.include_router(unraid_api.router, prefix="/api/unraid", tags=["unraid"])
app.include_router(homeassistant.router, prefix="/api/ha", tags=["homeassistant"])
app.include_router(trigger_router, tags=["trigger"])


@app.get("/api/weather")
async def get_weather():
    from backend.integrations.weather import fetch
    try:
        data = await fetch()
        return data
    except Exception as e:
        return {"error": str(e)}


@app.websocket("/ws/logs")
async def websocket_logs(websocket: WebSocket):
    from backend.api.agents import ws_manager
    await ws_manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except Exception:
        ws_manager.disconnect(websocket)
