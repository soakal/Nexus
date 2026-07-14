"""Tests for fetch_automation_index() — entity_id -> [automation names] mapping.

Verifies:
- Happy path: automation.* entities enumerated from fetch(), per-automation
  config GET builds an entity_id -> [friendly names] index from nested
  trigger/condition/action entity_id references.
- 401 on a per-automation config fetch degrades to a partial index with
  exactly one summary warning logged (not one per failure).
- Timeout on the initial fetch() degrades to an empty dict with exactly one
  warning logged, never raises.
- Result is cached for the TTL window (300s) — a second call within the
  window does not perform any new HTTP requests.
"""

import logging

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


@pytest.mark.asyncio
async def test_automation_index_happy_path():
    """Builds entity_id -> [automation names] from nested trigger/action entity_ids."""
    states = [
        {
            "entity_id": "automation.christmas_tree_plug_off",
            "attributes": {"id": "1700000000001", "friendly_name": "Christmas Tree Plug Off"},
        },
        {"entity_id": "light.left_garage_light", "state": "on"},
    ]
    automation_config = {
        "alias": "Christmas Tree Plug Off",
        "trigger": [{"platform": "time", "at": "23:59:00"}],
        "condition": [],
        "action": [
            {
                "service": "switch.turn_off",
                "target": {"entity_id": "switch.tall_light_lr_christmas_tree_plug"},
            }
        ],
    }

    with patch("httpx.AsyncClient") as mock_client_cls:
        states_resp = MagicMock(status_code=200)
        states_resp.raise_for_status = MagicMock()
        states_resp.json.return_value = states

        config_resp = MagicMock(status_code=200)
        config_resp.raise_for_status = MagicMock()
        config_resp.json.return_value = automation_config

        mock_client = AsyncMock()
        mock_client.__aenter__.return_value.get = AsyncMock(
            side_effect=[states_resp, config_resp]
        )
        mock_client_cls.return_value = mock_client

        from backend.integrations.homeassistant import fetch_automation_index
        index = await fetch_automation_index()

    assert index == {
        "switch.tall_light_lr_christmas_tree_plug": ["Christmas Tree Plug Off"],
    }


@pytest.mark.asyncio
async def test_automation_index_401_degrades_with_one_warning(caplog):
    """A 401 on the per-automation config GET degrades to a partial index and
    logs exactly one summary warning, never raises."""
    states = [
        {
            "entity_id": "automation.broken",
            "attributes": {"id": "1700000000002", "friendly_name": "Broken Automation"},
        },
    ]

    with patch("httpx.AsyncClient") as mock_client_cls:
        states_resp = MagicMock(status_code=200)
        states_resp.raise_for_status = MagicMock()
        states_resp.json.return_value = states

        import httpx as httpx_mod
        config_resp = MagicMock(status_code=401)

        def _raise_401():
            raise httpx_mod.HTTPStatusError(
                "401 Unauthorized", request=MagicMock(), response=config_resp
            )

        config_resp.raise_for_status = MagicMock(side_effect=_raise_401)

        mock_client = AsyncMock()
        mock_client.__aenter__.return_value.get = AsyncMock(
            side_effect=[states_resp, config_resp]
        )
        mock_client_cls.return_value = mock_client

        from backend.integrations.homeassistant import fetch_automation_index
        with caplog.at_level(logging.WARNING, logger="backend.integrations.homeassistant"):
            index = await fetch_automation_index()

    assert index == {}
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1


@pytest.mark.asyncio
async def test_automation_index_initial_fetch_timeout_degrades_empty(caplog):
    """A timeout on the initial fetch() (entity enumeration) degrades to an
    empty dict with exactly one warning, never raises."""
    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value.get = AsyncMock(side_effect=TimeoutError("timed out"))
        mock_client_cls.return_value = mock_client

        from backend.integrations.homeassistant import fetch_automation_index
        with caplog.at_level(logging.WARNING, logger="backend.integrations.homeassistant"):
            index = await fetch_automation_index()

    assert index == {}
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1


@pytest.mark.asyncio
async def test_automation_index_cached_for_ttl_window():
    """A second call within the 300s TTL window reuses the cached result and
    performs no new HTTP requests."""
    states = [
        {
            "entity_id": "automation.christmas_tree_plug_off",
            "attributes": {"id": "1700000000001", "friendly_name": "Christmas Tree Plug Off"},
        },
    ]
    automation_config = {
        "alias": "Christmas Tree Plug Off",
        "action": [{"service": "switch.turn_off", "target": {"entity_id": "switch.plug"}}],
    }

    with patch("httpx.AsyncClient") as mock_client_cls:
        states_resp = MagicMock(status_code=200)
        states_resp.raise_for_status = MagicMock()
        states_resp.json.return_value = states

        config_resp = MagicMock(status_code=200)
        config_resp.raise_for_status = MagicMock()
        config_resp.json.return_value = automation_config

        get_mock = AsyncMock(side_effect=[states_resp, config_resp])
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value.get = get_mock
        mock_client_cls.return_value = mock_client

        from backend.integrations.homeassistant import fetch_automation_index
        first = await fetch_automation_index()
        second = await fetch_automation_index()

    assert first == second == {"switch.plug": ["Christmas Tree Plug Off"]}
    # Only the first call's two GETs (states + one automation config) happened —
    # the cached second call made no new HTTP requests.
    assert get_mock.await_count == 2
