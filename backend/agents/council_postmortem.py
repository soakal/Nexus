"""Independently re-verify a Council-loop session's real commit range against
its own claims — the "did the Realist's prose match reality" check.

Council-loop (a separate repo, C:\\Users\\Brian\\Documents\\Council-loop) auto-commits
one step at a time against whatever `target_repo` it's building, and nobody
re-reads the diffs afterward. The Realist role is instructed to run its own
verification, but nothing captures whether it actually did — only its own
prose claim survives, in `.council/state/transcripts/cycle-NNNN.md`.

Triggered by Council-loop's own `run-loop.ps1` POSTing to `/api/trigger`
{"task_name": "council_postmortem"} at driver exit (see backend/api/trigger.py)
— NOT scheduled here on NEXUS's own scheduler, because `/goal` TRUNCATES
`.council/state/history.jsonl` on every new session, so a poller that missed
the window would lose that session's history permanently and silently.

Independence comes from being DETERMINISTIC (git + Python ast), not from using
a different model — Council-loop's own `.council/config.local.json` currently
assigns Arbiter=opus, Engineer=sonnet, Security=sonnet, Realist=opus, so
there's no smarter model to delegate to. The one LLM call here (extracting a
file allowlist from goal.md's prose Objective) uses Haiku purely because it's
the only role-free model and the task is extraction, not judgment.

Read-only. Never writes to the target repo (no checkout/revert/stash — only
git log/diff/cat-file/rev-parse). Never raises — every public entry point
returns a dict, even on total failure.
"""

import ast
import logging
import re
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

_EMPTY_TREE_HASH = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"

# Path-like tokens the test-claim check extracts from Realist/Engineer prose —
# deliberately simple (regex, no LLM): a run of path-safe characters containing
# at least one slash, ending in a dotted extension. Good enough for citations
# like "tests/test_roi.py" or "src\\kb\\store.py"; not a general path parser.
_PATH_TOKEN_RE = re.compile(r"[A-Za-z0-9_.\-/\\]*[/\\][A-Za-z0-9_.\-/\\]+\.[A-Za-z][A-Za-z0-9]{0,4}")

# Without this, the regex above matches ordinary prose too eagerly — verified
# live against a real transcript: a URL ("https://github.com/x/y") extracts as
# "github.com", and "and/or.So it goes" extracts as "and/or.So". Restricting
# to plausible code/doc extensions kills both (neither "com" nor "So" is in
# this set) without needing a real path parser. Digits are allowed in the
# extension (ps1/psm1) since Council-loop and some of its target repos are
# PowerShell-heavy — "ps" alone (no digit) is deliberately absent so a
# truncated "deploy.ps" (missing the trailing "1") isn't itself accepted as
# if it were a real extension.
_PLAUSIBLE_EXTENSIONS = {
    "py", "ps1", "psm1", "js", "jsx", "ts", "tsx", "json", "md", "yaml", "yml",
    "toml", "cfg", "ini", "txt", "sh", "bash", "sql", "html", "css",
    "env", "example", "lock", "csv",
}


def _looks_like_url(text: str, match_start: int) -> bool:
    """True if the match starts at (or just after) a URL's '://' — a URL's
    domain otherwise passes the extension allowlist check trivially for
    extensions like .io/.dev that are also real file extensions. The window
    must extend PAST match_start, not stop at it: the regex's leading
    `[/\\\\]` is what consumes the "//" of "https://", so the match itself
    starts ON the slashes, not after them — a window ending at match_start
    never contains the "//" and this check would silently never fire
    (found live: verified "://" never appeared in a look-behind-only window)."""
    window = text[max(0, match_start - 8):match_start + 3]
    return "://" in window


