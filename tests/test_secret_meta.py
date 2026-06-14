def test_stamp_meta_records_set_and_rotated(tmp_path, monkeypatch):
    import backend.secrets.vault as vault
    monkeypatch.setattr(vault, "META_PATH", tmp_path / "nexus.vault.meta")

    vault._stamp_meta("GITHUB_TOKEN")
    meta = vault.read_meta()

    assert "GITHUB_TOKEN" in meta
    assert "last_set" in meta["GITHUB_TOKEN"]
    assert "last_rotated" in meta["GITHUB_TOKEN"]
    # Both timestamps move together on a set (a set over an existing value is a rotation).
    assert meta["GITHUB_TOKEN"]["last_set"] == meta["GITHUB_TOKEN"]["last_rotated"]


def test_stamp_meta_preserves_other_keys(tmp_path, monkeypatch):
    import backend.secrets.vault as vault
    monkeypatch.setattr(vault, "META_PATH", tmp_path / "nexus.vault.meta")

    vault._stamp_meta("ANTHROPIC_API_KEY")
    vault._stamp_meta("HASS_TOKEN")
    meta = vault.read_meta()

    assert set(meta.keys()) == {"ANTHROPIC_API_KEY", "HASS_TOKEN"}


def test_read_meta_missing_file_returns_empty(tmp_path, monkeypatch):
    import backend.secrets.vault as vault
    monkeypatch.setattr(vault, "META_PATH", tmp_path / "does-not-exist.meta")
    assert vault.read_meta() == {}
