import os

from .vault import get_secret as _vault_get


def get_secret(key: str, fallback_env: bool = True) -> str:
    try:
        return _vault_get(key)
    except (KeyError, RuntimeError):
        if fallback_env and key in os.environ:
            return os.environ[key]
        raise
