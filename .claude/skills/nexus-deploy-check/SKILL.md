---
name: nexus-deploy-check
description: Verify a nexus deploy actually took effect before claiming it's live — push/pull/build/restart sequence plus the running_sha check that catches "pulled but never restarted". Use after any git push touching backend/ or frontend/, before telling anyone a change is deployed.
---

# Verifying a nexus deploy

A deploy isn't done when `git push` succeeds — it's done when the running process's SHA matches
what you pushed. This skill exists because "pulled but never restarted" is a real, previously-hit
failure mode.

## Sequence

1. **Confirm the push landed**: `git status -sb` shows `main...origin/main` in sync.
2. **Pull on nexus-lxc and capture the SHA**:
   ```bash
   ssh -i ~/.ssh/id_ed25519 root@100.84.21.43 'cd /opt/nexus && git pull --ff-only && git rev-parse HEAD'
   ```
3. **Frontend changed?** Build before restarting — preview serves `dist/`, not source:
   ```bash
   ssh -i ~/.ssh/id_ed25519 root@100.84.21.43 'cd /opt/nexus/frontend && npm run build'
   ```
4. **Restart the service(s) that changed**:
   ```bash
   ssh -i ~/.ssh/id_ed25519 root@100.84.21.43 'systemctl restart nexus-backend'   # add nexus-frontend if frontend changed
   ```
   Don't restart mid-run of a scheduled job (e.g. overnight `brain_organizer`) — check
   `journalctl -u nexus-backend -n 20` for what's active first if deploying off-hours.
5. **Verify live — the step that's actually load-bearing**:
   ```bash
   ssh -i ~/.ssh/id_ed25519 root@100.84.21.43 '
   curl -s http://127.0.0.1:8000/api/health
   KEY=$(cd /opt/nexus && venv/bin/python -c \
     "from backend.secrets.manager import get_secret; print(get_secret(\"NEXUS_API_KEY\"))" | tail -1)
   curl -s -H "Authorization: Bearer $KEY" http://127.0.0.1:8000/api/safety/status
   '
   ```
   `/api/health` must say `"status":"ok"`, and `/api/safety/status`'s `running_sha` must equal the
   SHA from step 2. If it doesn't match, the restart didn't take — check
   `journalctl -u nexus-backend -n 50` for a boot error rather than retrying the restart blindly.
6. **Runtime config lives at `/var/lib/nexus`, not `/opt/nexus`** — `.env`/`nexus.db` changes need
   to be made there, not in the checkout.

Report a deploy as done only after step 5 passes.
