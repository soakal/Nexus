#!/usr/bin/env python3
"""CLI: migrate existing .env secrets to encrypted vault. Usage: python tools/import_env.py --env-file .env"""
import sys
import pathlib
import argparse

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))


def main():
    parser = argparse.ArgumentParser(description="Import .env secrets into vault")
    parser.add_argument("--env-file", default=".env", help="Path to .env file")
    args = parser.parse_args()

    from backend.secrets.migrations import import_env_file, generate_vault_key, KEY_PATH

    if not KEY_PATH.exists():
        generate_vault_key()
        print("✓ Generated new vault key")

    try:
        imported, skipped = import_env_file(args.env_file)
        print(f"Imported {imported} secrets. Skipped {skipped} non-secret entries.")
        print(f"You can now remove credentials from {args.env_file}")
    except FileNotFoundError as e:
        print(f"ERROR: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
