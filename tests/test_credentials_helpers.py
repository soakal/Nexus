"""Unit tests for backend.secrets.credentials — the credential-key contract shared
by the Fernet vault and the Infisical client."""
import pytest

from backend.secrets.credentials import (
    CRED_FIELDS,
    build_credential_index,
    collect_credential,
    cred_key,
    cred_prefix,
    parse_cred_key,
)


def test_cred_key_and_prefix():
    assert cred_key("unraid", "host") == "cred:unraid:host"
    assert cred_prefix("unraid") == "cred:unraid:"
    assert cred_key("unraid", "host").startswith(cred_prefix("unraid"))


@pytest.mark.parametrize("raw", ["OPENAI_API_KEY", "cred:unraid", "cred:", ""])
def test_parse_cred_key_rejects_non_credential_keys(raw):
    assert parse_cred_key(raw) is None


def test_parse_cred_key_roundtrip():
    assert parse_cred_key(cred_key("unraid", "password")) == ("unraid", "password")


def test_build_credential_index_never_reads_password():
    keys = [
        "cred:unraid:host",
        "cred:unraid:user",
        "cred:unraid:password",
        "cred:ha:port",
        "OPENAI_API_KEY",  # non-credential key in the same keyspace
    ]
    read: list[str] = []

    def get_value(raw_key):
        read.append(raw_key)
        return raw_key.rsplit(":", 1)[-1] + "-value"

    index = build_credential_index(keys, get_value)

    assert index == {
        "unraid": {
            "host": "host-value",
            "user": "user-value",
            "port": None,
            "has_password": True,
        },
        "ha": {"host": None, "user": None, "port": "port-value", "has_password": False},
    }
    assert "cred:unraid:password" not in read


def test_collect_credential_fills_missing_fields_with_none():
    stored = {"cred:unraid:host": "tower", "cred:unraid:password": "hunter2"}

    def get_secret(raw_key):
        return stored[raw_key]  # KeyError for anything unset

    assert collect_credential("unraid", get_secret) == {
        "host": "tower",
        "user": None,
        "password": "hunter2",
        "port": None,
    }
    assert set(CRED_FIELDS) == {"host", "user", "password", "port"}
