---
name: nexus-digest-health-check
description: Diagnose and fix the daily Claude-features digest pipeline (cloud routine → digest/* PR → devbox 10:00 cron auto-merge → Brain vault + Telegram relay). Use when the digest didn't arrive or as a periodic health check. Has broken 4+ times (2026-07-29 auto-merge, 2026-08-15 branch target, 2026-08-16 TZ/venv/relay four-bug fix, 2026-08-17 PR base-branch mismatch stranding 3 days of digests on the dead master branch).
---

# Claude Digest Pipeline Health Check

The pipeline: a cloud routine (spec: `digests/claude-features/DIGEST_INSTRUCTIONS.md`) runs ~09:00
ET (observed finishing as late as 09:49), writes `digests/claude-features/YYYY-MM-DD.md` on branch
`digest/YYYY-MM-DD` off `main`, opens a PR into `main`. Then a cron job in `brian`'s crontab on
**devbox** (this machine, 192.168.1.63) fires 10:00 local (moved from 09:20 on 2026-08-17 — the
old time raced the cloud routine and regularly lost):

```
0 10 * * * cd /home/brian/repos/nexus && git pull --quiet && .relay-venv/bin/python tools/relay_claude_digest.py >> /home/brian/repos/nexus/logs/relay_claude_digest.cron.log 2>&1
```

`tools/relay_claude_digest.py` auto-merges any pending `digest/*` PR via local `gh` (same-repo,
same-owner `soakal`, single-digest-file diff only — fork PRs and multi-file PRs are refused),
pulls, POSTs new digest files to the Brain MCP `:8765/raw` on nexus-lxc (`brain_mcp_url`, currently
`http://100.84.21.43:8765`), sends Telegram, records in
`digests/claude-features/.relay_state.json`.

Check (a)-(e) in order — each has independently broken delivery before.

## (a) Branch agreement: instructions vs devbox checkout vs EACH open PR's actual base

```
cd /home/brian/repos/nexus && git branch --show-current
grep -n "default branch" digests/claude-features/DIGEST_INSTRUCTIONS.md
gh pr list --state open --json number,headRefName,baseRefName | jq -r '.[] | "\(.headRefName) -> base=\(.baseRefName)"'
```

Healthy: the first two say `main`, and every open `digest/*` PR's `baseRefName` also says `main`.
**Checking only the instructions file is NOT enough** — that's the 2026-08-17 bug: `DIGEST_INSTRUCTIONS.md`
already said `main` (fixed 2026-08-16), but 3 already-open PRs from before that fix still had
`baseRefName: master` and merged there silently, invisible to `main` for 3 days, with the
instructions check reading "healthy" the whole time. `master` is the FROZEN Windows archive
(2026-08-15) — a digest merged there reaches nobody. `tools/relay_claude_digest.py`'s
`_merge_pending_digest_prs()` now refuses (logs, doesn't merge) any PR whose base isn't `main` as
of commit `362fac3` — so this specific failure mode can no longer merge silently, but a
wrong-based PR still needs a human to retarget it on GitHub (`gh pr edit <n> --base main`, or close
and let the cloud routine re-open correctly) before it'll ever get picked up; the relay's log line
`NOT merging digest PR #N: base is 'master'...` is your signal to come do that. Also check for any
digest file that landed on `master` but never made it to `main` (`git log origin/master --oneline
-5 -- digests/`) — recoverable via `git checkout origin/master -- digests/claude-features/<date>.md`
onto `main` (relay will skip it if `.relay_state.json` already marks it relayed).

## (b) `.relay-venv` exists with deps

```
.relay-venv/bin/python -c "import httpx, pydantic_settings, cryptography; print('deps ok')"
grep -n relay-venv .gitignore   # must show .relay-venv/
```

Healthy: `deps ok`, and `.gitignore:43` has `.relay-venv/`. This venv is untracked and was once
wiped by a stray `git clean -fd` before it was gitignored. Fix (minimal deps, NOT full
requirements.txt — that pulls torch/whisper):

```
python3 -m venv .relay-venv && .relay-venv/bin/pip install httpx pydantic pydantic-settings cryptography
```

## (c) Cron fires at the right wall-clock time

```
timedatectl | grep "Time zone"    # must be America/Detroit
crontab -l                        # no TZ= line; job at 0 10 * * *
systemctl show cron -p ActiveEnterTimestamp   # restarted AFTER any TZ change
```

Ubuntu cron IGNORES a crontab `TZ=` line for SCHEDULING (`man 5 crontab`, LIMITATIONS) — it only
sets the job's env. Scheduling follows the SYSTEM timezone. The 2026-08-16 bug: daemon on UTC ran
the job at 05:20 ET, 4h before the digest existed. Fix: `timedatectl set-timezone America/Detroit`,
delete any misleading `TZ=` crontab line, `systemctl restart cron`.

## (d) `gh` auth (needed for the auto-merge)

```
gh auth status
```

Healthy: `Logged in to github.com account soakal (keyring)`, token `gho_...`, active. Token lives
in the GNOME keyring — doesn't expire but dies if the keyring is reset. Fix:
`gh auth login -h github.com`. Note the relay swallows `gh` failures silently — a broken auth just
leaves the PR unmerged and prints the "PR(s)/branch(es) pending review/merge" line instead.

## (e) Telegram bot token

```
cd /home/brian/repos/nexus && .relay-venv/bin/python -c "
from backend.config import get_settings; import httpx
t = get_settings().telegram_bot_token
print(httpx.get(f'https://api.telegram.org/bot{t}/getMe', timeout=10).json())"
```

Healthy: `"ok": True`, username `cwiaibot`. Token/chat id come from the vault/`.env` via
`get_settings().telegram_bot_token`/`.telegram_chat_id` (`backend/integrations/telegram.py`
sendMessage). A vault-Brain push with a dead Telegram token prints
`TELEGRAM NOTIFY FAILED (check TELEGRAM_BOT_TOKEN)` and exits 1 — check the cron log for it.

## Force a relay run now + confirm

```
cd /home/brian/repos/nexus && .relay-venv/bin/python tools/relay_claude_digest.py
tail -20 logs/relay_claude_digest.cron.log
```

Healthy output: `relayed YYYY-MM-DD.md` (or `nothing new to relay` when already delivered —
`.relay_state.json` lists what's been relayed; `pending review/merge` means the auto-merge failed,
go back to (d)). Confirm landing in the Brain vault:

```
ssh -i ~/.ssh/id_nexus_lxc root@nexus-lxc ls -lt /var/lib/nexus/knowledge/Brain/raw/ | head
```

Expect `claude-features-digest-YYYY-MM-DD.md` with today's date.
