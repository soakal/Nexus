---
name: nexus-lxc-access
description: Reach root inside a Proxmox LXC that has no SSH credential of its own in Infisical (e.g. proton-bridge/204) or whose sshd isn't reachable, via `pct exec` from the Proxmox host using its own stored credential instead. Use when a container-specific credential lookup in Infisical comes up empty, when direct `ssh root@<container-ip>` fails, or when you need to inspect or change something inside a container that was never given its own SSH access — covers finding the VMID, the never-printed fetch-and-use pattern, and why a write (not read-only) action through this path needs explicit confirmation first.
---

# nexus-lxc-access

Not every LXC in this homelab has its own stored credential. Infisical currently holds `cred:<name>:*`
entries for `nexus-lxc`, `devbox`, `unraid`/`Unraid-Main`, `MInt_Linux`, `lxc201`, and `rustdesk` — but
some containers (e.g. `proton-bridge`, VMID 204) have none at all. That doesn't mean the container is
unreachable; it means the credential lives one level up, on the Proxmox host itself.

## When to use this vs. a direct SSH credential

Check for a `cred:<name>:*` entry (or a working `ssh root@<container-ip>`) first — if one exists, use
it. This path is root-on-everything via the hypervisor: it works for *any* container regardless of that
container's own sshd/credential state, which makes it the right fallback, not the default shortcut past
a container that already has proper access configured.

## 1. Find the VMID

`pct list`, run on the Proxmox host, maps every container's name to its VMID and status:

```
VMID       Status     Lock         Name
204        running                 proton-bridge
207        running                 nexus
...
```

The container's Tailscale hostname usually matches this name (`proton-bridge` → VMID 204 here) — that's
the fastest way to go from "I know its Tailscale IP/hostname" to "I know its VMID."

## 2. The access pattern (password never printed, never on a command line)

From nexus-lxc (or anywhere `backend.secrets.infisical_client` and `paramiko` — v5.0.0, already in the
nexus venv — are available):

```python
import sys
sys.path.insert(0, '/opt/nexus')
from backend.secrets import infisical_client as ic
import paramiko

host = ic.get_secret('cred:proxmox:host')
user = ic.get_secret('cred:proxmox:user')
pw = ic.get_secret('cred:proxmox:password')

client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect(hostname=host, username=user, password=pw, timeout=10)

stdin, stdout, stderr = client.exec_command('pct exec 204 -- <command>')
print(stdout.read().decode())
client.close()
```

The password exists only inside this one Python process's memory — it's never interpolated into a shell
command line (where it would land in shell history / `ps`) and never printed. This is the same
fetch-then-use-without-printing discipline as any other Infisical secret: `get_secret()` returns it,
`paramiko.connect()` consumes it directly, nothing in between ever echoes it.

## 3. Why this works at all

`pct exec <vmid> -- <command>` is the scriptable equivalent of the Proxmox web UI's `>_ Shell` console —
the same ad hoc mechanism most of these containers were originally provisioned through (pasting a pubkey
in via that console). It runs as root inside the container's namespace from the Proxmox host, which is
why it needs neither the container's own sshd to be reachable nor any credential of that container's own
to exist.

## Gotchas

- **Compound commands need care.** `pct exec 204 -- sh -c "cmd1 && cmd2"` or careful quoting — a bare
  `pct exec 204 -- cmd1 && cmd2` runs `cmd2` on the *Proxmox host*, not inside the container, because the
  `&&` is parsed by the host's shell before `pct exec` ever sees it.
- **A `$(...)` command substitution has the same trap** — it evaluates on the Proxmox host's shell first,
  not inside the container, before `pct exec` runs. Resolve any value you need from inside the container
  with its own `pct exec` call, not a substitution wrapped around one.
- **This is a write-capable, high-privilege path.** Read-only inspection (`cat`, `systemctl status`,
  `journalctl`) is routine; anything that changes state inside the container (editing a config, restarting
  a service) is a meaningfully bigger action than inspection and should be treated that way — back up
  what you're about to change first, and confirm before restarting anything that isn't purely your own
  diagnostic tooling.
- **Expect a write through this path to be blocked on the first attempt, every time.** A mutating
  command (editing a unit file, restarting a service) run this way was refused outright by the
  permission classifier the first time it was tried (2026-09-06) — specifically because it changes
  state via a root-on-hypervisor credential, not because anything was wrong with the command. That is
  not a bug to retry past; get the user's explicit go-ahead first, then re-run the identical command —
  it goes through once confirmed. Read-only inspection with this same credential is not gated this way.
