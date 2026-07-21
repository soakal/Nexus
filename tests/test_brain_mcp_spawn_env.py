import os

from backend.main import _brain_mcp_spawn_env


def test_spawn_env_none_when_token_missing():
    assert _brain_mcp_spawn_env(None) is None


def test_spawn_env_none_when_token_blank():
    assert _brain_mcp_spawn_env("") is None


def test_spawn_env_carries_token_and_inherits_parent_env(monkeypatch):
    monkeypatch.setenv("NEXUS_TEST_PARENT_VAR", "parent-value")
    env = _brain_mcp_spawn_env("abc123")
    assert env is not None
    assert env["MCP_WRITE_TOKEN"] == "abc123"
    # Full copy of the parent env, not a minimal dict (breaks Windows child
    # startup if SystemRoot/PATH etc. are dropped).
    assert env["NEXUS_TEST_PARENT_VAR"] == "parent-value"
    assert env.keys() >= os.environ.keys()