def _git(cwd: str, *args: str, timeout: int = 30) -> str:
    """Run a git command in cwd, return stdout stripped. Raises RuntimeError
    on nonzero exit. Sync — every call site awaits this via asyncio.to_thread,
    never directly: NEXUS forces the SelectorEventLoop and spawns no in-loop
    subprocesses (see CLAUDE.md), so this must never be called from the loop.
    """
    result = subprocess.run(
        ["git", "-C", cwd, *args],
        capture_output=True, text=True, timeout=timeout,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def _effective_config(council_root: str) -> dict:
    import json
    script = str(Path(council_root) / "scripts" / "council_state.py")
    result = subprocess.run(
        [sys.executable, script, "effective-config"],
        cwd=council_root, capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"council_state.py effective-config failed: {result.stderr.strip()}")
    return json.loads(result.stdout)


def _read_history(council_root: str) -> list[dict]:
    import json
    path = Path(council_root) / ".council" / "state" / "history.jsonl"
    if not path.exists():
        return []
    lines = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except Exception:
            continue  # malformed line — skip, don't fail the whole read
        if isinstance(item, dict):
            lines.append(item)
    return lines


def _read_goal(council_root: str) -> str:
    path = Path(council_root) / ".council" / "state" / "goal.md"
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def _read_transcripts(council_root: str) -> str:
    """Concatenate every transcript's text — used only for regex extraction
    of cited test paths, never parsed as structured data."""
    tdir = Path(council_root) / ".council" / "state" / "transcripts"
    if not tdir.exists():
        return ""
    chunks = []
    for f in sorted(tdir.glob("cycle-*.md")):
        try:
            chunks.append(f.read_text(encoding="utf-8"))
        except Exception:
            continue
    return "\n".join(chunks)


def _derive_range(history: list[dict], target: str) -> tuple[str, str] | None:
    """Return (first_full_sha, last_full_sha) spanning the whole session, or
    None if the session has no real commits (every cycle deferred, or
    auto_commit=false)."""
    commits = [h.get("commit") for h in history if h.get("commit")]
    if not commits:
        return None

    first_short, last_short = commits[0], commits[-1]
    # --verify ...^{commit}, not a bare rev-parse: a bare `git rev-parse
    # <nonexistent-40-char-hex>` exits 0 and echoes the input back verbatim
    # (verified live) instead of failing — only ^{commit} actually confirms
    # the object exists AND is a commit. Without this, a 40-char SHA that no
    # longer resolves (Council-loop's Arbiter sometimes runs a bare `git
    # rev-parse HEAD`, unlike the 7-char `--short HEAD` history normally
    # stores) silently produces a bogus range instead of raising here, and
    # the failure resurfaces one step later with no notification at all.
    first = _git(target, "rev-parse", "--verify", f"{first_short}^{{commit}}")
    last = _git(target, "rev-parse", "--verify", f"{last_short}^{{commit}}")
    return (first, last)


def _range_expr(target: str, first: str, last: str) -> str:
    """first^..last, substituting the empty-tree hash if first is the repo's
    very first commit (no parent to diff against)."""
    try:
        _git(target, "rev-parse", "--verify", f"{first}^")
        base = f"{first}^"
    except RuntimeError:
        base = _EMPTY_TREE_HASH
    return f"{base}..{last}"


def _count_council_commits(target: str, rng: str, commit_prefix: str) -> tuple[int, int]:
    """Return (council_commit_count, foreign_commit_count) in rng, split by
    whether the subject line starts with commit_prefix (e.g. "council: cycle
    3: ..."). Foreign commits are counted, never scanned by the checks below."""
    log = _git(target, "log", "--format=%H%x00%s", rng)
    council, foreign = 0, 0
    for line in log.splitlines():
        if not line:
            continue
        _, _, subject = line.partition("\x00")
        if subject.startswith(commit_prefix):
            council += 1
        else:
            foreign += 1
    return council, foreign


def _normalize_path(p: str) -> str:
    """git diff --name-only always emits forward slashes with no leading
    './'; the goal-extraction LLM call is asked for paths "verbatim", and
    goal.md text (e.g. Windows paths, a leading './') would otherwise never
    match a real touched file. Normalize both sides through this."""
    return p.strip().replace("\\", "/").removeprefix("./")


def _check_scope_drift(target: str, rng: str, allowed: list[str]) -> list[dict]:
    touched = [f for f in _git(target, "diff", "--name-only", rng).splitlines() if f]
    allowed_norm = [_normalize_path(a) for a in allowed]
    findings = []
    for f in touched:
        fn = _normalize_path(f)
        if not any(fn == a or fn.startswith(a.rstrip("/") + "/") for a in allowed_norm):
            findings.append({
                "check": "scope_drift",
                "severity": "medium",
                "detail": f"'{f}' was touched but is outside the stated plan",
            })
    return findings


_PLACEHOLDER_BODY_TYPES = (ast.Pass,)


def _is_placeholder_body(body: list) -> str | None:
    """Return a reason string if a function/class body is a placeholder, else
    None. Placeholder = exactly `pass`, exactly `...`, a bare docstring with
    nothing else, or a body that's just `raise NotImplementedError(...)`."""
    stmts = list(body)
    # Drop a leading docstring-only Expr(Constant(str)) to see what's left.
    if stmts and isinstance(stmts[0], ast.Expr) and isinstance(stmts[0].value, ast.Constant) \
            and isinstance(stmts[0].value.value, str):
        rest = stmts[1:]
    else:
        rest = stmts

    if not rest:
        return "docstring-only body"
    if len(rest) == 1:
        s = rest[0]
        if isinstance(s, ast.Pass):
            return "body is `pass`"
        if isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant) and s.value.value is Ellipsis:
            return "body is `...`"
        if isinstance(s, ast.Raise) and isinstance(s.exc, ast.Call) \
                and isinstance(s.exc.func, ast.Name) and s.exc.func.id == "NotImplementedError":
            return "body is `raise NotImplementedError`"
        if isinstance(s, ast.Raise) and isinstance(s.exc, ast.Name) and s.exc.id == "NotImplementedError":
            return "body is `raise NotImplementedError`"
    return None


