import stat
from unittest.mock import patch


def test_secure_key_file_noop_when_missing(tmp_path, monkeypatch):
    import backend.secrets.vault as vault
    monkeypatch.setattr(vault, "KEY_PATH", tmp_path / "absent.key")
    # Must not raise when the key file does not exist.
    vault.secure_key_file()


def test_secure_key_file_sets_0600_on_posix(tmp_path, monkeypatch):
    import backend.secrets.vault as vault
    key = tmp_path / ".vault.key"
    key.write_text("secret-key-bytes")
    key.chmod(0o644)
    monkeypatch.setattr(vault, "KEY_PATH", key)

    vault.secure_key_file()

    mode = stat.S_IMODE(key.stat().st_mode)
    assert mode == 0o600


def test_secure_key_file_never_raises_on_error(tmp_path, monkeypatch):
    import backend.secrets.vault as vault
    key = tmp_path / ".vault.key"
    key.write_text("x")
    monkeypatch.setattr(vault, "KEY_PATH", key)
    # Force the underlying op to blow up; secure_key_file must swallow it.
    with patch("os.chmod", side_effect=OSError("boom")):
        vault.secure_key_file()  # must not raise
