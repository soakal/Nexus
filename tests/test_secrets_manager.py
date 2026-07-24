import logging

import pytest

from backend.secrets import manager

# Captured at collection time, before conftest's autouse mock_secrets fixture
# monkeypatches manager.get_secret for every test — these tests exercise the
# real routing/fallback logic, so they restore it explicitly (innermost patch
# wins, same pattern documented in conftest.py itself).
_real_get_secret = manager.get_secret


class _Settings:
    def __init__(self, *, backend="auto", url="", client_id="", client_secret="", project_id=""):
        self.secrets_backend = backend
        self.infisical_url = url
        self.infisical_client_id = client_id
        self.infisical_client_secret = client_secret
        self.infisical_project_id = project_id


def _patch_settings(monkeypatch, settings):
    monkeypatch.setattr("backend.config.get_settings", lambda: settings)


def test_auto_mode_picks_vault_when_infisical_env_incomplete(monkeypatch):
    _patch_settings(monkeypatch, _Settings(backend="auto", url="https://x", client_id="", client_secret="s", project_id="p"))
    assert manager._active_backend_name() == "vault"


def test_auto_mode_picks_infisical_when_all_four_set(monkeypatch):
    _patch_settings(monkeypatch, _Settings(backend="auto", url="https://x", client_id="c", client_secret="s", project_id="p"))
    assert manager._active_backend_name() == "infisical"


def test_secrets_backend_vault_forces_legacy_even_if_infisical_configured(monkeypatch):
    _patch_settings(monkeypatch, _Settings(backend="vault", url="https://x", client_id="c", client_secret="s", project_id="p"))
    assert manager._active_backend_name() == "vault"


def test_secrets_backend_infisical_forces_infisical(monkeypatch):
    _patch_settings(monkeypatch, _Settings(backend="infisical"))
    assert manager._active_backend_name() == "infisical"


def test_vault_backend_never_touches_infisical_module(monkeypatch):
    monkeypatch.setattr(manager, "get_secret", _real_get_secret)
    _patch_settings(monkeypatch, _Settings(backend="vault"))

    def _boom(*a, **k):
        raise AssertionError("infisical_client must not be called when backend=vault")

    monkeypatch.setattr("backend.secrets.infisical_client.get_secret", _boom)
    monkeypatch.setattr("backend.secrets.vault.get_secret", lambda k: "vault-value")
    assert manager.get_secret("ANY_KEY") == "vault-value"


def test_get_secret_falls_back_infisical_to_vault_and_logs_warning(monkeypatch, caplog):
    monkeypatch.setattr(manager, "get_secret", _real_get_secret)
    _patch_settings(monkeypatch, _Settings(backend="infisical", url="https://x", client_id="c", client_secret="s", project_id="p"))

    def infisical_miss(key):
        raise KeyError(key)

    monkeypatch.setattr("backend.secrets.infisical_client.get_secret", infisical_miss)
    monkeypatch.setattr("backend.secrets.vault.get_secret", lambda k: "from-legacy-vault")

    with caplog.at_level(logging.WARNING):
        assert manager.get_secret("SOME_KEY") == "from-legacy-vault"
    assert "legacy vault fallback" in caplog.text


def test_get_secret_falls_back_to_env_when_both_stores_miss(monkeypatch):
    monkeypatch.setattr(manager, "get_secret", _real_get_secret)
    _patch_settings(monkeypatch, _Settings(backend="infisical", url="https://x", client_id="c", client_secret="s", project_id="p"))

    def miss(key):
        raise KeyError(key)

    monkeypatch.setattr("backend.secrets.infisical_client.get_secret", miss)
    monkeypatch.setattr("backend.secrets.vault.get_secret", miss)
    monkeypatch.setenv("SOME_ENV_ONLY_KEY", "env-value")
    assert manager.get_secret("SOME_ENV_ONLY_KEY") == "env-value"


def test_get_secret_raises_when_all_three_miss(monkeypatch):
    monkeypatch.setattr(manager, "get_secret", _real_get_secret)
    _patch_settings(monkeypatch, _Settings(backend="vault"))

    def miss(key):
        raise KeyError(key)

    monkeypatch.setattr("backend.secrets.vault.get_secret", miss)
    monkeypatch.delenv("TRULY_MISSING_KEY", raising=False)
    with pytest.raises(KeyError):
        manager.get_secret("TRULY_MISSING_KEY")


def test_writes_are_active_backend_only_no_dual_write(monkeypatch):
    _patch_settings(monkeypatch, _Settings(backend="infisical", url="https://x", client_id="c", client_secret="s", project_id="p"))

    vault_calls = []
    infisical_calls = []
    monkeypatch.setattr("backend.secrets.vault.set_secret", lambda k, v: vault_calls.append((k, v)))
    monkeypatch.setattr("backend.secrets.infisical_client.set_secret", lambda k, v: infisical_calls.append((k, v)))

    manager.set_secret("K", "V")
    assert infisical_calls == [("K", "V")]
    assert vault_calls == []


def test_warm_up_passthrough_on_vault_backend_is_noop(monkeypatch):
    _patch_settings(monkeypatch, _Settings(backend="vault"))
    assert manager.warm_up() is True


def test_warm_up_delegates_to_infisical_backend(monkeypatch):
    _patch_settings(monkeypatch, _Settings(backend="infisical", url="https://x", client_id="c", client_secret="s", project_id="p"))
    monkeypatch.setattr("backend.secrets.infisical_client.warm_up", lambda: True)
    assert manager.warm_up() is True
