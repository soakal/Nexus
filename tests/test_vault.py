import pytest
import json
from pathlib import Path
from cryptography.fernet import Fernet


@pytest.fixture
def vault_dir(tmp_path):
    key = Fernet.generate_key()
    (tmp_path / ".vault.key").write_bytes(key)
    return tmp_path


def test_set_and_get_secret(vault_dir, monkeypatch):
    monkeypatch.chdir(vault_dir)
    import importlib
    import backend.secrets.vault as v
    importlib.reload(v)

    v.set_secret("TEST_KEY", "secret_value_123")
    assert v.get_secret("TEST_KEY") == "secret_value_123"


def test_list_keys(vault_dir, monkeypatch):
    monkeypatch.chdir(vault_dir)
    import importlib
    import backend.secrets.vault as v
    importlib.reload(v)

    v.set_secret("KEY_A", "val_a")
    v.set_secret("KEY_B", "val_b")
    keys = v.list_keys()
    assert "KEY_A" in keys
    assert "KEY_B" in keys


def test_delete_secret(vault_dir, monkeypatch):
    monkeypatch.chdir(vault_dir)
    import importlib
    import backend.secrets.vault as v
    importlib.reload(v)

    v.set_secret("DELETE_ME", "value")
    v.delete_secret("DELETE_ME")
    assert "DELETE_ME" not in v.list_keys()


def test_missing_vault_key(vault_dir, monkeypatch):
    monkeypatch.chdir(vault_dir)
    import importlib
    import backend.secrets.vault as v
    importlib.reload(v)
    # First write a secret so the vault file exists with an entry
    v.set_secret("TEMP_KEY", "some_value")
    # Now remove the vault key — decryption should fail
    (vault_dir / ".vault.key").unlink()
    importlib.reload(v)

    with pytest.raises(RuntimeError, match=".vault.key not found"):
        v.get_secret("TEMP_KEY")


def test_missing_secret_raises_key_error(vault_dir, monkeypatch):
    monkeypatch.chdir(vault_dir)
    import importlib
    import backend.secrets.vault as v
    importlib.reload(v)

    with pytest.raises(KeyError):
        v.get_secret("NONEXISTENT")


def test_vault_is_encrypted(vault_dir, monkeypatch):
    monkeypatch.chdir(vault_dir)
    import importlib
    import backend.secrets.vault as v
    importlib.reload(v)

    v.set_secret("MY_SECRET", "plaintext_value")
    vault_content = (vault_dir / "nexus.vault").read_text()
    assert "plaintext_value" not in vault_content
    data = json.loads(vault_content)
    assert "MY_SECRET" in data
    # Value should be encrypted (not the plaintext)
    assert data["MY_SECRET"] != "plaintext_value"
