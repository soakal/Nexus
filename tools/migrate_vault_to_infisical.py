#!/usr/bin/env python3
"""One-time (but safely re-runnable) migration of NEXUS's legacy Fernet vault
secrets into the live Infisical project.

Three mutually-exclusive modes:
  --dry-run   Read-only. Shows what would be written, no writes.
  --execute   Live migration. Reads every vault key, upserts it into Infisical.
  --verify    Read-only. Compares vault values against what's now in Infisical.

Exit codes: 0 = all OK. 1 = ran to completion but >=1 per-key FAIL/MISMATCH/
MISSING/ERROR. 2 = fatal/preflight failure (bad args, vault unreadable, empty
vault, canonical-key collision, Infisical unreachable).

Never prints a secret value, under any code path including errors — every
print statement below emits key names, paths, counts, and exception CLASS
names only.

Idempotency: re-running --execute is safe and converges to the same state.
On a first run every key is created. On a second run, flat keys and any
cred:<service>:<field> key whose <service> was already lowercase go straight
to PATCH (found in the warmed-up cache). Keys with an original mixed-case
service name (e.g. "cred:Unraid:host") are NOT found under their original
casing by the cold-cache existence check — infisical_client's internal
_vault_key_to_path_and_name() lowercases the service on write, and a fresh
process's bulk-fetch reconstruction is therefore always lowercase — so this
script's own pre-write "does it already exist" check misses them and prints
"created" again on every re-run. This is cosmetic only: infisical_client.
set_secret() itself falls back from POST to PATCH on a 400/409 "already
exists" response, so the actual write is a correct upsert either way. See
canonical_key() below, used by --verify to compare correctly regardless of
this casing quirk.

Explicit non-goals: this script never touches .env, never reads or sets
SECRETS_BACKEND, never imports backend.secrets.manager, and never restarts or
signals NEXUS. It only populates Infisical — flipping NEXUS's active secret
backend is a separate, later, deliberate action. Nothing is ever deleted or
modified in the legacy vault; the vault is strictly a read-only source here.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.secrets import vault, infisical_client  # noqa: E402


def canonical_key(key: str) -> str:
    """Normalize a cred:<service>:<field> key the same way a cold Infisical
    bulk-fetch reconstructs it (service lowercased) — flat keys are unchanged.
    Used for existence/comparison checks that must work across process
    restarts, where infisical_client's in-memory cache starts empty."""
    if key.startswith("cred:"):
        parts = key.split(":", 2)
        if len(parts) == 3:
            _, service, field = parts
            return f"cred:{service.lower()}:{field}"
    return key


def preflight() -> list:
    """Returns sorted vault keys, or exits 2 with a diagnostic."""
    if not vault.VAULT_PATH.exists() or not vault.KEY_PATH.exists():
        print("FATAL: nexus.vault or .vault.key not found — run this from the repo root.")
        raise SystemExit(2)

    keys = sorted(vault.list_keys())
    if not keys:
        print("FATAL: vault is empty — wrong working directory, or nothing to migrate?")
        raise SystemExit(2)

    seen = {}
    collisions = []
    for k in keys:
        c = canonical_key(k)
        if c in seen:
            collisions.append((seen[c], k))
        else:
            seen[c] = k
    if collisions:
        print("FATAL: canonical-key collision(s) — two vault keys would land on the same Infisical entry:")
        for a, b in collisions:
            print(f"  {a}  <->  {b}")
        raise SystemExit(2)

    if not infisical_client.warm_up():
        print("FATAL: Infisical unreachable — check INFISICAL_* settings and network connectivity.")
        raise SystemExit(2)

    return keys


def run_dry_run(keys: list) -> int:
    known = set(infisical_client.list_keys())
    total = len(keys)
    create = update = unreadable = 0
    for key in keys:
        try:
            vault.get_secret(key)  # prove it decrypts; value discarded, never printed
        except Exception as e:
            print(f"UNREADABLE {key} ({type(e).__name__})")
            unreadable += 1
            continue
        path, name = infisical_client._vault_key_to_path_and_name(key)
        action = "update" if canonical_key(key) in known else "create"
        if action == "create":
            create += 1
        else:
            update += 1
        print(f"PLAN {key} -> {path}:{name} [{action}]")
    print(f"SUMMARY total={total} create={create} update={update} unreadable={unreadable}")
    return 0 if unreadable == 0 else 1


def run_execute(keys: list) -> int:
    known = set(infisical_client.list_keys())
    total = len(keys)
    created = updated = failed = 0
    for key in keys:
        label = "updated" if canonical_key(key) in known else "created"
        try:
            value = vault.get_secret(key)
            infisical_client.set_secret(key, value)
        except Exception as e:
            detail = f" HTTP {e.response.status_code}" if hasattr(e, "response") and e.response is not None else ""
            print(f"FAIL {key} ({type(e).__name__}{detail})")
            failed += 1
            continue
        print(f"OK {key} [{label}]")
        if label == "created":
            created += 1
        else:
            updated += 1
    print(f"SUMMARY total={total} created={created} updated={updated} failed={failed}")
    return 0 if failed == 0 else 1


def run_verify(keys: list) -> int:
    total = len(keys)
    ok = mismatch = missing = error = 0
    vault_canonical = {canonical_key(k) for k in keys}
    for key in keys:
        try:
            expected = vault.get_secret(key)
        except Exception as e:
            print(f"ERROR {key} ({type(e).__name__})")
            error += 1
            continue
        try:
            actual = infisical_client.get_secret(canonical_key(key))
        except KeyError:
            print(f"MISSING {key}")
            missing += 1
            continue
        except Exception as e:
            print(f"ERROR {key} ({type(e).__name__})")
            error += 1
            continue
        if actual == expected:
            print(f"OK {key}")
            ok += 1
        else:
            print(f"MISMATCH {key}")
            mismatch += 1

    extra = 0
    for k in infisical_client.list_keys():
        if k not in vault_canonical:
            print(f"EXTRA {k}")
            extra += 1

    print(f"SUMMARY total={total} ok={ok} mismatch={mismatch} missing={missing} error={error} extra={extra}")
    return 0 if (mismatch == 0 and missing == 0 and error == 0) else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Migrate NEXUS's legacy vault secrets into Infisical. "
                     "Populates Infisical only — never flips NEXUS's active "
                     "secret backend. Exit codes: 0 ok, 1 per-key issues, "
                     "2 fatal/preflight.",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--dry-run", action="store_true", help="Show the migration plan, no writes.")
    group.add_argument("--execute", action="store_true", help="Perform the live migration.")
    group.add_argument("--verify", action="store_true", help="Compare vault values against Infisical (run as a fresh process, after --execute).")
    args = parser.parse_args(argv)

    keys = preflight()

    if args.dry_run:
        return run_dry_run(keys)
    if args.execute:
        return run_execute(keys)
    return run_verify(keys)


if __name__ == "__main__":
    sys.exit(main())