def _check_placeholders(target: str, rng: str, max_files: int = 200) -> list[dict]:
    findings = []
    changed = [f for f in _git(target, "diff", "--name-only", rng).splitlines() if f.endswith(".py")]
    if len(changed) > max_files:
        # Bounds the 2-subprocesses-per-file cost — an ordinary session never
        # comes close (ProcessForge's largest real cycle range touched ~12
        # files); a session this large is unusual enough to flag rather than
        # silently truncate or silently take minutes.
        findings.append({
            "check": "placeholder", "severity": "low",
            "detail": f"{len(changed)} changed .py files exceeds council_postmortem_max_files "
                      f"({max_files}) — skipping the placeholder scan for this session",
        })
        return findings

    for f in changed:
        try:
            source = _git(target, "show", f"{rng.split('..')[-1]}:{f}")
        except RuntimeError:
            continue  # file deleted in this range — nothing to parse
        try:
            tree = ast.parse(source)
        except SyntaxError as e:
            findings.append({
                "check": "placeholder",
                "severity": "high",
                "detail": f"{f} fails to parse ({e}) — the Realist claimed to have reviewed this diff",
            })
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                reason = _is_placeholder_body(node.body)
                if reason:
                    findings.append({
                        "check": "placeholder",
                        "severity": "medium",
                        "detail": f"{f}:{node.lineno} {node.name}() {reason}",
                    })

        # Added lines only — a TODO/FIXME on an unchanged context line is not
        # new cruft; on a `+` line it is.
        try:
            diff_text = _git(target, "diff", "-U0", rng, "--", f)
        except RuntimeError:
            diff_text = ""
        for line in diff_text.splitlines():
            if not line.startswith("+") or line.startswith("+++"):
                continue
            if re.search(r"\b(TODO|FIXME|XXX|HACK)\b", line) or "# ponytail:" in line:
                findings.append({
                    "check": "placeholder",
                    "severity": "low",
                    "detail": f"{f}: new line contains a cruft marker — {line[1:].strip()[:80]}",
                })
    return findings


