"""Independently re-verify a Council-loop session's real commit range against
its own claims — the "did the Realist's prose match reality" check.

Council-loop (a separate repo) auto-commits one step at a time against
whatever `target_repo` it's building, and nobody re-reads the diffs
afterward. The Realist role is instructed to run its own verification, but
nothing captures whether it actually did — only its own prose claim
survives, in `.council/state/transcripts/cycle-NNNN.md`.

Triggered by Council-loop's own `run-loop.sh` (or `run-loop.ps1` on Windows)
POSTing to `/api/trigger` {"task_name": "council_postmortem", "parameters":
<payload>} at driver exit (see backend/api/trigger.py) — NOT scheduled here
on NEXUS's own scheduler, because `/goal` TRUNCATES
`.council/state/history.jsonl` on every new session, so a poller that missed
the window would lose that session's history permanently and silently.

The payload carries raw git data (log/diff/file-listing output, gathered by
Council-loop/scripts/postmortem_payload.py on whichever machine actually
runs Council-loop) rather than this module reading it off local disk —
NEXUS runs on nexus-lxc, Council-loop runs wherever Brian is actively using
it, and those are no longer the same filesystem. Only the judgment stays
server-side: all deterministic checks below, plus the one LLM call.

Independence comes from being DETERMINISTIC (regex/set operations over the
supplied git data), not from using a different model — Council-loop's own
`.council/config.local.json` currently assigns Arbiter=opus, Engineer=sonnet,
Security=sonnet, Realist=opus, so there's no smarter model to delegate to.
The one LLM call here (extracting a file allowlist from goal.md's prose
Objective) uses Haiku purely because it's the only role-free model and the
task is extraction, not judgment.

Read-only by construction — this module runs no git commands at all; it only
ever reads fields off the payload dict. Never raises — every public entry
point returns a dict, even on total failure.
"""

import ast
import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

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


def _normalize_path(p: str) -> str:
    """git diff --name-only always emits forward slashes with no leading
    './'; the goal-extraction LLM call is asked for paths "verbatim", and
    goal.md text (e.g. Windows paths, a leading './') would otherwise never
    match a real touched file. Normalize both sides through this."""
    return p.strip().replace("\\", "/").removeprefix("./")


def _count_council_commits(log_text: str, commit_prefix: str) -> tuple[int, int]:
    """Return (council_commit_count, foreign_commit_count) from a `git log
    --format=%H%x00%s <range>` payload field, split by whether the subject
    line starts with commit_prefix (e.g. "council: cycle 3: ..."). Foreign
    commits are counted, never scanned by the checks below."""
    council, foreign = 0, 0
    for line in (log_text or "").splitlines():
        if not line:
            continue
        _, _, subject = line.partition("\x00")
        if subject.startswith(commit_prefix):
            council += 1
        else:
            foreign += 1
    return council, foreign


def _check_scope_drift(files_changed: list[str], allowed: list[str]) -> list[dict]:
    allowed_norm = [_normalize_path(a) for a in allowed]
    findings = []
    for f in files_changed:
        fn = _normalize_path(f)
        if not any(fn == a or fn.startswith(a.rstrip("/") + "/") for a in allowed_norm):
            findings.append({
                "check": "scope_drift",
                "severity": "medium",
                "detail": f"'{f}' was touched but is outside the stated plan",
            })
    return findings


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


def _check_placeholders(py_files: dict, py_changed_count: int, max_files: int = 200) -> list[dict]:
    """py_files: {path: {"source": str, "diff_u0": str}} — absent (None) when
    py_changed_count exceeded the client's own gathering cap."""
    findings = []
    if py_changed_count > max_files:
        # Bounds the payload/scan cost — an ordinary session never comes
        # close (ProcessForge's largest real cycle range touched ~12 files);
        # a session this large is unusual enough to flag rather than
        # silently truncate.
        findings.append({
            "check": "placeholder", "severity": "low",
            "detail": f"{py_changed_count} changed .py files exceeds council_postmortem_max_files "
                      f"({max_files}) — skipping the placeholder scan for this session",
        })
        return findings

    for f, data in (py_files or {}).items():
        source = data.get("source")
        if source is None:
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
        for line in (data.get("diff_u0") or "").splitlines():
            if not line.startswith("+") or line.startswith("+++"):
                continue
            if re.search(r"\b(TODO|FIXME|XXX|HACK)\b", line) or "# ponytail:" in line:
                findings.append({
                    "check": "placeholder",
                    "severity": "low",
                    "detail": f"{f}: new line contains a cruft marker — {line[1:].strip()[:80]}",
                })
    return findings


