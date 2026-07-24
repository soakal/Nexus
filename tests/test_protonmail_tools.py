import pytest
from unittest.mock import AsyncMock, patch


@pytest.mark.asyncio
async def test_protonmail_inbox_success():
    from backend.agents import tools
    with patch(
        "backend.integrations.protonmail.list_recent",
        new=AsyncMock(return_value='[{"subject": "hi"}]'),
    ) as mock_call:
        out = await tools._protonmail_inbox({"unread_only": True, "limit": 3})
    assert "hi" in out
    mock_call.assert_awaited_once_with(
        unread_only=True, from_address=None, subject=None, limit=3
    )


@pytest.mark.asyncio
async def test_protonmail_inbox_raise_returns_unavailable():
    from backend.agents import tools
    with patch(
        "backend.integrations.protonmail.list_recent",
        new=AsyncMock(side_effect=Exception("down")),
    ):
        out = await tools._protonmail_inbox({})
    assert out.startswith("protonmail_inbox unavailable:")


@pytest.mark.asyncio
async def test_protonmail_read_email_success():
    from backend.agents import tools
    with patch(
        "backend.integrations.protonmail.read_email",
        new=AsyncMock(return_value="body text"),
    ) as mock_call:
        out = await tools._protonmail_read_email({"email_id": "95", "page": 2})
    assert out == "body text"
    mock_call.assert_awaited_once_with("95", page=2)


@pytest.mark.asyncio
async def test_protonmail_read_email_missing_id():
    from backend.agents import tools
    out = await tools._protonmail_read_email({})
    assert "missing 'email_id'" in out


@pytest.mark.asyncio
async def test_protonmail_read_email_raise_returns_unavailable():
    from backend.agents import tools
    with patch(
        "backend.integrations.protonmail.read_email",
        new=AsyncMock(side_effect=Exception("down")),
    ):
        out = await tools._protonmail_read_email({"email_id": "1"})
    assert out.startswith("protonmail_read_email unavailable:")


@pytest.mark.asyncio
async def test_protonmail_status_reachable():
    from backend.agents import tools
    with patch("backend.integrations.protonmail.health_check", new=AsyncMock(return_value=True)):
        out = await tools._protonmail_status({})
    assert "reachable=True" in out


@pytest.mark.asyncio
async def test_protonmail_status_raise_returns_unavailable():
    from backend.agents import tools
    with patch(
        "backend.integrations.protonmail.health_check",
        new=AsyncMock(side_effect=Exception("down")),
    ):
        out = await tools._protonmail_status({})
    assert out.startswith("protonmail_status unavailable:")


def test_protonmail_tools_registered_no_broker_import():
    """protonmail tools must be in READ_TOOLS and tools.py must never import the broker."""
    import inspect
    from backend.agents import tools
    from backend.agents.tools import READ_TOOLS

    names = {t.name for t in READ_TOOLS}
    assert {"protonmail_inbox", "protonmail_read_email", "protonmail_status"} <= names

    src = inspect.getsource(tools)
    assert "import backend.safety.broker" not in src
    assert "from backend.safety" not in src
    assert "from backend.safety.broker" not in src


def test_protonmail_in_sources_registry():
    import inspect
    from backend.api import sources
    src = inspect.getsource(sources.sources_status)
    assert '"protonmail": protonmail' in src


def test_protonmail_in_uptime_registry():
    import inspect
    from backend import scheduler
    src = inspect.getsource(scheduler._record_uptime)
    assert '"protonmail": protonmail' in src
