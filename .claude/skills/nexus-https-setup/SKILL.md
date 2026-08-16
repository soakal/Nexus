---
name: nexus-https-setup
description: One-time setup steps to expose NEXUS over HTTPS via Tailscale serve on nexus-lxc (path mounts for /api and /ws, verifying the WS upgrade, and the APP_BASE_URL env var). Use when setting up a new NEXUS host or re-establishing Tailscale HTTPS after it stops working.
---

# NEXUS HTTPS via `tailscale serve`

Current host: `nexus-lxc` (Linux LXC, Tailscale IP `100.84.21.43`, `192.168.1.62` on the LAN) — the
Windows host this skill originally targeted (`win11-vm-proxmox`) was decommissioned 2026-08-15; see
`CLAUDE.md`'s branch-policy banner.

The frontend is already HTTPS-ready: when the page loads over `https:` it uses SAME-ORIGIN `/api`
+ `/ws` (tailscale serve path mounts) instead of `http://host:8000`, avoiding mixed-content
blocking. Plain-HTTP LAN clients keep hitting `:8000` directly from the same build (runtime branch
in `frontend/src/lib/api.js` + `ws.js`; `VITE_API_BASE`/`VITE_WS_BASE` stay top-priority overrides).

## One-time operational setup

Run as root on nexus-lxc (`ssh -i ~/.ssh/id_nexus_lxc root@nexus-lxc` from devbox, or directly if
already on the host — no elevated/PowerShell step needed on Linux):

```
tailscale serve --bg --set-path=/api http://127.0.0.1:8000/api
tailscale serve --bg --set-path=/ws  http://127.0.0.1:8000/ws
tailscale serve --bg http://127.0.0.1:3000
```

Then `tailscale serve status` to verify, open `https://nexus-lxc.tailfa52c.ts.net` and CHECK
THE SAFETY PAGE LIVE FEED (WS upgrade actually working). (The same host/listener may also carry a
`/mcp` path mount for Carl's MCP server, unrelated to NEXUS itself — see the
`mcp-server-claude-desktop` skill.)

If the WS fails through serve, fall back to:

```
tailscale serve --bg --https=8443 http://127.0.0.1:8000
```

and set `VITE_API_BASE` accordingly.

Set `APP_BASE_URL=https://nexus-lxc.tailfa52c.ts.net` in `.env` so Telegram deep links go HTTPS.

Requires MagicDNS + HTTPS certs enabled in the tailnet admin console.
