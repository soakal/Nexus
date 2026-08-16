"""Relay the daily Claude-features digest (written by a cloud routine into
digests/claude-features/*.md) into the Brain vault + a Telegram notify.

Run daily at 09:20 US/Eastern by a cron job in `brian`'s crontab on devbox,
shortly after the cloud routine's 09:00 America/New_York run. (It used to be
a Windows Scheduled Task; Windows production was decommissioned 2026-08-15.)
The cron entry does the `git pull` before invoking this.

Two things this script depends on that have each silently broken it before --
check them first if a digest stops arriving:
  * `.relay-venv/` must exist with httpx + pydantic-settings + cryptography.
  * The digest routine must target the SAME branch devbox has checked out
    (`main`). A digest merged to `master` -- the frozen Windows archive --
    is invisible here and relays to nobody. See DIGEST_INSTRUCTIONS.md.
"""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DIGEST_DIR = REPO_ROOT / "digests" / "claude-features"
STATE_FILE = DIGEST_DIR / ".relay_state.json"
_DATED_DIGEST = re.compile(r"^\d{4}-\d{2}-\d{2}\.md$")
_DIGEST_BRANCH = re.compile(r"^digest/(\d{4}-\d{2}-\d{2})$")
_REPO_OWNER = "soakal"

sys.path.insert(0, str(REPO_ROOT))


