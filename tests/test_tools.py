"""Tests for the Tier 2.1 read-only native tool-use loop.

Covers backend/agents/tools.py (the read-only registry) and
backend/agents/router.run_with_tools (the tool-use loop). NO real network /
Anthropic: anthropic.Anthropic is mocked for the loop, integration modules are
patched for the dispatchers.
"""

import inspect

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


# ---------------------------------------------------------------------------
# Helpers to build fake Anthropic Messages API responses.
# ---------------------------------------------------------------------------

def _text_block(text):
    b = MagicMock()
    b.type = "text"
    b.text = text
    return b


def _tool_use_block(name, tid, tinput):
    b = MagicMock()
    b.type = "tool_use"
    b.name = name
    b.id = tid
    b.input = tinput
    return b


def _resp(content, stop_reason):
    r = MagicMock()
    r.content = content
    r.stop_reason = stop_reason
    # Real usage would be plain ints; a MagicMock usage is treated as "no usage"
    # by _record_spend (no SpendLog row written) — fine for these tests.
    return r


# ===========================================================================
# tools.py — dispatchers (normal + raise), truncation, specs, blocklist.
# ===========================================================================

@pytest.mark.asyncio
async def test_homeassistant_status_normal_and_raise():
    from backend.agents import tools

    data = MagicMock()
    data.entities = [1, 2, 3]
    data.alerts = ["a.b", "c.d"]
    with patch("backend.integrations.homeassistant.fetch", new=AsyncMock(return_value=data)):
        out = await tools._homeassistant_status({})
    assert "3 entities" in out and "2 alert" in out

    with patch("backend.integrations.homeassistant.fetch", new=AsyncMock(side_effect=RuntimeError("boom"))):
        out = await tools._homeassistant_status({})
    assert out.startswith("homeassistant_status unavailable:")
    assert "boom" in out


@pytest.mark.asyncio
async def test_homeassistant_temperatures_normal_and_raise():
    from backend.agents import tools

    data = MagicMock()
    data.entities = [
        {"entity_id": "sensor.first_air_quality_monitor_temperature", "state": "77.54"},
        {"entity_id": "sensor.basement_temperature", "state": "72.86"},
        {"entity_id": "sensor.usw_pro_24_poe_temperature", "state": "unavailable"},  # excluded
        {"entity_id": "light.left_porch_light", "state": "on"},  # not a temp sensor
    ]
    with patch("backend.integrations.homeassistant.fetch", new=AsyncMock(return_value=data)):
        out = await tools._homeassistant_temperatures({})
    assert "first air quality monitor" in out
    assert "78°F" in out or "77°F" in out  # rounded from 77.54
    assert "basement" in out
    assert "usw_pro_24_poe" not in out  # unavailable readings excluded

    with patch("backend.integrations.homeassistant.fetch", new=AsyncMock(side_effect=RuntimeError("boom"))):
        out = await tools._homeassistant_temperatures({})
    assert out.startswith("homeassistant_temperatures unavailable:")
    assert "boom" in out


@pytest.mark.asyncio
async def test_unraid_status_normal_and_raise():
    from backend.agents import tools

    data = MagicMock()
    data.array_status = "started"
    data.storage_used_gb = 10.0
    data.storage_total_gb = 20.0
    data.docker_containers = [1, 2]
    data.cpu_pct = 5
    data.ram_pct = 50
    with patch("backend.integrations.unraid.fetch", new=AsyncMock(return_value=data)):
        out = await tools._unraid_status({})
    assert "array=started" in out and "docker=2" in out

    with patch("backend.integrations.unraid.fetch", new=AsyncMock(side_effect=Exception("x"))):
        out = await tools._unraid_status({})
    assert out.startswith("unraid_status unavailable:")


@pytest.mark.asyncio
async def test_unifi_status_normal_and_raise():
    from backend.agents import tools

    data = MagicMock()
    data.client_count = 7
    data.uplink_status = "ok"
    data.bandwidth_mbps = 0.0
    data.alerts = []
    with patch("backend.integrations.unifi.fetch", new=AsyncMock(return_value=data)):
        out = await tools._unifi_status({})
    assert "clients=7" in out and "uplink=ok" in out

    with patch("backend.integrations.unifi.fetch", new=AsyncMock(side_effect=Exception("x"))):
        out = await tools._unifi_status({})
    assert out.startswith("unifi_status unavailable:")