def _check_test_claims(target: str, last: str, history: list[dict], transcripts: str) -> list[dict]:
    text = transcripts + "\n" + "\n".join(h.get("notes", "") for h in history)
    candidates = set()
    for m in _PATH_TOKEN_RE.finditer(text):
        token = m.group(0).lstrip("/\\")
        ext = token.rsplit(".", 1)[-1].lower()
        if ext not in _PLAUSIBLE_EXTENSIONS:
            continue
        if _looks_like_url(text, m.start()):
            continue
        candidates.add(token)

    findings = []
    for path in sorted(candidates):
        try:
            _git(target, "cat-file", "-e", f"{last}:{path}")
        except RuntimeError:
            findings.append({
                "check": "test_claim",
                "severity": "high",
                "detail": f"cited '{path}' does not exist at the reviewed commit {last[:10]}",
            })
    return findings


async def _extract_allowed_paths(goal_text: str, model: str) -> tuple[list[str], bool]:
    """One LLM call (model from settings.council_postmortem_model — see
    config.py for why Haiku is the default): does the goal name specific
    files/paths, and if so which. explicit=False means the goal never named
    files, in which case the scope-drift check must be SKIPPED entirely — a
    fabricated allowlist would produce nothing but false positives."""
    if not goal_text.strip():
        return [], False

    from backend.agents.router import run_model
    prompt = f"""Read this build goal. Does it explicitly name specific files or paths that
are in-scope (e.g. "touch ONLY x.py, y.ps1")?

GOAL:
{goal_text}

Reply with ONLY a JSON object, no other text: {{"explicit": true|false, "paths": ["path1", ...]}}
"explicit" is true only if the goal names actual file/path tokens. "paths" is
the list of those tokens verbatim (empty list if explicit is false)."""

    try:
        raw = await run_model(model, prompt, label="council_postmortem", max_tokens=1024)
        import json
        data = json.loads(raw.strip().strip("`").removeprefix("json").strip())
        paths = [str(p) for p in (data.get("paths") or [])]
        explicit = bool(data.get("explicit")) and bool(paths)
        return paths, explicit
    except Exception as e:
        logger.warning(f"council_postmortem: allowlist extraction failed (skipping scope check): {e}")
        return [], False


def _format_summary(session: dict, target: str, rng: str | None, findings: list[dict],
                     council_commits: int, foreign_commits: int) -> str:
    repo_name = Path(target).name
    header = f"Council post-mortem — {repo_name}, {session.get('cycles', 0)} cycles"
    if rng:
        header += f", {council_commits} commit(s) ({rng})"
    lines = [header]
    for f in findings[:5]:
        lines.append(f"{f['check'].upper()}: {f['detail']}")
    if foreign_commits:
        lines.append(f"{foreign_commits} non-council commit(s) in range (not scanned).")
    return "\n".join(lines)[:800]


