import pytest


def _settings(**overrides):
    from backend.config import Settings
    return Settings(**overrides)


@pytest.mark.parametrize("value", ["07:00", "7:00", "23:59", "00:00", "7:5"])
def test_briefing_time_valid_forms(value):
    # mock_secrets (autouse) supplies the required secrets, so these pass.
    _settings(briefing_time=value).validate()


@pytest.mark.parametrize("value", ["24:00", "07:60", "7", "ab:cd", "07:00:00", "", "-1:00"])
def test_briefing_time_invalid_forms(value):
    with pytest.raises(ValueError, match="briefing_time"):
        _settings(briefing_time=value).validate()


def test_briefing_timezone_invalid_raises():
    with pytest.raises(ValueError, match="briefing_timezone"):
        _settings(briefing_timezone="Mars/Olympus_Mons").validate()


def test_validate_missing_required_secret_raises(monkeypatch):
    from backend.config import Settings
    real = Settings.anthropic_api_key.fget

    def boom(self):
        raise KeyError("ANTHROPIC_API_KEY")

    monkeypatch.setattr(Settings, "anthropic_api_key", property(boom))
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        _settings().validate()
    # restore not needed — monkeypatch undoes it, but keep ref to satisfy linters
    assert real is not None


def test_validate_lists_all_missing_secrets(monkeypatch):
    from backend.config import Settings
    monkeypatch.setattr(
        Settings, "anthropic_api_key",
        property(lambda self: (_ for _ in ()).throw(KeyError("ANTHROPIC_API_KEY"))),
    )
    monkeypatch.setattr(
        Settings, "nexus_api_key",
        property(lambda self: (_ for _ in ()).throw(KeyError("NEXUS_API_KEY"))),
    )
    with pytest.raises(RuntimeError) as exc:
        _settings().validate()
    msg = str(exc.value)
    assert "ANTHROPIC_API_KEY" in msg and "NEXUS_API_KEY" in msg


def test_validate_passes_with_optional_secret_missing(monkeypatch):
    # An optional secret being absent must NOT fail validation.
    from backend.config import Settings
    monkeypatch.setattr(
        Settings, "github_token",
        property(lambda self: (_ for _ in ()).throw(KeyError("GITHUB_TOKEN"))),
    )
    _settings().validate()


def test_brain_mcp_write_token_present_returns_value(monkeypatch):
    # mock_secrets (autouse) falls back to os.environ for keys it doesn't mock.
    monkeypatch.setenv("BRAIN_MCP_WRITE_TOKEN", "test-brain-mcp-token")
    assert _settings().brain_mcp_write_token == "test-brain-mcp-token"


def test_brain_mcp_write_token_absent_returns_empty_string():
    # Not in MOCK_SECRETS and not in the environment -> KeyError -> "".
    assert _settings().brain_mcp_write_token == ""


def test_brain_mcp_write_token_not_required_by_validate():
    # Optional secret: absent must not fail validation (mirrors github_token above).
    _settings().validate()
