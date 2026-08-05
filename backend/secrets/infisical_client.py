"""Infisical-backed secret store — same function surface as vault.py so it's a
drop-in behind backend/secrets/manager.py's routing seam.

Wire contract verified live against a real self-hosted Infisical instance
(v0.162.13) before writing this: Universal Auth login, raw-secret CRUD at
/api/v3/secrets/raw/{name}, bulk recursive read at /api/v3/secrets/raw, and
folder creation at /api/v1/folders (NOT auto-created — a secret write into a
folder that doesn't exist yet 404s, and re-creating an existing folder 400s).

cred:<service>:<field> keys map to folder /creds/<service>, secret name
FIELD (uppercased) — see _vault_key_to_path_and_name / _secret_path_to_vault_key.

Reads are served from an in-memory cache (module-level, thread-safe) refreshed
on a TTL by a background daemon thread — get_secret is called from Settings
properties on the request path, so it must never block on network I/O once
warmed up. On a refresh failure the existing cache is served indefinitely
(stale-serve); a cold cache (never fetched) falls through to one synchronous
fetch. After a failed fetch, further synchronous cold-cache attempts are
suppressed for FETCH_FAILURE_COOLDOWN_S (fast-fail RuntimeError, so manager.py
falls through to the legacy vault immediately instead of re-timing-out on
every distinct secret key during a startup burst) — the background refresh
loop is unaffected and remains the real recovery-detection mechanism, retrying
on its own cadence regardless of the cooldown. No secret value is ever written
to disk or logged.
"""
import logging
import re
import threading
import time
from datetime import datetime, timezone

import httpx

from backend.http_client import SSL_CONTEXT

logger = logging.getLogger(__name__)

_SERVICE_NAME_RE = re.compile(r"^[a-z0-9_-]+$")

# Suppresses the synchronous cold-cache re-fetch storm after a failure (e.g.
# a dozen+ Settings properties each triggering their own failed network
# round-trip during startup) — NOT applied to warm_up()/the background
# refresh loop, which always get their own real attempt.
FETCH_FAILURE_COOLDOWN_S = 30.0

_lock = threading.Lock()
_cache: dict = {}
_meta: dict = {}
_known_folders: set = set()
_token: str | None = None
_last_fetch_ok = False
_last_fetch_failed_at: float | None = None
_refresh_thread_started = False
_monotonic = time.monotonic  # test seam


def _settings():
    from backend.config import get_settings
    return get_settings()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _login() -> str:
    global _token
    s = _settings()
    with httpx.Client(verify=SSL_CONTEXT, base_url=s.infisical_url, timeout=10.0) as c:
        resp = c.post(
            "/api/v1/auth/universal-auth/login",
            json={"clientId": s.infisical_client_id, "clientSecret": s.infisical_client_secret},
        )
        resp.raise_for_status()
        _token = resp.json()["accessToken"]
    return _token


def _request(method: str, path: str, **kwargs) -> httpx.Response:
    global _token
    s = _settings()
    with httpx.Client(verify=SSL_CONTEXT, base_url=s.infisical_url, timeout=10.0) as c:
        if _token is None:
            _login()
        headers = kwargs.pop("headers", {}) or {}
        headers["Authorization"] = f"Bearer {_token}"
        resp = c.request(method, path, headers=headers, **kwargs)
        if resp.status_code == 401:
            _login()
            headers["Authorization"] = f"Bearer {_token}"
            resp = c.request(method, path, headers=headers, **kwargs)
        resp.raise_for_status()
        return resp


def _secret_path_to_vault_key(secret_path: str, secret_key: str):
    secret_path = secret_path.rstrip("/") or "/"
    if secret_path == "/":
        return secret_key
    parts = [p for p in secret_path.split("/") if p]
    if len(parts) == 2 and parts[0] == "creds":
        return f"cred:{parts[1]}:{secret_key.lower()}"
    return None  # outside our known structure — ignore


def _vault_key_to_path_and_name(key: str):
    if key.startswith("cred:"):
        parts = key.split(":", 2)
        if len(parts) != 3:
            raise ValueError(f"Malformed credential key: {key!r}")
        _, service, field = parts
        service = service.lower()
        if not _SERVICE_NAME_RE.match(service):
            raise ValueError(f"Invalid credential service name: {service!r}")
        return f"/creds/{service}", field.upper()
    return "/", key


def _ensure_folder(path: str) -> None:
    """Create the folder for a cred: path if it doesn't exist yet. Idempotent —
    Infisical 400s on a duplicate folder name, which we treat as success."""
    if path == "/" or path in _known_folders:
        return
    s = _settings()
    parent, _, name = path.rpartition("/")
    parent = parent or "/"
    try:
        _request(
            "POST", "/api/v1/folders",
            json={"workspaceId": s.infisical_project_id, "environment": s.infisical_env,
                  "name": name, "path": parent},
        )
    except httpx.HTTPStatusError as e:
        if e.response.status_code != 400:
            raise
    _known_folders.add(path)


