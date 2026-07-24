"""Read-only native tool registry for the task executor (Tier 2.1).

READ ONLY. Every tool in this module performs a side-effect-free READ against a
homelab integration (status snapshot, search). There are ZERO write tools here:
no call_service, no restart_docker, no set_filtering, no trigger_recording, no
relay/action/notify, no create_note. ALL side-effecting writes are deferred and
must go through `backend.safety.broker.execute_action` — they are NOT exposed to
the autonomous tool-use loop yet (a later tier wires writes in behind the broker
with policy gating + confirmation). This file must never import the broker.

Each tool is a `ReadTool`: a name, a description, an `input_schema`, and an async
`dispatch(input: dict) -> str` that wraps the underlying integration call in
try/except and always returns a short string (success summary or
"<name> unavailable: <error>"), truncated to MAX_TOOL_RESULT_CHARS.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

MAX_TOOL_RESULT_CHARS = 1500

_TRUNCATE_SUFFIX = "\n…[truncated]"


def _truncate(s: str) -> str:
    """Guarantee len(result) <= MAX_TOOL_RESULT_CHARS.

    A string at or under the cap is returned unchanged (boundary len == cap is
    NOT truncated). When over, the head is clipped so that head + suffix fits
    exactly within the cap.
    """
    if s is None:
        return ""
    if len(s) <= MAX_TOOL_RESULT_CHARS:
        return s
    head_len = MAX_TOOL_RESULT_CHARS - len(_TRUNCATE_SUFFIX)
    if head_len < 0:
        head_len = 0
    return s[:head_len] + _TRUNCATE_SUFFIX


def _safe(obj, attr, default):
    """getattr with a default; never raises on a missing field."""
    try:
        return getattr(obj, attr, default)
    except Exception:
        return default


@dataclass(frozen=True)
class ReadTool:
    name: str
    description: str
    input_schema: dict
    dispatch: Callable[[dict], Awaitable[str]]

    def anthropic_spec(self) -> dict:
        """Custom-tool spec for the Anthropic Messages API.

        NOTE: no "type" key — that field is only for hosted/server tools (e.g.
        web_search_20250305). Local custom tools omit it.
        """
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }


_NO_ARGS_SCHEMA = {"type": "object", "properties": {}}
_QUERY_SCHEMA = {
    "type": "object",
    "properties": {"query": {"type": "string", "description": "search query"}},
    "required": ["query"],
}
_PROTONMAIL_INBOX_SCHEMA = {
    "type": "object",
    "properties": {
        "from_address": {"type": "string", "description": "filter by sender address"},
        "subject": {"type": "string", "description": "filter by subject text"},
        "unread_only": {"type": "boolean", "description": "only unread messages"},
        "limit": {"type": "integer", "description": "max messages to return (default 10)"},
    },
}
_PROTONMAIL_READ_EMAIL_SCHEMA = {
    "type": "object",
    "properties": {
        "email_id": {"type": "string", "description": "email_id from protonmail_inbox"},
        "page": {"type": "integer", "description": "body page number for long emails (default 1)"},
    },
    "required": ["email_id"],
}


# ---------------------------------------------------------------------------
# Dispatchers — each is async (input: dict) -> str. On ANY error they return a
# compact "<name> unavailable: <e>" string (truncated); on success a compact
# summary (truncated). They NEVER raise.
# ---------------------------------------------------------------------------

async def _homeassistant_status(_input: dict) -> str:
    try:
        from backend.integrations import homeassistant
        data = await homeassistant.fetch()
        entities = _safe(data, "entities", [])
        alerts = _safe(data, "alerts", [])
        summary = f"Home Assistant: {len(entities)} entities, {len(alerts)} alert(s)"
        if alerts:
            summary += f" [{', '.join(str(a) for a in alerts[:3])}]"
        return _truncate(summary)
    except Exception as e:
        return _truncate(f"homeassistant_status unavailable: {e}")


async def _homeassistant_temperatures(_input: dict) -> str:
    """Every room/device temperature sensor HA currently reports -- kept as its
    own tool (not folded into homeassistant_status) so a query that doesn't
    need this doesn't pay for it, and so a new sensor (e.g. a newly-added air
    quality monitor) is queryable by name without any per-sensor wiring."""
    try:
        from backend.agents.chat import extract_temperature_sensors
        from backend.integrations import homeassistant
        data = await homeassistant.fetch()
        sensors = extract_temperature_sensors(data)
        if not sensors:
            return "No temperature sensors currently reporting."
        lines = [f"- {s['label']}: {s['value_f']:.0f}°F ({s['entity_id']})" for s in sensors]
        return _truncate("\n".join(lines))
    except Exception as e:
        return _truncate(f"homeassistant_temperatures unavailable: {e}")


async def _unraid_status(_input: dict) -> str:
    try:
        from backend.integrations import unraid
        data = await unraid.fetch()
        docker_count = len(_safe(data, "docker_containers", []))
        summary = (
            f"Unraid: array={_safe(data, 'array_status', 'unknown')}, "
            f"storage={_safe(data, 'storage_used_gb', 0)}/{_safe(data, 'storage_total_gb', 0)} GB, "
            f"docker={docker_count} containers, "
            f"cpu={_safe(data, 'cpu_pct', 0)}%, ram={_safe(data, 'ram_pct', 0)}%"
        )
        return _truncate(summary)
    except Exception as e:
        return _truncate(f"unraid_status unavailable: {e}")


async def _unifi_status(_input: dict) -> str:
    try:
        from backend.integrations import unifi
        data = await unifi.fetch()
        alerts = _safe(data, "alerts", [])
        summary = (
            f"UniFi: clients={_safe(data, 'client_count', 0)}, "
            f"uplink={_safe(data, 'uplink_status', 'unknown')}, "
            f"bandwidth={_safe(data, 'bandwidth_mbps', 0)} Mbps, "
            f"{len(alerts)} alert(s)"
        )
        if alerts:
            summary += f" [{', '.join(str(a) for a in alerts[:3])}]"
        return _truncate(summary)
    except Exception as e:
        return _truncate(f"unifi_status unavailable: {e}")


async def _adguard_status(_input: dict) -> str:
    try:
        from backend.integrations import adguard
        data = await adguard.fetch()
        summary = (
            f"AdGuard: {_safe(data, 'blocked_today', 0)} blocked today "
            f"({_safe(data, 'blocked_pct', 0)}%), "
            f"filtering={_safe(data, 'filtering_enabled', True)}"
        )
        return _truncate(summary)
    except Exception as e:
        return _truncate(f"adguard_status unavailable: {e}")


async def _channels_status(_input: dict) -> str:
    try:
        from backend.integrations import channels_dvr
        data = await channels_dvr.fetch()
        rec_now = _safe(data, "recording_now", [])
        rec_str = ", ".join(
            (r.get("title", "") if isinstance(r, dict) else str(r)) for r in rec_now
        ) if rec_now else "nothing"
        failed = _safe(data, "failed_recordings", [])
        if not isinstance(failed, list):
            failed = []
        if failed:
            titles = ", ".join(
                (f.get("title", "?") if isinstance(f, dict) else str(f)) for f in failed[:5]
            )
            failed_str = f", failed/skipped(24h)={len(failed)} [{titles}]"
        else:
            # ALWAYS emit the count, even when zero — otherwise a healthy "no
            # failures" run leaves the field absent, and the verifier can't confirm
            # the "zero failed recordings" criterion (reads a missing field as
            # "data unavailable" and rejects the goal). An explicit =0 satisfies it.
            failed_str = ", failed/skipped(24h)=0"
        summary = (
            f"Channels DVR: recording={rec_str}, "
            f"storage={_safe(data, 'storage_used_gb', 0)}/{_safe(data, 'storage_total_gb', 0)} GB"
            f"{failed_str}"
        )
        return _truncate(summary)
    except Exception as e:
        return _truncate(f"channels_status unavailable: {e}")


async def _weather(_input: dict) -> str:
    try:
        from backend.integrations import weather
        data = await weather.fetch()
        summary = f"Weather: {_safe(data, 'summary', 'unavailable')} ({_safe(data, 'temp_f', 0)}°F)"
        return _truncate(summary)
    except Exception as e:
        return _truncate(f"weather unavailable: {e}")


async def _github_status(_input: dict) -> str:
    try:
        from backend.integrations import github
        data = await github.fetch()
        open_prs = _safe(data, "open_prs", [])
        stale_prs = _safe(data, "stale_prs", [])
        titles = ", ".join(
            (p.get("title", "") if isinstance(p, dict) else str(p)) for p in open_prs[:3]
        )
        summary = f"GitHub: {len(open_prs)} open PR(s), {len(stale_prs)} stale"
        if titles:
            summary += f" [{titles}]"
        return _truncate(summary)
    except Exception as e:
        return _truncate(f"github_status unavailable: {e}")


async def _hermes_status(_input: dict) -> str:
    try:
        from backend.integrations import hermes
        status = await hermes.get_status()
        summary = (
            f"Hermes: alive={_safe(status, 'alive', False)}, "
            f"last_seen={_safe(status, 'last_seen', None)}, "
            f"pending_actions={_safe(status, 'pending_actions', 0)}"
        )
        return _truncate(summary)
    except Exception as e:
        return _truncate(f"hermes_status unavailable: {e}")


async def _proxmox_updates(_input: dict) -> str:
    try:
        from backend.integrations import hermes
        # Read-only: relays the "proxmox updates" intent to Hermes, which queries
        # the Proxmox apt/update API and returns a pending-update summary string.
        result = await hermes.relay("proxmox updates")
        return _truncate(result if isinstance(result, str) else str(result))
    except Exception as e:
        return _truncate(f"proxmox_updates unavailable: {e}")


async def _proxmox_backups(_input: dict) -> str:
    try:
        from backend.integrations import hermes
        # Read-only: relays "backup status" to Hermes, which queries the PVE
        # tasks API for the latest vzdump job outcome.
        result = await hermes.relay("backup status")
        return _truncate(result if isinstance(result, str) else str(result))
    except Exception as e:
        return _truncate(f"proxmox_backups unavailable: {e}")


async def _protonmail_inbox(input: dict) -> str:
    input = input or {}
    try:
        from backend.integrations import protonmail
        result = await protonmail.list_recent(
            unread_only=bool(input.get("unread_only", False)),
            from_address=input.get("from_address"),
            subject=input.get("subject"),
            limit=int(input.get("limit", 10)),
        )
        return _truncate(result if isinstance(result, str) else str(result))
    except Exception as e:
        return _truncate(f"protonmail_inbox unavailable: {e}")


async def _protonmail_read_email(input: dict) -> str:
    input = input or {}
    email_id = input.get("email_id")
    if not email_id or not str(email_id).strip():
        return "protonmail_read_email unavailable: missing 'email_id'"
    try:
        from backend.integrations import protonmail
        result = await protonmail.read_email(str(email_id).strip(), page=int(input.get("page", 1)))
        return _truncate(result if isinstance(result, str) else str(result))
    except Exception as e:
        return _truncate(f"protonmail_read_email unavailable: {e}")


async def _protonmail_status(_input: dict) -> str:
    try:
        from backend.integrations import protonmail
        from backend.config import get_settings
        alive = await protonmail.health_check()
        account = get_settings().protonmail_account
        return _truncate(f"Proton Mail: reachable={alive}, account={account}")
    except Exception as e:
        return _truncate(f"protonmail_status unavailable: {e}")


async def _vault_search(input: dict) -> str:
    query = (input or {}).get("query")
    if not query or not str(query).strip():
        return "vault_search unavailable: missing 'query'"
    try:
        from backend.integrations.obsidian import vault_search
        result = await vault_search(str(query).strip())
        return _truncate(result if isinstance(result, str) else str(result))
    except Exception as e:
        return _truncate(f"vault_search unavailable: {e}")


async def _web_search(input: dict) -> str:
    query = (input or {}).get("query")
    if not query or not str(query).strip():
        return "web_search unavailable: missing 'query'"
    try:
        from backend.integrations.web_search import search
        result = await search(str(query).strip())
        return _truncate(result if isinstance(result, str) else str(result))
    except Exception as e:
        return _truncate(f"web_search unavailable: {e}")


READ_TOOLS: list[ReadTool] = [
    ReadTool(
        name="homeassistant_status",
        description="Read a live Home Assistant snapshot: entity count and current alerts.",
        input_schema=_NO_ARGS_SCHEMA,
        dispatch=_homeassistant_status,
    ),
    ReadTool(
        name="homeassistant_temperatures",
        description="Read every room/device temperature sensor Home Assistant currently reports (°F), by label and entity_id.",
        input_schema=_NO_ARGS_SCHEMA,
        dispatch=_homeassistant_temperatures,
    ),
    ReadTool(
        name="unraid_status",
        description="Read live Unraid status: array state, storage used/total, docker count, CPU/RAM.",
        input_schema=_NO_ARGS_SCHEMA,
        dispatch=_unraid_status,
    ),
    ReadTool(
        name="unifi_status",
        description="Read live UniFi network status: client count, uplink status, bandwidth, alerts.",
        input_schema=_NO_ARGS_SCHEMA,
        dispatch=_unifi_status,
    ),
    ReadTool(
        name="adguard_status",
        description="Read live AdGuard Home status: queries blocked today, block percentage, filtering on/off.",
        input_schema=_NO_ARGS_SCHEMA,
        dispatch=_adguard_status,
    ),
    ReadTool(
        name="channels_status",
        description="Read live Channels DVR status: what is recording now and storage used/total.",
        input_schema=_NO_ARGS_SCHEMA,
        dispatch=_channels_status,
    ),
    ReadTool(
        name="weather",
        description="Read the current local weather summary and temperature.",
        input_schema=_NO_ARGS_SCHEMA,
        dispatch=_weather,
    ),
    ReadTool(
        name="github_status",
        description="Read GitHub status: open pull requests (count + first few titles) and stale PR count.",
        input_schema=_NO_ARGS_SCHEMA,
        dispatch=_github_status,
    ),
    ReadTool(
        name="hermes_status",
        description="Read the Hermes homelab bot status: alive, last seen, pending actions. READ ONLY — does not command Hermes.",
        input_schema=_NO_ARGS_SCHEMA,
        dispatch=_hermes_status,
    ),
    ReadTool(
        name="proxmox_updates",
        description="Read pending Proxmox (PVE) system updates: how many apt packages are upgradable on the node, via Hermes. READ ONLY — does not install anything.",
        input_schema=_NO_ARGS_SCHEMA,
        dispatch=_proxmox_updates,
    ),
    ReadTool(
        name="proxmox_backups",
        description="Read the status of the latest Proxmox vzdump VM backup job via Hermes. READ ONLY.",
        input_schema=_NO_ARGS_SCHEMA,
        dispatch=_proxmox_backups,
    ),
    ReadTool(
        name="protonmail_inbox",
        description="Read recent Proton Mail inbox messages (id, date, sender, subject), optionally filtered by sender/subject/unread. READ ONLY.",
        input_schema=_PROTONMAIL_INBOX_SCHEMA,
        dispatch=_protonmail_inbox,
    ),
    ReadTool(
        name="protonmail_read_email",
        description="Read the full body of one Proton Mail email by email_id (from protonmail_inbox). READ ONLY.",
        input_schema=_PROTONMAIL_READ_EMAIL_SCHEMA,
        dispatch=_protonmail_read_email,
    ),
    ReadTool(
        name="protonmail_status",
        description="Read Proton Mail MCP reachability status. READ ONLY.",
        input_schema=_NO_ARGS_SCHEMA,
        dispatch=_protonmail_status,
    ),
    ReadTool(
        name="vault_search",
        description="Search the user's personal Obsidian knowledge vault. Use for 'my notes', 'my vault', or saved personal knowledge.",
        input_schema=_QUERY_SCHEMA,
        dispatch=_vault_search,
    ),
    ReadTool(
        # Renamed from "web_search" to avoid colliding with Anthropic's HOSTED
        # web_search tool (router._WEB_SEARCH_TOOL) when both are sent in the same
        # tools= list. This local DuckDuckGo tool is a CLIENT-SIDE custom tool we
        # dispatch ourselves; the hosted one returns server results we never
        # dispatch. Distinct names let them coexist.
        name="ddg_search",
        description="Live web search (DuckDuckGo + GitHub releases). Use for current versions, news, prices, dates, or real-time facts.",
        input_schema=_QUERY_SCHEMA,
        dispatch=_web_search,
    ),
]


def tool_specs() -> list[dict]:
    """Anthropic custom-tool specs (no 'type' key) for every read tool."""
    return [t.anthropic_spec() for t in READ_TOOLS]


def dispatcher_map() -> dict[str, Callable[[dict], Awaitable[str]]]:
    """Map tool name -> async dispatcher."""
    return {t.name: t.dispatch for t in READ_TOOLS}


def planner_tool_block() -> str:
    """"- name: description" lines describing the tools to the planner."""
    return "\n".join(f"- {t.name}: {t.description}" for t in READ_TOOLS)
