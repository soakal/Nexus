"""Vault management CLI.

Usage:
  python tools/manage_vault.py list              — list secret names (no values)
  python tools/manage_vault.py set KEY           — set a secret (masked prompt)
"""
import argparse
import getpass
import sys
from pathlib import Path

# Ensure the project root is on the path so backend imports work.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def cmd_list(_args) -> None:
    from backend.secrets.vault import list_keys
    keys = list_keys()
    if not keys:
        print("(vault is empty)")
        return
    for k in sorted(keys):
        print(k)


def cmd_set(args) -> None:
    key = args.key
    value = getpass.getpass(f"Value for {key} (input hidden): ")
    if not value.strip():
        print("Aborted — empty value not stored.", file=sys.stderr)
        sys.exit(1)
    from backend.secrets.vault import set_secret
    set_secret(key, value.strip())
    print(f"OK — {key} stored in vault.")


def cmd_delete(args) -> None:
    key = args.key
    confirm = input(f"Delete '{key}' from vault? [y/N] ").strip().lower()
    if confirm != "y":
        print("Aborted.")
        sys.exit(0)
    from backend.secrets.vault import delete_secret, read_meta, META_PATH
    import json
    delete_secret(key)
    meta = read_meta()
    if key in meta:
        del meta[key]
        META_PATH.write_text(json.dumps(meta, indent=2))
    print(f"OK — {key} removed from vault and metadata.")


def main() -> None:
    parser = argparse.ArgumentParser(description="NEXUS vault manager")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("list", help="List secret names (no values)")

    p_set = sub.add_parser("set", help="Set a secret value (masked prompt)")
    p_set.add_argument("key", help="Secret name, e.g. HERMES_WEBHOOK_SECRET")

    p_del = sub.add_parser("delete", help="Delete a secret from the vault")
    p_del.add_argument("key", help="Secret name to remove")

    args = parser.parse_args()
    if args.command == "list":
        cmd_list(args)
    elif args.command == "set":
        cmd_set(args)
    elif args.command == "delete":
        cmd_delete(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
