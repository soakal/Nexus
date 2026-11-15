"""/api/health must not raise (NameError regression) and must expose running_sha."""
import asyncio, json
from backend import main, version


def _run(tmp_path, monkeypatch, key=True, vault=True):
    monkeypatch.chdir(tmp_path)
    if key: (tmp_path / ".vault.key").write_text("k")
    if vault: (tmp_path / "nexus.vault").write_text("v")
    r = asyncio.run(main.health())
    return r if isinstance(r, dict) else json.loads(r.body)


def test_ok_reports_running_sha(tmp_path, monkeypatch):
    monkeypatch.setattr(version, "_running_sha", "abc123")
    assert _run(tmp_path, monkeypatch) == {"status": "ok", "running_sha": "abc123"}


def test_ok_with_unreadable_sha_is_none(tmp_path, monkeypatch):
    monkeypatch.setattr(version, "_running_sha", None)
    assert _run(tmp_path, monkeypatch)["running_sha"] is None


def test_vault_states_unchanged(tmp_path, monkeypatch):
    assert _run(tmp_path, monkeypatch, key=False)["status"] == "vault_missing"