@pytest.mark.asyncio
async def test_adguard_status_normal_and_raise():
    from backend.agents import tools

    data = MagicMock()
    data.blocked_today = 99
    data.blocked_pct = 12.5
    data.filtering_enabled = True
    with patch("backend.integrations.adguard.fetch", new=AsyncMock(return_value=data)):
        out = await tools._adguard_status({})
    assert "99 blocked today" in out and "filtering=True" in out

    with patch("backend.integrations.adguard.fetch", new=AsyncMock(side_effect=Exception("x"))):
        out = await tools._adguard_status({})
    assert out.startswith("adguard_status unavailable:")


@pytest.mark.asyncio
async def test_channels_status_normal_and_raise():
    from backend.agents import tools

    data = MagicMock()
    data.recording_now = [{"title": "Show A"}, {"title": "Show B"}]
    data.storage_used_gb = 1.0
    data.storage_total_gb = 2.0
    with patch("backend.integrations.channels_dvr.fetch", new=AsyncMock(return_value=data)):
        out = await tools._channels_status({})
    assert "Show A" in out and "Show B" in out

    with patch("backend.integrations.channels_dvr.fetch", new=AsyncMock(side_effect=Exception("x"))):
        out = await tools._channels_status({})
    assert out.startswith("channels_status unavailable:")


@pytest.mark.asyncio
async def test_channels_status_surfaces_failed_recordings():
    from backend.agents import tools

    data = MagicMock()
    data.recording_now = []
    data.storage_used_gb = 1.0
    data.storage_total_gb = 2.0
    data.failed_recordings = [{"title": "Big Game", "reason": "failed"}]
    with patch("backend.integrations.channels_dvr.fetch", new=AsyncMock(return_value=data)):
        out = await tools._channels_status({})
    assert "failed/skipped(24h)=1" in out and "Big Game" in out


@pytest.mark.asyncio
async def test_channels_status_emits_zero_failed_count():
    """Healthy 'no failures' case still emits failed/skipped(24h)=0 so the verifier
    can confirm the zero-failures criterion (a missing field reads as 'unavailable')."""
    from backend.agents import tools

    data = MagicMock()
    data.recording_now = []
    data.storage_used_gb = 1.0
    data.storage_total_gb = 2.0
    data.failed_recordings = []
    with patch("backend.integrations.channels_dvr.fetch", new=AsyncMock(return_value=data)):
        out = await tools._channels_status({})
    assert "failed/skipped(24h)=0" in out


@pytest.mark.asyncio
async def test_weather_normal_and_raise():
    from backend.agents import tools

    data = MagicMock()
    data.summary = "Clear, 70.0°F"
    data.temp_f = 70.0
    with patch("backend.integrations.weather.fetch", new=AsyncMock(return_value=data)):
        out = await tools._weather({})
    assert "Clear" in out

    with patch("backend.integrations.weather.fetch", new=AsyncMock(side_effect=Exception("x"))):
        out = await tools._weather({})
    assert out.startswith("weather unavailable:")


@pytest.mark.asyncio
async def test_github_status_normal_and_raise():
    from backend.agents import tools

    data = MagicMock()
    data.open_prs = [{"title": "Fix bug"}, {"title": "Add feature"}]
    data.stale_prs = [{"title": "Fix bug"}]
    with patch("backend.integrations.github.fetch", new=AsyncMock(return_value=data)):
        out = await tools._github_status({})
    assert "2 open PR" in out and "1 stale" in out and "Fix bug" in out

    with patch("backend.integrations.github.fetch", new=AsyncMock(side_effect=Exception("x"))):
        out = await tools._github_status({})
    assert out.startswith("github_status unavailable:")


@pytest.mark.asyncio
async def test_hermes_status_normal_and_raise():
    from backend.agents import tools

    status = MagicMock()
    status.alive = True
    status.last_seen = None
    status.pending_actions = 3
    with patch("backend.integrations.hermes.get_status", new=AsyncMock(return_value=status)):
        out = await tools._hermes_status({})
    assert "alive=True" in out and "pending_actions=3" in out

    with patch("backend.integrations.hermes.get_status", new=AsyncMock(side_effect=Exception("x"))):
        out = await tools._hermes_status({})
    assert out.startswith("hermes_status unavailable:")