def _bulk_fetch() -> bool:
    """Refresh the in-memory cache from Infisical. Leaves the existing cache
    untouched on failure (stale-serve). Returns True on success. Always makes
    a real network attempt — the fetch-failure cooldown is enforced only by
    get_secret()'s cold-cache path, never here, so warm_up() and the
    background refresh loop always get their own real attempt."""
    global _cache, _meta, _last_fetch_ok, _last_fetch_failed_at
    s = _settings()
    try:
        resp = _request(
            "GET", "/api/v3/secrets/raw",
            params={"workspaceId": s.infisical_project_id, "environment": s.infisical_env,
                    "secretPath": "/", "recursive": "true"},
        )
        data = resp.json()
        new_cache: dict = {}
        new_meta: dict = {}
        folders_seen: set = set()
        for entry in data.get("secrets", []):
            secret_path = entry.get("secretPath", "/")
            if secret_path != "/":
                folders_seen.add(secret_path.rstrip("/"))
            vault_key = _secret_path_to_vault_key(secret_path, entry["secretKey"])
            if vault_key is None:
                continue
            new_cache[vault_key] = entry["secretValue"]
            updated = entry.get("updatedAt") or entry.get("createdAt")
            new_meta[vault_key] = {"last_set": updated, "last_rotated": updated}
        with _lock:
            _cache = new_cache
            _meta = new_meta
            _known_folders.update(folders_seen)
        _last_fetch_ok = True
        _last_fetch_failed_at = None
        return True
    except Exception as e:
        logger.warning(f"Infisical bulk fetch failed, serving stale cache: {e}")
        _last_fetch_ok = False
        _last_fetch_failed_at = _monotonic()
        return False


def _refresh_loop() -> None:
    while True:
        time.sleep(max(5, _settings().infisical_cache_ttl_s))
        _bulk_fetch()


def warm_up() -> bool:
    """Initial fetch + start the background refresh thread. Never raises."""
    global _refresh_thread_started
    try:
        ok = _bulk_fetch()
    except Exception as e:
        logger.warning(f"Infisical warm_up failed: {e}")
        ok = False
    with _lock:
        if not _refresh_thread_started:
            threading.Thread(target=_refresh_loop, name="infisical-refresh", daemon=True).start()
            _refresh_thread_started = True
    return ok


def get_secret(key: str) -> str:
    with _lock:
        has_cache = bool(_cache)
        failed_at = _last_fetch_failed_at
        if key in _cache:
            return _cache[key]
    if not has_cache:
        if failed_at is not None and _monotonic() - failed_at < FETCH_FAILURE_COOLDOWN_S:
            raise RuntimeError(
                "Infisical recently unreachable (fetch-failure cooldown active) "
                "and no cached secrets available"
            )
        if not _bulk_fetch():
            raise RuntimeError("Infisical unreachable and no cached secrets available")
        with _lock:
            if key in _cache:
                return _cache[key]
    raise KeyError(f"Secret '{key}' not in Infisical project")


def set_secret(key: str, value: str) -> None:
    path, name = _vault_key_to_path_and_name(key)
    _ensure_folder(path)
    s = _settings()
    body = {"workspaceId": s.infisical_project_id, "environment": s.infisical_env,
             "secretPath": path, "secretValue": value}
    with _lock:
        exists = key in _cache
    url = f"/api/v3/secrets/raw/{name}"
    if exists:
        _request("PATCH", url, json=body)
    else:
        try:
            _request("POST", url, json=body)
        except httpx.HTTPStatusError as e:
            if e.response.status_code in (400, 409):
                _request("PATCH", url, json=body)
            else:
                raise
    with _lock:
        _cache[key] = value
        _meta[key] = {"last_set": _now_iso(), "last_rotated": _now_iso()}


def delete_secret(key: str) -> None:
    """No-op (mirrors vault.py's dict.pop(key, None)) if the key isn't present."""
    path, name = _vault_key_to_path_and_name(key)
    s = _settings()
    try:
        _request(
            "DELETE", f"/api/v3/secrets/raw/{name}",
            json={"workspaceId": s.infisical_project_id, "environment": s.infisical_env, "secretPath": path},
        )
    except httpx.HTTPStatusError as e:
        if e.response.status_code != 404:
            raise
    with _lock:
        _cache.pop(key, None)
        _meta.pop(key, None)


def list_keys() -> list:
    with _lock:
        return list(_cache.keys())


def read_meta() -> dict:
    with _lock:
        return dict(_meta)


# ── Credential helpers (namespaced cred:<service>:<field> keys) ──────────────

def list_credentials() -> dict:
    """Return {service: {host, user, port, has_password}} — never includes password values."""
    with _lock:
        keys = list(_cache.keys())
    result: dict = {}
    for raw_key in keys:
        if not raw_key.startswith("cred:"):
            continue
        parts = raw_key.split(":", 2)
        if len(parts) != 3:
            continue
        _, service, field = parts
        if service not in result:
            result[service] = {"host": None, "user": None, "port": None, "has_password": False}
        if field == "password":
            result[service]["has_password"] = True
        elif field in ("host", "user", "port"):
            with _lock:
                result[service][field] = _cache.get(raw_key)
    return result


def set_credential(service: str, field: str, value: str) -> None:
    set_secret(f"cred:{service}:{field}", value)


def get_credential(service: str) -> dict:
    """Server-side only — returns password in plain text. Never send over API."""
    result = {}
    for field in ("host", "user", "password", "port"):
        try:
            result[field] = get_secret(f"cred:{service}:{field}")
        except KeyError:
            result[field] = None
    return result


def delete_credential(service: str) -> None:
    for field in ("host", "user", "password", "port"):
        delete_secret(f"cred:{service}:{field}")
