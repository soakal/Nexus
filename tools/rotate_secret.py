#!/usr/bin/env python3
"""CLI: replace a secret value and log rotation timestamp."""
import sys
import pathlib
import argparse

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))


def main():
    parser = argparse.ArgumentParser(description="Rotate a vault secret")
    parser.add_argument("--key", required=True, help="Secret key name")
    args = parser.parse_args()

    from getpass import getpass
    from backend.secrets.vault import set_secret, KEY_PATH

    if not KEY_PATH.exists():
        print("ERROR: .vault.key not found.")
        sys.exit(1)

    new_value = getpass(f"New value for {args.key}: ")
    if not new_value:
        print("Value cannot be empty.")
        sys.exit(1)

    # set_secret stamps last_set/last_rotated in nexus.vault.meta.
    set_secret(args.key, new_value)

    print(f"✓ {args.key} rotated")


if __name__ == "__main__":
    main()
