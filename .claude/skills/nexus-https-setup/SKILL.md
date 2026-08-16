---
name: nexus-https-setup
description: One-time PowerShell setup steps to expose NEXUS over HTTPS via Tailscale serve (path mounts for /api and /ws, verifying the WS upgrade, and the APP_BASE_URL env var). Use when setting up a new NEXUS host or re-establishing Tailscale HTTPS after it stops working.
---

# NEXUS HTTPS via `tailscale serve`

The frontend is already HTTPS-ready: when the page loads over `https:` it uses SAME-ORIGIN `/api`
+ `/ws` (tailscale serve path mounts) instead of `http://host:8000`, avoiding mixed-content
blocking. Plain-HTTP LAN clients keep hitting `:8000` directly from the same build (runtime branch
in `frontend/src/lib/api.js` + `ws.js`; `VITE_API_BASE`/`VITE_WS_BASE` stay top-priority overrides).

## One-time operational setup (elevated PowerShell)

```
tailscale serve --bg --set-path=/api http://127.0.0.1:8000/api
tailscale serve --bg --set-path=/ws  http://127.0.0.1:8000/ws
tailscale serve --bg http://127.0.0.1:3000
```

Then `tailscale serve status` to verify, open `https://win11-vm-proxmox.tailfa52c.ts.net` and CHECK
THE SAFETY PAGE LIVE FEED (WS upgrade actually working).

If the WS fails through serve, fall back to:

```
tailscale serve --bg --https=8443 http://127.0.0.1:8000
```

and set `VITE_API_BASE` accordingly.

Set `APP_BASE_URL=https://win11-vm-proxmox.tailfa52c.ts.net` in `.env` so Telegram deep links go
HTTPS.

Requires MagicDNS + HTTPS certs enabled in the tailnet admin console.
