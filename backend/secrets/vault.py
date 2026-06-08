import json
import pathlib

from cryptography.fernet import Fernet

VAULT_PATH = pathlib.Path("nexus.vault")
KEY_PATH   = pathlib.Path(".vault.key")

def _load_fernet() -> Fernet:
    if not KEY_PATH.exists():
        raise RuntimeError(".vault.key not found. Run setup.ps1 first.")
    return Fernet(KEY_PATH.read_bytes())

def get_secret(key: str) -> str:
    vault = json.loads(VAULT_PATH.read_text()) if VAULT_PATH.exists() else {}
    if key not in vault:
        raise KeyError(f"Secret '{key}' not in vault")
    return _load_fernet().decrypt(vault[key].encode()).decode()

def set_secret(key: str, value: str) -> None:
    vault = json.loads(VAULT_PATH.read_text()) if VAULT_PATH.exists() else {}
    vault[key] = _load_fernet().encrypt(value.encode()).decode()
    VAULT_PATH.write_text(json.dumps(vault, indent=2))

def delete_secret(key: str) -> None:
    vault = json.loads(VAULT_PATH.read_text()) if VAULT_PATH.exists() else {}
    vault.pop(key, None)
    VAULT_PATH.write_text(json.dumps(vault, indent=2))

def list_keys() -> list:
    vault = json.loads(VAULT_PATH.read_text()) if VAULT_PATH.exists() else {}
    return list(vault.keys())
