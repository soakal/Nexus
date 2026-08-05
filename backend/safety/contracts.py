"""Integration response-shape contracts — the "still 200 OK, but quietly wrong"
detector.

An integration's fetch() can succeed (no exception, cache holds a value) while
still returning a payload that's silently blank or malformed — e.g. an auth
token that expired returns an empty list instead of raising, and the briefing
renders that empty list as "0 open PRs" fact. The 2-minute uptime job and each
integration's own health_check() only answer "is it reachable" — neither
catches this failure mode. backend/agents/watchdog.py::check_integration_contracts
reads the registry below on a schedule and pages when a real consumer's field
would be lying.

Everything in this module is PURE: no I/O, no await, no DB. Same contract as
backend/safety/hermes_actions.py ("PURE: no I/O, no await, no DB").

`consumer` on every FieldContract is REQUIRED and non-blank by convention
(enforced by tests/test_contract_canary.py::test_every_contract_names_a_consumer)
— it makes it structurally impossible to add an assertion without naming the
real code it protects, and it's what makes the alert message point at the
code that will render the lie, not just the field that's wrong.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class FieldContract:
    field: str
    kinds: tuple[type, ...]
    rule: str  # "type" | "not_default" | "positive" | "nonempty" | "is_default"
    default: object = None  # sentinel value, for rule in {"not_default", "is_default"}
    consumer: str = ""  # file:line of the real consumer this protects — REQUIRED
    note: str = ""

    def __post_init__(self):
        if not self.consumer:
            raise ValueError(f"FieldContract for '{self.field}' must name a consumer")


def check_object(obj, contracts: tuple[FieldContract, ...]) -> list[str]:
    """Pure. Returns a list of human-readable breach strings; [] means the
    contract holds. NEVER raises: a missing attribute is a breach (a string),
    not an AttributeError."""
    breaches = []
    for c in contracts:
        try:
            missing = object()
            value = getattr(obj, c.field, missing)
            if value is missing:
                breaches.append(f"{c.field} is missing (consumer {c.consumer})")
                continue

            if c.kinds and not isinstance(value, c.kinds):
                breaches.append(
                    f"{c.field} is {type(value).__name__}, expected "
                    f"{'/'.join(k.__name__ for k in c.kinds)} (consumer {c.consumer})"
                )
                continue

            if c.rule == "type":
                pass  # the isinstance check above already covers it
            elif c.rule == "not_default":
                if value == c.default:
                    breaches.append(f"{c.field} is still the default {c.default!r} (consumer {c.consumer})")
            elif c.rule == "is_default":
                if value != c.default:
                    breaches.append(
                        f"{c.field} is {value!r}, expected it to still be the "
                        f"unwired default {c.default!r} (consumer {c.consumer})"
                    )
            elif c.rule == "positive":
                if not (value > 0):
                    breaches.append(f"{c.field} is {value!r}, expected > 0 (consumer {c.consumer})")
            elif c.rule == "nonempty":
                if len(value) == 0:
                    breaches.append(f"{c.field} is empty (consumer {c.consumer})")
        except Exception as e:
            breaches.append(f"{c.field} raised while checking ({e}) (consumer {c.consumer})")
    return breaches


CONTRACTS: dict[str, tuple[FieldContract, ...]] = {
    "homeassistant": (
        FieldContract("entities", (list,), "nonempty",
                       consumer="briefing.py:232, chat.py:545, homeassistant.py:153"),
        FieldContract("alerts", (list,), "type", consumer="briefing.py:233"),
        FieldContract("cloud_alerts", (list,), "type", consumer="api/homeassistant.py:36"),
    ),
    "unraid": (
        FieldContract("array_status", (str,), "not_default", default="unknown",
                       consumer="briefing.py:241, Dashboard.jsx:357"),
        FieldContract("parity_status", (str,), "not_default", default="unknown",
                       consumer="briefing.py:242"),
        FieldContract("storage_total_gb", (int, float), "positive", consumer="briefing.py:245"),
        FieldContract("docker_containers", (list,), "type",
                       consumer="briefing.py:246, Dashboard.jsx:369"),
        FieldContract("disk_health", (list,), "type", consumer="homelab_watch.py:check_disk_temps"),
        # cpu_pct/ram_pct (2026-07-29): fetch() now wires these from live GraphQL
        # `metrics.cpu.percentTotal`/`metrics.memory.percentTotal` (verified via
        # __type introspection + a live query against the real server) — no
        # longer dead defaults, so the "assert it's STILL dead" is_default
        # contract is gone. They diverge from each other, though: a genuinely
        # idle CPU can legitimately round to 0.0 (`round(0.04, 1) == 0.0`), so
        # cpu_pct only gets "type" (real number, nothing stronger claimable).
        # ram_pct never legitimately reads 0.0 on a running server — same
        # never-zero discipline as storage_total_gb above and mem_total_gb
        # elsewhere in this file — so it keeps real breach-detection value via
        # "positive": a metrics section silently going null again (same class
        # of regression that left parity_status stuck at its default for
        # months before the canary caught it) would round to 0.0 and get
        # missed under "type" alone.
        FieldContract("cpu_pct", (int, float), "type", consumer="tools.py:142"),
        FieldContract("ram_pct", (int, float), "positive", consumer="tools.py:142"),
        # mover_running's real reader is briefing.py, NOT tools.py (tools.py's
        # unraid_status tool never reads it) — different from cpu_pct/ram_pct,
        # which WERE dead-field reads until 2026-07-29 above. Kept as is_default: the
        # integration's own fetch() still never assigns it (unraid.py), so
        # briefing.py:243's read of it is itself always the dataclass default
        # until someone wires the assignment up.
        FieldContract("mover_running", (bool,), "is_default", default=False,
                       consumer="briefing.py:243 (fetch() never assigns this field)"),
    ),
    "unifi": (
        FieldContract("client_count", (int,), "positive", consumer="briefing.py:236"),
        FieldContract("uplink_status", (str,), "not_default", default="unknown",
                       consumer="briefing.py:237"),
        # bandwidth_mbps/alerts (2026-07-29): fetch() now wires both from the
        # already-fetched stat/health response's `wan` subsystem (tx_bytes-r +
        # rx_bytes-r -> Mbps, verified live moving between polls like a real
        # gauge) and a live GET .../list/alarm call (verified 200 + real
        # {"data": [...]} shape against the real Dream Machine Pro Max) — no
        # longer dead defaults. bandwidth_mbps legitimately can be a low
        # number on a quiet network, so "type" (not "not_default") is correct
        # — same reasoning as unraid.cpu_pct above (ram_pct is the one
        # exception that keeps a stronger rule, see above). alerts staying an
        # empty list is a legitimate healthy state (no active alarms), so
        # "type" (not "nonempty") is correct there too.
        FieldContract("bandwidth_mbps", (int, float), "type", consumer="tools.py:157"),
        FieldContract("alerts", (list,), "type", consumer="tools.py:153"),
    ),
    "proxmox": (
        FieldContract("node", (str,), "nonempty", consumer="proxmox.py:114-116"),
        FieldContract("node_status", (str,), "not_default", default="unknown",
                       consumer="Dashboard.jsx:417"),
        FieldContract("vms", (list,), "nonempty", consumer="Dashboard.jsx:430, HomeAssistant.jsx:18"),
        FieldContract("mem_total_gb", (int, float), "positive",
                       consumer="proxmox.py:59 (no downstream consumer yet; same never-zero discipline as unraid/channels_dvr)"),
    ),
    "channels_dvr": (
        FieldContract("storage_total_gb", (int, float), "positive",
                       consumer="briefing.py:260, Media.jsx:70"),
        FieldContract("recording_now", (list,), "type", consumer="briefing.py:257, tools.py:190"),
        FieldContract("upcoming", (list,), "type", consumer="briefing.py:258"),
        FieldContract("failed_recordings", (list,), "type", consumer="tools.py:194"),
        # library_shows/library_movies deliberately excluded: unlike unraid's
        # cpu_pct/ram_pct (assigned nowhere, read by tools.py — a real "dead
        # value gets read" bug), these two are never assigned by fetch() at all
        # (channels_dvr.py never sets them) AND have zero downstream readers
        # anywhere in the repo. No consumer exists to protect, so no contract
        # entry — matches the "consumer is required and must be real" rule
        # this module enforces on every other field.
    ),
    "adguard": (
        FieldContract("queries_today", (int,), "positive", consumer="briefing.py:263"),
        FieldContract("blocked_today", (int,), "type", consumer="briefing.py:264"),
        FieldContract("filtering_enabled", (bool, type(None)), "type",
                       consumer="briefing.py:227,266,284 (None means status read failed)"),
    ),
    "weather": (
        FieldContract("condition", (str,), "not_default", default="Unknown",
                       consumer="Dashboard.jsx:164"),
        FieldContract("summary", (str,), "not_default", default="Weather unavailable",
                       consumer="briefing.py:271,273"),
        FieldContract("temp_f", (int, float), "type", consumer="Dashboard.jsx:161"),
        FieldContract("high_f", (int, float), "type", consumer="briefing.py:273"),
        FieldContract("low_f", (int, float), "type", consumer="briefing.py:273"),
    ),
    "github": (
        FieldContract("open_prs", (list,), "type", consumer="briefing.py:249"),
        FieldContract("assigned_issues", (list,), "type", consumer="briefing.py:250"),
        FieldContract("stale_prs", (list,), "type", consumer="briefing.py:251"),
        # recent_commits deliberately excluded: fetch()-assigned real data,
        # zero downstream readers anywhere in the repo today.
    ),
    "obsidian": (
        FieldContract("open_tasks", (list,), "type", consumer="briefing.py:254"),
        FieldContract("recent_notes", (list,), "nonempty",
                       consumer="obsidian.py:159 (vault is never empty; catches a bad obsidian_vault_path)"),
    ),
    "calendar": (
        # feeds_ok/feeds_total deliberately NOT asserted here: fetch() already
        # raises RuntimeError when feeds_ok==0 (a total-failure "positive" rule
        # would be tautologically true on every object this canary ever sees,
        # since a 0 can only occur on a path that never reaches check_object).
        # A partial failure (feeds_ok < feeds_total, one feed silently dead)
        # would be the real 200-but-quietly-wrong case, but that needs a
        # cross-field comparison this framework's single-field rules don't
        # express — left as a known gap rather than a fake assertion.
        #
        # `summary`, not `events`: briefing.py/today.py never touch .events
        # directly — both consume the string-returning get_today_events()
        # wrapper (which reads .summary), so that's the field a shape change
        # would actually break.
        FieldContract("summary", (str,), "type", consumer="briefing.py:201,288 (via get_today_events/calendar_block), api/today.py:14,18"),
    ),
    "openrouter": (
        # model_count/credit_remaining/credit_limit gained real consumers
        # 2026-08-05 (Dashboard.jsx's OpenRouter card, briefing.py's narrated
        # section) -- previously excluded below as consumerless.
        # credit_limit/credit_remaining stay nullable (OpenRouter's own
        # "no cap" convention for an unlimited key) so both allow NoneType,
        # same pattern as adguard's filtering_enabled above.
        FieldContract("model_count", (int,), "positive", consumer="Dashboard.jsx:OpenRouter card"),
        FieldContract("credit_limit", (int, float, type(None)), "type",
                       consumer="Dashboard.jsx:OpenRouter card (unguarded .toFixed(2) once non-None), briefing.py:_build_openrouter_section (None means unlimited key)"),
        FieldContract("credit_remaining", (int, float, type(None)), "type",
                       consumer="Dashboard.jsx:OpenRouter card, briefing.py:_build_openrouter_section (None means unlimited key OR credit_limit/credit_remaining uncorrelated in the API response)"),
        FieldContract("usage", (int, float), "type", consumer="Dashboard.jsx:OpenRouter card, briefing.py:_build_openrouter_section"),
    ),
}

EXCLUDED: dict[str, str] = {
    "speedtest": "no fetch(); run_speedtest() downloads 25MB + uploads 5MB per call — a canary must not trigger that on a schedule",
    "protonmail": "no fetch(); reads are parameterised (list_recent), and inbox_summary() never raises — it returns the error as a string, so it's unassertable",
    "hermes": "no fetch() — it's now a pure action-relay bridge + liveness probe (get_status/health_check), not a data integration with a shape to protect",
}


def covered() -> set[str]:
    return set(CONTRACTS) | set(EXCLUDED)
