import json
import logging
import os
import pathlib
import stat
from datetime import datetime

from cryptography.fernet import Fernet

logger = logging.getLogger(__name__)

VAULT_PATH = pathlib.Path("nexus.vault")
KEY_PATH   = pathlib.Path(".vault.key")
META_PATH  = pathlib.Path("nexus.vault.meta")


def secure_key_file() -> None:
    """Best-effort: restrict .vault.key so only the current user can read it.

    The key decrypts every secret in the vault, so it must not be world-readable.
    POSIX -> chmod 0600. Windows -> icacls: drop inherited ACEs and grant only the
    current user. Never raises — a permissions failure must not block startup."""
    if not KEY_PATH.exists():
        return
    try:
        if os.name == "nt":
            import getpass
            import subprocess
            user = os.environ.get("USERNAME") or getpass.getuser()
            # Grants only the current user. NEXUS runs as the logged-in user (tray
            # via HKCU Run key), so this can't lock itself out. If NEXUS ever runs as
            # a Windows service (LocalSystem), add "SYSTEM:F" to the grant list too.
            subprocess.run(
                ["icacls", str(KEY_PATH), "/inheritance:r", "/grant:r", f"{user}:F"],
                check=False, capture_output=True,
            )
        else:
            os.chmod(KEY_PATH, stat.S_IRUSR | stat.S_IWUSR)  # 0600
    except Exception as e:
        logger.warning(f"Could not harden .vault.key permissions: {e}")


def read_meta() -> dict:
    """Non-secret metadata (set/rotation timestamps). Values-free, safe to track."""
    return json.loads(META_PATH.read_text()) if META_PATH.exists() else {}


def _stamp_meta(key: str) -> None:
    """Record when a secret was last set/rotated. Setting a value over an existing
    one is a rotation, so both timestamps move together — matching tools/rotate_secret.py."""
    meta = read_meta()
    entry = meta.get(key) or {}
    now = datetime.utcnow().isoformat()
    entry["last_set"] = now
    entry["last_rotated"] = now
    meta[key] = entry
    META_PATH.write_text(json.dumps(meta, indent=2))

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
    _stamp_meta(key)

def delete_secret(key: str) -> None:
    vault = json.loads(VAULT_PATH.read_text()) if VAULT_PATH.exists() else {}
    vault.pop(key, None)
    VAULT_PATH.write_text(json.dumps(vault, indent=2))

def list_keys() -> list:
    vault = json.loads(VAULT_PATH.read_text()) if VAULT_PATH.exists() else {}
    return list(vault.keys())
