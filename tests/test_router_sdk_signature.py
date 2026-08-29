"""Guards against the exact class of bug that sank PR #36: a kwarg the router
builds that the INSTALLED anthropic SDK version doesn't actually accept.
Every existing router test mocks `anthropic.Anthropic` with a MagicMock,
which accepts any kwargs and would never have caught
`output_config` on SDK 0.40.0 raising a real TypeError in production.

This binds the exact kwargs shapes router.py builds against the real,
installed `Messages.create`/`Messages.stream` signatures -- no network call,
no mock, just `inspect.signature(...).bind(...)`. A signature mismatch fails
here instead of in production.
"""
import inspect

import anthropic


def _bind(method, kwargs):
    """`method` is an unbound instance method (e.g. Messages.create) -- bind
    with a placeholder `self` since we only care whether the REST of the
    kwargs match the installed SDK's signature."""
    inspect.signature(method).bind(object(), **kwargs)


def test_create_sync_kwargs_bind_to_installed_sdk():
    """Mirrors _create_sync's kwargs (router.py)."""
    kwargs = {
        "model": "claude-sonnet-5",
        "max_tokens": 8192,
        "messages": [{"role": "user", "content": "hi"}],
        "system": "be terse",
        "tools": [{"type": "web_search_20260209", "name": "web_search", "max_uses": 5}],
    }
    _bind(anthropic.resources.messages.Messages.create, kwargs)


def test_create_sync_raw_kwargs_bind_to_installed_sdk():
    """Mirrors _create_sync_raw's kwargs (router.py) -- full messages list + tools."""
    kwargs = {
        "model": "claude-sonnet-5",
        "max_tokens": 8192,
        "messages": [{"role": "user", "content": "hi"}],
        "system": "be terse",
        "tools": [{"name": "vault_search", "description": "search", "input_schema": {"type": "object", "properties": {}}}],
    }
    _bind(anthropic.resources.messages.Messages.create, kwargs)


def test_create_streaming_sync_kwargs_bind_to_installed_sdk():
    """Mirrors _create_streaming_sync's kwargs (router.py) -- uses .stream, not .create."""
    kwargs = {
        "model": "claude-sonnet-5",
        "max_tokens": 8192,
        "messages": [{"role": "user", "content": "hi"}],
        "system": "be terse",
    }
    _bind(anthropic.resources.messages.Messages.stream, kwargs)


def test_bare_client_construction_has_no_unexpected_required_args():
    """Mirrors router.get_client() / secrets._test_anthropic() -- just api_key."""
    inspect.signature(anthropic.Anthropic).bind(api_key="sk-test")
    inspect.signature(anthropic.AsyncAnthropic).bind(api_key="sk-test")
