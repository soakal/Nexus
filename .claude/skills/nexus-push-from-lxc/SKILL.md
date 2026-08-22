---
name: nexus-push-from-lxc
description: Get a commit authored on nexus-lxc onto origin/main. As of 2026-08-22 nexus-lxc has its own write-enabled deploy key and plain `git push` just works; this documents that, the devbox-token-borrowing fallback (and why it must never be persisted), and the author-on-devbox alternative. Use when you have commits sitting on nexus-lxc's /opt/nexus, or when a push from there is refused.
---

# Pushing a nexus-lxc-authored commit to origin/main

## Primary path: just push (since 2026-08-22)

nexus-lxc's `/opt/nexus` checkout has its **own SSH deploy key with write access** to
`github.com/soakal/Nexus`. Nothing needs to be borrowed, exported, or typed:

```bash
ssh -i ~/.ssh/id_ed25519 root@100.84.21.43 'cd /opt/nexus && git push'
```

The pieces, so you can recognise them if one goes missing:

- Key: `/root/.ssh/id_ed25519_nexus_deploy` (on nexus-lxc), registered on GitHub as a
  **write-enabled** deploy key on the `soakal/Nexus` repo.
- SSH config alias `github.com-nexus-deploy` in `/root/.ssh/config`, binding that key to
  `github.com` via `IdentityFile` + `IdentitiesOnly yes`. The alias exists so root's default
  `id_ed25519` (the devbox↔nexus-lxc key, which GitHub does not know) can't be offered first and
  get the connection rejected before the right key is tried.
- `/opt/nexus`'s `origin` already points at `git@github.com-nexus-deploy:soakal/Nexus.git`.

Verify all three at once:

```bash
ssh -i ~/.ssh/id_ed25519 root@100.84.21.43 'cd /opt/nexus && git remote -v && ssh -T git@github.com-nexus-deploy'
```

Healthy: the remote reads `git@github.com-nexus-deploy:soakal/Nexus.git`, and the `ssh -T` prints
`Hi soakal/Nexus! You've successfully authenticated, but GitHub does not provide shell access.`
Exit code 1 from `ssh -T` is normal — GitHub always closes the session; read the message, not the
status. A **deploy key is repo-scoped**, so the greeting names the repo, not the user.

If it says `Permission denied (publickey)`, the key was revoked or rotated — use the fallback
below, and re-register a key rather than leaving the fallback as the permanent state.

Remember `/opt/nexus` is the **deployed** checkout: it must be on `main` and clean before you
push, and after any push you still owe it a restart if backend Python changed
(`systemctl restart nexus-backend`).

## Prefer this instead: author on devbox, let nexus-lxc pull

For anything more than a one-line fix, don't author on nexus-lxc at all. Editing on the live
deployed checkout means your working tree *is* production until you're done, and an accidental
restart mid-edit runs whatever half-finished state is on disk.

```bash
# on devbox
git clone https://github.com/soakal/Nexus.git ~/work/nexus && cd ~/work/nexus
# ...edit, commit, test (see nexus-test-without-venv)...
git push
ssh -i ~/.ssh/id_ed25519 root@100.84.21.43 'cd /opt/nexus && git pull && systemctl restart nexus-backend'
```

devbox's `gh` is authenticated as `soakal` with repo write access, and its credential helper
handles HTTPS pushes with no token juggling. This is the normal deploy loop; the deploy key exists
for the cases where a commit genuinely originated on the LXC (a hotfix typed at 2am against live
state, a config file only that host has).

## Fallback only: borrowing devbox's `gh` token

Use this **only** if the deploy key is revoked or rotated and you need a push through right now.

```bash
# on devbox
TOKEN=$(gh auth token)
git push "https://x-access-token:${TOKEN}@github.com/soakal/Nexus.git" HEAD:main
```

To push a commit that lives on nexus-lxc this way, bundle it to devbox first rather than sending
the token the other direction — `git bundle create /tmp/x.bundle main` on the LXC, `scp` it back,
`git fetch /tmp/x.bundle main`, push from devbox.

**Hard rules for this fallback:**

- **Never** `git remote set-url` a token-bearing URL. That writes the credential into
  `.git/config` in plaintext, where it survives every later command and every `git remote -v` in a
  transcript. Pass the URL as a one-shot argument, as above.
- **Never** type the token literally on a command line. Keep it in `$TOKEN` (as above) so it never
  reaches shell history — and prefer a leading space, or `unset TOKEN` afterwards.
- **This repo runs gitleaks in CI** (added 2026-08-11, same commit as the deploy-drift watchdog).
  A leaked `gho_`/`ghp_` token in a tracked file fails the build, and the correct response is to
  revoke the token, not to whitelist the finding.
- Revert to the deploy key as soon as it's restored. A borrowed user token acts as *Brian*, with
  his full account scope, across every repo he can reach; the deploy key is scoped to this one
  repo and can be revoked without collateral.

## After any push, deploy is still a separate step

Pushing to `origin/main` changes nothing on the running system. `/api/safety/status` exposes
`running_sha`; when it stops matching `git -C /opt/nexus rev-parse HEAD`, that's deploy drift, and
the watchdog reports it. The fix is always the same:

```bash
ssh -i ~/.ssh/id_ed25519 root@100.84.21.43 'cd /opt/nexus && git pull && systemctl restart nexus-backend && sleep 10 && curl -sf http://localhost:8000/api/health'
```

Healthy: `{"status":"ok"}`, and `journalctl -u nexus-backend --since "1 min ago"` clean. Markdown-
only changes (skills, docs) need the `git pull` so the file exists in the repo everyone reads
from, but **no restart**.
