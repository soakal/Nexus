#!/usr/bin/env python3
"""Minimal Infisical get/set for devbox-side scripts (cron_run.sh) that can't
import the full `backend` package. Same wire contract as
backend/secrets/infisical_client.py (Universal Auth login, raw-secret CRUD at
/api/v3/secrets/raw/{name}) but standalone -- stdlib + requests only, reads
INFISICAL_* creds straight out of this repo's own .env.

Usage:
    infisical_secret.py get cred:devbox:claude_code_oauth_token
    infisical_secret.py set cred:devbox:claude_code_oauth_token <value>

Key convention (matches infisical_client.py): "cred:<service>:<field>" maps
to folder /creds/<service>, secret name FIELD (uppercased).
"""
import re
import sys
from pathlib import Path

import requests

REPO_DIR = Path(__file__).resolve().parent.parent
_SERVICE_NAME_RE = re.compile(r"^[a-z0-9_-]+$")


def _load_env() -> dict:
    env = {}
    env_path = REPO_DIR / ".env"
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        env[k.strip()] = v.strip()
    return env


def _key_to_path_and_name(key: str):
    if not key.startswith("cred:"):
        raise ValueError(f"expected a 'cred:<service>:<field>' key, got: {key!r}")
    _, service, field = key.split(":", 2)
    service = service.lower()
    if not _SERVICE_NAME_RE.match(service):
        raise ValueError(f"invalid credential service name: {service!r}")
    return f"/creds/{service}", field.upper()


def _login(env: dict) -> str:
    resp = requests.post(
        f"{env['INFISICAL_URL']}/api/v1/auth/universal-auth/login",
        json={"clientId": env["INFISICAL_CLIENT_ID"], "clientSecret": env["INFISICAL_CLIENT_SECRET"]},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()["accessToken"]


def _ensure_folder(env: dict, token: str, path: str) -> None:
    if path == "/":
        return
    parent, _, name = path.rpartition("/")
    parent = parent or "/"
    resp = requests.post(
        f"{env['INFISICAL_URL']}/api/v1/folders",
        headers={"Authorization": f"Bearer {token}"},
        json={"workspaceId": env["INFISICAL_PROJECT_ID"], "environment": env.get("INFISICAL_ENV", "prod"),
              "name": name, "path": parent},
        timeout=10,
    )
    if resp.status_code not in (200, 201, 400):
        resp.raise_for_status()


def cmd_get(key: str) -> int:
    env = _load_env()
    path, name = _key_to_path_and_name(key)
    token = _login(env)
    resp = requests.get(
        f"{env['INFISICAL_URL']}/api/v3/secrets/raw/{name}",
        headers={"Authorization": f"Bearer {token}"},
        params={"workspaceId": env["INFISICAL_PROJECT_ID"], "environment": env.get("INFISICAL_ENV", "prod"),
                "secretPath": path},
        timeout=10,
    )
    if resp.status_code == 404:
        print(f"not found: {key}", file=sys.stderr)
        return 1
    resp.raise_for_status()
    print(resp.json()["secret"]["secretValue"], end="")
    return 0


def cmd_set(key: str, value: str) -> int:
    env = _load_env()
    path, name = _key_to_path_and_name(key)
    token = _login(env)
    _ensure_folder(env, token, path)
    body = {"workspaceId": env["INFISICAL_PROJECT_ID"], "environment": env.get("INFISICAL_ENV", "prod"),
            "secretPath": path, "secretValue": value}
    url = f"{env['INFISICAL_URL']}/api/v3/secrets/raw/{name}"
    resp = requests.post(url, headers={"Authorization": f"Bearer {token}"}, json=body, timeout=10)
    if resp.status_code in (400, 409):
        resp = requests.patch(url, headers={"Authorization": f"Bearer {token}"}, json=body, timeout=10)
    resp.raise_for_status()
    print(f"stored {key}", file=sys.stderr)
    return 0


def main() -> int:
    if len(sys.argv) == 3 and sys.argv[1] == "get":
        return cmd_get(sys.argv[2])
    if len(sys.argv) == 4 and sys.argv[1] == "set":
        return cmd_set(sys.argv[2], sys.argv[3])
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
