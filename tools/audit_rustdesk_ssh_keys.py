"""One-shot audit: flag cred:rustdesk:ssh_pubkey_* entries in Infisical for
machines that no longer exist.

Each ssh_pubkey_<name> entry trusts a specific machine's SSH access to the
rustdesk relay host. A key for a decommissioned machine (Windows box retired,
a renamed/rebuilt container, a one-off setup key nobody removed) is live
attack surface with no offsetting benefit -- the machine it was issued to is
gone, so nothing legitimate needs it anymore.

This never deletes anything. It lists which pubkey names have no live match
against the current Tailscale device list or Proxmox container list (names
only, from `tailscale status` and `pct list` output; secret VALUES are never
read or printed, only Infisical's own key NAMES). Run again after any future
homelab machine is retired or renamed; there's no scheduled version of this
because key churn here is occasional, not continuous.

KNOWN FALSE POSITIVE/NEGATIVE, found live on the 2026-09-06 run and left
unresolved deliberately rather than papered over with a fragile heuristic:

  - False NEGATIVE: `ssh_pubkey_nexus-windows-restarts-lxc` reported "OK
    (matched)" even though the machine it names (a Windows box that used to
    restart nexus-lxc) was decommissioned and no longer appears in Tailscale
    AT ALL -- not even offline. The match happened because this LXC's own
    Proxmox container is literally named "nexus" (not "nexus-lxc"), and the
    token-split matcher below caught that unrelated coincidental hit. Tighter
    matching (whole-suffix substring instead of token-split) doesn't fix
    this either -- "nexus" is still a real live name and still a substring
    of "nexus-windows-restarts-lxc" either way.
  - False POSITIVE risk: `ssh_pubkey_claude-proxmox-deploy` and
    `ssh_pubkey_claude-processforge-lxc-setup` are named after a PURPOSE
    ("a key Claude uses to deploy to the Proxmox host", "a key used once to
    set up processforge"), not a machine identity -- there is no
    "proxmox"-named Tailscale device or container to match against even
    though the Proxmox host is very much alive, so purpose-named keys can
    read as stale when they're actually fine, or vice versa.

The takeaway: treat every line below as a candidate for a human to look at,
never as a verdict. A "NO LIVE MATCH" is not proof of staleness and an "OK"
is not proof the grant is still needed -- this script can only check "does a
plausible hostname token still exist," which is a necessary check, not a
sufficient one.

Usage: python tools/audit_rustdesk_ssh_keys.py
"""
import re
import sys

sys.path.insert(0, "/opt/nexus")


def _live_tailscale_names() -> set[str]:
    """Every Tailscale device name in this tailnet, online or offline --
    offline still means the device exists, just isn't currently reachable.
    A name is only flagged if it's absent from this list ENTIRELY."""
    from backend.secrets import infisical_client as ic  # noqa: F401  (side-effect: warm up cache path used below)
    import subprocess

    result = subprocess.run(["tailscale", "status"], capture_output=True, text=True, timeout=10)
    names = set()
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and re.match(r"^\d+\.\d+\.\d+\.\d+$", parts[0]):
            names.add(parts[1].lower())
    return names


def _live_proxmox_container_names() -> set[str]:
    """Every LXC container name on the Proxmox host, via pct list (see
    nexus-lxc-access skill) -- covers containers that may not run Tailscale
    themselves but are still real, current infrastructure."""
    from backend.secrets import infisical_client as ic
    import paramiko

    host = ic.get_secret("cred:proxmox:host")
    user = ic.get_secret("cred:proxmox:user")
    password = ic.get_secret("cred:proxmox:password")

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(hostname=host, username=user, password=password, timeout=10)
    try:
        _, stdout, _ = client.exec_command("pct list", timeout=15)
        lines = stdout.read().decode().splitlines()[1:]
    finally:
        client.close()
    return {line.split()[-1].lower() for line in lines if line.strip()}


def _rustdesk_pubkey_names() -> list[str]:
    """Key NAMES only (never values) under cred:rustdesk:ssh_pubkey_*."""
    from backend.secrets import infisical_client as ic

    ic._bulk_fetch()
    with ic._lock:
        keys = list(ic._cache.keys())
    return sorted(k for k in keys if k.startswith("cred:rustdesk:ssh_pubkey"))


def main() -> None:
    print(
        "NOTE: 'NO LIVE MATCH' is not proof of staleness and 'OK' is not proof a "
        "grant is still needed -- see this file's module docstring for a known "
        "false-positive and false-negative found on the 2026-09-06 run. A human "
        "must confirm every line below before removing anything.\n"
    )
    live_names = _live_tailscale_names() | _live_proxmox_container_names()

    for key in _rustdesk_pubkey_names():
        # cred:rustdesk:ssh_pubkey_brian_nexus-lxc-207 -> token candidates to
        # try matching: the whole suffix, and each hyphen/underscore-split
        # word -- a key name is usually <person>_<machine>[-<vmid>], not a
        # bare hostname, so a single substring test against the raw suffix
        # would under-match; splitting and testing each word (plus the
        # trailing digits-stripped form, for the "-207"/"-104" vmid suffix
        # pattern) is deliberately loose -- a false "still fine" from an
        # accidental substring hit is the safe failure direction here, a
        # human confirms before anything gets deleted either way.
        suffix = key.removeprefix("cred:rustdesk:ssh_pubkey_")
        tokens = re.split(r"[_-]", suffix.lower())
        matched = any(t in live_names for t in tokens if t)
        status = "OK (matched)" if matched else "NO LIVE MATCH -- possibly stale, confirm before removing"
        print(f"{key}: {status}")


if __name__ == "__main__":
    main()