@pytest.mark.asyncio
async def test_proxmox_updates_passthrough_and_raise():
    from backend.agents import tools

    # Relays "proxmox updates" to Hermes; returns its response string verbatim.
    with patch("backend.integrations.hermes.relay",
               new=AsyncMock(return_value="3 pending update(s) on pve: libc6, openssl")) as rl:
        out = await tools._proxmox_updates({})
    assert "3 pending update(s)" in out
    rl.assert_awaited_once_with("proxmox updates")

    with patch("backend.integrations.hermes.relay", new=AsyncMock(side_effect=Exception("x"))):
        out = await tools._proxmox_updates({})
    assert out.startswith("proxmox_updates unavailable:")


@pytest.mark.asyncio
async def test_vault_search_passthrough_and_missing_query():
    from backend.agents import tools

    with patch("backend.integrations.obsidian.vault_search", new=AsyncMock(return_value="note hit")):
        out = await tools._vault_search({"query": "ideas"})
    assert out == "note hit"

    # Missing / blank query -> compact unavailable string, no integration call.
    assert await tools._vault_search({}) == "vault_search unavailable: missing 'query'"
    assert await tools._vault_search({"query": "  "}) == "vault_search unavailable: missing 'query'"

    with patch("backend.integrations.obsidian.vault_search", new=AsyncMock(side_effect=Exception("x"))):
        out = await tools._vault_search({"query": "ideas"})
    assert out.startswith("vault_search unavailable:")


@pytest.mark.asyncio
async def test_web_search_passthrough_and_missing_query():
    from backend.agents import tools

    with patch("backend.integrations.web_search.search", new=AsyncMock(return_value="web hit")):
        out = await tools._web_search({"query": "latest python"})
    assert out == "web hit"

    assert await tools._web_search({}) == "web_search unavailable: missing 'query'"

    with patch("backend.integrations.web_search.search", new=AsyncMock(side_effect=Exception("x"))):
        out = await tools._web_search({"query": "x"})
    assert out.startswith("web_search unavailable:")


@pytest.mark.asyncio
async def test_all_dispatchers_never_raise_on_error():
    """Every registered tool returns a string (never raises) when its integration
    blows up."""
    from backend.agents import tools

    integ_targets = [
        "backend.integrations.homeassistant.fetch",
        "backend.integrations.unraid.fetch",
        "backend.integrations.unifi.fetch",
        "backend.integrations.adguard.fetch",
        "backend.integrations.channels_dvr.fetch",
        "backend.integrations.weather.fetch",
        "backend.integrations.github.fetch",
        "backend.integrations.hermes.get_status",
        "backend.integrations.hermes.relay",
        "backend.integrations.obsidian.vault_search",
        "backend.integrations.web_search.search",
    ]
    patchers = [patch(t, new=AsyncMock(side_effect=Exception("kaboom"))) for t in integ_targets]
    for p in patchers:
        p.start()
    try:
        for t in tools.READ_TOOLS:
            # query tools need a query so they actually hit the integration
            arg = {"query": "q"} if "query" in t.input_schema.get("properties", {}) else {}
            out = await t.dispatch(arg)
            assert isinstance(out, str)
            assert "unavailable" in out
    finally:
        for p in patchers:
            p.stop()


def test_truncate_boundary_and_over():
    from backend.agents.tools import MAX_TOOL_RESULT_CHARS, _truncate

    cap = MAX_TOOL_RESULT_CHARS
    # Under cap unchanged.
    s = "a" * 10
    assert _truncate(s) == s
    # Boundary len == cap unchanged.
    s_boundary = "b" * cap
    assert _truncate(s_boundary) == s_boundary
    assert len(_truncate(s_boundary)) == cap
    # Over cap -> truncated, length never exceeds cap.
    s_over = "c" * (cap + 500)
    out = _truncate(s_over)
    assert len(out) <= cap
    assert out.endswith("…[truncated]")