async def run_postmortem(*, since: str | None = None) -> dict:
    """Independently re-verify a Council-loop session against its target
    repo's real commit range. Best-effort: NEVER raises. Returns a summary
    dict; see module docstring for the full design rationale.

    `since` is accepted (Council-loop's run-loop.ps1 sends its own
    `$runStart`) but deliberately NOT used to filter history: `/goal`
    truncates `.council/state/history.jsonl` on every new session, so the
    whole file already IS exactly one session — there's nothing left to
    filter by timestamp. Kept as a parameter so the caller's contract doesn't
    need to change if a future version needs it (e.g. once Council-loop
    stops truncating history and starts appending across sessions)."""
    import asyncio

    target = None  # bound early so the outer except below can still report
                   # which repo (if any) was being checked when it failed.
    try:
        from backend.config import get_settings
        s = get_settings()
        if not getattr(s, "council_postmortem_enabled", True):
            return {"ok": False, "skipped": "council_postmortem_enabled is False"}

        council_root = getattr(s, "council_loop_path", None)
        if not council_root or not Path(council_root).exists():
            return {"ok": False, "skipped": f"council_loop_path not found: {council_root}"}

        cfg = await asyncio.to_thread(_effective_config, council_root)
        target = cfg["target_repo"]
        commit_prefix = cfg.get("commit_prefix", "council:")

        history = await asyncio.to_thread(_read_history, council_root)
        goal_text = await asyncio.to_thread(_read_goal, council_root)
        transcripts = await asyncio.to_thread(_read_transcripts, council_root)

        session = {
            "goal": goal_text.strip()[:200],
            "cycles": len(history),
        }

        try:
            derived = await asyncio.to_thread(_derive_range, history, target)
        except Exception as e:
            # A commit SHA the history recorded no longer resolves in the
            # target repo (rebase/amend/force-push, or target_repo pointed
            # somewhere new since this history was written). This must NOT
            # fall through to the outer except and die silently — that would
            # make a permanently-broken post-mortem indistinguishable from a
            # clean one on Brian's phone, the worst failure mode for a feature
            # whose whole premise is "nobody re-reads this otherwise" (found
            # live 2026-07-26 against real Council-loop state).
            from backend import events
            notified = await events.notify_phone(
                f"Council post-mortem — {Path(target).name}: could not verify this "
                f"session ({e}). The target repo's git history may have diverged "
                f"from what Council-loop recorded (rebase/amend/force-push?).",
                kind="council_postmortem",
            )
            return {
                "ok": False, "session": session, "target_repo": target, "range": None,
                "findings": [{"check": "range_resolution", "severity": "high", "detail": str(e)}],
                "council_commits": 0, "foreign_commits": 0,
                "notified": notified, "skipped": None,
            }

        if derived is None:
            return {
                "ok": True, "session": session, "target_repo": target, "range": None,
                "findings": [], "council_commits": 0, "foreign_commits": 0,
                "notified": False, "skipped": "no commits in session — nothing to check",
            }
        first, last = derived
        rng = await asyncio.to_thread(_range_expr, target, first, last)

        council_commits, foreign_commits = await asyncio.to_thread(
            _count_council_commits, target, rng, commit_prefix
        )

        model = getattr(s, "council_postmortem_model", "claude-haiku-4-5-20251001")
        allowed_paths, explicit = await _extract_allowed_paths(goal_text, model)

        max_files = getattr(s, "council_postmortem_max_files", 200)
        findings: list[dict] = []
        if explicit:
            findings += await asyncio.to_thread(_check_scope_drift, target, rng, allowed_paths)
        findings += await asyncio.to_thread(_check_placeholders, target, rng, max_files)
        findings += await asyncio.to_thread(_check_test_claims, target, last, history, transcripts)

        if getattr(s, "council_postmortem_run_tests", False):
            # Opt-in only (default False) — running a foreign repo's configured
            # test_commands inside this process is a much bigger trust step
            # than reading its git history. Not implemented in v1; the flag
            # exists so a future version can wire it in without a schema change.
            logger.info("council_postmortem_run_tests is True but running tests is not yet implemented")

        notified = False
        if findings:
            from backend import events
            body = _format_summary(session, target, rng, findings, council_commits, foreign_commits)
            notified = await events.notify_phone(body, kind="council_postmortem")

        return {
            "ok": True, "session": session, "target_repo": target, "range": rng,
            "findings": findings, "council_commits": council_commits,
            "foreign_commits": foreign_commits, "notified": notified, "skipped": None,
        }
    except Exception as e:
        logger.warning(f"run_postmortem error (ignored): {e}")
        # Best-effort notify here too, not just for the _derive_range case
        # above — any other failure (a bad effective-config call, a git
        # subprocess timeout on a huge diff, a malformed history.jsonl this
        # session's own read/parse layer didn't catch) is exactly the same
        # "nobody re-reads this otherwise" silent-failure risk F1 fixed for
        # one call site; this closes it for the rest of the function too.
        notified = False
        try:
            from backend import events
            where = f" ({Path(target).name})" if target else ""
            notified = await events.notify_phone(
                f"Council post-mortem{where}: failed to run ({e}). Check "
                f"NEXUS logs — this session was not verified.",
                kind="council_postmortem",
            )
        except Exception:
            pass
        return {"ok": False, "skipped": str(e), "notified": notified}
