"""TOFU (trust-on-first-use) TLS certificate pinning for the Unraid GraphQL API.

Unraid's local GraphQL endpoint normally only has a self-signed LAN
certificate, so validating it against public CAs was never an option --
which is why `backend/integrations/unraid.py` used to construct its httpx
client with `verify=False`. That disables certificate validation entirely:
any network-adjacent attacker able to intercept the TCP session can present
*any* certificate and the client will happily complete the handshake and
send the `x-api-key` header -- straight to the attacker -- on every request,
including `fetch()`/`health_check()` reads AND `_docker_mutation()` (the
write path behind `restart_docker`, which issues `docker { stop }`/
`docker { start }` GraphQL mutations) (finding F16).

This module replaces the blanket `verify=False` with the same trust model
SSH's `known_hosts` uses (identical design to the sibling fix for UniFi,
`backend/integrations/unifi_tls_pinning.py`, finding F15 -- adapted here
since Unraid has no login/cookie flow, just a static `x-api-key` header
sent with every GraphQL POST):

* First successful connection to a given "host:port" -- the certificate's
  SHA-256 fingerprint is captured and persisted to `.unraid_known_hosts.json`
  (gitignored; a relative path resolved against the process's cwd, the same
  convention `backend/database.py`'s `DB_PATH = pathlib.Path("nexus.db")`
  already uses -- NEXUS's cwd is always the repo root).
* Every later connection -- the presented certificate's fingerprint must
  match the pinned one exactly, or the connection is refused *before any
  request bytes are written to the wire* (see `_PinningNetworkStream.start_tls`
  below), with an `UnraidCertificateMismatchError` explaining the mismatch and
  how to deliberately re-trust a new certificate.

Implementation notes: httpx's `verify=` parameter can't express "capture on
first connect, pin thereafter", so this builds a custom
`httpx.AsyncBaseTransport` backed by a custom `httpcore.AsyncConnectionPool`
with a custom `network_backend`. That backend's `connect_tcp`/
`connect_unix_socket` wrap the returned network stream so its `start_tls()`
extracts the peer certificate (`ssl_object.getpeercert(binary_form=True)`),
fingerprints it, and pins-or-verifies BEFORE returning the TLS-wrapped
stream up to httpcore's HTTP/1.1 connection logic -- which is the last
point control passes through before any HTTP data (including the
`x-api-key` header, on reads AND on the docker stop/start mutations) is
sent, so a mismatch is refused pre-credential-exposure, not after.
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
from pathlib import Path

import httpcore
import httpx

logger = logging.getLogger(__name__)

# Relative to cwd, same convention as backend/database.py's DB_PATH -- NEXUS
# always runs from the repo root, so this lands next to nexus.db.
KNOWN_HOSTS_PATH = Path(".unraid_known_hosts.json")

# The known-hosts file is tiny and touched at most once per distinct
# host:port in the steady state (every later connection is a read-only
# comparison) -- a single coarse process-wide lock is simplest and correct,
# no need for per-key locking.
_LOCK = threading.Lock()


class UnraidCertificateMismatchError(Exception):
    """An Unraid GraphQL endpoint presented a TLS certificate that does not
    match the one pinned in .unraid_known_hosts.json on a prior connection
    to the same host:port.

    This is a fail-closed refusal: the connection is aborted before any HTTP
    data (including the `x-api-key` header, on both reads and docker
    stop/start mutations) is written to the wire. If the mismatch is
    expected -- the server's certificate was legitimately regenerated, or
    the server/its network path was replaced -- deliberately re-trust the
    new certificate the same way you'd edit SSH's known_hosts: delete that
    host's entry (or the whole file) from .unraid_known_hosts.json, then
    reconnect to pin the new certificate.
    """


def _load_known_hosts(path: Path) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("unraid known_hosts file %s unreadable (%s); treating as empty", path, exc)
        return {}
    return data if isinstance(data, dict) else {}


def _save_known_hosts(path: Path, data: dict) -> None:
    tmp_path = path.with_name(path.name + ".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
        f.write("\n")
    tmp_path.replace(path)


def _fingerprint(der_cert: bytes) -> str:
    return hashlib.sha256(der_cert).hexdigest()


def _pin_or_verify(host_key: str, fingerprint: str, path: Path) -> None:
    """TOFU check for one connection's presented certificate.

    Pins on first use for `host_key`; on every later connection, raises
    `UnraidCertificateMismatchError` (fail closed) if the fingerprint no
    longer matches -- it never silently re-pins over a mismatch.
    """
    with _LOCK:
        known = _load_known_hosts(path)
        pinned = known.get(host_key)
        if pinned is None:
            known[host_key] = fingerprint
            _save_known_hosts(path, known)
            logger.info("unraid: pinned new TLS certificate for %s (trust-on-first-use)", host_key)
            return
        if pinned != fingerprint:
            raise UnraidCertificateMismatchError(
                f"TLS certificate mismatch for Unraid GraphQL endpoint {host_key}: "
                f"presented certificate (sha256={fingerprint}) does NOT match "
                f"the one pinned on {host_key}'s first connection "
                f"(sha256={pinned}). Refusing the connection before sending "
                "any request data (including the x-api-key header). If this "
                "certificate change is expected (server replaced / cert "
                "regenerated), deliberately re-trust it by removing the "
                f"\"{host_key}\" entry (or the whole file) from {path}, then "
                "reconnect to pin the new certificate -- the same way you'd "
                "edit SSH's known_hosts."
            )
        # Matches the pin -- proceed exactly as before.


class _PinningNetworkStream(httpcore.AsyncNetworkStream):
    """Wraps a real AsyncNetworkStream so `start_tls()` pins/verifies the
    peer certificate before the TLS-wrapped stream is handed back to
    httpcore -- i.e. before any HTTP request bytes can be written."""

    def __init__(self, stream: httpcore.AsyncNetworkStream, host_key: str, known_hosts_path: Path):
        self._stream = stream
        self._host_key = host_key
        self._known_hosts_path = known_hosts_path

    async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        return await self._stream.read(max_bytes, timeout=timeout)

    async def write(self, buffer: bytes, timeout: float | None = None) -> None:
        await self._stream.write(buffer, timeout=timeout)

    async def aclose(self) -> None:
        await self._stream.aclose()

    async def start_tls(
        self,
        ssl_context,
        server_hostname: str | None = None,
        timeout: float | None = None,
    ) -> "httpcore.AsyncNetworkStream":
        tls_stream = await self._stream.start_tls(
            ssl_context, server_hostname=server_hostname, timeout=timeout
        )

        ssl_object = tls_stream.get_extra_info("ssl_object")
        der_cert = ssl_object.getpeercert(binary_form=True) if ssl_object is not None else None
        if not der_cert:
            await tls_stream.aclose()
            raise UnraidCertificateMismatchError(
                f"Could not read a TLS certificate from {self._host_key} to "
                "verify against the pinned fingerprint; refusing the "
                "connection rather than proceeding unverified."
            )

        try:
            _pin_or_verify(self._host_key, _fingerprint(der_cert), self._known_hosts_path)
        except UnraidCertificateMismatchError:
            await tls_stream.aclose()
            raise

        return _PinningNetworkStream(tls_stream, self._host_key, self._known_hosts_path)

    def get_extra_info(self, info: str):
        return self._stream.get_extra_info(info)


class _PinningNetworkBackend(httpcore.AsyncNetworkBackend):
    """Wraps the real async network backend so every connection it opens is
    routed through `_PinningNetworkStream` before use."""

    def __init__(self, known_hosts_path: Path):
        self._inner = httpcore.AnyIOBackend()
        self._known_hosts_path = known_hosts_path

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options=None,
    ) -> httpcore.AsyncNetworkStream:
        stream = await self._inner.connect_tcp(
            host,
            port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )
        return _PinningNetworkStream(stream, f"{host}:{port}", self._known_hosts_path)

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options=None,
    ) -> httpcore.AsyncNetworkStream:
        stream = await self._inner.connect_unix_socket(
            path, timeout=timeout, socket_options=socket_options
        )
        return _PinningNetworkStream(stream, f"unix:{path}", self._known_hosts_path)

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


class PinnedTLSTransport(httpx.AsyncBaseTransport):
    """An httpx async transport that accepts a self-signed Unraid GraphQL
    certificate like `verify=False` did -- except the certificate is pinned
    TOFU-style (SSH known_hosts style) instead of never being checked at
    all. See module docstring for the full design."""

    def __init__(self, known_hosts_path: Path = KNOWN_HOSTS_PATH):
        # Same "accept a self-signed cert" baseline verify=False produced --
        # our own pinning check (in _PinningNetworkStream.start_tls) is what
        # actually authenticates the connection now.
        ssl_context = httpx.create_ssl_context(verify=False)
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=ssl_context,
            network_backend=_PinningNetworkBackend(known_hosts_path),
        )

    async def __aenter__(self) -> "PinnedTLSTransport":
        await self._pool.__aenter__()
        return self

    async def __aexit__(self, exc_type=None, exc_value=None, traceback=None) -> None:
        await self._pool.__aexit__(exc_type, exc_value, traceback)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        req = httpcore.Request(
            method=request.method,
            url=httpcore.URL(
                scheme=request.url.raw_scheme,
                host=request.url.raw_host,
                port=request.url.port,
                target=request.url.raw_path,
            ),
            headers=request.headers.raw,
            content=request.stream,
            extensions=request.extensions,
        )
        resp = await self._pool.handle_async_request(req)
        return httpx.Response(
            status_code=resp.status,
            headers=resp.headers,
            stream=_AsyncResponseStream(resp.stream),
            extensions=resp.extensions,
        )

    async def aclose(self) -> None:
        await self._pool.aclose()


class _AsyncResponseStream(httpx.AsyncByteStream):
    def __init__(self, httpcore_stream):
        self._httpcore_stream = httpcore_stream

    async def __aiter__(self):
        async for part in self._httpcore_stream:
            yield part

    async def aclose(self) -> None:
        if hasattr(self._httpcore_stream, "aclose"):
            await self._httpcore_stream.aclose()


def build_transport() -> PinnedTLSTransport:
    """Factory shared by unraid.py's three httpx.AsyncClient call sites
    (fetch, health_check, _docker_mutation -- the write path behind
    restart_docker) so all of them pin/verify identically."""
    return PinnedTLSTransport()
