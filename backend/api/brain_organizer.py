import json
import os
import subprocess
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException

from backend.auth import require_api_key

router = APIRouter()

_MODULE_DIR = Path(__file__).parent.parent.parent / "modules" / "brain-organizer"
_PROCESSED = _MODULE_DIR / "processed.json"
_LOG = _MODULE_DIR / "logs" / "organizer.log"
_CONFIG = _MODULE_DIR / "config.json"
_running: list = [None]  # mutable slot tracking a Run Now subprocess


def _count_pending(config: dict) -> int:
    vault = Path(config["vault_path"])
    raw = vault / config["raw_folder"]
    backup = vault / config["backup_folder"]
    if not raw.exists():
        return 0
    count = 0
    for ext in (".md", ".txt"):
        for f in raw.rglob(f"*{ext}"):
            if not f.is_file():
                continue
            try:
                f.relative_to(backup)
                continue
            except ValueError:
                pass
            count += 1
    return count


@router.get("/status")
async def brain_organizer_status(_=Depends(require_api_key)):
    running = False
    if _running[0] is not None:
        if _running[0].poll() is None:
            running = True
        else:
            _running[0] = None

    succeeded = 0
    failed = 0
    last_run = None

    if _PROCESSED.exists():
        try:
            data = json.loads(_PROCESSED.read_text(encoding="utf-8"))
            for entry in data.values():
                ts = entry.get("timestamp")
                if entry.get("status") == "failed":
                    failed += 1
                else:
                    succeeded += 1
                if ts and (last_run is None or ts > last_run):
                    last_run = ts
        except Exception:
            pass

    pending = 0
    if _CONFIG.exists():
        try:
            config = json.loads(_CONFIG.read_text(encoding="utf-8"))
            pending = _count_pending(config)
        except Exception:
            pass

    log_tail: list[str] = []
    if _LOG.exists():
        try:
            lines = _LOG.read_text(encoding="utf-8").splitlines()
            # Last 5 meaningful lines (INFO/WARNING/ERROR only)
            log_tail = [
                ln for ln in lines[-20:]
                if any(tag in ln for tag in ("[INFO]", "[WARNING]", "[ERROR]"))
            ][-5:]
        except Exception:
            pass

    return {
        "last_run": last_run,
        "succeeded": succeeded,
        "failed": failed,
        "pending": pending,
        "running": running,
        "log_tail": log_tail,
    }


@router.post("/reset-failed")
async def brain_organizer_reset_failed(_=Depends(require_api_key)):
    """Remove all failed entries from processed.json so the organiser retries them."""
    if not _PROCESSED.exists():
        return {"reset": 0}
    try:
        data = json.loads(_PROCESSED.read_text(encoding="utf-8"))
        before = len(data)
        data = {k: v for k, v in data.items() if v.get("status") != "failed"}
        tmp = _PROCESSED.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(_PROCESSED)
        return {"reset": before - len(data)}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/run")
async def brain_organizer_run(_=Depends(require_api_key)):
    if _running[0] is not None and _running[0].poll() is None:
        raise HTTPException(status_code=409, detail="Brain Organizer is already running")

    python_exe = _MODULE_DIR / "venv" / "Scripts" / "python.exe"
    script = _MODULE_DIR / "brain_organizer.py"
    if not python_exe.exists() or not script.exists():
        raise HTTPException(status_code=503, detail="Brain Organizer module not found")

    env = os.environ.copy()
    try:
        from backend.config import get_settings
        s = get_settings()
        for attr, var in [
            ("anthropic_api_key", "ANTHROPIC_API_KEY"),
            ("openrouter_api_key", "OPENROUTER_API_KEY"),
            ("hermes_host", "HERMES_HOST"),
        ]:
            try:
                val = getattr(s, attr, None)
            except Exception:
                val = None
            if val:
                env[var] = str(val)
    except Exception:
        pass

    proc = subprocess.Popen(
        [str(python_exe), str(script)],
        cwd=str(_MODULE_DIR),
        env=env,
    )
    _running[0] = proc
    return {"started": True, "pid": proc.pid}
