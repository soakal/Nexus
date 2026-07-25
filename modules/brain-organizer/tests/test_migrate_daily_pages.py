"""Unit tests for migrate_daily_pages.py."""
from __future__ import annotations

import sys
from pathlib import Path

import migrate_daily_pages as mdp
import pytest


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    v = tmp_path / "Brain"
    (v / "wiki").mkdir(parents=True)
    (v / "wiki" / "processed").mkdir(parents=True)
    (v / "_meta").mkdir(parents=True)
    return v


def _write(path: Path, content: str = "content") -> Path:
    path.write_text(content, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# find_daily_pages — strict scoping
# ---------------------------------------------------------------------------

def test_find_daily_pages_matches_bare_date_stems(vault: Path) -> None:
    _write(vault / "wiki" / "2026-07-08.md")
    _write(vault / "wiki" / "2026-07-08b.md")

    found = mdp.find_daily_pages(vault / "wiki")

    assert {f.name for f in found} == {"2026-07-08.md", "2026-07-08b.md"}


def test_find_daily_pages_excludes_legacy_prefixed_names(vault: Path) -> None:
    """Pre-guard artifacts like 'Daily-Operations-Log-2026-07-08.md' are NOT
    a bare date stem -- left alone for manual review, not auto-migrated."""
    _write(vault / "wiki" / "Daily-Operations-Log-2026-07-08.md")
    _write(vault / "wiki" / "Morning-Briefing-2026-06-28.md")

    found = mdp.find_daily_pages(vault / "wiki")

    assert found == []


def test_find_daily_pages_excludes_topic_pages(vault: Path) -> None:
    _write(vault / "wiki" / "AdGuard.md")
    _write(vault / "wiki" / "Daily-Log.md")

    found = mdp.find_daily_pages(vault / "wiki")

    assert found == []


def test_find_daily_pages_non_recursive_ignores_processed_subfolder(vault: Path) -> None:
    """wiki/processed/2026-07-08.md must NOT be picked up -- only wiki root."""
    _write(vault / "wiki" / "processed" / "2026-07-08.md")

    found = mdp.find_daily_pages(vault / "wiki")

    assert found == []


# ---------------------------------------------------------------------------
# apply_migration — moves + backs up, never deletes
# ---------------------------------------------------------------------------

def test_apply_migration_moves_and_backs_up(vault: Path) -> None:
    page = _write(vault / "wiki" / "2026-07-08.md", "# 2026-07-08\ncontent here")
    daily_folder = vault / "wiki" / "daily"
    meta_folder = vault / "_meta"

    mdp.apply_migration([page], daily_folder, meta_folder)

    moved = daily_folder / "2026-07-08.md"
    assert moved.exists()
    assert moved.read_text(encoding="utf-8") == "# 2026-07-08\ncontent here"
    assert not page.exists()

    backups = list((meta_folder / "merged-backups").glob("*2026-07-08.md"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == "# 2026-07-08\ncontent here"


def test_apply_migration_skips_existing_destination(vault: Path) -> None:
    """A pre-existing file at the destination is never overwritten -- left in
    place for manual review rather than risking silent data loss."""
    page = _write(vault / "wiki" / "2026-07-08.md", "root version")
    daily_folder = vault / "wiki" / "daily"
    daily_folder.mkdir(parents=True)
    _write(daily_folder / "2026-07-08.md", "already-there version")
    meta_folder = vault / "_meta"

    mdp.apply_migration([page], daily_folder, meta_folder)

    assert page.exists()  # untouched
    # A skipped file must leave NO backup either -- a backup with no
    # corresponding move is misleading (implies a move that never happened).
    assert list((meta_folder / "merged-backups").glob("*")) == []
    assert (daily_folder / "2026-07-08.md").read_text(encoding="utf-8") == "already-there version"


def test_apply_migration_repairs_dangling_registry_entries(vault: Path) -> None:
    """Brian's real topics-registry.json already has 10 entries pointing at
    wiki-root paths that this migration moves away from -- apply_migration
    must repoint them at the new daily-folder path, not leave them dangling."""
    page = _write(vault / "wiki" / "2026-07-25.md", "content")
    meta_folder = vault / "_meta"
    import json
    registry_path = meta_folder / "topics-registry.json"
    registry_path.write_text(json.dumps({
        "2026-07-25": str(vault / "wiki" / "2026-07-25.md"),
        "AdGuard": str(vault / "wiki" / "AdGuard.md"),  # unrelated entry, must survive untouched
    }), encoding="utf-8")

    mdp.apply_migration([page], vault / "wiki" / "daily", meta_folder)

    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    assert registry["2026-07-25"] == str(vault / "wiki" / "daily" / "2026-07-25.md")
    assert registry["AdGuard"] == str(vault / "wiki" / "AdGuard.md")


def test_apply_migration_removes_stale_catalog_cache(vault: Path) -> None:
    page = _write(vault / "wiki" / "2026-07-08.md")
    meta_folder = vault / "_meta"
    cache = meta_folder / "wiki-catalog.json"
    cache.write_text("{}", encoding="utf-8")

    mdp.apply_migration([page], vault / "wiki" / "daily", meta_folder)

    assert not cache.exists()


def test_dry_run_never_touches_filesystem(vault: Path, capsys: pytest.CaptureFixture) -> None:
    page = _write(vault / "wiki" / "2026-07-08.md", "content")
    config_path = vault.parent / "config.json"
    import json
    config_path.write_text(json.dumps({
        "vault_path": str(vault), "wiki_folder": "wiki",
        "daily_folder": "wiki/daily", "meta_folder": "_meta",
    }), encoding="utf-8")

    old_argv = sys.argv
    sys.argv = ["migrate_daily_pages.py", "--config", str(config_path)]
    try:
        rc = mdp.main()
    finally:
        sys.argv = old_argv

    assert rc == 0
    assert page.exists()  # untouched
    assert not (vault / "wiki" / "daily").exists()
    out = capsys.readouterr().out
    assert "Dry run" in out
