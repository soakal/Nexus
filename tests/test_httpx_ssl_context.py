"""Regression guards for the 2026-08-05 httpx SSL-context reuse fix.

Background: httpx.AsyncClient()/Client() build a fresh ssl.SSLContext
SYNCHRONOUSLY at construction time by default (~1.3-1.45s on this host,
suspected Windows Defender scanning the certifi PEM bundle each time) and it
BLOCKS THE EVENT LOOP while doing so. backend/http_client.py builds one
SSL_CONTEXT at import instead and every real call site reuses it via
verify=SSL_CONTEXT. Two categories are deliberately exempt (verify=False and
transport=, see that module's docstring) -- these tests encode all three
accepted forms as the allowlist itself, not a separate exemption list that can
silently rot.

Same "paren-scanning regression guard" style as
test_spend_report.py::test_no_unlabeled_llm_calls_in_agents -- a static source
scan, not a live parser. Known limitation (same as that test): a `)` inside a
string literal in the argument list would confuse the scanner. Every real call
site in this repo passes only simple kwargs, so this is an accepted precedent,
not a gap worth a real parser for.
"""
import pathlib
import re

_SCAN_ROOTS = ("backend", "tools")
_CLIENT_RE = re.compile(r"\b_?httpx\.(?:Async)?Client\(")
_CREATE_CTX_RE = re.compile(r"\bcreate_ssl_context\(")


def _iter_py_files():
    for root_name in _SCAN_ROOTS:
        root = pathlib.Path(root_name)
        if not root.exists():
            continue
        for f in root.rglob("*.py"):
            if "tests" in f.parts or "modules" in f.parts:
                continue
            yield f


def _scan_paren(src: str, start: int) -> str:
    """Return the argument text of a call whose '(' is at src[start-1]."""
    depth, i = 1, start
    while i < len(src) and depth:
        if src[i] == "(":
            depth += 1
        elif src[i] == ")":
            depth -= 1
        i += 1
    return src[start:i]


def test_every_httpx_client_reuses_the_shared_ssl_context():
    offenders = []
    for f in _iter_py_files():
        if f.name == "http_client.py":
            # This is the module that DEFINES SSL_CONTEXT, not a consumer of
            # it -- its own docstring prose legitimately describes the raw
            # httpx.AsyncClient() pattern for explanatory purposes, which
            # would otherwise false-positive here.
            continue
        src = f.read_text(encoding="utf-8")
        for m in _CLIENT_RE.finditer(src):
            args = _scan_paren(src, m.end())
            if "verify=SSL_CONTEXT" in args or "verify=False" in args or "transport=" in args:
                continue
            if "**kwargs" in args and "SSL_CONTEXT" in src[:m.start()]:
                # backend/integrations/protonmail.py's MCP client factory
                # builds its AsyncClient from a kwargs dict (to match the mcp
                # library's McpHttpClientFactory Protocol) containing
                # `"verify": SSL_CONTEXT` -- correct at runtime, but a static
                # text scan can't see inside the dict. Only accepted when the
                # module actually imports SSL_CONTEXT somewhere above this
                # call, so an unrelated **kwargs client can't slip through.
                continue
            lineno = src.count("\n", 0, m.start()) + 1
            offenders.append(f"{f}:{lineno}: {src[m.start():m.start() + 80]!r}")
    assert not offenders, (
        "httpx client(s) built without a shared SSL context -- each costs ~1.3s of "
        "BLOCKED EVENT LOOP on this host. Pass verify=SSL_CONTEXT (from "
        "backend.http_client), or verify=False / transport= if TLS is deliberately "
        f"handled another way. See backend/http_client.py. Offenders: {offenders}"
    )


def test_only_one_module_builds_an_expensive_ssl_context():
    """Guards against a re-added module-level create_ssl_context() silently
    doubling boot cost (exactly what NOT consolidating speedtest.py's old
    local _SSL_CONTEXT onto the shared one would have caused: 2.6s instead of
    1.3s at boot, since both would run before the loop ever serves)."""
    offenders = []
    for f in _iter_py_files():
        src = f.read_text(encoding="utf-8")
        for m in _CREATE_CTX_RE.finditer(src):
            if f.name == "http_client.py":
                continue
            args = _scan_paren(src, m.end())
            if "verify=False" in args:
                continue  # the TLS-pinning modules' cheap no-verify path
            lineno = src.count("\n", 0, m.start()) + 1
            offenders.append(f"{f}:{lineno}")
    assert not offenders, (
        "create_ssl_context() called outside backend/http_client.py without verify=False -- "
        f"this doubles the ~1.3s boot-time cost instead of reusing the shared context: {offenders}"
    )


def test_shared_ssl_context_is_imported_before_the_event_loop_serves():
    """Guards the placement, which is the whole point of the fix -- a correct
    SSL_CONTEXT imported lazily inside lifespan() would restore the exact
    boot-time stall this fix exists to close."""
    main_src = pathlib.Path("backend/main.py").read_text(encoding="utf-8")
    assert re.search(r"(?m)^(import backend\.http_client|from backend\.http_client import)", main_src), (
        "backend/main.py must import backend.http_client at MODULE LEVEL (column 0), not "
        "inside lifespan() -- uvicorn imports this module during config.load(), which runs "
        "BEFORE startup() binds the listening socket (server.py: load() at line ~76 precedes "
        "startup() at ~84), so a module-level import pays the ~1.3s cost before anything is "
        "served. An import inside lifespan() would pay it during a live request instead."
    )

    run_src = pathlib.Path("run.py").read_text(encoding="utf-8")
    assert "backend.http_client" in run_src, (
        "run.py should also import backend.http_client, before uvicorn.run(), so the "
        "production path pays the cost on the bare main thread before an event loop even exists."
    )
    http_client_idx = run_src.index("backend.http_client")
    uvicorn_import_idx = run_src.index("import uvicorn")
    assert http_client_idx < uvicorn_import_idx, (
        "run.py imports backend.http_client AFTER importing uvicorn -- move it earlier so the "
        "shared SSL context is built before uvicorn (and therefore before any event loop) exists."
    )
