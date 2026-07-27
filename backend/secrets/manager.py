"""Routing seam between callers and the active secret backend.

secrets_backend = "auto" (default) picks infisical iff all four INFISICAL_*
settings are non-empty, else legacy vault. Force a specific backend with
secrets_backend = "infisical" | "vault" for testing/rollback.

get_secret falls back infisical -> legacy vault -> os.environ, matching the
transition-window design (see plan D6): a key missing from Infisical but
still present in the not-yet-retired vault keeps working, logged so the
14-day soak has a concrete signal to watch. Writes are active-backend-only —
no dual-write, since drift between two stores is worse than re-running the
one-time migration script.
"""
import logging
import os

logger = logging.getLogger(__name__)


def _active_backend_name() -> str:
    from backend.config import get_settings
    s = get_settings()
    mode = getattr(s, "secrets_backend", "auto")
    if mode in ("infisical", "vault"):
        return mode
    # auto
    if s.infisical_url and s.infisical_client_id and s.infisical_client_secret and s.infisical_project_id:
        return "infisical"
    return "vault"


def _backend():
    if _active_backend_name() == "infisical":
        from . import infisical_client
        return infisical_client
    from . import vault
    return vault


def get_secret(key: str, fallback_env: bool = True) -> str:
    backend = _backend()
    last_error: Exception | None = None
    try:
        return backend.get_secret(key)
    except (KeyError, RuntimeError) as e:
        # A RuntimeError here is a backend FAILURE (unreachable Infisical, bad
        # machine-identity creds, undecryptable vault) — not a missing key. It
        # must not vanish into an indistinguishable "not found" at the end.
        last_error = e
        if isinstance(e, RuntimeError):
            logger.warning(f"secret backend '{backend.__name__}' failed for '{key}': {e}")

    if backend.__name__.endswith("infisical_client"):
        from . import vault
        try:
            value = vault.get_secret(key)
            logger.warning(f"secret '{key}' served from legacy vault fallback")
            return value
        except (KeyError, RuntimeError) as e:
            last_error = e
            if isinstance(e, RuntimeError):
                logger.warning(f"legacy vault fallback failed for '{key}': {e}")

    if fallback_env and key in os.environ:
        return os.environ[key]
    raise KeyError(f"Secret '{key}' not found in any backend") from last_error


def set_secret(key: str, value: str) -> None:
    _backend().set_secret(key, value)


def delete_secret(key: str) -> None:
    _backend().delete_secret(key)


def list_keys() -> list:
    return _backend().list_keys()


def read_meta() -> dict:
    return _backend().read_meta()


def list_credentials() -> dict:
    return _backend().list_credentials()


def set_credential(service: str, field: str, value: str) -> None:
    _backend().set_credential(service, field, value)


def get_credential(service: str) -> dict:
    return _backend().get_credential(service)


def delete_credential(service: str) -> None:
    _backend().delete_credential(service)


def warm_up() -> bool:
    backend = _backend()
    warm = getattr(backend, "warm_up", None)
    if warm is None:
        return True  # vault backend has no warm-up concept
    return warm()
