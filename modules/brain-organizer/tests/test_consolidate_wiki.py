"""Unit tests for consolidate_wiki.py -- import-seam + dry-run pinning.

Spec #2 SS8.4 criteria 55, 56, 59: the SS8.1 dedup deleted four local copies
of _normalize_title/_make_temp_path/load_config/_extract_page_entry from this
script in favor of importing the shared brain_organizer versions -- these
tests guard the resulting import seam and the dry-run entry point so a future
edit can't silently reintroduce a local copy or break --config dry-run.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import brain_organizer as bo
import consolidate_wiki as cw
import pytest


# ---------------------------------------------------------------------------
# crit 55 -- consolidate_wiki defines no local _normalize_title,
# _make_temp_path, or load_config; all three must resolve to the shared
# brain_organizer definitions (imported, not locally redefined).
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", ["_normalize_title", "_make_temp_path", "load_config"])
def test_no_local_redefinition_of_shared_helpers(name: str) -> None:
    fn = getattr(cw, name)
    assert fn.__module__ == "brain_organizer", (
        f"consolidate_wiki.{name} must be imported from brain_organizer, not "
        f"locally redefined (got __module__={fn.__module__!r})"
    )


# ---------------------------------------------------------------------------
# crit 56 -- consolidate_wiki._extract_page_entry is a thin wrapper around
# brain_organizer._extract_page_entry: "summary" must be byte-identical to
# the shared version's output for the same file, plus a "chars" key the
# shared version doesn't produce.
# ---------------------------------------------------------------------------

def test_extract_page_entry_summary_matches_shared_and_adds_chars(tmp_path: Path) -> None:
    page = tmp_path / "Financial-Forecast.md"
    page.write_text(
        "# Financial Forecast\n\n"
        "This page tracks quarterly revenue projections and the assumptions "
        "behind them.\n\n"
        "## Details\n\nMore content here.\n",
        encoding="utf-8",
    )

    shared_entry = bo._extract_page_entry(page)
    wrapper_entry = cw._extract_page_entry(page)

    assert wrapper_entry["summary"] == shared_entry["summary"]
    assert wrapper_entry["summary"] != ""
    assert "chars" in wrapper_entry
    assert wrapper_entry["chars"] == len(page.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# crit 59 -- cw.main() run in dry-run mode (no --apply) against a tmp wiki
# still produces a valid _meta/consolidation-plan.json and exits 0.
# ---------------------------------------------------------------------------

def test_main_dry_run_produces_valid_consolidation_plan(
    tmp_path: Path,
    tmp_vault: Path,
    tmp_config: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wiki_folder = tmp_vault / "wiki"
    (wiki_folder / "Financial-Forecast.md").write_text(
        "# Financial Forecast\n\nQuarterly revenue projections.\n", encoding="utf-8"
    )
    (wiki_folder / "Financial-Forecasting.md").write_text(
        "# Financial Forecasting\n\n"
        "More detail on quarterly revenue projections and assumptions.\n",
        encoding="utf-8",
    )

    # tmp_config's vault_path is already isolated under tmp_path/Brain (never
    # the real C:/Users/Brian/iCloudDrive/.../Brain/ vault) -- see conftest's
    # _no_real_secrets_in_tests fixture docstring for why that matters.
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(tmp_config), encoding="utf-8")

    monkeypatch.setattr(sys, "argv", ["consolidate_wiki.py", "--config", str(config_path)])

    rc = cw.main()

    assert rc == 0

    plan_path = tmp_vault / "_meta" / "consolidation-plan.json"
    assert plan_path.exists()
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    assert isinstance(plan, dict)
    assert "generated_at" in plan
    assert "clusters" in plan
    assert isinstance(plan["clusters"], list)

    # --apply was never passed -- dry run must never touch source files.
    assert (wiki_folder / "Financial-Forecast.md").exists()
    assert (wiki_folder / "Financial-Forecasting.md").exists()
