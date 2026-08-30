"""Relay the vault-signals digest (written by a cloud routine into
digests/vault-signals/*.md, see VAULT_SIGNALS_INSTRUCTIONS.md) into NEXUS's
own OutcomeFlag table via a POST to POST /api/safety/flags (backend/api/
safety.py::create_flag, which delegates server-side to
backend.agents.outcomes.record_flag).

Modeled structurally on the sibling tools/relay_claude_digest.py (dated-file
scan, .relay_state.json tracking already-processed filenames, best-effort/
fail-quiet philosophy) but scoped to core relay only -- NO gh auto-merge, NO
pending-PR notice. That machinery is deliberately deferred to a later cycle;
see VAULT_SIGNALS_INSTRUCTIONS.md for context.

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
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DIGEST_DIR = REPO_ROOT / "digests" / "vault-signals"
STATE_FILE = DIGEST_DIR / ".relay_state.json"
_DATED_DIGEST = re.compile(r"^\d{4}-\d{2}-\d{2}\.md$")
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
