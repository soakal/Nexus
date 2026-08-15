"""Tests for the wiki fragmentation audit — the read-only survivor of the
old raw->wiki ingestion watcher (deleted 2026-08-14, fix-plan Phase 5.1).
"""

import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from backend.agents import wiki_ingest


# ---------------------------------------------------------------------------
# _is_daily_note drift guard (robustness spec §8.3, criterion 58)
#
# brain_organizer.py runs in its own venv (modules/brain-organizer/venv);
# wiki_ingest.py runs in NEXUS's. A shared import across venvs isn't
# available, so this hand-copied twin can only be kept honest by comparing
# source text directly. This is exactly the mechanism that would have caught
# F1 at edit time (the "event-hermes-" literal was generalized in one file
# and left stale in the other).
# ---------------------------------------------------------------------------

_ORGANIZER_PATH = (
    Path(__file__).resolve().parent.parent
    / "modules"
    / "brain-organizer"
    / "brain_organizer.py"
)
_INGEST_PATH = Path(__file__).resolve().parent.parent / "backend" / "agents" / "wiki_ingest.py"

_MIRRORED_PATTERN_NAMES = (
    "_DAILY_NOTE_STEM_PAT",
    "_DAILY_NOTE_NAME_PAT",
    "_DATE_IN_STEM_PAT",
    "_EVENT_NOTE_PREFIX_PAT",
)


def _module_assignment_source(source: str, name: str) -> str:
    """Return the normalized source of the top-level `name = ...` assignment."""
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == name for t in node.targets
        ):
            return ast.unparse(node.value)
    raise AssertionError(f"module-level assignment {name!r} not found")


def _function_body_source(source: str, name: str) -> str:
    """Return the normalized source of a top-level function's body, with its
    docstring stripped (the two files' docstrings intentionally cross-
    reference each other by differing file path, so they must NOT be part
    of the equality check -- only the executable logic must match)."""
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                body = body[1:]
            return "\n".join(ast.unparse(stmt) for stmt in body)
    raise AssertionError(f"top-level function {name!r} not found")


def test_daily_note_guard_stays_in_sync_with_brain_organizer_mirror() -> None:
    organizer_src = _ORGANIZER_PATH.read_text(encoding="utf-8")
    ingest_src = _INGEST_PATH.read_text(encoding="utf-8")

    for pat_name in _MIRRORED_PATTERN_NAMES:
        organizer_pat = _module_assignment_source(organizer_src, pat_name)
        ingest_pat = _module_assignment_source(ingest_src, pat_name)
        assert organizer_pat == ingest_pat, (
            f"{pat_name} diverged between brain_organizer.py and wiki_ingest.py -- "
            "the daily-note guard is a hand-copied mirror; both must change together"
        )

    organizer_body = _function_body_source(organizer_src, "_is_daily_note")
    ingest_body = _function_body_source(ingest_src, "_is_daily_note")
    assert organizer_body == ingest_body, (
        "_is_daily_note body diverged between brain_organizer.py and wiki_ingest.py -- "
        "the daily-note guard is a hand-copied mirror; both must change together"
    )


# ---------------------------------------------------------------------------
# weekly_fragmentation_report — must go through the :8765 MCP write surface,
# never a direct pathlib write to the vault (the migration's confirmed
# direct-vault-write inconsistency, fixed here).
# ---------------------------------------------------------------------------

def test_weekly_fragmentation_report_posts_via_obsidian_not_direct_write(monkeypatch, tmp_path):
    vault = tmp_path / "vault"
    wiki_dir = vault / "Brain" / "wiki"
    wiki_dir.mkdir(parents=True)
    # 5 small same-prefix stub files -> exactly the >=5 cluster threshold.
    for i in range(5):
        (wiki_dir / f"topic-{i}.md").write_text("x")

    s = SimpleNamespace(obsidian_vault_path=str(vault))
    monkeypatch.setattr("backend.config.get_settings", lambda: s)

    posted = AsyncMock()
    monkeypatch.setattr("backend.integrations.obsidian.write_fragmentation_report", posted)

    import asyncio
    result = asyncio.run(wiki_ingest.weekly_fragmentation_report())

    assert result == {"clusters": 1}
    posted.assert_awaited_once()
    (content,) = posted.await_args.args
    assert "Fragmentation report" in content
    assert "topic-*" in content
    # Never fell back to writing Inbox.md directly.
    assert not (wiki_dir / "Inbox.md").exists()


def test_weekly_fragmentation_report_no_clusters_never_posts(monkeypatch, tmp_path):
    vault = tmp_path / "vault"
    (vault / "Brain" / "wiki").mkdir(parents=True)
    s = SimpleNamespace(obsidian_vault_path=str(vault))
    monkeypatch.setattr("backend.config.get_settings", lambda: s)

    posted = AsyncMock()
    monkeypatch.setattr("backend.integrations.obsidian.write_fragmentation_report", posted)

    import asyncio
    result = asyncio.run(wiki_ingest.weekly_fragmentation_report())

    assert result == {"clusters": 0}
    posted.assert_not_called()


def test_wiki_ingest_module_has_no_direct_vault_write():
    """Regression guard, now module-wide: wiki_ingest.py's ingestion watcher
    (ingest_file/_import_reference_doc/run_all_unprocessed, which used to be
    the carved-out exception here) was deleted 2026-08-14 (fix-plan Phase
    5.1, confirmed zero remaining callers) -- the whole module is now just
    the fragmentation audit, so there's no longer any live reason for a
    direct pathlib write to exist anywhere in this file. If one reappears,
    it must go through obsidian.write_fragmentation_report() (the :8765 MCP
    write surface), not a fresh direct write."""
    ingest_src = _INGEST_PATH.read_text(encoding="utf-8")
    for banned in ("_append_text", "_write_text", "_write_bytes", ".write_text(", ".write_bytes(", "open("):
        assert banned not in ingest_src, (
            f"wiki_ingest.py contains {banned!r} -- must post via "
            "obsidian.write_fragmentation_report(), not a direct vault write"
        )
