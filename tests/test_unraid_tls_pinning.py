"""Real (non-mocked) tests for the TOFU TLS certificate pinning transport
that replaced Unraid's `verify=False` (finding F16).

These spin up a genuine TLS server on localhost with a freshly-generated
self-signed certificate and drive real connections through
`backend.integrations.unraid_tls_pinning.PinnedTLSTransport` via a real
`httpx.AsyncClient` -- the exact transport `fetch()`/`health_check()`/
`_docker_mutation()` construct via `build_transport()` -- rather than mocking
httpx.AsyncClient the way tests/test_unraid.py does. That lets these tests
assert on the actual security property: a first connection pins, a matching
later connection succeeds, and a *different* certificate on the same
host:port is refused and does not silently overwrite the pin.

A second block of tests goes one step further than the generic transport
tests and drives the real `backend.integrations.unraid._docker_mutation`
function itself (the write path behind `restart_docker`'s stop/start calls)
against the real TLS test server, to specifically confirm the docker
MUTATION path is covered by pinning -- not just the read paths
(`fetch`/`health_check`).
"""
from __future__ import annotations

import asyncio
import datetime
import json
import socket
import ssl

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from backend.integrations import unraid_tls_pinning as pinning


def _run_on_selector_loop(coro):
    """Runs `coro` to completion on a fresh event loop constructed explicitly,
    rather than whatever loop pytest-asyncio's `asyncio_mode = "auto"` would
    otherwise hand us.
    """
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _generate_cert(tmp_path, name: str) -> tuple[str, str, str]:
    """Generates a fresh self-signed cert/key pair, writes them to PEM files
    under tmp_path, and returns (key_path, cert_path, sha256_hex_fingerprint)."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")]
    )
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName("127.0.0.1")]), critical=False
        )
        .sign(key, hashes.SHA256())
    )

    key_path = tmp_path / f"{name}.key"
    cert_path = tmp_path / f"{name}.crt"
    key_path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))

    fingerprint = cert.fingerprint(hashes.SHA256()).hex()
    return str(key_path), str(cert_path), fingerprint


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _TLSTestServer:
    """A minimal real TLS + HTTP/1.1 server used to drive genuine TLS
    handshakes against a chosen self-signed certificate.

    Also records whether it ever received any request bytes on a given
    connection, and can serve a GraphQL-shaped JSON body -- used by the
    docker-mutation tests below to prove the `x-api-key` header (and the
    mutation body) never reaches the wire when the pinning check refuses
    the connection at the TLS layer.
    """

    def __init__(self, cert_path: str, key_path: str, port: int, body: bytes = b'{"data": {"array": {"state": "started"}}}'):
        self._cert_path = cert_path
        self._key_path = key_path
        self._port = port
        self._body = body
        self._server: asyncio.base_events.Server | None = None
        self.received_any_request = False

    async def start(self) -> None:
        ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ssl_context.load_cert_chain(certfile=self._cert_path, keyfile=self._key_path)
        self._server = await asyncio.start_server(
            self._handle, "127.0.0.1", self._port, ssl=ssl_context
        )

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            data = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=2)
            if data:
                self.received_any_request = True
        except Exception:
            pass
        body = self._body
        response = (
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: application/json\r\n"
            b"Content-Length: " + str(len(body)).encode() + b"\r\n"
            b"Connection: close\r\n\r\n" + body
        )
        try:
            writer.write(response)
            await writer.drain()
        except Exception:
            pass
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()


# ---------------------------------------------------------------------------
# Generic transport tests (mirrors the sibling UniFi suite, F15)
# ---------------------------------------------------------------------------


def test_tofu_pins_fingerprint_on_first_connection(tmp_path):
    """First-ever connection to a host:port must capture and persist the
    certificate's fingerprint -- and the request must still succeed, i.e.
    zero behavior change for a brand-new install."""
    _run_on_selector_loop(_tofu_pins_fingerprint_on_first_connection(tmp_path))


async def _tofu_pins_fingerprint_on_first_connection(tmp_path):
    key_path, cert_path, expected_fp = _generate_cert(tmp_path, "cert1")
    port = _free_port()
    server = _TLSTestServer(cert_path, key_path, port)
    await server.start()
    try:
        known_hosts = tmp_path / "known_hosts.json"
        assert not known_hosts.exists()

        transport = pinning.PinnedTLSTransport(known_hosts_path=known_hosts)
        async with httpx.AsyncClient(timeout=5, transport=transport) as client:
            resp = await client.get(f"https://127.0.0.1:{port}/")

        assert resp.status_code == 200

        data = json.loads(known_hosts.read_text())
        assert data[f"127.0.0.1:{port}"] == expected_fp
    finally:
        await server.stop()


def test_tofu_matching_cert_succeeds_on_subsequent_connection(tmp_path):
    """A later connection (a fresh transport/client -- the exact same code
    path a new fetch/health_check/_docker_mutation call takes) against an
    unchanged certificate must succeed, and must not touch the pin."""
    _run_on_selector_loop(_tofu_matching_cert_succeeds_on_subsequent_connection(tmp_path))


async def _tofu_matching_cert_succeeds_on_subsequent_connection(tmp_path):
    key_path, cert_path, expected_fp = _generate_cert(tmp_path, "cert1")
    port = _free_port()
    server = _TLSTestServer(cert_path, key_path, port)
    await server.start()
    try:
        known_hosts = tmp_path / "known_hosts.json"

        transport1 = pinning.PinnedTLSTransport(known_hosts_path=known_hosts)
        async with httpx.AsyncClient(timeout=5, transport=transport1) as client:
            resp1 = await client.get(f"https://127.0.0.1:{port}/")
        assert resp1.status_code == 200

        # Brand new transport + client, same known_hosts file -- this is
        # exactly what happens on every subsequent call in unraid.py, since
        # each of fetch()/health_check()/_docker_mutation() opens a fresh
        # httpx.AsyncClient per call.
        transport2 = pinning.PinnedTLSTransport(known_hosts_path=known_hosts)
        async with httpx.AsyncClient(timeout=5, transport=transport2) as client:
            resp2 = await client.get(f"https://127.0.0.1:{port}/")
        assert resp2.status_code == 200

        data = json.loads(known_hosts.read_text())
        assert data[f"127.0.0.1:{port}"] == expected_fp
    finally:
        await server.stop()


def test_tofu_rejects_mismatched_cert_and_does_not_repin(tmp_path):
    """A DIFFERENT certificate presented for the same host:port (the MITM /
    replaced-server case) must be refused with a clear error, and must NOT
    silently overwrite the original pin. Also confirms no request bytes
    (including the x-api-key header a real caller would send) ever reach
    the mismatched server."""
    _run_on_selector_loop(_tofu_rejects_mismatched_cert_and_does_not_repin(tmp_path))


async def _tofu_rejects_mismatched_cert_and_does_not_repin(tmp_path):
    key_path1, cert_path1, fp1 = _generate_cert(tmp_path, "cert1")
    key_path2, cert_path2, fp2 = _generate_cert(tmp_path, "cert2")
    assert fp1 != fp2
    port = _free_port()
    known_hosts = tmp_path / "known_hosts.json"

    server1 = _TLSTestServer(cert_path1, key_path1, port)
    await server1.start()
    try:
        transport1 = pinning.PinnedTLSTransport(known_hosts_path=known_hosts)
        async with httpx.AsyncClient(timeout=5, transport=transport1) as client:
            resp = await client.get(f"https://127.0.0.1:{port}/")
        assert resp.status_code == 200
    finally:
        await server1.stop()

    pinned_after_first_connect = json.loads(known_hosts.read_text())
    assert pinned_after_first_connect[f"127.0.0.1:{port}"] == fp1

    server2 = _TLSTestServer(cert_path2, key_path2, port)
    await server2.start()
    try:
        transport2 = pinning.PinnedTLSTransport(known_hosts_path=known_hosts)
        async with httpx.AsyncClient(timeout=5, transport=transport2) as client:
            with pytest.raises(pinning.UnraidCertificateMismatchError, match="does NOT match"):
                await client.get(f"https://127.0.0.1:{port}/")
        # The refusal must happen at the TLS layer, before the HTTP request
        # line/headers (which would carry x-api-key on a real call) are
        # ever written to the wire.
        assert server2.received_any_request is False
    finally:
        await server2.stop()

    # Fail closed AND no silent re-pin -- the original fingerprint must
    # still be the one on file.
    pinned_after_mismatch = json.loads(known_hosts.read_text())
    assert pinned_after_mismatch[f"127.0.0.1:{port}"] == fp1
    assert pinned_after_mismatch[f"127.0.0.1:{port}"] != fp2


def test_pin_or_verify_pins_on_first_use(tmp_path):
    """Unit-level check of the pinning primitive itself: first call for a
    host_key stores the fingerprint."""
    path = tmp_path / "known_hosts.json"
    pinning._pin_or_verify("unraid.example:443", "a" * 64, path)
    data = json.loads(path.read_text())
    assert data == {"unraid.example:443": "a" * 64}


def test_pin_or_verify_matching_fingerprint_is_a_noop(tmp_path):
    path = tmp_path / "known_hosts.json"
    pinning._pin_or_verify("unraid.example:443", "a" * 64, path)
    # Should not raise, and should not rewrite anything.
    pinning._pin_or_verify("unraid.example:443", "a" * 64, path)
    data = json.loads(path.read_text())
    assert data == {"unraid.example:443": "a" * 64}


def test_pin_or_verify_mismatch_raises_and_does_not_repin(tmp_path):
    path = tmp_path / "known_hosts.json"
    pinning._pin_or_verify("unraid.example:443", "a" * 64, path)

    with pytest.raises(pinning.UnraidCertificateMismatchError, match="does NOT match"):
        pinning._pin_or_verify("unraid.example:443", "b" * 64, path)

    # Must still hold the original pin, not the mismatched one.
    data = json.loads(path.read_text())
    assert data == {"unraid.example:443": "a" * 64}


def test_pin_or_verify_error_message_explains_how_to_re_trust(tmp_path):
    path = tmp_path / "known_hosts.json"
    pinning._pin_or_verify("unraid.example:443", "a" * 64, path)
    with pytest.raises(pinning.UnraidCertificateMismatchError) as excinfo:
        pinning._pin_or_verify("unraid.example:443", "b" * 64, path)
    message = str(excinfo.value)
    assert "unraid.example:443" in message
    assert str(path) in message
    assert "reconnect" in message.lower() or "re-pin" in message.lower()


# ---------------------------------------------------------------------------
# Docker MUTATION path coverage (_docker_mutation / restart_docker) -- the
# write path behind restart_docker. F16's finding explicitly named this a
# write path sending x-api-key under verify=False, so pinning must be
# proven against this call site specifically, not just fetch()/health_check().
# ---------------------------------------------------------------------------


def test_docker_mutation_pins_on_first_call_and_succeeds(tmp_path, monkeypatch):
    """The real backend.integrations.unraid._docker_mutation function (no
    httpx mocking) -- the exact function restart_docker's stop/start calls
    go through -- must pin on its first real connection and succeed, i.e.
    zero behavior change for a brand-new install on the mutation path."""
    _run_on_selector_loop(_docker_mutation_pins_on_first_call_and_succeeds(tmp_path, monkeypatch))


async def _docker_mutation_pins_on_first_call_and_succeeds(tmp_path, monkeypatch):
    from backend.config import get_settings
    from backend.integrations import unraid

    key_path, cert_path, expected_fp = _generate_cert(tmp_path, "cert1")
    port = _free_port()
    body = b'{"data": {"docker": {"stop": {"id": "hash1:hash2", "state": "EXITED"}}}}'
    server = _TLSTestServer(cert_path, key_path, port, body=body)
    await server.start()

    known_hosts = tmp_path / "known_hosts.json"
    settings = get_settings()
    monkeypatch.setattr(settings, "unraid_host", f"127.0.0.1:{port}")
    monkeypatch.setattr(unraid, "build_transport", lambda: pinning.PinnedTLSTransport(known_hosts_path=known_hosts))

    try:
        assert not known_hosts.exists()
        ok, err = await unraid._docker_mutation("stop", "hash1:hash2")
        assert ok is True
        assert err == ""
        assert server.received_any_request is True

        data = json.loads(known_hosts.read_text())
        assert data[f"127.0.0.1:{port}"] == expected_fp
    finally:
        await server.stop()


def test_docker_mutation_reconnect_succeeds_via_same_code_path(tmp_path, monkeypatch):
    """A second _docker_mutation call (restart_docker's `start`, following
    its `stop`) against the SAME unchanged certificate must succeed via the
    identical real code path -- not a mock standing in for the second call."""
    _run_on_selector_loop(_docker_mutation_reconnect_succeeds_via_same_code_path(tmp_path, monkeypatch))


async def _docker_mutation_reconnect_succeeds_via_same_code_path(tmp_path, monkeypatch):
    from backend.config import get_settings
    from backend.integrations import unraid

    key_path, cert_path, expected_fp = _generate_cert(tmp_path, "cert1")
    port = _free_port()
    body = b'{"data": {"docker": {"start": {"id": "hash1:hash2", "state": "RUNNING"}}}}'
    server = _TLSTestServer(cert_path, key_path, port, body=body)
    await server.start()

    known_hosts = tmp_path / "known_hosts.json"
    settings = get_settings()
    monkeypatch.setattr(settings, "unraid_host", f"127.0.0.1:{port}")
    monkeypatch.setattr(unraid, "build_transport", lambda: pinning.PinnedTLSTransport(known_hosts_path=known_hosts))

    try:
        ok1, _ = await unraid._docker_mutation("stop", "hash1:hash2")
        assert ok1 is True

        # Fresh call, same known_hosts -- exactly what restart_docker does
        # between its stop and start mutations.
        ok2, err2 = await unraid._docker_mutation("start", "hash1:hash2")
        assert ok2 is True
        assert err2 == ""

        data = json.loads(known_hosts.read_text())
        assert data[f"127.0.0.1:{port}"] == expected_fp
    finally:
        await server.stop()


def test_docker_mutation_mismatched_cert_fails_closed_and_does_not_repin(tmp_path, monkeypatch):
    """The critical write-path case: a docker stop/start mutation whose
    connection hits a MISMATCHED certificate (MITM / server replaced) must
    fail (ok=False), must NOT silently re-pin over the original fingerprint,
    and must NOT put the x-api-key header / mutation body on the wire."""
    _run_on_selector_loop(_docker_mutation_mismatched_cert_fails_closed_and_does_not_repin(tmp_path, monkeypatch))


async def _docker_mutation_mismatched_cert_fails_closed_and_does_not_repin(tmp_path, monkeypatch):
    from backend.config import get_settings
    from backend.integrations import unraid

    key_path1, cert_path1, fp1 = _generate_cert(tmp_path, "cert1")
    key_path2, cert_path2, fp2 = _generate_cert(tmp_path, "cert2")
    assert fp1 != fp2
    port = _free_port()
    known_hosts = tmp_path / "known_hosts.json"

    settings = get_settings()
    monkeypatch.setattr(settings, "unraid_host", f"127.0.0.1:{port}")
    monkeypatch.setattr(unraid, "build_transport", lambda: pinning.PinnedTLSTransport(known_hosts_path=known_hosts))

    # First: pin against cert1 via a real stop mutation.
    body1 = b'{"data": {"docker": {"stop": {"id": "hash1:hash2", "state": "EXITED"}}}}'
    server1 = _TLSTestServer(cert_path1, key_path1, port, body=body1)
    await server1.start()
    try:
        ok, _ = await unraid._docker_mutation("stop", "hash1:hash2")
        assert ok is True
    finally:
        await server1.stop()

    pinned_after_first = json.loads(known_hosts.read_text())
    assert pinned_after_first[f"127.0.0.1:{port}"] == fp1

    # Now the server behind the same host:port presents a DIFFERENT
    # certificate (the MITM / replaced-server scenario) for the `start` call.
    server2 = _TLSTestServer(cert_path2, key_path2, port)
    await server2.start()
    try:
        ok2, err2 = await unraid._docker_mutation("start", "hash1:hash2")
        assert ok2 is False
        assert "does NOT match" in err2
        # The mutation's x-api-key header + GraphQL body must never have
        # reached this (mismatched) server.
        assert server2.received_any_request is False
    finally:
        await server2.stop()

    # No silent re-pin -- the ORIGINAL fingerprint must still be on file.
    pinned_after_mismatch = json.loads(known_hosts.read_text())
    assert pinned_after_mismatch[f"127.0.0.1:{port}"] == fp1
    assert pinned_after_mismatch[f"127.0.0.1:{port}"] != fp2


def test_restart_docker_uses_pinned_transport_for_both_mutations(tmp_path, monkeypatch):
    """End-to-end: restart_docker's real stop-then-start sequence (only
    resolve_container_id/fetch/asyncio.sleep are stubbed -- _docker_mutation
    itself is the real function) goes through the real pinned transport for
    BOTH mutations, against a real TLS server."""
    _run_on_selector_loop(_restart_docker_uses_pinned_transport_for_both_mutations(tmp_path, monkeypatch))


async def _restart_docker_uses_pinned_transport_for_both_mutations(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock
    from backend.config import get_settings
    from backend.integrations import unraid

    key_path, cert_path, expected_fp = _generate_cert(tmp_path, "cert1")
    port = _free_port()
    body = b'{"data": {"docker": {"stop": {"id": "hash1:hash2", "state": "EXITED"}}}}'
    server = _TLSTestServer(cert_path, key_path, port, body=body)
    await server.start()

    known_hosts = tmp_path / "known_hosts.json"
    settings = get_settings()
    monkeypatch.setattr(settings, "unraid_host", f"127.0.0.1:{port}")
    monkeypatch.setattr(unraid, "build_transport", lambda: pinning.PinnedTLSTransport(known_hosts_path=known_hosts))

    from backend.integrations.unraid import UnraidData
    stopped_data = UnraidData(docker_containers=[
        {"id": "hash1:hash2", "name": "plex", "status": "Exited", "state": "EXITED"}
    ])

    monkeypatch.setattr(unraid, "resolve_container_id", AsyncMock(return_value="hash1:hash2"))
    mock_fetch = AsyncMock(return_value=stopped_data)
    mock_fetch.invalidate = lambda: None
    monkeypatch.setattr(unraid, "fetch", mock_fetch)
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())

    try:
        result = await unraid.restart_docker("plex")
    finally:
        await server.stop()

    assert result == {"success": True}
    data = json.loads(known_hosts.read_text())
    assert data[f"127.0.0.1:{port}"] == expected_fp
