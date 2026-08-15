"""Tests for backend/integrations/lxc_host.py -- the SSH restart path to the
LXC instance (2026-08-15). Mirrors tests/test_unraid.py's SSH prune tests,
same mocking pattern (paramiko is never touched for real)."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _ssh_client(exit_status: int, stdout_text: str = "", stderr_text: str = ""):
    """Mock a paramiko.SSHClient instance -- see test_unraid.py's identical
    helper for the full reasoning on why .read() uses side_effect, not a
    fixed return_value (lxc_host._drain() loops to real EOF)."""
    client = MagicMock()
    stdout_mock = MagicMock()
    stdout_mock.read.side_effect = [stdout_text.encode("utf-8"), b""] + [b""] * 8
    stdout_mock.channel.recv_exit_status.return_value = exit_status
    stderr_mock = MagicMock()
    stderr_mock.read.side_effect = [stderr_text.encode("utf-8"), b""] + [b""] * 8
    stdin_mock = MagicMock()
    client.exec_command.return_value = (stdin_mock, stdout_mock, stderr_mock)
    return client


def _fake_settings(**overrides):
    settings = MagicMock()
    settings.lxc_ssh_private_key = "FAKE-PEM-KEY-MATERIAL"
    settings.lxc_ssh_host = "192.168.1.62"
    settings.lxc_ssh_user = "root"
    settings.lxc_ssh_port = 22
    settings.lxc_ssh_restart_timeout_s = 120
    for k, v in overrides.items():
        setattr(settings, k, v)
    return settings


@pytest.mark.asyncio
async def test_restart_nexus_services_success():
    client = _ssh_client(0, "", "")
    with patch("backend.integrations.lxc_host.paramiko.SSHClient", return_value=client), \
         patch("backend.integrations.lxc_host.paramiko.Ed25519Key.from_private_key"), \
         patch("backend.config.get_settings", return_value=_fake_settings()):
        from backend.integrations.lxc_host import restart_nexus_services
        result = await restart_nexus_services()

    assert result == {"ok": True, "restarted": True}


@pytest.mark.asyncio
async def test_restart_nexus_services_nonzero_exit_raises_with_stderr():
    client = _ssh_client(1, "", "no such command: nexus-lxc-restart")
    with patch("backend.integrations.lxc_host.paramiko.SSHClient", return_value=client), \
         patch("backend.integrations.lxc_host.paramiko.Ed25519Key.from_private_key"), \
         patch("backend.config.get_settings", return_value=_fake_settings()):
        from backend.integrations.lxc_host import restart_nexus_services
        with pytest.raises(RuntimeError, match="no such command: nexus-lxc-restart"):
            await restart_nexus_services()


@pytest.mark.asyncio
async def test_restart_nexus_services_missing_secret_never_connects():
    """LXC_SSH_PRIVATE_KEY missing -> RuntimeError before any network
    attempt; paramiko.SSHClient must never be instantiated."""

    class _FakeSettingsNoKey:
        @property
        def lxc_ssh_private_key(self):
            raise KeyError("LXC_SSH_PRIVATE_KEY")

        lxc_ssh_host = "192.168.1.62"
        lxc_ssh_user = "root"
        lxc_ssh_port = 22
        lxc_ssh_restart_timeout_s = 120

    with patch("backend.integrations.lxc_host.paramiko.SSHClient") as mock_ssh_cls, \
         patch("backend.config.get_settings", return_value=_FakeSettingsNoKey()):
        from backend.integrations.lxc_host import restart_nexus_services
        with pytest.raises(RuntimeError, match="LXC_SSH_PRIVATE_KEY not configured"):
            await restart_nexus_services()

    mock_ssh_cls.assert_not_called()


@pytest.mark.asyncio
async def test_restart_nexus_services_missing_host_never_connects():
    with patch("backend.integrations.lxc_host.paramiko.SSHClient") as mock_ssh_cls, \
         patch("backend.config.get_settings", return_value=_fake_settings(lxc_ssh_host="")):
        from backend.integrations.lxc_host import restart_nexus_services
        with pytest.raises(RuntimeError, match="LXC_SSH_HOST not configured"):
            await restart_nexus_services()

    mock_ssh_cls.assert_not_called()


@pytest.mark.asyncio
async def test_restart_nexus_services_sends_sentinel_not_real_command():
    """The exact string sent to exec_command is the sentinel, never the real
    systemctl command -- the server-side forced command maps it, not this
    code. If this ever sent the real command directly, a weakened/removed
    forced-command restriction on the LXC would become an unrestricted
    arbitrary-command channel."""
    client = _ssh_client(0, "", "")
    with patch("backend.integrations.lxc_host.paramiko.SSHClient", return_value=client), \
         patch("backend.integrations.lxc_host.paramiko.Ed25519Key.from_private_key"), \
         patch("backend.config.get_settings", return_value=_fake_settings()):
        from backend.integrations.lxc_host import restart_nexus_services
        await restart_nexus_services()

    sent_command = client.exec_command.call_args.args[0]
    assert sent_command == "nexus-lxc-restart"
    assert "systemctl" not in sent_command


@pytest.mark.asyncio
async def test_restart_nexus_services_ssh_exception_raises_and_still_closes():
    import paramiko as _paramiko

    client = _ssh_client(0)
    client.exec_command.side_effect = _paramiko.SSHException("connection dropped")
    with patch("backend.integrations.lxc_host.paramiko.SSHClient", return_value=client), \
         patch("backend.integrations.lxc_host.paramiko.Ed25519Key.from_private_key"), \
         patch("backend.config.get_settings", return_value=_fake_settings()):
        from backend.integrations.lxc_host import restart_nexus_services
        with pytest.raises(RuntimeError):
            await restart_nexus_services()

    client.close.assert_called_once()


@pytest.mark.asyncio
async def test_restart_nexus_services_creates_known_hosts_file_if_absent(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    known_hosts = tmp_path / ".lxc_ssh_known_hosts"
    assert not known_hosts.exists()

    client = _ssh_client(0, "", "")
    with patch("backend.integrations.lxc_host.paramiko.SSHClient", return_value=client), \
         patch("backend.integrations.lxc_host.paramiko.Ed25519Key.from_private_key"), \
         patch("backend.config.get_settings", return_value=_fake_settings()):
        from backend.integrations.lxc_host import restart_nexus_services
        await restart_nexus_services()

    assert known_hosts.exists()


@pytest.mark.asyncio
async def test_restart_nexus_services_blocking_call_goes_through_to_thread():
    """restart_nexus_services must run the blocking paramiko call via
    asyncio.to_thread, never inline on the event loop."""
    with patch(
        "backend.integrations.lxc_host.asyncio.to_thread",
        new_callable=AsyncMock,
        return_value=(0, "", ""),
    ) as mock_to_thread:
        from backend.integrations import lxc_host
        result = await lxc_host.restart_nexus_services()

    assert mock_to_thread.await_args.args[0] is lxc_host._ssh_restart_sync
    assert result == {"ok": True, "restarted": True}
