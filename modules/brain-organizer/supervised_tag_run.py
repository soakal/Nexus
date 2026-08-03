"""
supervised_tag_run.py — One-time supervised harness for spec #3
(frontmatter tags, docs/brain-organizer-frontmatter-tags-spec.md) §6.7
"live-vault outcome" acceptance criteria 44-47 (+48, already-landed catalog
check, re-verified here for completeness).

§9 "Suggested implementation order" step 7 calls for exactly this: "Full
suite green, one supervised manual run against a vault copy, then §6.7
measured against the live run." Criteria 44-47 are only measurable from a
real tagged run against real vault content — they cannot be pinned down by
a synthetic unit test.

What this script does:
  1. Copies the live Brain vault (raw/, wiki/ — which nests wiki/daily/ —
     and _meta/) plus the module's processed.json ledger into a fresh
     throwaway directory. NEVER touches the live vault itself (see
     `_assert_safe_destination`, enforced before any write happens).
  2. Builds a temp config identical to the real config.json except
     vault_path/processed_file/logs_folder, which point ONLY at the copy.
     Every other value (haiku_model, sonnet_model, tags_enabled,
     tag_max_new_per_note, ...) is the real production value — read, never
     hardcoded, so this run genuinely exercises production behavior.
     NOTE: brain_organizer._USAGE_LOG (real Anthropic token-usage accounting,
     consumed by NEXUS's spend governor) is a module-level constant hardcoded
     to the real modules/brain-organizer/logs/usage.jsonl — it is NOT one of
     the three overridden keys above and is deliberately left un-redirected.
     This run makes real billed API calls (see step 3 below), so that real
     spend SHOULD land in the real usage log the governor reads; redirecting
     it into the throwaway copy would hide genuine cost from NEXUS.
  3. Runs brain_organizer.run() against the copy. This makes REAL Haiku/
     Sonnet API calls and sends the run's own real Telegram summary —
     Brian pre-authorized both explicitly for this one supervised run (see
     Council-loop/.council/state/goal.md's "Pre-authorization" note).
  4. Diffs the copy's wiki pages before/after and reports criteria 44-47
     (+48) as an explicit PASS/FAIL table with evidence.

This is a REPORT-ONLY harness: it never attempts to "fix" a failing
criterion, only measures and reports it. It is also not meant to be re-run
casually — every invocation makes real billed API calls and sends a real
Telegram message. The throwaway copy is left on disk (path printed at the
end) for manual inspection; nothing here deletes it automatically.

Usage:
    venv/Scripts/python.exe modules/brain-organizer/supervised_tag_run.py --confirm

    (--confirm is required — an accidental bare invocation prints a warning
    and exits 2 instead of making real API calls / sending a real Telegram
    message.)
"""
from __future__ import annotations

import re
import shutil
import sys
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))
import brain_organizer as bo  # noqa: E402 -- must follow the sys.path insert above

# The live vault, hardcoded per the RISK guard this script was built under —
# deliberately NOT derived solely from config.json, so a corrupted or
# hand-edited config can't silently narrow what counts as "the real vault".
_LIVE_VAULT = Path(r"C:\Users\Brian\iCloudDrive\iCloud~md~obsidian\Brain")

# Mirrors _extract_page_entry's own pseudo-frontmatter key-line regex in
# brain_organizer.py — crit 48's "no summary starts with a frontmatter key"
# check must use the identical definition of "looks like a key line", not a
# paraphrase.
_KEY_LINE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*\s*:")


class UnsafeDestination(RuntimeError):
    """Raised when the prospective copy destination is unsafe relative to the live vault."""


def _assert_safe_destination(dest: Path, configured_vault: Path) -> None:
    """Abort BEFORE any write if `dest` resolves to be inside, equal to, or
    an ancestor of the live vault — checked against both the hardcoded
    live-vault path and whatever config.json currently says vault_path is,
    so a drifted config can't weaken the guard.
    """
    dest_r = dest.resolve()
    anchors = {_LIVE_VAULT.resolve(), configured_vault.resolve()}
    for anchor in anchors:
        if dest_r == anchor or dest_r.is_relative_to(anchor) or anchor.is_relative_to(dest_r):
            raise UnsafeDestination(
                f"REFUSING to proceed: destination {dest_r!s} is inside/equal to/an "
                f"ancestor of the live vault {anchor!s}. No files were copied or written."
            )


