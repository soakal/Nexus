#!/usr/bin/env python3
"""CLI: add or update a secret in the NEXUS vault. Usage: python tools/encrypt_secret.py"""
import sys
import os
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))


def main():
    from getpass import getpass
    from backend.secrets.vault import set_secret, KEY_PATH

    if not KEY_PATH.exists():
        print("ERROR: .vault.key not found. Run setup.ps1 first.")
        sys.exit(1)

    key = input("Secret key name (e.g. ANTHROPIC_API_KEY): ").strip()
    if not key:
        print("Key name cannot be empty.")
        sys.exit(1)

    value = getpass(f"Value for {key}: ")
    if not value:
        print("Value cannot be empty.")
        sys.exit(1)

    set_secret(key, value)
    _log_set_timestamp(key)
    print(f"✓ {key} saved to vault")


def _log_set_timestamp(key: str):
    import json
    from datetime import datetime
    meta_path = pathlib.Path("nexus.vault.meta")
    meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
    if key not in meta:
        meta[key] = {}
    meta[key]["last_set"] = datetime.utcnow().isoformat()
    meta_path.write_text(json.dumps(meta, indent=2))


if __name__ == "__main__":
    main()
