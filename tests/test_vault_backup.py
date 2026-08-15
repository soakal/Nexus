"""Tests for backend/backup.py (vault→Unraid) + the scheduler failure alert.

Tier A3.2/A3.3: the off-VM bundle must include a restorable nexus.db and a
failed off-VM backup must page the phone.
"""
import os
import pathlib
import sqlite3
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy import text
from sqlmodel import create_engine


def _make_tmp_engine(db_path: pathlib.Path):
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )
    with engine.connect() as conn:
        conn.execute(text("PRAGMA journal_mode=WAL"))
        conn.execute(text("CREATE TABLE IF NOT EXISTS _test (id INTEGER PRIMARY KEY)"))
        conn.execute(text("INSERT INTO _test (id) VALUES (42)"))
        conn.commit()
    return engine


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Tmp project root with vault files, tmp engine, tmp Unraid 'share'."""
    db_file = tmp_path / "nexus.db"
    engine = _make_tmp_engine(db_file)
    monkeypatch.setattr("backend.database.engine", engine)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("backend.agents.backup._db_path", lambda: str(db_file))

    vault = tmp_path / "nexus.vault"
    meta = tmp_path / "nexus.vault.meta"
    key = tmp_path / ".vault.key"
    vault.write_text('{"encrypted": "data"}')
    meta.write_text('{"names": []}')
    key.write_bytes(b"k" * 32)

    share = tmp_path / "share"

    s = MagicMock()
    s.unraid_backup_path = str(share)
    s.unraid_backup_include_key = True
    s.unraid_backup_user = ""
    s.unraid_backup_password = ""
    monkeypatch.setattr("backend.config.get_settings", lambda: s)

    monkeypatch.setattr("backend.secrets.vault.VAULT_PATH", vault)
    monkeypatch.setattr("backend.secrets.vault.META_PATH", meta)
    monkeypatch.setattr("backend.secrets.vault.KEY_PATH", key)

    return {"tmp": tmp_path, "share": share, "db": db_file,
            "vault": vault, "meta": meta, "key": key, "s": s}


def test_backup_vault_includes_db(env):
    from backend.backup import backup_vault
    result = backup_vault()
    assert result["ok"] is True

    share = env["share"]
    assert (share / "nexus.db").exists(), "db snapshot missing from share root"
    hist = list((share / "history").iterdir())
    assert len(hist) == 1
    assert (hist[0] / "nexus.db").exists(), "db snapshot missing from history"

    # The shipped snapshot must be a real, consistent database
    con = sqlite3.connect(str(share / "nexus.db"))
    try:
        assert con.execute("SELECT id FROM _test").fetchone() == (42,)
        assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        con.close()


def test_backup_vault_db_snapshot_failure_is_nonfatal(env, monkeypatch):
    """A db-snapshot failure must not break the vault half of the backup."""
    def _boom(_dest):
        raise RuntimeError("disk on fire")
    monkeypatch.setattr("backend.agents.backup.snapshot_db_to", _boom)

    from backend.backup import backup_vault
    result = backup_vault()
    assert result["ok"] is True
    assert (env["share"] / "nexus.vault").exists()
    assert not (env["share"] / "nexus.db").exists()


def test_smb_share_and_subpath_splits_unc():
    from backend.backup import _smb_share_and_subpath
    assert _smb_share_and_subpath(
        "\\\\192.168.1.50\\Computer Backup\\Nexus_backup-lxc"
    ) == ("Computer Backup", "Nexus_backup-lxc")


def test_backup_vault_posix_unc_stages_locally_and_rclone_syncs(env, monkeypatch, tmp_path):
    """On POSIX with a UNC-style unraid_backup_path (no kernel CIFS mount
    available in an unprivileged LXC -- see _STAGING_ROOT's docstring),
    backup_vault() must write to the local staging root (unchanged
    shutil-based logic) and then call rclone sync exactly once against the
    real remote path, never touching _mount_unc (Windows-only)."""
    env["s"].unraid_backup_path = "\\\\192.168.1.50\\Computer Backup\\Nexus_backup-lxc"
    staging = tmp_path / "staging"
    monkeypatch.setattr("backend.backup._STAGING_ROOT", staging)
    monkeypatch.setattr("backend.backup.os.name", "posix")

    mount_calls = []
    monkeypatch.setattr("backend.backup._mount_unc", lambda *a, **k: mount_calls.append(a))
    sync_calls = []

    def _fake_sync(local_dir, unc_path):
        sync_calls.append((local_dir, unc_path))
        return True

    monkeypatch.setattr("backend.backup._rclone_sync", _fake_sync)

    from backend.backup import backup_vault
    result = backup_vault()

    assert result["ok"] is True
    assert result["dest"] == "\\\\192.168.1.50\\Computer Backup\\Nexus_backup-lxc"
    assert not mount_calls, "must never call the Windows-only _mount_unc on POSIX"
    assert (staging / "nexus.vault").exists()
    assert sync_calls == [(staging, "\\\\192.168.1.50\\Computer Backup\\Nexus_backup-lxc")]


def test_backup_knowledge_windows_is_noop(env, monkeypatch):
    monkeypatch.setattr("backend.backup.os.name", "nt")
    from backend.backup import backup_knowledge
    result = backup_knowledge()
    assert result["ok"] is False


def test_backup_knowledge_posix_calls_rclone_sync(env, monkeypatch, tmp_path):
    env["s"].unraid_backup_path = "\\\\192.168.1.50\\Computer Backup\\Nexus_backup-lxc"
    knowledge = tmp_path / "knowledge"
    knowledge.mkdir()
    (knowledge / "Brain").mkdir()
    env["s"].obsidian_vault_path = str(knowledge)
    monkeypatch.setattr("backend.backup.os.name", "posix")

    captured = {}
    def _fake_run(cmd, timeout=None):
        captured["cmd"] = cmd
        return MagicMock(returncode=0, stderr="")
    monkeypatch.setattr("backend.backup._run_rclone", _fake_run)

    from backend.backup import backup_knowledge
    result = backup_knowledge()

    assert result["ok"] is True
    assert captured["cmd"][:2] == ["rclone", "sync"]
    assert captured["cmd"][2] == str(knowledge)
    assert captured["cmd"][3] == "nexus-unraid:Computer Backup/Nexus_backup-lxc/knowledge"


def test_rclone_sync_excludes_knowledge_subtree(env, monkeypatch):
    """backup_vault()'s sync must never wipe backup_knowledge()'s separate
    knowledge/ subtree living in the same remote root -- reproduced live
    2026-08-14 (a vault backup deleted the whole knowledge mirror). The
    fix is a root-anchored --exclude; this asserts the flag is actually in
    the command _run_rclone receives, not just that _rclone_sync "did
    something"."""
    from backend.backup import _rclone_sync

    captured = {}
    def _fake_run(cmd, timeout=None):
        captured["cmd"] = cmd
        return MagicMock(returncode=0, stderr="")
    monkeypatch.setattr("backend.backup._run_rclone", _fake_run)

    ok = _rclone_sync(pathlib.Path("/tmp/staging"), "\\\\192.168.1.50\\Computer Backup\\Nexus_backup-lxc")

    assert ok is True
    cmd = captured["cmd"]
    assert "--exclude" in cmd
    assert cmd[cmd.index("--exclude") + 1] == "/knowledge/**"


@pytest.mark.skipif(__import__("shutil").which("rclone") is None, reason="needs a real rclone binary")
def test_rclone_sync_exclude_preserves_knowledge_deletes_stale(env, monkeypatch, tmp_path):
    """Semantic test with a REAL rclone binary against a local-filesystem
    'remote' (rclone treats a plain path as local, not remote -- no network
    involved): a vault sync must delete a stale top-level file (mirror-sync
    still works) while leaving a pre-existing knowledge/ file untouched."""
    from backend.backup import _rclone_sync, _rclone_remote

    remote_dir = tmp_path / "remote"
    (remote_dir / "knowledge").mkdir(parents=True)
    (remote_dir / "knowledge" / "keep.md").write_text("keep me")
    (remote_dir / "stale.txt").write_text("delete me")

    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "nexus.vault").write_text("{}")

    monkeypatch.setattr("backend.backup._rclone_remote", lambda unc, sub="": str(remote_dir))
    # This test wants the REAL _run_rclone (real subprocess -> real rclone
    # binary against a local path) -- override the autouse guard, which
    # otherwise hard-fails any un-mocked rclone invocation by design.
    import subprocess as _subprocess
    monkeypatch.setattr(
        "backend.backup._run_rclone",
        lambda args, timeout: _subprocess.run(args, capture_output=True, text=True, timeout=timeout),
    )

    ok = _rclone_sync(staging, "\\\\192.168.1.50\\Computer Backup\\Nexus_backup-lxc")

    assert ok is True
    assert (remote_dir / "knowledge" / "keep.md").exists(), "knowledge/ must survive a vault sync"
    assert not (remote_dir / "stale.txt").exists(), "a genuinely stale top-level file must still be pruned"
    assert (remote_dir / "nexus.vault").exists()


