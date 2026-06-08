#!/usr/bin/env python3
"""CLI: reveal a secret value. Usage: python tools/decrypt_secret.py --key KEY --confirm"""
import sys
import pathlib
import argparse
import threading

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))


def main():
    parser = argparse.ArgumentParser(description="Reveal a vault secret")
    parser.add_argument("--key", required=True, help="Secret key name")
    parser.add_argument("--confirm", action="store_true", help="Required to prevent accidental use")
    args = parser.parse_args()

    if not args.confirm:
        print("Add --confirm flag to reveal a secret value.")
        sys.exit(1)

    from backend.secrets.vault import get_secret

    try:
        value = get_secret(args.key)
    except KeyError:
        print(f"ERROR: '{args.key}' not found in vault")
        sys.exit(1)
    except RuntimeError as e:
        print(f"ERROR: {e}")
        sys.exit(1)

    print(f"\n{args.key} = {value}\n")
    print("--- This value will be cleared from this message in 30 seconds ---")

    def clear():
        import os
        print(f"\r{' ' * 80}\r--- cleared ---", flush=True)

    t = threading.Timer(30.0, clear)
    t.start()
    try:
        input()
    except KeyboardInterrupt:
        pass
    finally:
        t.cancel()


if __name__ == "__main__":
    main()