@pytest.mark.asyncio
async def test_dispatcher_result_truncated_to_cap():
    from backend.agents import tools
    from backend.agents.tools import MAX_TOOL_RESULT_CHARS

    huge = "x" * (MAX_TOOL_RESULT_CHARS + 1000)
    with patch("backend.integrations.web_search.search", new=AsyncMock(return_value=huge)):
        out = await tools._web_search({"query": "q"})
    assert len(out) <= MAX_TOOL_RESULT_CHARS


def test_tool_specs_shape_no_type_key():
    from backend.agents.tools import READ_TOOLS, tool_specs

    specs = tool_specs()
    assert len(specs) == len(READ_TOOLS)
    for spec in specs:
        assert set(spec.keys()) == {"name", "description", "input_schema"}
        assert "type" not in spec  # custom tools, not hosted/server tools


def test_dispatcher_map_keys_match_registry():
    from backend.agents.tools import READ_TOOLS, dispatcher_map

    dmap = dispatcher_map()
    assert set(dmap.keys()) == {t.name for t in READ_TOOLS}
    expected = {
        "homeassistant_status", "homeassistant_temperatures", "unraid_status", "unifi_status",
        "adguard_status", "channels_status", "weather", "github_status", "hermes_status",
        "proxmox_updates", "proxmox_backups", "vault_search", "ddg_search",
    }
    assert set(dmap.keys()) == expected
    # ITEM 5: the local DuckDuckGo tool was renamed to avoid colliding with the
    # hosted web_search tool — the registry must expose ddg_search, NOT web_search.
    assert "ddg_search" in dmap
    assert "web_search" not in dmap


def test_local_and_hosted_search_tools_coexist_distinct_names():
    """The combined tools list (hosted web_search + local custom tools) must have
    all-distinct names: hosted 'web_search' and local 'ddg_search' never collide."""
    from backend.agents import router
    from backend.agents.tools import tool_specs

    combined = [router._WEB_SEARCH_TOOL] + tool_specs()
    names = [t["name"] for t in combined]
    assert len(names) == len(set(names))  # all distinct
    assert "web_search" in names   # hosted
    assert "ddg_search" in names   # local custom


def test_planner_tool_block_lists_all_tools():
    from backend.agents.tools import READ_TOOLS, planner_tool_block

    block = planner_tool_block()
    for t in READ_TOOLS:
        assert f"- {t.name}:" in block


# ---------------------------------------------------------------------------
# ITEM 5 — read-only guarantee.
# ---------------------------------------------------------------------------

_WRITE_FN_NAMES = {
    "call_service", "restart_docker", "set_filtering", "disable_for_minutes",
    "trigger_recording", "relay", "action", "notify", "execute_action",
    "create_note", "write_daily_note",
}


def test_no_dispatcher_references_a_write_function():
    """No dispatcher's source CALLS a known write function, and the tools module
    never imports the safety broker.

    We match a write fn name only when it is *called* (`name(`) or imported, not
    as a substring of a legitimate read field (e.g. `pending_actions` must not
    trip on `action`)."""
    import re
    from backend.agents import tools

    def references_write_call(src: str) -> str | None:
        for forbidden in _WRITE_FN_NAMES:
            # `forbidden(` as a call, or `import forbidden` / `, forbidden` in an import.
            if re.search(rf"(?<![\w.]){re.escape(forbidden)}\s*\(", src):
                return forbidden
            if re.search(rf"\bimport\b[^\n]*\b{re.escape(forbidden)}\b", src):
                return forbidden
        return None

    for t in tools.READ_TOOLS:
        src = inspect.getsource(t.dispatch)
        hit = references_write_call(src)
        assert hit is None, f"{t.name} references write fn {hit}"

    module_src = inspect.getsource(tools)
    # No broker IMPORT (the module docstring may NAME the broker in prose to
    # explain that writes are deferred behind it — that mention is allowed; an
    # actual import is not).
    import_lines = [
        ln for ln in module_src.splitlines()
        if ln.strip().startswith(("import ", "from "))
    ]
    for ln in import_lines:
        assert "broker" not in ln, f"tools.py imports the broker: {ln!r}"
    # Whole-module sweep: no write fn is CALLED or IMPORTED anywhere in tools.py.
    hit = references_write_call(module_src)
    assert hit is None, f"tools.py references write fn {hit}"