def test_backup_vault_posix_reports_failure_when_rclone_sync_fails(env, monkeypatch, tmp_path):
    """CR-1: a real rclone failure (network blip, bad remote config, auth
    failure) must surface as ok: False -- previously _rclone_sync swallowed
    the error internally and backup_vault() unconditionally returned
    ok: True, silently defeating scheduler.py::_vault_backup()'s phone
    escalation (which only fires on ok: False). backup_knowledge() already
    got this right; this is backup_vault() catching up."""
    env["s"].unraid_backup_path = "\\\\192.168.1.50\\Computer Backup\\Nexus_backup-lxc"
    monkeypatch.setattr("backend.backup._STAGING_ROOT", tmp_path / "staging")
    monkeypatch.setattr("backend.backup.os.name", "posix")
    monkeypatch.setattr("backend.backup._mount_unc", lambda *a, **k: None)

    def _failing_run(cmd, timeout=None):
        return MagicMock(returncode=1, stderr="connection refused")
    monkeypatch.setattr("backend.backup._run_rclone", _failing_run)

    from backend.backup import backup_vault
    result = backup_vault()

    assert result["ok"] is False
    assert "rclone" in result["error"].lower()
    # The local staging copy still happened -- only the offsite half failed.
    assert (tmp_path / "staging" / "nexus.vault").exists()


