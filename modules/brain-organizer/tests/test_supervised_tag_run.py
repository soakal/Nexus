"""Tests for supervised_tag_run.py's path-safety guards --
_assert_safe_destination and _assert_relative -- the file's entire safety
argument against the irreplaceable live vault. This is a pure, hermetic unit
test: no real Anthropic API calls, no real vault access, no Telegram, and no
invocation of the live/real-API harness path (main()/run()).

Covers this cycle's Security fix specifically: _assert_relative previously
only rejected absolute config-key paths, leaving a '..' traversal gap (e.g.
raw_folder='../../escape') that could resolve outside the throwaway copy
dir despite looking "relative". The fix (supervised_tag_run.py ~line 97)
also rejects any path whose Path.parts contain '..'.

Also covers the Realist's follow-up finding: _assert_relative's guarded-keys
tuple in main() previously omitted "backup_folder" -- the one vault-relative
config key the pipeline actually deletes files from (_prune_old_backups's
f.unlink() in brain_organizer.py) -- leaving it unchecked for absolute/'..'
values despite being joined to vault_path identically to the other four keys.

One test below (test_main_reports_inconclusive_not_pass_on_zero_work_run)
does invoke main() itself, but hermetically: bo.load_config/validate_config/
run are monkeypatched so no real config.json, live vault, Anthropic API call,
or Telegram message is ever touched. It pins the other fix landed this
round -- a run that touches nothing and calls suggest_tags() zero times must
report INCONCLUSIVE (exit 3) for all of crit 44-48, never a vacuous PASS.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import supervised_tag_run as st


# ---------------------------------------------------------------------------
# _assert_relative -- defense in depth against '..' traversal in config keys
# ---------------------------------------------------------------------------


def test_assert_relative_rejects_dotdot_traversal() -> None:
    """The gap this cycle's Security review closed: a relative-looking path
    containing '..' must still be refused, since it could resolve outside
    the throwaway copy dir regardless of vault_path."""
    config = {"raw_folder": "../escape"}

    with pytest.raises(st.UnsafeDestination):
        st._assert_relative(config, ("raw_folder",))


def test_assert_relative_rejects_dotdot_traversal_nested() -> None:
    """Same gap, one level deeper: '..' anywhere in the path parts, not just
    as the leading component, must be rejected."""
    config = {"wiki_folder": "wiki/../../escape"}

    with pytest.raises(st.UnsafeDestination):
        st._assert_relative(config, ("wiki_folder",))


def test_assert_relative_rejects_absolute_path() -> None:
    """Pre-existing guard: an absolute path is still refused."""
    config = {"meta_folder": "C:\\Users\\Brian\\elsewhere"}

    with pytest.raises(st.UnsafeDestination):
        st._assert_relative(config, ("meta_folder",))


def test_assert_relative_accepts_plain_relative_path() -> None:
    """A genuinely relative path with no traversal component must pass
    through without raising -- this is the real production shape of
    raw_folder/wiki_folder/meta_folder/daily_folder in config.json."""
    config = {"raw_folder": "raw", "wiki_folder": "wiki", "daily_folder": "wiki/daily"}

    st._assert_relative(config, ("raw_folder", "wiki_folder", "daily_folder"))  # must not raise


def test_assert_relative_rejects_backup_folder_traversal() -> None:
    """backup_folder is the ONLY vault-relative config key the pipeline actually
    deletes files from (_prune_old_backups -> f.unlink(), brain_organizer.py),
    joined to vault_path identically to raw_folder/wiki_folder/meta_folder/
    daily_folder (brain_organizer.py:302, 348, 2555). It must be covered by the
    same rejection logic as those keys, not left out."""
    config = {"backup_folder": "../escape"}

    with pytest.raises(st.UnsafeDestination):
        st._assert_relative(config, ("backup_folder",))


def test_assert_relative_rejects_backup_folder_absolute() -> None:
    config = {"backup_folder": "C:\\Users\\Brian\\elsewhere"}

    with pytest.raises(st.UnsafeDestination):
        st._assert_relative(config, ("backup_folder",))


def test_assert_relative_accepts_backup_folder_plain_relative_path() -> None:
    """The real production shape of backup_folder in config.json: 'raw\\backups'."""
    config = {"backup_folder": "raw\\backups"}

    st._assert_relative(config, ("backup_folder",))  # must not raise


def test_main_assert_relative_call_includes_backup_folder() -> None:
    """Pins the actual call site in main() (supervised_tag_run.py) so this key
    can never silently drop out of the guarded tuple again -- reads the source
    rather than invoking main() itself, since main() makes real API calls and
    requires --confirm."""
    import inspect

    source = inspect.getsource(st.main)
    assert '"backup_folder"' in source, (
        "main()'s _assert_relative(...) call must include \"backup_folder\" in "
        "its guarded-keys tuple"
    )


# ---------------------------------------------------------------------------
# _assert_safe_destination -- refuse to write inside/at/above the live vault
# ---------------------------------------------------------------------------


def test_assert_safe_destination_rejects_dest_equal_to_configured_vault(tmp_path: Path) -> None:
    configured_vault = tmp_path / "Brain"
    configured_vault.mkdir()

    with pytest.raises(st.UnsafeDestination):
        st._assert_safe_destination(configured_vault, configured_vault)


def test_assert_safe_destination_rejects_dest_inside_configured_vault(tmp_path: Path) -> None:
    configured_vault = tmp_path / "Brain"
    configured_vault.mkdir()
    dest = configured_vault / "throwaway_copy"

    with pytest.raises(st.UnsafeDestination):
        st._assert_safe_destination(dest, configured_vault)


def test_assert_safe_destination_rejects_configured_vault_inside_dest(tmp_path: Path) -> None:
    """dest as an ancestor of the vault (a poisoned/misconfigured vault_path
    nested under dest) must also be refused, not just the reverse case."""
    dest = tmp_path / "throwaway_copy"
    configured_vault = dest / "Brain"

    with pytest.raises(st.UnsafeDestination):
        st._assert_safe_destination(dest, configured_vault)


def test_assert_safe_destination_accepts_safe_sibling_directory(tmp_path: Path) -> None:
    """A destination that is neither the vault, inside it, nor an ancestor
    of it must pass through without raising."""
    configured_vault = tmp_path / "Brain"
    configured_vault.mkdir()
    dest = tmp_path / "throwaway_copy"

    st._assert_safe_destination(dest, configured_vault)  # must not raise


# ---------------------------------------------------------------------------
# main() -- a no-op run (nothing touched, suggest_tags() never called) must
# report INCONCLUSIVE for all of crit 44-48, never a vacuous PASS. This is
# the Realist's other follow-up finding this round: removing the early-return
# in _check_crit44 alone doesn't prove main()'s own `inconclusive` gate
# (supervised_tag_run.py ~line 413) actually short-circuits before ever
# reaching _check_crit44/45/46/47/48 on an empty before/after snapshot pair.
#
# Hermetic: bo.load_config/bo.validate_config/bo.run are monkeypatched so
# no real config.json, live vault, Anthropic API, or Telegram call is ever
# touched, and tempfile.gettempdir() is redirected under tmp_path so the
# throwaway copy is cleaned up by pytest instead of littering the real temp
# dir. _write_evidence_artifact is monkeypatched to capture its arguments
# instead of writing -- this must NEVER overwrite the real, already-landed
# docs/supervised-tag-run-evidence.md produced by the actual supervised run.
# ---------------------------------------------------------------------------


def test_main_reports_inconclusive_not_pass_on_zero_work_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "Brain"
    (vault / "raw").mkdir(parents=True)
    (vault / "wiki" / "daily").mkdir(parents=True)
    (vault / "meta").mkdir()
    processed_file = tmp_path / "processed.json"
    processed_file.write_text("{}", encoding="utf-8")

    fake_config = {
        "vault_path": str(vault),
        "raw_folder": "raw",
        "wiki_folder": "wiki",
        "meta_folder": "meta",
        "daily_folder": "wiki/daily",
        "backup_folder": "raw/backups",
        "logs_folder": str(tmp_path / "logs"),
        "processed_file": str(processed_file),
        "haiku_model": "haiku-fake",
        "sonnet_model": "sonnet-fake",
        "tag_max_new_per_note": 1,
        "catalog_summary_chars": 300,
    }

    captured: dict[str, object] = {}

    def _fake_write_evidence_artifact(module_root, run_started, touched, suggest_tags_call_count, results):  # type: ignore[no-untyped-def]
        captured["touched"] = touched
        captured["suggest_tags_call_count"] = suggest_tags_call_count
        captured["results"] = results
        return module_root / "docs" / "TEST-DID-NOT-WRITE-THIS.md"

    monkeypatch.setattr(st.sys, "argv", ["supervised_tag_run.py", "--confirm"])
    monkeypatch.setattr(st.bo, "load_config", lambda path=None: fake_config)
    monkeypatch.setattr(st.bo, "validate_config", lambda c: c)
    monkeypatch.setattr(st.bo, "run", lambda **kwargs: 0)  # no-op: never touches vault, never calls suggest_tags
    monkeypatch.setattr(st.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(st, "_write_evidence_artifact", _fake_write_evidence_artifact)

    exit_code = st.main()

    assert exit_code == 3, "a zero-work run must exit 3 (INCONCLUSIVE), never 0 (PASS)"
    assert captured["touched"] == []
    assert captured["suggest_tags_call_count"] == 0
    results = captured["results"]
    assert [num for num, _d, _s, _e in results] == ["44", "45", "46", "47", "48"]
    assert all(status == "INCONCLUSIVE" for _n, _d, status, _e in results), (
        f"every criterion must report INCONCLUSIVE on a no-op run, got: {results}"
    )
    assert not any(status == "PASS" for _n, _d, status, _e in results), (
        "a no-op run must never report PASS for any criterion (the vacuous-pass bug this test pins)"
    )
