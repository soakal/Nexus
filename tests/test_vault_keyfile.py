import os
import stat
from unittest.mock import patch

import pytest


def test_secure_key_file_noop_when_missing(tmp_path, monkeypatch):
    import backend.secrets.vault as vault
    monkeypatch.setattr(vault, "KEY_PATH", tmp_path / "absent.key")
    # Must not raise when the key file does not exist.
    vault.secure_key_file()


@pytest.mark.skipif(os.name == "nt", reason="POSIX chmod path")
def test_secure_key_file_sets_0600_on_posix(tmp_path, monkeypatch):
    import backend.secrets.vault as vault
    key = tmp_path / ".vault.key"
    key.write_text("secret-key-bytes")
    key.chmod(0o644)
    monkeypatch.setattr(vault, "KEY_PATH", key)

    vault.secure_key_file()

    mode = stat.S_IMODE(key.stat().st_mode)
    assert mode == 0o600


@pytest.mark.skipif(os.name != "nt", reason="Windows icacls path")
def test_secure_key_file_invokes_icacls_on_windows(tmp_path, monkeypatch):
    import backend.secrets.vault as vault
    key = tmp_path / ".vault.key"
    key.write_text("secret-key-bytes")
    monkeypatch.setattr(vault, "KEY_PATH", key)

    with patch("subprocess.run") as mock_run:
        vault.secure_key_file()
        mock_run.assert_called_once()
        args = mock_run.call_args[0][0]
        assert args[0] == "icacls"
        assert "/inheritance:r" in args


def test_secure_key_file_never_raises_on_error(tmp_path, monkeypatch):
    import backend.secrets.vault as vault
    key = tmp_path / ".vault.key"
    key.write_text("x")
    monkeypatch.setattr(vault, "KEY_PATH", key)
    # Force the underlying op to blow up; secure_key_file must swallow it.
    if os.name == "nt":
        ctx = patch("subprocess.run", side_effect=OSError("boom"))
    else:
        ctx = patch("os.chmod", side_effect=OSError("boom"))
    with ctx:
        vault.secure_key_file()  # must not raise
