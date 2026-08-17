"""NEXUS backend entrypoint."""
import sys

# Build the shared TLS context (backend/http_client.py) before uvicorn is even
# imported: it's a BLOCKING call on the event loop regardless of host (~1.3s
# on the Windows host this was measured on, see that module's docstring), and
# paying it here means it lands on the bare main thread before any event loop
# exists, instead of stalling live requests later.
import backend.http_client  # noqa: E402,F401

import uvicorn  # noqa: E402

if __name__ == "__main__":
    reload = "--reload" in sys.argv
    # loop="asyncio" (not "auto") so uvicorn doesn't probe for uvloop.
    uvicorn.run("backend.main:app", host="0.0.0.0", port=8000, loop="asyncio", reload=reload)
