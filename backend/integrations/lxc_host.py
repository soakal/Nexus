"""Native NEXUS-restart-on-the-LXC path via SSH (2026-08-15).

Lets Windows's Telegram poller restart the LXC instance's `nexus-backend`/
`nexus-frontend` systemd units remotely (`/restart lxc`) -- deliberately SSH,
not an authenticated HTTP call to the LXC's own :8000: the restart channel
must not depend on the process being restarted, and sshd is independent of
NEXUS entirely. Same hardened forced-command SSH pattern as
`backend/integrations/unraid.py`'s Phase 7d docker-prune path -- see that
module's own docstring for the full reasoning (TOFU host-key pinning,
Ed25519-from-memory, sentinel string mapped server-side, never a real
command sent over the wire).

The actual shell command (`systemctl restart nexus-backend nexus-frontend`)
is fixed SERVER-SIDE by an SSH forced-command restriction in the LXC's own
`authorized_keys` (out of scope for this codebase -- installed manually on
the LXC). `_RESTART_SENTINEL` below is intentionally NOT a real, runnable
command: this code sends a fixed sentinel string over SSH and relies
entirely on the server-side forced-command mapping it to the real restart.
If that server-side restriction is ever weakened or removed, this fails
LOUDLY ("command not found") instead of silently becoming an unrestricted
arbitrary-command channel.
"""
import asyncio
import io
import logging
import pathlib

import paramiko

logger = logging.getLogger(__name__)

_RESTART_SENTINEL = "nexus-lxc-restart"          # NOT the real command -- see module docstring
_SSH_KNOWN_HOSTS = pathlib.Path(".lxc_ssh_known_hosts")
_MAX_SSH_OUTPUT_BYTES = 65536


def _drain(stream) -> str:
    """Read a paramiko ChannelFile to genuine EOF, keeping only the first
    _MAX_SSH_OUTPUT_BYTES. Must fully drain (not just bounded-read) so the
    channel window empties and recv_exit_status() can't hang."""
    chunks = []
    total = 0
    while True:
        chunk = stream.read(_MAX_SSH_OUTPUT_BYTES)
        if not chunk:
            break
        if total < _MAX_SSH_OUTPUT_BYTES:
            chunks.append(chunk)
            total += len(chunk)
    return b"".join(chunks)[:_MAX_SSH_OUTPUT_BYTES].decode("utf-8", errors="replace")


def _ssh_restart_sync() -> tuple[int, str, str]:
    """Blocking, synchronous SSH call -- run ONLY via asyncio.to_thread (never
    call this directly from a coroutine; paramiko has no async API).

    Returns (exit_status, stdout_text, stderr_text). Raises RuntimeError if
    LXC_SSH_PRIVATE_KEY or lxc_ssh_host isn't configured; any paramiko
    exception (AuthenticationException, SSHException, BadHostKeyException,
    socket errors, timeout) propagates as-is -- the caller
    (restart_nexus_services) converts it to a RuntimeError.
    """
    from backend.config import get_settings
    settings = get_settings()

    try:
        pem = settings.lxc_ssh_private_key
    except Exception:
        raise RuntimeError("LXC_SSH_PRIVATE_KEY not configured")

    host = settings.lxc_ssh_host
    if not host:
        raise RuntimeError("LXC_SSH_HOST not configured")
    port = settings.lxc_ssh_port
    user = settings.lxc_ssh_user

    # Ed25519, not RSA, loaded from an in-memory string (io.StringIO), never a
    # file path -- the key material lives only in the vault, never touches disk.
    key = paramiko.Ed25519Key.from_private_key(io.StringIO(pem))

    client = paramiko.SSHClient()
    # The file must exist BEFORE any host-key API call -- see unraid.py's
    # identical comment: AutoAddPolicy.missing_host_key() -> save_host_keys()
    # -> load_host_keys() -> a plain open(filename, "r") that raises
    # FileNotFoundError on a file that's never existed.
    _SSH_KNOWN_HOSTS.touch(exist_ok=True)
    try:
        client.load_host_keys(str(_SSH_KNOWN_HOSTS))
    except Exception:
        if _SSH_KNOWN_HOSTS.stat().st_size > 0:
            logger.warning("Could not load %s (exists but unreadable) -- treating as first-use", _SSH_KNOWN_HOSTS)
    # TOFU (trust-on-first-use), NOT the same as disabling host-key checking --
    # see unraid.py's identical comment for why this must never be
    # "simplified" into StrictHostKeyChecking=no / paramiko.WarningPolicy.
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    try:
        client.connect(
            host,
            port=port,
            username=user,
            pkey=key,
            look_for_keys=False,
            allow_agent=False,
            timeout=10,
            banner_timeout=10,
            auth_timeout=10,
        )
        client.save_host_keys(str(_SSH_KNOWN_HOSTS))

        # systemctl restart is synchronous -- exit 0 means the units genuinely
        # restarted by the time this call returns, not fire-and-forget.
        # Restarting nexus-backend/nexus-frontend does not kill sshd or this
        # SSH session.
        stdin, stdout, stderr = client.exec_command(
            _RESTART_SENTINEL, timeout=settings.lxc_ssh_restart_timeout_s
        )
        out = _drain(stdout)
        err = _drain(stderr)
        exit_status = stdout.channel.recv_exit_status()
    finally:
        client.close()

    return exit_status, out, err


async def restart_nexus_services() -> dict:
    """Restart the LXC's nexus-backend + nexus-frontend systemd units over SSH.

    Runs the blocking paramiko call in a thread (asyncio.to_thread) since
    paramiko has no async API. Returns {"ok": True, "restarted": True} on
    success.

    Raises RuntimeError on any failure -- a non-zero remote exit status, or any
    SSH-layer exception (auth failure, connection drop, timeout, bad host key).
    The "not configured" RuntimeErrors from _ssh_restart_sync propagate as-is
    (never double-wrapped); any other exception is re-raised with context.
    """
    try:
        exit_status, out, err = await asyncio.to_thread(_ssh_restart_sync)
    except RuntimeError as e:
        if "not configured" in str(e):
            raise
        raise RuntimeError(f"LXC restart via SSH failed: {e}")
    except Exception as e:
        raise RuntimeError(f"LXC restart via SSH failed: {e}")

    if exit_status != 0:
        raise RuntimeError(f"LXC restart failed (exit {exit_status}): {err.strip()[:500]}")

    return {"ok": True, "restarted": True}