def _check_test_claims(ls_tree_last: list[str], last: str, history: list[dict], transcripts: str) -> list[dict]:
    text = (transcripts or "") + "\n" + "\n".join(h.get("notes", "") for h in (history or []))
    candidates = set()
    for m in _PATH_TOKEN_RE.finditer(text):
        token = m.group(0).lstrip("/\\")
        ext = token.rsplit(".", 1)[-1].lower()
        if ext not in _PLAUSIBLE_EXTENSIONS:
            continue
        if _looks_like_url(text, m.start()):
            continue
        candidates.add(token)

    existing = {_normalize_path(p) for p in (ls_tree_last or [])}
    findings = []
    for path in sorted(candidates):
        if _normalize_path(path) not in existing:
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


def _format_summary(session: dict, repo_name: str, rng: str | None, findings: list[dict],
                     council_commits: int, foreign_commits: int) -> str:
    header = f"Council post-mortem — {repo_name}, {session.get('cycles', 0)} cycles"
    if rng:
        header += f", {council_commits} commit(s) ({rng})"
    lines = [header]
    for f in findings[:5]:
        lines.append(f"{f['check'].upper()}: {f['detail']}")
    if foreign_commits:
        lines.append(f"{foreign_commits} non-council commit(s) in range (not scanned).")
    return "\n".join(lines)[:800]


async def run_postmortem(payload: dict | None = None) -> dict:
    """Independently re-verify a Council-loop session against its target
    repo's real commit range, using raw git data the caller (wherever
    Council-loop actually runs — see Council-loop/scripts/postmortem_payload.py)
    gathered and sent. Best-effort: NEVER raises. Returns a summary dict; see
    module docstring for the full design rationale.

    payload fields (all produced by postmortem_payload.py): target_repo_name,
    commit_prefix, goal, history, transcripts, and either range_error (git
    data couldn't be gathered), range=None (no commits this session), or the
    full set: range, last, log, files_changed, ls_tree_last, py_changed_count,
    py_files.
    """
    repo_name = "unknown"
    try:
        from backend.config import get_settings
        s = get_settings()
        if not getattr(s, "council_postmortem_enabled", True):
            return {"ok": False, "skipped": "council_postmortem_enabled is False"}

        if not payload:
            return {"ok": False, "skipped": "no payload — caller must send git data "
                                             "(see Council-loop/scripts/postmortem_payload.py)"}

        repo_name = payload.get("target_repo_name") or "unknown"
        commit_prefix = payload.get("commit_prefix", "council:")
        history = payload.get("history") or []
        goal_text = payload.get("goal") or ""
        transcripts = payload.get("transcripts") or ""

        session = {
            "goal": goal_text.strip()[:200],
            "cycles": len(history),
        }

        if payload.get("range_error"):
            # Same "must not fall through and die silently" concern as the
            # old local-git version: a permanently-broken post-mortem must
            # not be indistinguishable from a clean one on Brian's phone.
            from backend import events
            e = payload["range_error"]
            notified = await events.notify_phone(
                f"Council post-mortem — {repo_name}: could not verify this "
                f"session ({e}). The target repo's git history may have diverged "
                f"from what Council-loop recorded (rebase/amend/force-push?).",
                kind="council_postmortem",
            )
            return {
                "ok": False, "session": session, "target_repo": repo_name, "range": None,
                "findings": [{"check": "range_resolution", "severity": "high", "detail": str(e)}],
                "council_commits": 0, "foreign_commits": 0,
                "notified": notified, "skipped": None,
            }

        rng = payload.get("range")
        if rng is None:
            return {
                "ok": True, "session": session, "target_repo": repo_name, "range": None,
                "findings": [], "council_commits": 0, "foreign_commits": 0,
                "notified": False, "skipped": "no commits in session — nothing to check",
            }

        last = payload["last"]
        council_commits, foreign_commits = _count_council_commits(payload.get("log", ""), commit_prefix)

        model = getattr(s, "council_postmortem_model", "claude-haiku-4-5-20251001")
        allowed_paths, explicit = await _extract_allowed_paths(goal_text, model)

        max_files = getattr(s, "council_postmortem_max_files", 200)
        findings: list[dict] = []
        if explicit:
            findings += _check_scope_drift(payload.get("files_changed") or [], allowed_paths)
        findings += _check_placeholders(payload.get("py_files") or {}, payload.get("py_changed_count", 0), max_files)
        findings += _check_test_claims(payload.get("ls_tree_last") or [], last, history, transcripts)

        notified = False
        if findings:
            from backend import events
            body = _format_summary(session, repo_name, rng, findings, council_commits, foreign_commits)
            notified = await events.notify_phone(body, kind="council_postmortem")

        return {
            "ok": True, "session": session, "target_repo": repo_name, "range": rng,
            "findings": findings, "council_commits": council_commits,
            "foreign_commits": foreign_commits, "notified": notified, "skipped": None,
        }
    except Exception as e:
        logger.warning(f"run_postmortem error (ignored): {e}")
        notified = False
        try:
            from backend import events
            notified = await events.notify_phone(
                f"Council post-mortem ({repo_name}): failed to run ({e}). Check "
                f"NEXUS logs — this session was not verified.",
                kind="council_postmortem",
            )
        except Exception:
            pass
        return {"ok": False, "skipped": str(e), "notified": notified}
