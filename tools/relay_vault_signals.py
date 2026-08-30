"""Relay the vault-signals digest (written by a cloud routine into
digests/vault-signals/*.md, see VAULT_SIGNALS_INSTRUCTIONS.md) into NEXUS's
own OutcomeFlag table via a POST to POST /api/safety/flags (backend/api/
safety.py::create_flag, which delegates server-side to
backend.agents.outcomes.record_flag).

Modeled structurally on the sibling tools/relay_claude_digest.py (dated-file
scan, .relay_state.json tracking already-processed filenames, best-effort/
fail-quiet philosophy). As of this cycle it also mirrors that script's gh
auto-merge trust boundary: `_open_and_merge_pending_digest_prs()`, called
first thing in `main()`, opens a PR for any pushed `digest/vault-YYYY-MM-DD`
branch that doesn't have one yet (only after verifying via `git diff` that
the branch touches nothing but its own dated digest file), then merges it --
and any branch that already had an open PR -- through the same checks
`relay_claude_digest.py::_merge_pending_digest_prs` applies: single-file
diff, same-repo/owner only, base must be `main`. See
VAULT_SIGNALS_INSTRUCTIONS.md for the routine-facing side of this (the
routine only ever commits and pushes the branch, never attempts to open a PR
itself; this relay is the sole thing that ever opens or merges one).

The digest format is unpinned (no dated digest file has ever gone through
this relay yet), so `_extract_findings` deliberately tolerates two shapes:
`## ` markdown sections (each `-`/`*`/numbered-list bullet line under a
section is its own finding, prefixed with the section title, regardless of
indentation -- a nested sub-bullet is NOT folded into its parent, it becomes
its own finding too; a bulletless section's own prose is one finding) and
flat `-`/`*`/numbered bullet lines outside any section (one finding per
bullet line). Whichever a given digest run actually used, findings come out
the same shape: a block of prose text.

Each finding is POSTed as:
    {"source": "vault_signals", "check": <slug>, "summary": <text>, "severity": "medium"}
to {NEXUS_BASE_URL or http://192.168.1.62:8000}/api/safety/flags, Bearer-
authed via NEXUS_API_KEY (env) then ~/.config/nexus/api_key (file), mirroring
Council-loop/scripts/postmortem_payload.py's own `_api_key()` contract
exactly. Unlike the old never-raising `record_flag` call this replaced, an
HTTP POST can genuinely fail -- so a file is only ever marked relayed in
.relay_state.json when EVERY one of its findings' POSTs actually succeeded;
a missing key or a failed POST leaves the file unmarked so the finding isn't
lost, rather than silently dropped (see `_relay_file`'s docstring).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DIGEST_DIR = REPO_ROOT / "digests" / "vault-signals"
STATE_FILE = DIGEST_DIR / ".relay_state.json"
_DATED_DIGEST = re.compile(r"^\d{4}-\d{2}-\d{2}\.md$")
_VAULT_DIGEST_BRANCH = re.compile(r"^digest/vault-(\d{4}-\d{2}-\d{2})$")
_REPO_OWNER = "soakal"
# Matches a `-`/`*` bullet OR an ordered-list item ("1. "/"1) ") -- shared by
# every bullet-detection site in _extract_findings so both list styles are
# recognized identically.
_BULLET = re.compile(r"^(?:[-*]|\d+[.)])\s+")

# Per-file finding cap -- a malformed/huge digest (or one that ignores the
# "only what's new/changed/stale" instruction) must not flood OutcomeFlag
# with hundreds of rows in one relay pass. 20 is a generous ceiling for a
# once-daily "what's new" digest; raise it if a real run legitimately needs
# more.
MAX_FINDINGS_PER_FILE = 20

_DEFAULT_BASE_URL = "http://192.168.1.62:8000"


def _pr_only_touches(number: int, expected_file: str) -> bool:
    """True only if PR #`number`'s entire diff is a single added/changed file
    at `expected_file`. Same security-boundary reasoning as
    relay_claude_digest.py::_pr_only_touches (duplicated here rather than
    imported, to keep the two relay scripts independent as the module
    docstring already establishes): the digest-writing routine processes
    untrusted vault content every run, so this is what stops a prompt
    injection against THAT routine from getting anything but the digest
    file itself auto-merged onto main."""
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


def _branch_diff_is_single_file(branch: str, expected_file: str) -> bool:
    """True only if `branch`'s entire diff against origin/main is exactly
    one file at `expected_file`. There's no PR yet to `gh pr view --json
    files` against for a branch that hasn't had one opened -- this checks
    the same single-file invariant `_pr_only_touches` checks post-PR,
    directly via git, so a PR is never even created for a branch that
    doesn't pass it."""
    try:
        fetch = subprocess.run(
            ["git", "fetch", "origin", branch],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
        if fetch.returncode != 0:
            return False
        diff = subprocess.run(
            ["git", "diff", "--name-only", "origin/main...FETCH_HEAD"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
        if diff.returncode != 0:
            return False
        files = [line.strip() for line in diff.stdout.splitlines() if line.strip()]
        return files == [expected_file]
    except Exception:
        return False


def _open_and_merge_pending_digest_prs() -> list[str]:
    """Best-effort: open a PR for any `digest/vault-YYYY-MM-DD` branch
    pushed to origin that doesn't have one yet, then merge it -- and any
    branch that already had an open PR -- via the local `gh` CLI.

    This relay already runs locally under Brian's own scheduled task + `gh`
    auth, the same trusted boundary relay_claude_digest.py::
    _merge_pending_digest_prs relies on, so it's safe for it to both open
    and merge the PR the digest-writing routine is deliberately barred from
    merging itself. Never raises -- any git/gh failure (not installed, not
    authed, network, no matching branches, merge conflict) is a logged skip;
    a missed auto-open/auto-merge just leaves the branch/PR for the next
    scheduled run or a manual merge.

    Every merge is gated by the same checks relay_claude_digest.py's sibling
    function applies, in the same order: base must be `main` (this repo has
    no branch protection or CI, so a PR based on the frozen `master` archive
    or an unexpected base must never auto-merge), the PR must not be
    cross-repo and must be authored by/owned by `_REPO_OWNER` (rejects a
    stranger's fork PR whose branch name they fully control), and the PR's
    entire diff must be exactly the branch's own dated digest file
    (`_pr_only_touches`) -- checked via `_branch_diff_is_single_file` BEFORE
    a PR-less branch ever gets a PR opened for it, and again via
    `_pr_only_touches` right before every merge, so a PR that changed after
    creation still can't slip through.
    """
    try:
        ls = subprocess.run(
            ["git", "ls-remote", "--heads", "origin", "digest/vault-*"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
        )
        if ls.returncode != 0:
            return []
        branches = []
        for line in ls.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            ref = line.split("\t")[-1]
            if ref.startswith("refs/heads/"):
                name = ref[len("refs/heads/"):]
                if _VAULT_DIGEST_BRANCH.match(name):
                    branches.append(name)
    except Exception:
        return []
    if not branches:
        return []

    try:
        pr_list = subprocess.run(
            [
                "gh", "pr", "list", "--state", "open",
                "--json", "number,headRefName,baseRefName,isDraft,isCrossRepository,author,headRepositoryOwner",
                "--limit", "20",
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
        if pr_list.returncode != 0:
            return []
        open_prs = json.loads(pr_list.stdout)
        if not isinstance(open_prs, list):
            return []
    except Exception:
        return []

    # Only a same-repo, soakal-owned PR counts as "already open" here -- a
    # public-repo forker needs no special access to open a PR named e.g.
    # digest/vault-2026-01-01 from their own fork. Without this filter, such
    # a PR would shadow this dict lookup for a genuine same-named origin
    # branch (skipping straight to the gating checks below, which correctly
    # reject the fork PR -- so this was never a merge/security hole) and
    # block this script from ever auto-creating the legitimate first-party
    # PR for that date. Security-fix note: the isCrossRepository/owner/base
    # checks further down are UNCHANGED and still re-run on every PR
    # (including ones found here) -- this filter only affects which PR is
    # treated as "already exists", not what's allowed to merge.
    open_by_branch = {
        pr["headRefName"]: pr
        for pr in open_prs
        if isinstance(pr, dict)
        and pr.get("headRefName")
        and not pr.get("isCrossRepository")
        and isinstance(pr.get("headRepositoryOwner"), dict)
        and pr["headRepositoryOwner"].get("login") == _REPO_OWNER
        and isinstance(pr.get("author"), dict)
        and pr["author"].get("login") == _REPO_OWNER
    }

    merged: list[str] = []
    for branch in branches:
        date_match = _VAULT_DIGEST_BRANCH.match(branch)
        if not date_match:
            continue
        expected_file = f"digests/vault-signals/{date_match.group(1)}.md"

        pr = open_by_branch.get(branch)
        if pr is None:
            if not _branch_diff_is_single_file(branch, expected_file):
                continue
            try:
                create = subprocess.run(
                    [
                        "gh", "pr", "create", "--base", "main", "--head", branch,
                        "--title", f"vault-signals digest {date_match.group(1)}",
                        "--body", "Automated vault-signals digest -- see VAULT_SIGNALS_INSTRUCTIONS.md.",
                    ],
                    cwd=REPO_ROOT,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=30,
                )
                if create.returncode != 0:
                    print(f"gh pr create failed for {branch}: {create.stderr.strip()}")
                    continue
            except Exception as e:
                print(f"gh pr create raised for {branch}: {e}")
                continue
            try:
                view = subprocess.run(
                    [
                        "gh", "pr", "view", branch,
                        "--json", "number,headRefName,baseRefName,isDraft,isCrossRepository,author,headRepositoryOwner",
                    ],
                    cwd=REPO_ROOT,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=30,
                )
                if view.returncode != 0:
                    continue
                pr = json.loads(view.stdout)
                if not isinstance(pr, dict):
                    continue
            except Exception:
                continue

        number = pr.get("number")
        if number is None:
            continue
        if pr.get("baseRefName") != "main":
            print(f"NOT merging vault-signals digest PR #{number}: base is {pr.get('baseRefName')!r}, not 'main' -- retarget it on GitHub")
            continue
        owner = pr.get("headRepositoryOwner")
        author = pr.get("author")
        if pr.get("isCrossRepository"):
            continue
        if not isinstance(owner, dict) or owner.get("login") != _REPO_OWNER:
            continue
        if not isinstance(author, dict) or author.get("login") != _REPO_OWNER:
            continue
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
            pull = subprocess.run(
                ["git", "pull", "--quiet"],
                cwd=REPO_ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
            )
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


def _extract_findings(content: str) -> list[str]:
    """Pull candidate findings out of a digest's markdown body.

    Two shapes tolerated (the digest format is unpinned, see module
    docstring): a `## ` heading starts a new section, and EACH `-`/`*`/
    numbered-list bullet line under it is its own finding, prefixed with the
    section title for context (e.g. "Work — <bullet text>") so the slug
    stays keyed off the bullet's own stable text rather than the whole
    section's ever-changing bullet list. Indentation is discarded (every
    line is `.strip()`'d before matching), so a nested sub-bullet becomes
    its own finding too, not folded into its parent. Non-bullet prose
    directly under a section (no bullets at all) is its own single finding.
    A bullet line encountered OUTSIDE any active `## ` section is its own
    finding, same as before.
    """
    findings: list[str] = []
    section_title: str | None = None
    section_body: list[str] = []
    section_had_bullet = False

    def flush() -> None:
        if section_title is not None and not section_had_bullet:
            text = " ".join([section_title, *section_body]).strip()
            if text:
                findings.append(text)

    for raw_line in content.splitlines():
        line = raw_line.strip()
        if line.startswith("## "):
            flush()
            section_title = line[3:].strip()
            section_body = []
            section_had_bullet = False
        elif section_title is not None:
            bullet_match = _BULLET.match(line)
            if bullet_match:
                section_had_bullet = True
                bullet_text = _BULLET.sub("", line).strip()
                findings.append(f"{section_title} — {bullet_text}")
            elif line:
                section_body.append(line)
        elif _BULLET.match(line):
            findings.append(_BULLET.sub("", line).strip())
    flush()
    return findings


def _slugify(text: str) -> str:
    """Derive a stable, deterministic slug from a finding's own text so a
    still-open finding re-surfaced on a later digest bumps the SAME
    OutcomeFlag row instead of duplicating it (record_flag dedups on
    source:check).

    # ponytail: the slug is derived from wording, not meaning -- if a later
    # digest rewords the same underlying finding, it gets treated as a new
    # finding (new flag) rather than a bump. Known, accepted limitation for
    # v1; revisit only if reworded-finding churn shows up in practice.
    """
    stripped = re.sub(r"^\s*\d+[.)]\s*", "", text)  # leading "1. "/"1) " numbering
    slug = re.sub(r"[^a-z0-9]+", "-", stripped.lower()).strip("-")
    digest = hashlib.sha1(stripped.encode("utf-8")).hexdigest()[:8]
    return (slug[:48].strip("-") or "finding") + "-" + digest


def _load_relayed() -> set[str]:
    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            return {name for name in data if isinstance(name, str)}
        except Exception as e:
            print(f"could not read {STATE_FILE.name}, treating as empty: {e}")
    return set()


def _save_relayed(names: set[str]) -> None:
    STATE_FILE.write_text(json.dumps(sorted(names), indent=2), encoding="utf-8")


def _api_key() -> str | None:
    """NEXUS_API_KEY env var, then ~/.config/nexus/api_key -- mirrors
    Council-loop/scripts/postmortem_payload.py's own `_api_key()` exactly,
    plus a read guard (security audit fix) so a malformed/unreadable key
    file (bad permissions, non-UTF8 bytes) degrades to "no key" the same as
    a missing file, rather than crashing main() with an unguarded read."""
    key = os.environ.get("NEXUS_API_KEY")
    if key:
        return key
    path = Path.home() / ".config" / "nexus" / "api_key"
    try:
        if path.exists():
            return path.read_text(encoding="utf-8").strip() or None
    except Exception as e:
        print(f"could not read {path}: {e}")
    return None


def _post_flag(base_url: str, key: str, check: str, summary: str) -> bool:
    """POST one finding to NEXUS's POST /api/safety/flags. Returns True on a
    2xx response, False on any failure (bad status, network error, timeout,
    malformed response) -- never raises. Exposed as its own module-level
    function so tests can patch it without opening a real socket."""
    body = json.dumps(
        {"source": "vault_signals", "check": check, "summary": summary, "severity": "medium"}
    ).encode("utf-8")
    req = urllib.request.Request(
        f"{base_url}/api/safety/flags",
        data=body,
        method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return 200 <= resp.status < 300
    except Exception as e:
        print(f"POST /api/safety/flags failed for {check}: {e}")
        return False


def _relay_file(path: Path, base_url: str, key: str) -> bool:
    """Relay every finding in one digest file. Returns True only if EVERY
    finding's POST succeeded -- callers must only mark this file relayed in
    .relay_state.json when this returns True, since (unlike the old
    never-raising record_flag call this replaced) an HTTP POST can genuinely
    fail, and marking a file relayed despite a failed POST would lose that
    finding forever. A misbehaving _post_flag call is caught per-finding and
    must not stop the rest of the batch -- but a read failure (e.g.
    path.read_text()) can still raise; callers must still guard the read."""
    content = path.read_text(encoding="utf-8")
    findings = _extract_findings(content)[:MAX_FINDINGS_PER_FILE]
    all_ok = True
    posted = 0
    for finding in findings:
        slug = _slugify(finding)
        summary = finding[:300]
        try:
            ok = _post_flag(base_url, key, slug, summary)
        except Exception as e:
            print(f"POST failed for {path.name} ({slug}): {e}")
            ok = False
        if ok:
            posted += 1
        else:
            all_ok = False
    print(f"{path.name}: {posted}/{len(findings)} finding(s) posted")
    return all_ok


def main() -> int:
    merged = _open_and_merge_pending_digest_prs()
    if merged:
        print(f"auto-merged {len(merged)} vault-signals digest PR(s): {', '.join(merged)}")

    if not DIGEST_DIR.exists():
        print("no digests/vault-signals/ dir yet — nothing to relay")
        return 0

    key = _api_key()
    if not key:
        print("vault-signals relay skipped: no NEXUS_API_KEY (env or ~/.config/nexus/api_key)")
        return 0

    relayed = _load_relayed()
    files = sorted(
        p for p in DIGEST_DIR.glob("*.md")
        if _DATED_DIGEST.match(p.name) and p.name not in relayed
    )
    if not files:
        print("nothing new to relay")
        return 0

    base_url = os.environ.get("NEXUS_BASE_URL", _DEFAULT_BASE_URL)

    any_failed = False
    for f in files:
        try:
            ok = _relay_file(f, base_url, key)
        except Exception as e:
            print(f"FAILED to relay {f.name}: {e}")
            any_failed = True
            continue
        if ok:
            relayed.add(f.name)
        else:
            any_failed = True

    _save_relayed(relayed)
    return 1 if any_failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
