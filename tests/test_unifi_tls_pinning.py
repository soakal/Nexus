"""Real (non-mocked) tests for the TOFU TLS certificate pinning transport
that replaced UniFi's `verify=False` (finding F15).

These spin up a genuine TLS server on localhost with a freshly-generated
self-signed certificate and drive real connections through
`backend.integrations.unifi_tls_pinning.PinnedTLSTransport` via a real
`httpx.AsyncClient` -- the exact transport `fetch()`/`health_check()`/
`_stamgr_cmd()` construct via `build_transport()` -- rather than mocking
httpx.AsyncClient the way tests/test_unifi.py does. That lets these tests
assert on the actual security property: a first connection pins, a matching
later connection succeeds, and a *different* certificate on the same
host:port is refused and does not silently overwrite the pin.
"""
from __future__ import annotations

import asyncio
import datetime
import json
import socket
import ssl
import sys

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from backend.integrations import unifi_tls_pinning as pinning


def _run_on_selector_loop(coro):
    """Runs `coro` to completion on a fresh SelectorEventLoop rather than
    whatever loop pytest-asyncio's `asyncio_mode = "auto"` would otherwise
    hand us (WindowsProactorEventLoopPolicy's default on this platform).

    This matches run.py's own documented reason for forcing Selector in
    production ("ProactorEventLoop ... under concurrent httpx fetches"):
    abruptly closing a TLS stream after a pinning mismatch -- exactly what
    `_PinningNetworkStream.start_tls` does on purpose, to refuse the
    connection before any data is sent -- triggers a Proactor-specific
    SSL-transport teardown bug on Windows that hangs the test process
    (reproduced independently of this test suite while writing it). NEXUS
    itself never hits this: run.py pins SelectorEventLoop before uvicorn
    ever builds its loop, so production always runs this exact transport
    code under Selector, never Proactor.
    """
    if sys.platform == "win32":
        loop = asyncio.SelectorEventLoop()
    else:
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
    """A minimal real TLS + HTTP/1.1 server used only to drive genuine TLS
    handshakes against a chosen self-signed certificate."""

    def __init__(self, cert_path: str, key_path: str, port: int):
        self._cert_path = cert_path
        self._key_path = key_path
        self._port = port
        self._server: asyncio.base_events.Server | None = None

    async def start(self) -> None:
        ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ssl_context.load_cert_chain(certfile=self._cert_path, keyfile=self._key_path)
        self._server = await asyncio.start_server(
            self._handle, "127.0.0.1", self._port, ssl=ssl_context
        )

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=2)
        except Exception:
            pass
        body = b"ok"
        response = (
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: text/plain\r\n"
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
        assert resp.text == "ok"

        data = json.loads(known_hosts.read_text())
        assert data[f"127.0.0.1:{port}"] == expected_fp
    finally:
        await server.stop()


def test_tofu_matching_cert_succeeds_on_subsequent_connection(tmp_path):
    """A later connection (a fresh transport/client -- the exact same code
    path a new block_client/unblock_client/fetch call takes) against an
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
        # exactly what happens on every subsequent call in unifi.py, since
        # each of fetch()/health_check()/_stamgr_cmd() opens a fresh
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
    replaced-controller case) must be refused with a clear error, and must
    NOT silently overwrite the original pin."""
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
            with pytest.raises(pinning.UnifiCertificateMismatchError, match="does NOT match"):
                await client.get(f"https://127.0.0.1:{port}/")
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
    pinning._pin_or_verify("unifi.example:443", "a" * 64, path)
    data = json.loads(path.read_text())
    assert data == {"unifi.example:443": "a" * 64}


def test_pin_or_verify_matching_fingerprint_is_a_noop(tmp_path):
    path = tmp_path / "known_hosts.json"
    pinning._pin_or_verify("unifi.example:443", "a" * 64, path)
    # Should not raise, and should not rewrite anything.
    pinning._pin_or_verify("unifi.example:443", "a" * 64, path)
    data = json.loads(path.read_text())
    assert data == {"unifi.example:443": "a" * 64}


def test_pin_or_verify_mismatch_raises_and_does_not_repin(tmp_path):
    path = tmp_path / "known_hosts.json"
    pinning._pin_or_verify("unifi.example:443", "a" * 64, path)

    with pytest.raises(pinning.UnifiCertificateMismatchError, match="does NOT match"):
        pinning._pin_or_verify("unifi.example:443", "b" * 64, path)

    # Must still hold the original pin, not the mismatched one.
    data = json.loads(path.read_text())
    assert data == {"unifi.example:443": "a" * 64}


def test_pin_or_verify_error_message_explains_how_to_re_trust(tmp_path):
    path = tmp_path / "known_hosts.json"
    pinning._pin_or_verify("unifi.example:443", "a" * 64, path)
    with pytest.raises(pinning.UnifiCertificateMismatchError) as excinfo:
        pinning._pin_or_verify("unifi.example:443", "b" * 64, path)
    message = str(excinfo.value)
    assert "unifi.example:443" in message
    assert str(path) in message
    assert "reconnect" in message.lower() or "re-pin" in message.lower()
