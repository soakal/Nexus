"""Deploy-drift detection — reads the repo's git HEAD off disk, pure stdlib.

Leaf module: no imports from the rest of `backend/`, no subprocess, no
asyncio, sync only. Used by `backend/agents/watchdog.py`'s deploy-drift
check to compare the commit the running process was booted from against
the repo's current HEAD, catching the "git pull happened but nobody
restarted NEXUS" failure mode (see this repo's CLAUDE.md "Restart After
Council Build" note for a real prior incident of this exact shape).

Worktree checkouts are OUT OF SCOPE: a worktree's `.git` is a file (a
`gitdir:` pointer), not a directory, so `get_git_head()` returns `None` for
one and the deploy-drift check silently no-ops there — production runs from
a normal checkout, not a worktree, so this is not a real gap.
"""
import pathlib

REPO_ROOT: pathlib.Path = pathlib.Path(__file__).resolve().parents[1]

_HEX40 = frozenset("0123456789abcdef")


def _is_hex40(s: str) -> bool:
    return len(s) == 40 and set(s.lower()) <= _HEX40


def get_git_head(repo_root: pathlib.Path | None = None) -> str | None:
    """Best-effort read of the repo's current HEAD commit SHA.

    Returns a 40-char lowercase hex string, or None on any failure
    (no .git dir, worktree gitdir-pointer file, unreadable/garbage refs,
    permission errors, etc). Never raises.
    """
    try:
        root = repo_root if repo_root is not None else REPO_ROOT
        git_dir = root / ".git"
        if not git_dir.is_dir():
            return None

        head_content = (git_dir / "HEAD").read_text(encoding="utf-8").strip()

        if head_content.startswith("ref: "):
            ref = head_content[len("ref: "):].strip()
        elif _is_hex40(head_content):
            return head_content.lower()
        else:
            return None

        loose_ref_path = git_dir / ref
        if loose_ref_path.exists():
            sha = loose_ref_path.read_text(encoding="utf-8").strip()
            return sha.lower() if _is_hex40(sha) else None

        packed_refs_path = git_dir / "packed-refs"
        if packed_refs_path.exists():
            for line in packed_refs_path.read_text(encoding="utf-8").splitlines():
                if not line or line.startswith("#") or line.startswith("^"):
                    continue
                parts = line.split(" ", 1)
                if len(parts) != 2:
                    continue
                sha, refname = parts[0], parts[1].strip()
                if refname == ref and _is_hex40(sha):
                    return sha.lower()

        return None
    except Exception:
        return None


_running_sha: str | None = None


def capture_running_sha() -> str | None:
    """Capture the repo's current HEAD once, at boot. Sets and returns the
    module-level running-SHA cache read later by running_sha()."""
    global _running_sha
    _running_sha = get_git_head()
    return _running_sha


def running_sha() -> str | None:
    """The SHA captured at boot by capture_running_sha(), or None if it was
    never captured or resolution failed."""
    return _running_sha


def reset() -> None:
    """Test hook — clears the captured running SHA. Mirrors watchdog.reset()."""
    global _running_sha
    _running_sha = None