def _assert_relative(config: dict[str, Any], keys: tuple[str, ...]) -> None:
    """Defense in depth alongside _assert_safe_destination: every vault-
    relative config key must actually be relative AND contain no '..'
    traversal component, so nothing in the copy's config can resolve
    outside the copy dir regardless of vault_path.
    """
    for key in keys:
        path = Path(config[key])
        if path.is_absolute() or ".." in path.parts:
            raise UnsafeDestination(
                f"config key {key!r}={config[key]!r} is absolute or contains '..' — refusing "
                "to run (it could resolve outside the throwaway copy)."
            )


def _copy_vault(real_config: dict[str, Any], dest: Path) -> None:
    """Copy raw/, wiki/ (which nests wiki/daily/), _meta/, and
    processed.json into `dest`. Caller must have already validated `dest`
    via _assert_safe_destination.
    """
    real_vault = Path(real_config["vault_path"])
    dest.mkdir(parents=True, exist_ok=False)

    for key in ("raw_folder", "wiki_folder", "meta_folder"):
        src = real_vault / real_config[key]
        if src.exists():
            shutil.copytree(src, dest / real_config[key])

    # Belt-and-suspenders: copy daily_folder separately too, in case it
    # ever points somewhere not already nested under wiki_folder.
    daily_src = real_vault / real_config["daily_folder"]
    daily_dst = dest / real_config["daily_folder"]
    if daily_src.exists() and not daily_dst.exists():
        shutil.copytree(daily_src, daily_dst)

    processed_src = Path(real_config["processed_file"])
    processed_dst = dest / "processed.json"
    if processed_src.exists():
        shutil.copy2(processed_src, processed_dst)
    else:
        processed_dst.write_text("{}", encoding="utf-8")


def _build_temp_config(real_config: dict[str, Any], dest: Path) -> dict[str, Any]:
    """Real config verbatim, except vault_path/processed_file/logs_folder —
    the only three keys this harness is allowed to override (per RISK) —
    which point exclusively at the throwaway copy.
    """
    temp_config = dict(real_config)
    temp_config["vault_path"] = str(dest)
    temp_config["processed_file"] = str(dest / "processed.json")
    temp_config["logs_folder"] = str(dest / "logs")
    return temp_config


# ---------------------------------------------------------------------------
# Before/after snapshotting
# ---------------------------------------------------------------------------

def _top_level_wiki_pages(vault_path: Path, wiki_folder: str) -> list[Path]:
    """Same universe build_wiki_catalog scans: wiki_folder.glob('*.md'),
    non-recursive — naturally excludes wiki/daily/ (crit 38/44's daily
    exclusion) and wiki/processed/.
    """
    return sorted((vault_path / wiki_folder).glob("*.md"))


def _is_fenced(text: str) -> bool:
    """Mirrors _find_frontmatter's own fence detection: the first non-blank
    line (after BOM-stripping) starts with a code fence.
    """
    stripped = text.lstrip("\ufeff")
    for line in stripped.splitlines():
        if line.strip():
            return line.strip().startswith("```")
    return False


def _frontmatter_other_keys(text: str) -> set[str]:
    """Top-level frontmatter keys other than 'tags', lowercased. Only looks
    inside the block _find_frontmatter locates — same anchoring every
    frontmatter helper in brain_organizer.py uses.
    """
    fm = bo._find_frontmatter(text)
    if fm is None:
        return set()
    start, end = fm
    lines = text.lstrip("\ufeff").splitlines()
    keys: set[str] = set()
    for line in lines[start:end]:
        stripped = line.strip()
        if _KEY_LINE_RE.match(stripped) and not stripped.lower().startswith("tags"):
            keys.add(stripped.split(":", 1)[0].strip().lower())
    return keys


