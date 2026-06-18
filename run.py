"""NEXUS backend entrypoint.

Pins the Selector event loop on Windows BEFORE uvicorn is imported/started. This
MUST happen here (not in backend/main.py): uvicorn creates its event loop inside
Server.run() *before* it imports the app module, so a policy set at app-import
time is too late and the server stays on the default ProactorEventLoop — which
raises OSError [WinError 64] "The specified network name is no longer available"
under concurrent connections (both the /api/health fan-out AND the listening
socket's accept path), surfacing as "app not loading data" / a dead listener.

The SelectorEventLoop handles that concurrency/churn cleanly. NEXUS spawns no
in-loop subprocesses (the memo watcher is a daemon thread), so Selector's limits
(no subprocess transport, ~512 sockets) do not apply.

Launched by start.ps1:  python run.py  [--reload]
"""
import asyncio
import sys

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import uvicorn

if __name__ == "__main__":
    reload = "--reload" in sys.argv
    # loop="asyncio" (not "auto") so uvicorn uses the policy we just set instead of
    # probing for uvloop and falling back to its own Windows loop handling.
    uvicorn.run("backend.main:app", host="0.0.0.0", port=8000, loop="asyncio", reload=reload)
