"""One process-wide TLS context, built once at import.

Why this exists
---------------
`httpx.AsyncClient()` builds a fresh `ssl.SSLContext` SYNCHRONOUSLY inside its
constructor -- before it has seen a single URL -- by calling
`ssl.create_default_context(cafile=certifi.where())`. On the Windows host this
ran on before it was decommissioned (2026-08-15), that cost ~1.3s (measured
2026-08-05: 1241ms per AsyncClient(), vs 24ms for
`ssl.create_default_context()` with no cafile -- so it is the certifi PEM bundle
load specifically, not SSL setup in general; Windows Defender scanning the
bundle on every open was the suspected cause there). It is a BLOCKING call on
the event loop thread regardless of host.

That was already bad. `backend/state_workers.py::prime_state_workers()` -- run
once eagerly at boot from main.py's lifespan -- made it critical: it walks 22
collectors across ~18 integration modules back to back, so every restart fired
~20 sequential 1.3s loop-blocking stalls. Confirmed live: a ~60-90s window after
each restart where the whole app was intermittently unresponsive, /api/health
included.

Reusing one context makes an AsyncClient() construction ~0.4ms instead of
~1241ms. Pass it as `verify=`:

    from backend.http_client import SSL_CONTEXT
    async with httpx.AsyncClient(verify=SSL_CONTEXT, timeout=5) as client:
        ...

An `ssl.SSLContext` is designed to be shared across many connections and many
threads -- that is the whole point of the object. Nothing may MUTATE it.

Deliberately NOT applied to two kinds of call site
--------------------------------------------------
* `verify=False` (proxmox.py x5, scheduler.py:123) -- httpx's no-verify branch
  (`_config.py::load_ssl_context_no_verify`) never loads the CA bundle at all;
  measured 0.6ms. There is nothing to save, and forcing verification on would
  break those deliberately-unverified self-signed endpoints.
* `transport=` (unifi.py x3, unraid.py x3) -- `AsyncClient._init_transport`
  returns a supplied transport IMMEDIATELY and never constructs the default
  `AsyncHTTPTransport`, so httpx builds no SSL context and a `verify=` argument
  passed alongside is a silent no-op. Those transports own their own TLS (TOFU
  pinning), built with the cheap `create_ssl_context(verify=False)`.

`tests/test_httpx_ssl_context.py` enforces all three rules by static scan, so a
newly-added call site cannot silently reintroduce the stall.

Caveats
-------
* This context is built with httpx's defaults: http2=False (ALPN advertises only
  http/1.1) and no client certificate. A future call site needing either MUST
  build its own context rather than reusing this one.
* Built at import, so `SSL_CERT_FILE`/`SSL_CERT_DIR`/`SSLKEYLOGFILE` are read
  once per process, not per client. That matches previous behavior for any
  realistic deployment (the env does not change mid-process).
* Import cost is paid wherever this module is first imported. `run.py` imports
  it before `uvicorn.run()` so on the production path the 1.3s lands on the bare
  main thread, before an event loop even exists; `backend/main.py` imports it at
  module level so every other entrypoint (pytest, `-m uvicorn`) still
  gets it during uvicorn's `config.load()` -- which uvicorn runs BEFORE
  `startup()` binds the listening socket (server.py:76 vs :84), i.e. before
  anything is served and long before prime_state_workers() runs.
"""

import httpx

SSL_CONTEXT = httpx.create_ssl_context()