def _snapshot(vault_path: Path, wiki_folder: str) -> dict[str, dict[str, Any]]:
    snap: dict[str, dict[str, Any]] = {}
    for f in _top_level_wiki_pages(vault_path, wiki_folder):
        text = f.read_text(encoding="utf-8-sig")
        snap[f.name] = {
            "sha": bo.compute_sha256(f),
            "tags": {t.lower() for t in bo._parse_frontmatter_tags(text)},
            "other_keys": _frontmatter_other_keys(text),
            "has_frontmatter": bo._find_frontmatter(text) is not None,
            "fenced": _is_fenced(text),
        }
    return snap


def _touched_pages(before: dict[str, dict[str, Any]], after: dict[str, dict[str, Any]]) -> list[str]:
    """Pages the run actually wrote: new since before, or content changed."""
    touched = []
    for name, after_info in after.items():
        before_info = before.get(name)
        if before_info is None or before_info["sha"] != after_info["sha"]:
            touched.append(name)
    return sorted(touched)


# ---------------------------------------------------------------------------
# Criteria 44-48
# ---------------------------------------------------------------------------

def _check_crit44(after: dict[str, dict[str, Any]], touched: list[str]) -> tuple[bool, str]:
    """Caller MUST guard against touched == [] before calling this (see main()'s
    `inconclusive` check) — an empty `touched` here would trivially pass with
    zero evidence, which is exactly the vacuous-pass bug this harness must not
    reproduce. There is deliberately no early-return for that case."""
    failing = [n for n in touched if not (after[n]["has_frontmatter"] and after[n]["tags"])]
    if failing:
        return False, f"{len(failing)}/{len(touched)} touched page(s) missing valid non-empty tags: {failing[:5]}"
    return True, f"All {len(touched)} touched non-daily page(s) have valid frontmatter with a non-empty tags: block. Examples: {touched[:5]}"


def _check_crit45(before: dict[str, dict[str, Any]], after: dict[str, dict[str, Any]]) -> tuple[bool, str]:
    problems = []
    for name, before_info in before.items():
        after_info = after.get(name)
        if after_info is None:
            continue  # page removal is not a pipeline behavior; nothing to compare
        lost_tags = before_info["tags"] - after_info["tags"]
        lost_keys = before_info["other_keys"] - after_info["other_keys"]
        if lost_tags or lost_keys:
            problems.append((name, sorted(lost_tags), sorted(lost_keys)))
    if problems:
        return False, f"{len(problems)} page(s) lost a pre-existing tag or frontmatter key: {problems[:5]}"
    return True, f"No page lost a pre-existing tag or non-tag frontmatter key (checked {len(before)} page(s))."


def _check_crit46(before: dict[str, dict[str, Any]], after: dict[str, dict[str, Any]]) -> tuple[bool, str]:
    fenced_before = sorted(n for n, i in before.items() if i["fenced"])
    fenced_after = sorted(n for n, i in after.items() if i["fenced"])
    ok = fenced_before == fenced_after
    evidence = f"fenced before={len(fenced_before)} {fenced_before}; fenced after={len(fenced_after)} {fenced_after}"
    if fenced_before and len(fenced_before) != 3:
        evidence += " (note: spec's own §0.3 measured 3 at spec-writing time; baseline here differs — reporting anyway)"
    return ok, evidence


def _check_crit47(
    before: dict[str, dict[str, Any]],
    after: dict[str, dict[str, Any]],
    notes_reached_suggest_tags: int,
    tag_max_new_per_note: int,
) -> tuple[bool, str]:
    tags_before: set[str] = set()
    for i in before.values():
        tags_before |= i["tags"]
    tags_after: set[str] = set()
    for i in after.values():
        tags_after |= i["tags"]
    growth = len(tags_after) - len(tags_before)
    budget = notes_reached_suggest_tags * tag_max_new_per_note
    ok = growth <= budget
    evidence = (
        f"distinct tags before={len(tags_before)} after={len(tags_after)} growth={growth}; "
        f"notes_reached_suggest_tags={notes_reached_suggest_tags} x tag_max_new_per_note="
        f"{tag_max_new_per_note} => budget={budget}"
    )
    return ok, evidence


