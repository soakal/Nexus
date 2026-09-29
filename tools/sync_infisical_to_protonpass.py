#!/usr/bin/env python3
"""Weekly one-way export of every Infisical secret into individual Proton
Pass items in the Break-Glass vault -- one item per credential/key, so the
vault is browsable/searchable like a normal password manager instead of one
giant text blob.

cred:<service>:<field> keys (host/user/password/port) become a Login item
per service. Every other (flat) key becomes a Secure Note item.

This script treats the Break-Glass vault as fully owned by this sync: on
every run it trashes EVERY active item currently in the vault, then
recreates the full current set from Infisical. Do not store anything else
in that vault manually -- it will be trashed on the next run (recoverable
from Proton's own trash, but still).

Read-only against Infisical. Auth: relies on an already-active `pass-cli`
session (logged in via `pass-cli login` as the real account -- Proton's
Personal Access Tokens are read-only and cannot create/edit items, so no
token login is attempted here).

Never prints a secret value under any code path -- only counts/timestamps.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

NEXUS_ROOT = Path(__file__).resolve().parent.parent
WORKDIR = Path("/var/lib/nexus")
VAULT_NAME = "Break-Glass"
REASON = "weekly automated break-glass backup sync"

sys.path.insert(0, str(NEXUS_ROOT))
os.chdir(WORKDIR)  # infisical_client's Settings reads .env relative to cwd

from backend.secrets import infisical_client as ic  # noqa: E402


def export_infisical():
    ic.warm_up()
    keys = sorted(ic.list_keys())
    if not keys:
        raise RuntimeError("Infisical cache empty after warm_up() -- refusing to sync an empty export")

    creds: dict[str, dict] = {}
    flat: dict[str, str] = {}
    for k in keys:
        if k.startswith("cred:"):
            parts = k.split(":", 2)
            if len(parts) == 3:
                _, service, field = parts
                creds.setdefault(service, {})[field] = ic.get_secret(k)
                continue
        flat[k] = ic.get_secret(k)
    return creds, flat


def run_pass_cli(args, env, input_text=None, check=True):
    result = subprocess.run(
        ["pass-cli", *args],
        env=env,
        input=input_text,
        capture_output=True,
        text=True,
    )
    if check and result.returncode != 0:
        # pass-cli's own stderr never echoes secret values back -- it's
        # operation-status text ("NotAllowed", rate limit messages, etc) --
        # so this is safe to surface, unlike the item payloads themselves.
        raise RuntimeError(
            f"pass-cli {' '.join(args)} failed (exit {result.returncode}): {result.stderr.strip()}"
        )
    return result


def trash_all_existing(env):
    # Trash by --item-id, not --item-title: titles aren't unique (duplicates
    # from a prior bug proved this), and title-based trash silently no-ops
    # on an ambiguous match, so duplicates from bad runs never get cleaned
    # up. IDs are unambiguous.
    listing = run_pass_cli(["item", "list", "--vault-name", VAULT_NAME, "--output", "json"], env)
    items = json.loads(listing.stdout).get("items", [])
    for item in items:
        if item.get("state") == "Trashed":
            continue
        # --item-id=<val> (not two separate args): some base64url item IDs
        # start with '-', which pass-cli's arg parser otherwise misreads as
        # a flag instead of the value, silently failing the trash call.
        result = run_pass_cli(
            ["item", "trash", "--vault-name", VAULT_NAME, f"--item-id={item['id']}"],
            env, check=False,
        )
        if result.returncode != 0:
            print(f"WARNING: failed to trash existing item {item['title']!r} ({item['id']})", file=sys.stderr)


def create_credential_item(service, fields, env):
    # Custom item type, not Login -- cred:<service>:* groups have arbitrary
    # field sets (2 to 15+ fields per service, not just host/user/password),
    # and Login's fixed 4-field schema silently drops anything else.
    payload = {
        "title": service,
        "note": "",
        "sections": [
            {
                "section_name": "Credentials",
                "fields": [
                    {"field_name": field, "field_type": "hidden", "value": value}
                    for field, value in sorted(fields.items())
                ],
            }
        ],
    }
    run_pass_cli(
        ["item", "create", "custom", "--vault-name", VAULT_NAME, "--from-template", "-"],
        env, input_text=json.dumps(payload),
    )


def create_note(key, value, env):
    payload = {"title": key, "note": value}
    run_pass_cli(
        ["item", "create", "note", "--vault-name", VAULT_NAME, "--from-template", "-"],
        env, input_text=json.dumps(payload),
    )


def main() -> int:
    env = os.environ.copy()
    env["PROTON_PASS_AGENT_REASON"] = REASON

    info = run_pass_cli(["info"], env, check=False)
    if info.returncode != 0:
        raise RuntimeError(
            "No active pass-cli session -- run `pass-cli login` interactively on this "
            "host first (Proton's Personal Access Tokens can't write items, so this "
            "can't self-recover)."
        )

    creds, flat = export_infisical()

    trash_all_existing(env)

    for service, fields in creds.items():
        create_credential_item(service, fields, env)
    for key, value in flat.items():
        create_note(key, value, env)

    print(
        f"OK: synced {len(creds)} credential items + {len(flat)} note items "
        f"to Proton Pass vault '{VAULT_NAME}'"
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"FAIL: {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)
