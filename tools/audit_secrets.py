#!/usr/bin/env python3
"""CLI: list all secrets with timestamps. Usage: python tools/audit_secrets.py"""
import sys
import pathlib
import json

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))


def main():
    from backend.secrets.vault import list_keys, KEY_PATH

    if not KEY_PATH.exists():
        print("ERROR: .vault.key not found. Run setup.ps1 first.")
        sys.exit(1)

    keys = list_keys()
    if not keys:
        print("Vault is empty.")
        return

    meta_path = pathlib.Path("nexus.vault.meta")
    meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}

    print(f"\n{'KEY':<30} {'LAST SET':<22} {'LAST ROTATED':<22}")
    print("-" * 76)
    for key in sorted(keys):
        key_meta = meta.get(key, {})
        last_set = key_meta.get("last_set", "unknown")[:19] if key_meta.get("last_set") else "unknown"
        last_rotated = key_meta.get("last_rotated", "never")[:19] if key_meta.get("last_rotated") else "never"
        print(f"{key:<30} {last_set:<22} {last_rotated:<22}")
    print()


if __name__ == "__main__":
    main()