def _check_crit48(dest: Path, meta_folder: str, wiki_folder: str, catalog_summary_chars: int) -> tuple[bool, str]:
    catalog = bo.build_wiki_catalog(dest / wiki_folder, dest / meta_folder, catalog_summary_chars)
    missing_tags_key = [p["filename"] for p in catalog if "tags" not in p]
    bad_summary = [p["filename"] for p in catalog if _KEY_LINE_RE.match(p.get("summary", "").strip())]
    ok = not missing_tags_key and not bad_summary
    evidence = (
        f"{len(catalog)} catalog entries checked; missing 'tags' key: {missing_tags_key[:5]}; "
        f"summary starts with a frontmatter-key-like line: {bad_summary[:5]}"
    )
    return ok, evidence


_CRIT_DESCRIPTIONS: dict[str, str] = {
    "44": "Every page the run wrote has valid frontmatter with a non-empty tags: block.",
    "45": "No page lost a pre-existing tag or non-tag frontmatter key.",
    "46": "Fenced-page count stays the same — none added.",
    "47": "Distinct-tag count grows by at most (notes processed x tag_max_new_per_note).",
    "48": "Catalog entries carry a tags key; no summary starts with a frontmatter key.",
}


def _write_evidence_artifact(
    module_root: Path,
    run_started: datetime,
    touched: list[str],
    suggest_tags_call_count: int,
    results: list[tuple[str, str, str, str]],
) -> Path:
    """Write a durable evidence artifact (overwritten on every --confirm run) so a
    future audit -- e.g. the council goal's own spec-closure check -- can cite this
    run's crit 44-48 table without needing the throwaway vault copy, which is
    untracked and may already be gone by the time anyone looks."""
    docs_dir = module_root / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    out_path = docs_dir / "supervised-tag-run-evidence.md"

    statuses = [status for _n, _d, status, _e in results]
    if any(s == "INCONCLUSIVE" for s in statuses):
        overall = "INCONCLUSIVE"
    elif all(s == "PASS" for s in statuses):
        overall = "PASS"
    else:
        overall = "FAIL"

    lines = [
        "# Supervised tag run — evidence",
        "",
        "Generated by `supervised_tag_run.py` for spec #3 "
        "(`docs/brain-organizer-frontmatter-tags-spec.md`) §6.7 criteria 44-47 (+48).",
        "This file is overwritten on every `--confirm` invocation — it reflects the "
        "most recent supervised run only.",
        "",
        f"- **Run timestamp (UTC):** {run_started.isoformat()}",
        f"- **Pages touched (written) this run:** {len(touched)} {touched[:10]}",
        f"- **`suggest_tags()` calls this run:** {suggest_tags_call_count}",
        f"- **Overall status:** {overall}",
        "",
        "## Criteria 44-48",
        "",
        "| Crit | Result | Description | Evidence |",
        "|---|---|---|---|",
    ]
    for num, desc, status, ev in results:
        lines.append(f"| {num} | {status} | {desc.replace('|', chr(92) + '|')} | {ev.replace('|', chr(92) + '|')} |")
    lines.append("")

    out_path.write_text("\n".join(lines), encoding="utf-8")
    return out_path


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    if "--confirm" not in sys.argv:
        print(
            "supervised_tag_run.py makes REAL Anthropic API calls and sends a REAL "
            "Telegram message (against a throwaway vault copy, never the live vault). "
            "This is a one-time supervised tool, not for casual/CI reruns — "
            "re-invoke with --confirm to proceed."
        )
        return 2

    run_started = datetime.now(UTC)
    module_root = Path(__file__).parent
    real_config = bo.validate_config(bo.load_config(module_root / "config.json"))
    _assert_relative(
        real_config,
        ("raw_folder", "wiki_folder", "meta_folder", "daily_folder", "backup_folder"),
    )

    dest = Path(tempfile.gettempdir()) / (
        f"brain_organizer_supervised_run_{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}_{uuid.uuid4().hex[:8]}"
    )
    configured_vault = Path(real_config["vault_path"])
    _assert_safe_destination(dest, configured_vault)  # ABORT before any write on failure

    print(f"Copying live vault into throwaway dir: {dest}")
    _copy_vault(real_config, dest)

    temp_config = _build_temp_config(real_config, dest)
    dest_r = dest.resolve()
    temp_vault_r = Path(temp_config["vault_path"]).resolve()
    if temp_vault_r != dest_r and not temp_vault_r.is_relative_to(dest_r):
        raise UnsafeDestination("temp_config vault_path escaped the copy dir — aborting before run()")

    before = _snapshot(dest, temp_config["wiki_folder"])

    suggest_tags_calls = {"count": 0}
    _orig_suggest_tags = bo.suggest_tags

    def _counting_suggest_tags(*args: Any, **kwargs: Any) -> list[str]:
        suggest_tags_calls["count"] += 1
        return _orig_suggest_tags(*args, **kwargs)

    bo.suggest_tags = _counting_suggest_tags
    try:
        print("Running brain_organizer.run() against the copy (real API calls + real Telegram summary)...")
        exit_code = bo.run(_config=temp_config)
    finally:
        bo.suggest_tags = _orig_suggest_tags

    print(f"run() exit code: {exit_code}")

    after = _snapshot(dest, temp_config["wiki_folder"])
    touched = _touched_pages(before, after)
    print(f"Pages touched (written) this run: {len(touched)} {touched[:10]}")
    print(f"suggest_tags() calls this run: {suggest_tags_calls['count']}")

    # A run that touched nothing and never called suggest_tags() measured
    # NOTHING -- crit 44 previously returned a vacuous True here, and crit
    # 45-47 trivially "pass" on identical before/after snapshots too. Report
    # INCONCLUSIVE for all of 44-48 instead of ever printing PASS for a no-op.
    inconclusive = len(touched) == 0 or suggest_tags_calls["count"] == 0

    results: list[tuple[str, str, str, str]] = []  # (num, desc, status, evidence)
    if inconclusive:
        reason = (
            f"Run touched {len(touched)} page(s) and made {suggest_tags_calls['count']} "
            "suggest_tags() call(s) -- nothing was measured this run, so crit 44-48 "
            "cannot be scored. A no-op run must never report PASS; re-run with real, "
            "unprocessed content present in raw/."
        )
        for num, desc in _CRIT_DESCRIPTIONS.items():
            results.append((num, desc, "INCONCLUSIVE", reason))
    else:
        ok, ev = _check_crit44(after, touched)
        results.append(("44", _CRIT_DESCRIPTIONS["44"], "PASS" if ok else "FAIL", ev))

        ok, ev = _check_crit45(before, after)
        results.append(("45", _CRIT_DESCRIPTIONS["45"], "PASS" if ok else "FAIL", ev))

        ok, ev = _check_crit46(before, after)
        results.append(("46", _CRIT_DESCRIPTIONS["46"], "PASS" if ok else "FAIL", ev))

        ok, ev = _check_crit47(before, after, suggest_tags_calls["count"], real_config["tag_max_new_per_note"])
        results.append(("47", _CRIT_DESCRIPTIONS["47"], "PASS" if ok else "FAIL", ev))

        ok, ev = _check_crit48(dest, temp_config["meta_folder"], temp_config["wiki_folder"], temp_config["catalog_summary_chars"])
        results.append(("48", _CRIT_DESCRIPTIONS["48"], "PASS" if ok else "FAIL", ev))

    print()
    print("=" * 100)
    print(f"{'Crit':<5} {'Result':<13} Description")
    print("-" * 100)
    for num, desc, status, ev in results:
        print(f"{num:<5} {status:<13} {desc}")
        print(f"        evidence: {ev}")
    print("=" * 100)

    evidence_path = _write_evidence_artifact(module_root, run_started, touched, suggest_tags_calls["count"], results)
    print(f"\nEvidence artifact written: {evidence_path}")
    print(f"Throwaway copy left on disk for inspection: {dest}")
    print("(this harness never touches the live vault; remove the copy manually if not needed)")

    if inconclusive:
        return 3
    return 0 if all(status == "PASS" for _n, _d, status, _e in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