def test_set_secret_default_env_cannot_reach_real_backup(monkeypatch, tmp_path):
    """The conftest-level regression this whole fix exists for: set_secret()
    calls backup_vault() on every write (backend/secrets/vault.py:81-82),
    best-effort. With NO settings mock at all -- the real Settings object,
    resolving unraid_backup_path however the process environment says to --
    a secret save must never be able to reach a real Unraid share. This is
    exactly the scenario every OTHER test in the suite is in by default
    (most tests never touch backup settings at all), so this proves
    conftest.py's env-level UNRAID_BACKUP_PATH="" override actually holds
    for the general case, not just for this file's own carefully-mocked
    `env` fixture."""
    from cryptography.fernet import Fernet
    import backend.secrets.vault as vault_mod

    vault_file = tmp_path / "nexus.vault"
    meta_file = tmp_path / "nexus.vault.meta"
    key_file = tmp_path / ".vault.key"
    key_file.write_bytes(Fernet.generate_key())
    monkeypatch.setattr(vault_mod, "VAULT_PATH", vault_file)
    monkeypatch.setattr(vault_mod, "META_PATH", meta_file)
    monkeypatch.setattr(vault_mod, "KEY_PATH", key_file)

    # No get_settings() mock -- real Settings, real env resolution. The
    # autouse _isolate_backup_targets guard would raise AssertionError if
    # backup_vault() ever reached a real rclone call; this must complete
    # cleanly with no exception propagating (set_secret's own try/except
    # already swallows a backup_vault failure -- this proves it never even
    # gets that far because backup_vault degrades to "not configured").
    vault_mod.set_secret("TEST_KEY", "test-value")

    # Read back via the real vault file directly, not vault_mod.get_secret --
    # the autouse mock_secrets fixture has that name monkeypatched to a
    # fake MOCK_SECRETS lookup for the whole suite, unrelated to this test's
    # own concern (proving the WRITE was isolated from real backup targets).
    from cryptography.fernet import Fernet as _Fernet
    import json as _json
    vault_contents = _json.loads(vault_file.read_text())
    fernet = _Fernet(key_file.read_bytes())
    assert fernet.decrypt(vault_contents["TEST_KEY"].encode()).decode() == "test-value"

    # get_settings() is a plain module-level singleton (not functools.lru_cache) --
    # force a fresh Settings() so this assertion reflects the current env,
    # not whatever an earlier test in the session already cached.
    import backend.config as config_mod
    monkeypatch.setattr(config_mod, "_settings_instance", None)
    assert config_mod.get_settings().unraid_backup_path == ""

    from backend.backup import backup_vault
    result = backup_vault()
    assert result["ok"] is False
    assert "not configured" in result["error"]