# ===========================================================================
# router.run_with_tools — the loop.
# ===========================================================================

@pytest.mark.asyncio
async def test_run_with_tools_tool_use_then_text():
    """One tool_use round, then a final text answer. create called twice; the
    second request carries the tool_result in its messages."""
    from backend.agents import router

    r1 = _resp([_tool_use_block("weather", "tid1", {})], stop_reason="tool_use")
    r2 = _resp([_text_block("It is sunny.")], stop_reason="end_turn")

    mock_client = MagicMock()
    mock_client.messages.create.side_effect = [r1, r2]

    dispatch = {"weather": AsyncMock(return_value="Weather: Clear, 70F")}

    with patch("anthropic.Anthropic", return_value=mock_client):
        out = await router.run_with_tools(
            model=router.SONNET_MODEL, max_tokens=1024, prompt="weather?",
            system="", tool_specs=[{"name": "weather", "description": "d", "input_schema": {"type": "object", "properties": {}}}],
            dispatch=dispatch,
        )

    assert out == "It is sunny."
    assert mock_client.messages.create.call_count == 2
    # Second call's messages include the tool_result.
    second_messages = mock_client.messages.create.call_args_list[1].kwargs["messages"]
    flat = [
        c for m in second_messages if isinstance(m.get("content"), list)
        for c in m["content"] if isinstance(c, dict)
    ]
    tool_results = [c for c in flat if c.get("type") == "tool_result"]
    assert tool_results and tool_results[0]["content"] == "<tool_output>\nWeather: Clear, 70F\n</tool_output>"
    assert tool_results[0]["tool_use_id"] == "tid1"


@pytest.mark.asyncio
async def test_run_with_tools_unknown_tool_continues():
    """An unknown tool name yields 'unknown tool: X' as the result and the loop
    continues to a final answer."""
    from backend.agents import router

    r1 = _resp([_tool_use_block("nope", "tidX", {})], stop_reason="tool_use")
    r2 = _resp([_text_block("done anyway")], stop_reason="end_turn")
    mock_client = MagicMock()
    mock_client.messages.create.side_effect = [r1, r2]

    with patch("anthropic.Anthropic", return_value=mock_client):
        out = await router.run_with_tools(
            model=router.SONNET_MODEL, max_tokens=1024, prompt="x",
            system="", tool_specs=[], dispatch={},
        )

    assert out == "done anyway"
    second_messages = mock_client.messages.create.call_args_list[1].kwargs["messages"]
    flat = [
        c for m in second_messages if isinstance(m.get("content"), list)
        for c in m["content"] if isinstance(c, dict)
    ]
    tr = [c for c in flat if c.get("type") == "tool_result"]
    assert tr and tr[0]["content"] == "<tool_output>\nunknown tool: nope\n</tool_output>"


@pytest.mark.asyncio
async def test_run_with_tools_none_input_coerced():
    """A tool_use block whose .input is not a dict is coerced to {} before dispatch."""
    from backend.agents import router

    seen = {}

    async def disp(inp):
        seen["inp"] = inp
        return "ok"

    r1 = _resp([_tool_use_block("weather", "t1", None)], stop_reason="tool_use")
    r2 = _resp([_text_block("final")], stop_reason="end_turn")
    mock_client = MagicMock()
    mock_client.messages.create.side_effect = [r1, r2]

    with patch("anthropic.Anthropic", return_value=mock_client):
        out = await router.run_with_tools(
            model=router.SONNET_MODEL, max_tokens=1024, prompt="x",
            system="", tool_specs=[], dispatch={"weather": disp},
        )
    assert out == "final"
    assert seen["inp"] == {}


@pytest.mark.asyncio
async def test_run_with_tools_max_rounds_perpetual_tool_use():
    """If the model never stops calling tools, the loop bails at max_rounds and
    create is called at most max_rounds times."""
    from backend.agents import router

    def make_tool_use(*a, **k):
        return _resp([_tool_use_block("weather", "t", {})], stop_reason="tool_use")

    mock_client = MagicMock()
    mock_client.messages.create.side_effect = make_tool_use

    with patch("anthropic.Anthropic", return_value=mock_client):
        out = await router.run_with_tools(
            model=router.SONNET_MODEL, max_tokens=1024, prompt="x",
            system="", tool_specs=[], dispatch={"weather": AsyncMock(return_value="r")},
            max_rounds=2,
        )

    assert isinstance(out, str)
    assert mock_client.messages.create.call_count <= 2
    # No text in any response -> the sentinel message.
    assert out == "(tool loop reached max rounds without a final answer)"


