"""Migrate legacy SSH password vault keys to the cred:<service>:<field> namespace.

Old keys (with timestamp shown for reference):
  HERMES_SSH_PASSWORD  (set 2026-06-20)
  hermes_ssh_password  (set 2026-06-23, newer — wins on conflict)
  LXC201_SSH_PASSWORD  (set 2026-06-20)

New keys written:
  cred:hermes:host   = 192.168.1.55
  cred:hermes:user   = root
  cred:hermes:password = <newer hermes password>
  cred:lxc201:host   = 192.168.1.56
  cred:lxc201:user   = root
  cred:lxc201:password = <lxc201 password>

Run with no args to migrate (idempotent).
Run with --purge-old after confirming to delete the old keys.

Usage:
  cd "C:\\Users\\Brian\\Documents\\Agentic os\\nexus"
  .\\venv\\Scripts\\python.exe tools\\migrate_credentials.py
  .\\venv\\Scripts\\python.exe tools\\migrate_credentials.py --purge-old
"""
import sys
import pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

from backend.secrets.vault import get_secret, set_credential, set_secret, delete_secret, list_keys

PURGE = "--purge-old" in sys.argv

def try_get(key):
    try:
        return get_secret(key)
    except KeyError:
        return None

def migrate():
    keys = list_keys()
    print(f"Vault has {len(keys)} keys.\n")

    # Hermes: prefer the newer hermes_ssh_password (2026-06-23) over HERMES_SSH_PASSWORD
    hermes_pw = try_get("hermes_ssh_password") or try_get("HERMES_SSH_PASSWORD")
    if hermes_pw:
        existing = try_get("cred:hermes:password")
        if existing:
            print("cred:hermes:password already set — skipping (idempotent)")
        else:
            set_credential("hermes", "host", "192.168.1.55")
            set_credential("hermes", "user", "root")
            set_credential("hermes", "password", hermes_pw)
            print("Migrated -> cred:hermes:{host,user,password}")
    else:
        print("No Hermes SSH password found in vault — skipping hermes migration")

    # LXC201
    lxc201_pw = try_get("LXC201_SSH_PASSWORD")
    if lxc201_pw:
        existing = try_get("cred:lxc201:password")
        if existing:
            print("cred:lxc201:password already set — skipping (idempotent)")
        else:
            set_credential("lxc201", "host", "192.168.1.56")
            set_credential("lxc201", "user", "root")
            set_credential("lxc201", "password", lxc201_pw)
            print("Migrated -> cred:lxc201:{host,user,password}")
    else:
        print("No LXC201_SSH_PASSWORD found — skipping lxc201 migration")

    old_keys = [k for k in ["HERMES_SSH_PASSWORD", "hermes_ssh_password", "LXC201_SSH_PASSWORD"] if k in list_keys()]
    if old_keys:
        print(f"\nOld keys still in vault: {old_keys}")
        if PURGE:
            for k in old_keys:
                delete_secret(k)
                print(f"  Deleted {k}")
            print("Purge complete.")
        else:
            print("Run with --purge-old to delete them after confirming the new cred:* keys work.")
    else:
        print("\nNo old keys to clean up.")

if __name__ == "__main__":
    migrate()