def _pending_digest_branches() -> list[str]:
    """Best-effort check for `digest/*` branches pushed to origin but not yet
    merged to main (i.e. a digest PR still awaiting review/merge).

    The digest routine now lands its output on a branch + PR instead of
    committing straight to main (see DIGEST_INSTRUCTIONS.md) -- without
    this check, a pending PR and "no digest ran today" both print the exact
    same "nothing new to relay" line, and the daily Brain/Telegram delivery
    silently stops with zero signal that anything changed. Uses only `git`,
    which the devbox cron entry already requires (it runs `git pull` before
    this script) -- no new dependency, no new credential. Returns [] on any
    failure (git missing, no network, unexpected output, etc.) rather than
    raising; this is a best-effort notice, not a hard requirement.
    """
    try:
        result = subprocess.run(
            ["git", "ls-remote", "--heads", "origin", "digest/*"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
        )
        if result.returncode != 0:
            return []
        branches = []
        for line in result.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split("\t")
            ref = parts[-1] if parts else ""
            if ref.startswith("refs/heads/"):
                branches.append(ref[len("refs/heads/"):])
        return branches
    except Exception:
        return []


def _pr_only_touches(number: int, expected_file: str) -> bool:
    """True only if PR #`number`'s entire diff is a single added/changed file
    at `expected_file`.

    This is the actual security boundary, not the branch-name/owner check
    above: the cloud routine itself processes untrusted web content every
    run (that's the whole reason it's barred from pushing straight to
    main -- see DIGEST_INSTRUCTIONS.md). A prompt injection against THAT
    routine, not against this relay, could in principle steer it into
    committing something other than the digest file on its own branch. This
    check means such an injection can only ever poison the digest's prose
    (already treated as untrusted summary text on its way to Brain/Telegram
    -- never executed), never actual repo code -- anything else on the
    branch blocks the auto-merge outright and falls back to the existing
    manual-review path.
    """
    try:
        result = subprocess.run(
            ["gh", "pr", "view", str(number), "--json", "files"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
        if result.returncode != 0:
            return False
        data = json.loads(result.stdout)
        files = data.get("files")
        if not isinstance(files, list) or len(files) != 1:
            return False
        return files[0].get("path") == expected_file
    except Exception:
        return False


def _merge_pending_digest_prs() -> list[str]:
    """Merge any open digest/* PR via the local `gh` CLI before checking for
    new files to relay.

    The cloud routine is barred from pushing straight to main (see
    DIGEST_INSTRUCTIONS.md's 2026-07-27 security fix -- it processes
    untrusted web content every run, so it opens a PR instead of committing
    directly). This relay already runs locally under Brian's own scheduled
    task + `gh` auth -- a materially different, trusted boundary -- so it's
    safe for it to do the merge the cloud agent is deliberately barred from
    doing itself. Best-effort: any `gh` failure (not installed, not authed,
    network, merge conflict) is swallowed -- a missed auto-merge just falls
    back to the existing "pending PR" notice in main().

    Two checks gate every merge, both required (see docstrings): this repo
    is PUBLIC with no branch protection and no CI, so `gh pr list` returning
    a stranger's fork PR is a real, not theoretical, input.
    """
    try:
        result = subprocess.run(
            [
                "gh", "pr", "list", "--state", "open",
                "--json", "number,headRefName,isDraft,isCrossRepository,author,headRepositoryOwner",
                "--limit", "20",
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
        if result.returncode != 0:
            return []
        prs = json.loads(result.stdout)
        if not isinstance(prs, list):
            return []
    except Exception:
        return []

    merged = []
    for pr in prs:
        if not isinstance(pr, dict):
            continue
        branch = pr.get("headRefName") or ""
        date_match = _DIGEST_BRANCH.match(branch)
        number = pr.get("number")
        if not date_match or number is None:
            continue

        # Reject any fork PR outright -- gh pr list returns every open PR
        # targeting this repo, including cross-repo PRs whose branch name
        # an attacker fully controls (e.g. a stranger opening
        # digest/2026-07-30 from their own fork). Without this, anyone
        # could get arbitrary code auto-merged onto main and pulled onto
        # Brian's machine.
        owner = pr.get("headRepositoryOwner")
        author = pr.get("author")
        if pr.get("isCrossRepository"):
            continue
        if not isinstance(owner, dict) or owner.get("login") != _REPO_OWNER:
            continue
        if not isinstance(author, dict) or author.get("login") != _REPO_OWNER:
            continue

        expected_file = f"digests/claude-features/{date_match.group(1)}.md"
        if not _pr_only_touches(number, expected_file):
            continue

        try:
            if pr.get("isDraft"):
                subprocess.run(
                    ["gh", "pr", "ready", str(number)],
                    cwd=REPO_ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
                )
            merge_result = subprocess.run(
                ["gh", "pr", "merge", str(number), "--merge", "--delete-branch"],
                cwd=REPO_ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
            )
            if merge_result.returncode == 0:
                merged.append(branch)
        except Exception:
            continue

    if merged:
        pull_ok = False
        try:
            pull = subprocess.run(["git", "pull", "--quiet"], cwd=REPO_ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
            pull_ok = pull.returncode == 0
        except Exception:
            pass
        if not pull_ok:
            print(
                f"WARNING: merged {len(merged)} digest PR(s) but `git pull` "
                "failed -- today's digest may not relay until the next "
                "scheduled run pulls it"
            )
    return merged


def _load_relayed() -> set[str]:
    if STATE_FILE.exists():
        return set(json.loads(STATE_FILE.read_text(encoding="utf-8")))
    return set()


def _save_relayed(names: set[str]) -> None:
    STATE_FILE.write_text(json.dumps(sorted(names), indent=2), encoding="utf-8")


async def _push_to_brain(filename: str, content: str) -> bool:
    import httpx
    from backend.config import get_settings
    from backend.http_client import SSL_CONTEXT

    settings = get_settings()
    url = f"{settings.brain_mcp_url.rstrip('/')}/raw"
    headers = {}
    try:
        if settings.brain_mcp_token:
            headers["Authorization"] = f"Bearer {settings.brain_mcp_token}"
    except Exception:
        pass
    async with httpx.AsyncClient(verify=SSL_CONTEXT, timeout=15) as client:
        resp = await client.post(url, json={"content": content, "filename": filename}, headers=headers)
        resp.raise_for_status()
        return True


async def _notify_telegram(date_str: str, content: str) -> bool:
    from backend.integrations import telegram

    body = content if len(content) <= 3500 else content[:3500] + "\n\n...(truncated, full digest saved to the Brain vault)"
    payload = {
        "type": "claude_features_digest",
        "content": f"Claude + AI Digest — {date_str}\n\n{body}",
    }
    return await telegram.notify(payload)


async def main() -> int:
    if not DIGEST_DIR.exists():
        print("no digests/claude-features/ dir yet — nothing to relay")
        return 0

    merged = _merge_pending_digest_prs()
    if merged:
        print(f"auto-merged {len(merged)} digest PR(s): {', '.join(merged)}")

    relayed = _load_relayed()
    files = sorted(
        p for p in DIGEST_DIR.glob("*.md")
        if _DATED_DIGEST.match(p.name) and p.name not in relayed
    )
    if not files:
        pending = _pending_digest_branches()
        if pending:
            print(
                f"nothing new to relay on main -- but {len(pending)} digest "
                f"PR(s)/branch(es) pending review/merge: {', '.join(pending)} "
                "(check GitHub for an open PR before assuming today's digest "
                "didn't run)"
            )
        else:
            print("nothing new to relay")
        return 0

    any_failed = False
    for f in files:
        content = f.read_text(encoding="utf-8")
        date_str = f.stem
        try:
            await _push_to_brain(f"claude-features-digest-{date_str}.md", content)
            ok = await _notify_telegram(date_str, content)
            relayed.add(f.name)
            if ok:
                print(f"relayed {f.name}")
            else:
                print(f"relayed {f.name} to Brain vault but TELEGRAM NOTIFY FAILED (check TELEGRAM_BOT_TOKEN)")
                any_failed = True
        except Exception as e:
            print(f"FAILED to relay {f.name}: {e}")
            any_failed = True

    _save_relayed(relayed)
    return 1 if any_failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