@pytest.mark.asyncio
async def test_run_with_tools_budget_exceeded_mid_loop_propagates():
    """A BudgetExceeded raised by the daily brake mid-loop propagates out of
    run_with_tools (so a durable task can finalize failed/budget_exceeded)."""
    from backend.agents import router
    from backend.safety.governor import BudgetExceeded

    r1 = _resp([_tool_use_block("weather", "t1", {})], stop_reason="tool_use")
    r2 = _resp([_text_block("never reached")], stop_reason="end_turn")
    mock_client = MagicMock()
    mock_client.messages.create.side_effect = [r1, r2]

    calls = {"n": 0}

    def fake_check_budget(*a, **k):
        calls["n"] += 1
        if calls["n"] >= 2:  # allow round 1, trip before round 2
            raise BudgetExceeded("daily", spend=30.0, cap=25.0)

    with patch("anthropic.Anthropic", return_value=mock_client), \
         patch("backend.safety.governor.check_budget", side_effect=fake_check_budget):
        with pytest.raises(BudgetExceeded):
            await router.run_with_tools(
                model=router.SONNET_MODEL, max_tokens=1024, prompt="x",
                system="", tool_specs=[], dispatch={"weather": AsyncMock(return_value="r")},
            )


@pytest.mark.asyncio
async def test_run_with_tools_mixed_text_and_tool_use_continues():
    """A response with BOTH text and a tool_use block but stop_reason=tool_use
    continues the loop (does not early-return the partial text)."""
    from backend.agents import router

    r1 = _resp(
        [_text_block("let me check"), _tool_use_block("weather", "t1", {})],
        stop_reason="tool_use",
    )
    r2 = _resp([_text_block("the answer")], stop_reason="end_turn")
    mock_client = MagicMock()
    mock_client.messages.create.side_effect = [r1, r2]

    with patch("anthropic.Anthropic", return_value=mock_client):
        out = await router.run_with_tools(
            model=router.SONNET_MODEL, max_tokens=1024, prompt="x",
            system="", tool_specs=[], dispatch={"weather": AsyncMock(return_value="r")},
        )
    assert out == "the answer"
    assert mock_client.messages.create.call_count == 2


@pytest.mark.asyncio
async def test_run_with_tools_text_only_other_stop_reason_returns():
    """A text-only response with a non-tool_use stop_reason returns immediately
    (single create)."""
    from backend.agents import router

    r1 = _resp([_text_block("direct answer")], stop_reason="end_turn")
    mock_client = MagicMock()
    mock_client.messages.create.return_value = r1

    with patch("anthropic.Anthropic", return_value=mock_client):
        out = await router.run_with_tools(
            model=router.SONNET_MODEL, max_tokens=1024, prompt="x",
            system="", tool_specs=[], dispatch={},
        )
    assert out == "direct answer"
    assert mock_client.messages.create.call_count == 1


@pytest.mark.asyncio
async def test_run_with_tools_web_search_tool_added():
    """When web_search=True the hosted web_search tool is prepended to the tools list."""
    from backend.agents import router

    r1 = _resp([_text_block("a")], stop_reason="end_turn")
    mock_client = MagicMock()
    mock_client.messages.create.return_value = r1

    with patch("anthropic.Anthropic", return_value=mock_client):
        await router.run_with_tools(
            model=router.SONNET_MODEL, max_tokens=1024, prompt="x",
            system="", tool_specs=[{"name": "weather", "description": "d", "input_schema": {"type": "object", "properties": {}}}],
            dispatch={}, web_search=True,
        )
    tools_sent = mock_client.messages.create.call_args.kwargs["tools"]
    assert tools_sent[0] == router._WEB_SEARCH_TOOL
    assert any(t.get("name") == "weather" for t in tools_sent if isinstance(t, dict))