def test_restore_vault_posix_unc_fails_loud_not_silent(env, monkeypatch):
    """CR-3: restore_vault() has no POSIX/rclone implementation (YAGNI --
    the real 2026-08-14 restore drill used a manual `rclone copy` fine).
    On POSIX with a UNC unraid_backup_path it must say so explicitly
    instead of silently mis-resolving the UNC string as a local path with
    backslash-literal filenames (which used to "fail safe" only by
    accident, with a misleading "no vault files found" message)."""
    env["s"].unraid_backup_path = "\\\\192.168.1.50\\Computer Backup\\Nexus_backup-lxc"
    monkeypatch.setattr("backend.backup.os.name", "posix")

    from backend.backup import restore_vault
    result = restore_vault()

    assert result["ok"] is False
    assert "not supported on POSIX" in result["error"]
    assert "rclone copy" in result["error"]


def test_backup_knowledge_missing_vault_path_fails_cleanly(env, monkeypatch, tmp_path):
    env["s"].unraid_backup_path = "\\\\192.168.1.50\\Computer Backup\\Nexus_backup-lxc"
    env["s"].obsidian_vault_path = str(tmp_path / "does-not-exist")
    monkeypatch.setattr("backend.backup.os.name", "posix")

    from backend.backup import backup_knowledge
    result = backup_knowledge()
    assert result["ok"] is False
    assert "obsidian_vault_path" in result["error"]


def test_restore_vault_restores_db(env):
    from backend.backup import backup_vault, restore_vault
    assert backup_vault()["ok"] is True

    # Wipe the local files, then restore from the share. Dispose the engine
    # first — on Windows an open handle blocks unlink.
    import backend.database as dbmod
    dbmod.engine.dispose()
    env["db"].unlink()
    env["vault"].unlink()
    result = restore_vault()
    assert result["ok"] is True
    assert env["db"].exists(), "nexus.db not restored"
    assert env["vault"].exists(), "nexus.vault not restored"


@pytest.mark.asyncio
async def test_vault_backup_failure_notifies_phone(monkeypatch):
    """scheduler._vault_backup must page the phone when backup_vault fails."""
    notify = AsyncMock()
    monkeypatch.setattr(
        "backend.backup.backup_vault",
        lambda: {"ok": False, "dest": "", "error": "share unreachable"},
    )
    with patch("backend.events.notify_phone", new=notify):
        from backend.scheduler import _vault_backup
        await _vault_backup()

    notify.assert_awaited_once()
    args, kwargs = notify.await_args
    assert "BACKUP FAILED" in args[0]
    assert kwargs.get("kind") == "backup_failed"
