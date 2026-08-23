import asyncio
import json
import logging
import re
import time
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

# Sections sourced from single-source, unverified third-party content (a real
# email subject line) rather than a cross-checked NEXUS integration --
# extract_and_store would otherwise store an unread subject as a 0.9+
# confidence "durable fact", and the goal proposer's KNOWN FACTS context
# treats durable facts as grounds for an autonomous investigation + phone
# notification (the original incident: "Dropbox Storage Limit Hit" from one
# unread email got echoed into Priority Actions and then fact-extracted as
# fact). Strip these sections before fact extraction; they stay in the
# stored/displayed briefing. "Inbox" is a Gmail-era LLM-narrated heading
# retired when mail moved to Proton (see _build_protonmail_section) -- kept
# here defensively in case it's ever reintroduced or appears in an old stored
# briefing. "Proton Mail" is appended AFTER the LLM call (never reaches the
# prompt at all), but its raw subject lines still must not reach fact
# extraction, same reasoning. "Open Items" (rollout step 6,
# docs/outcome-tracker-spec.md §4.2) is likewise appended AFTER the LLM
# call, from raw OutcomeFlag summaries -- without this entry,
# extract_and_store would turn e.g. "garage door has been open for over 30
# minutes" into a durable 0.9-confidence Fact, which the goal proposer then
# treats as grounds for an autonomous investigation, the exact incident
# class this list exists to contain. "Claude Usage" is likewise appended
# AFTER the LLM call, from the raw statusline capture file (see
# backend/integrations/claude_usage.py) -- a percentage/reset-time pair has
# nothing for the LLM to synthesize and no business becoming a durable Fact.
# "OpenRouter" (2026-08-05) is the same shape of section for the same reason
# -- a credit/usage number, appended from backend.integrations.openrouter's
# already-cross-checked fetch(), never narrated or fact-extracted.
_UNVERIFIED_FACT_SECTIONS = (
    "Priority Actions", "Inbox", "Proton Mail", "Open Items", "Claude Usage", "OpenRouter",
)


def _strip_unverified_sections(text: str) -> str:
    for heading in _UNVERIFIED_FACT_SECTIONS:
        text = re.sub(rf"## {re.escape(heading)}.*?(?=\n## |\Z)", "", text, flags=re.DOTALL)
    return text


def _truncate_subject(subject: str, cap: int = 80) -> str:
    subject = subject or "(no subject)"
    return subject if len(subject) <= cap else subject[: cap - 1] + "…"


def _parse_email_list(result) -> list | None:
    """result is either an Exception (integration failure) or the JSON text
    from protonmail.list_recent(). Returns the parsed emails list, [] if the
    shape is unexpected, or None if the source itself was unavailable."""
    if isinstance(result, Exception):
        return None
    try:
        data = json.loads(result)
        emails = data.get("emails")
        if not isinstance(emails, list):
            return []
        return [e for e in emails if isinstance(e, dict)]
    except Exception:
        return None


def _needing_reply(unread_result, drafted_ids: set, cap: int = 5) -> list | None:
    unread_emails = _parse_email_list(unread_result)
    if unread_emails is None:
        return None
    return [e for e in unread_emails if e.get("email_id") in drafted_ids][:cap]


def _build_protonmail_section(unread_result, drafts_result, drafted_ids: set) -> str:
    """Deterministic, factual '## Proton Mail' section, assembled in Python and
    appended AFTER the LLM call — never fed into the prompt. Mail data here is
    already a finished judgment (the auto-draft scheduler's conservative
    classifier already decided which unread emails need a reply), so there is
    nothing for the LLM to synthesize; feeding it in would only risk the same
    manufactured-urgency problem _UNVERIFIED_FACT_SECTIONS exists to contain.
    Never raises for any input shape (Exception, malformed JSON, missing keys).
    """
    unread_emails = _parse_email_list(unread_result)
    draft_emails = _parse_email_list(drafts_result)

    if unread_emails is None and draft_emails is None:
        return "## Proton Mail\nProton Mail data unavailable."

    needing_reply = [e for e in (unread_emails or []) if e.get("email_id") in drafted_ids][:5]
    drafts = (draft_emails or [])[:10]

    if not needing_reply and not drafts and unread_emails is not None and draft_emails is not None:
        return "## Proton Mail\nNothing needing attention."

    lines = ["## Proton Mail"]

    if unread_emails is None:
        lines.append("Unread-mail data unavailable.")
    elif needing_reply:
        lines.append(
            f"{len(needing_reply)} unread email(s) previously judged (conservative classifier) to need a personal reply:"
        )
        for e in needing_reply:
            sender = e.get("sender") or "(unknown sender)"
            lines.append(f'- {sender} — "{_truncate_subject(e.get("subject"))}"')

    if draft_emails is None:
        lines.append("Drafts-folder data unavailable.")
    elif drafts:
        lines.append(f"{len(drafts)} draft(s) awaiting your review in Proton Drafts:")
        for e in drafts:
            subject = _truncate_subject(e.get("subject"))
            recipients = e.get("recipients")
            if recipients:
                to = ", ".join(recipients) if isinstance(recipients, list) else recipients
                lines.append(f'- "{subject}" → {to}')
            else:
                lines.append(f'- "{subject}"')

    if len(lines) == 1:
        lines.append("Nothing needing attention.")

    return "\n".join(lines)


def _coerce_flag_list(result) -> list:
    """Defensive coercion for the two outcome-tracker gather results
    (rollout step 6, open_flags()/recently_closed()). asyncio.gather's
    return_exceptions=True already turns a raised exception into the
    element itself, but several existing briefing tests patch
    sqlmodel.Session/backend.database.engine with bare MagicMocks -- a
    non-list leaking through (e.g. a MagicMock) must degrade to [] here too,
    never reach string formatting below."""
    if isinstance(result, Exception) or not isinstance(result, list):
        return []
    return result


def _format_flag_summary_line(flag) -> str:
    """One-line '- [severity] source:check — summary' rendering for the
    KNOWN OPEN ITEMS / RECENTLY CLOSED prompt blocks. Never raises --
    defensive .get() reads only; a non-dict element renders as ''."""
    if not isinstance(flag, dict):
        return ""
    severity = flag.get("severity") or "medium"
    source = flag.get("source") or "?"
    check = flag.get("check") or "?"
    summary = flag.get("summary") or ""
    return f"- [{severity}] {source}:{check} — {summary}"


def _build_flag_prompt_block(flags: list, cap: int) -> str:
    """Renders a capped list of flag dicts for BRIEFING_PROMPT's KNOWN OPEN
    ITEMS / RECENTLY CLOSED sections (rollout step 6, spec §4.1). Degrades
    to '(none)' on an empty or all-invalid input."""
    lines = [ln for ln in (_format_flag_summary_line(f) for f in flags[:cap]) if ln]
    return "\n".join(lines) if lines else "(none)"


def _format_suppression_sentence(hint_report, cap: int) -> str:
    """Spec §3.6 addendum: 'Currently auto-suppressed: <fp> (NN% false
    alarm, until <date>)[, ...].' appended after the existing calibration
    line, sourced from calibration.hint_report(30)'s "suppressed" group
    (status=="active" CalibrationHint rows). `hint_report` is an
    Exception/non-dict on a gather failure or a zeroed report on any
    internal error (hint_report itself never raises) -- both degrade to
    '', same discipline as _format_calibration_line. Zero active hints ->
    '', no trailing punctuation artifacts. Capped at `cap` entries, same
    bound as the base line, for the same reason."""
    if not isinstance(hint_report, dict):
        return ""
    suppressed = hint_report.get("suppressed")
    if not isinstance(suppressed, list) or not suppressed:
        return ""
    try:
        parts = []
        for hint in suppressed[:cap]:
            if not isinstance(hint, dict):
                continue
            fp = hint.get("fingerprint")
            if not fp:
                continue
            pct = round(hint.get("fp_rate", 0.0) * 100)
            retest_at = hint.get("retest_at")
            until = retest_at.split("T")[0] if isinstance(retest_at, str) and retest_at else "unknown"
            parts.append(f"{fp} ({pct}% false alarm, until {until})")
    except Exception:
        return ""
    if not parts:
        return ""
    return f" Currently auto-suppressed: {', '.join(parts)}."


def _format_calibration_line(calibration, cap: int, hint_report=None) -> str:
    """One-line 'Flag calibration (30d): source:check — N raised, M
    false_positive[, ...].' advisory line for BRIEFING_PROMPT (spec §4.4,
    the briefing's half of calibration_summary()'s two v1 consumers --
    the other is digest.build_autonomy_digest, whose exact formatting
    convention this mirrors). `calibration` is outcomes.calibration_summary's
    {"source:check": {status: count}} shape, or an Exception/anything else
    on a gather failure -- both degrade to '(none)', never raise. Capped at
    `cap` (outcome_flag_briefing_max, same constant the KNOWN OPEN ITEMS/
    RECENTLY CLOSED blocks already use) source:check pairs to bound prompt
    growth; each pair itself is one bounded summary line, not a list, so no
    further per-line truncation is needed.

    `hint_report` (spec §3.6 addendum, optional -- calibration.hint_report(30)'s
    result) appends a 'Currently auto-suppressed: ...' sentence after the
    existing prefix/fallback when active hints exist; never modifies the
    prefix or '(none)' fallback text itself, and appends nothing on an empty
    or errored hint_report."""
    if not isinstance(calibration, dict) or not calibration:
        return "Flag calibration (30d): (none)" + _format_suppression_sentence(hint_report, cap)
    try:
        parts = ", ".join(
            f"{key} — {sum(counts.values())} raised, {counts.get('false_positive', 0)} false_positive"
            for key, counts in list(calibration.items())[:cap]
            if isinstance(counts, dict)
        )
    except Exception:
        return "Flag calibration (30d): (none)" + _format_suppression_sentence(hint_report, cap)
    base = f"Flag calibration (30d): {parts}." if parts else "Flag calibration (30d): (none)"
    return base + _format_suppression_sentence(hint_report, cap)


def _format_flag_age(iso_str) -> str:
    """Best-effort 'Ns/Nm/Nh/Nd ago' rendering of an ISO timestamp -- mirrors
    telegram_commands._format_age's convention (kept as a local copy rather
    than a cross-module import of that module's private helper). Never
    raises -- a malformed/missing timestamp degrades to '?'."""
    if not iso_str:
        return "?"
    try:
        then = datetime.fromisoformat(iso_str)
    except (ValueError, TypeError):
        return "?"
    seconds = (datetime.utcnow() - then).total_seconds()
    if seconds < 60:
        return "just now"
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes}m ago"
    hours = int(minutes // 60)
    if hours < 24:
        return f"{hours}h ago"
    days = int(hours // 24)
    return f"{days}d ago"


def _build_open_items_section(flags: list) -> str:
    """Deterministic, post-LLM '## Open Items' section (rollout step 6, spec
    §4.2) -- assembled in Python from a fresh outcomes.open_flags() read (or
    the pre-LLM list on a fresh-read failure), never fed back through the
    LLM, so it can't hallucinate a resolved item back into existence. Added
    to _UNVERIFIED_FACT_SECTIONS above so extract_and_store never durably
    facts-extracts a flag summary. Never raises -- non-dict elements are
    dropped defensively."""
    lines = ["## Open Items"]
    for f in flags:
        if not isinstance(f, dict):
            continue
        severity = f.get("severity") or "medium"
        source = f.get("source") or "?"
        check = f.get("check") or "?"
        summary = f.get("summary") or ""
        age = _format_flag_age(f.get("created_at"))
        lines.append(f"- #{f.get('id')} [{severity}] {source}:{check} — {summary} ({age})")
    if len(lines) == 1:
        lines.append("No open items.")
    return "\n".join(lines)


def _format_epoch_countdown(epoch) -> str:
    """Best-effort, SELF-CONTAINED 'resets in Nh Nm' / 'resets any moment' /
    'reset time unknown' clause for a unix-seconds timestamp -- returns the
    full clause (not a bare duration) so no call site needs to prefix
    "resets in " itself; that used to produce the nonsensical "resets in due
    now" once a capture's 5-hour window had already elapsed, the normal
    overnight state for this feature. Never raises. Includes a day tier
    (unlike a bare hour count) since the 7-day window can be over 100 hours
    out."""
    # time.time() (not datetime.utcnow().timestamp(), which misinterprets a
    # naive UTC datetime as local time and silently shifts the result by the
    # host's UTC offset) -- caught by this module's own test, not guessed.
    try:
        seconds = float(epoch) - time.time()
    except (TypeError, ValueError):
        return "reset time unknown"
    if seconds <= 0:
        return "resets any moment"
    minutes = int(seconds // 60)
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    if days:
        return f"resets in {days}d {hours}h"
    if hours:
        return f"resets in {hours}h {minutes}m"
    return f"resets in {minutes}m"


def _format_epoch_age(epoch) -> str:
    """Best-effort 'Ns/Nm/Nh/Nd ago' rendering of a unix-seconds timestamp --
    same convention as _format_flag_age, but for the epoch-seconds shape
    claude_usage.py's captured_at uses instead of an ISO string. Never
    raises -- a malformed/missing value degrades to '?'."""
    try:
        seconds = time.time() - float(epoch)
    except (TypeError, ValueError):
        return "?"
    if seconds < 0:
        seconds = 0
    if seconds < 60:
        return "just now"
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes}m ago"
    hours = int(minutes // 60)
    if hours < 24:
        return f"{hours}h ago"
    days = int(hours // 24)
    return f"{days}d ago"


def _build_claude_usage_section(result) -> str:
    """Deterministic, factual '## Claude Usage' section, assembled in Python
    and appended AFTER the LLM call -- never fed into the prompt (see
    _UNVERIFIED_FACT_SECTIONS above). `result` is either an Exception (gather
    failure) or the ClaudeUsageData dataclass from
    backend.integrations.claude_usage.fetch(). Never raises for any input
    shape -- a missing/malformed capture reads as an honest 'no data' line,
    never a blank/wrong number, matching that module's own stale-not-wrong
    contract."""
    from backend.integrations.claude_usage import ClaudeUsageData

    if isinstance(result, Exception) or not isinstance(result, ClaudeUsageData):
        return "## Claude Usage\nClaude Code usage data unavailable."
    if not result.available:
        return "## Claude Usage\nNo Claude Code session captured yet."

    def _window(label: str, w) -> str:
        # getattr, not attribute access -- w is documented as
        # ClaudeUsageWindow | None, but this function's own docstring
        # promises "never raises for any input shape," so a malformed w
        # (e.g. a dict, from a future shape change) must degrade to "no
        # data," not an AttributeError that kills the whole briefing.
        used_percentage = getattr(w, "used_percentage", None)
        if w is None or used_percentage is None:
            return f"- {label}: no data"
        resets_at = getattr(w, "resets_at", None)
        reset = f", {_format_epoch_countdown(resets_at)}" if resets_at is not None else ""
        return f"- {label}: {used_percentage:.0f}% used{reset}"

    lines = ["## Claude Usage", _window("5-hour", result.five_hour), _window("7-day", result.seven_day)]
    if result.captured_at is not None:
        lines.append(f"- captured {_format_epoch_age(result.captured_at)}")
    return "\n".join(lines)


def _build_openrouter_section(result) -> str:
    """Deterministic, factual '## OpenRouter' section, assembled in Python
    and appended AFTER the LLM call -- same reasoning as
    _build_claude_usage_section above (see _UNVERIFIED_FACT_SECTIONS). `result`
    is either an Exception (gather failure) or the OpenRouterData dataclass
    from backend.integrations.openrouter.fetch(). Never raises."""
    from backend.integrations.openrouter import OpenRouterData

    if isinstance(result, Exception) or not isinstance(result, OpenRouterData):
        return "## OpenRouter\nOpenRouter usage data unavailable."
    if not result.available:
        return "## OpenRouter\nOpenRouter usage data unavailable."

    lines = ["## OpenRouter"]

    # Real account balance (GET /api/v1/credits) leads the section -- this is
    # "the balance" in the everyday sense, distinct from and can be much
    # larger than the per-key cap below (live-verified: a key's
    # limit_remaining read $0/exhausted while the account itself held
    # $12.65 of real balance).
    if isinstance(result.account_balance, (int, float)) and isinstance(result.account_total_credits, (int, float)):
        lines.append(f"- Balance: ${result.account_balance:.2f} of ${result.account_total_credits:.2f} purchased")
    else:
        lines.append("- Balance: unknown")

    # Per-key spending cap, secondary -- omitted entirely for an unlimited
    # key. Cumulative usage isn't separately reported anywhere in that case
    # (only usage_daily, via the "Today" line below); the real account
    # balance line above already conveys what matters.
    if result.credit_limit is not None:
        if isinstance(result.credit_remaining, (int, float)):
            # credit_limit set but credit_remaining absent/null is a real,
            # reachable shape (an API response where the two fields aren't
            # correlated) -- must degrade to "unknown," never crash the
            # whole briefing on a NoneType format spec.
            lines.append(f"- Key limit: ${result.credit_remaining:.2f} remaining of ${result.credit_limit:.2f}")
        else:
            lines.append(f"- Key limit: unknown remaining of ${result.credit_limit:.2f}")

    if result.usage_daily:
        lines.append(f"- Today: ${result.usage_daily:.2f} used")
    lines.append(f"- {result.model_count} models available")
    return "\n".join(lines)


# Unraid parity_status values that mean "no parity concern" (a check finished
# clean or none is running) -- distinct from "unknown" (a read problem, not a
# breach, same reasoning as check_unraid_array's own status handling). Real
# values observed are disk_ok/unknown; existing test fixtures use "idle".
# Kept as its own allowlist constant (not folded into a single "not unknown"
# check) so a genuinely-bad status (e.g. "failed"/a running check) is never
# silently swallowed by loosening this set later.
_UNRAID_PARITY_HEALTHY_STATUSES = {"idle", "disk_ok"}


def _build_docker_status_line(docker_containers: list, expected_rows: list) -> str:
    """Deterministic 'N of M expected' Docker status line for the briefing's
    System Health section -- replaces raw container-count narration (the
    briefing repeatedly said "Only 3 Docker containers running -- verify
    this is intentional" with no baseline to judge that against, an
    unresolvable nag open in Brian's vault since 2026-06-08) with a computed
    comparison against the declared ExpectedResource baseline
    (backend/agents/expected_resources.py). Falls back to a bare count when
    no baseline has been declared yet (expected_resources.seed_from_live()
    hasn't run), so the line is never blank."""
    docker_expected = [r for r in expected_rows if r.get("kind") == "docker"]
    if not docker_expected:
        return f"{len(docker_containers)} Docker container(s) running (no expected-state baseline declared)."

    # Numerator and denominator must range over the SAME set -- counting every
    # live running container against only the declared-running ones produced
    # "9 of 3 expected Docker containers running" the moment anything
    # undeclared was up.
    live_running = {
        c.get("name") for c in docker_containers
        if (c.get("state") or "").upper() == "RUNNING"
    }
    expected_running_names = {
        r.get("identifier") for r in docker_expected if r.get("desired_state") == "running"
    }
    expected_running = len(expected_running_names)
    running = len(expected_running_names & live_running)
    if running == expected_running:
        return f"{running} of {expected_running} expected Docker containers running."
    return f"{running} of {expected_running} expected Docker containers running — see Open Items."


async def _record_briefing_flags(context: dict) -> None:
    """Deterministic, structured flag write path for the briefing (spec
    docs/outcome-tracker-spec.md §2.2-C, rollout step 5 of 7). Reads only
    from the already-built `context` dict -- never re-fetches, never touches
    the LLM prompt/response, matching _build_protonmail_section's "assembled
    in Python, never in the prompt" discipline. Records/clears exactly six
    flags; symmetric clear_flag calls let yesterday's flag (e.g. a stale PR)
    auto-resolve once the underlying condition clears.

    This function is itself the write path only (unchanged from rollout step
    5) -- the read path it feeds (BRIEFING_PROMPT's KNOWN OPEN ITEMS/
    RECENTLY CLOSED blocks, the '## Open Items' section, the
    _UNVERIFIED_FACT_SECTIONS entry, and the open_flags/recently_closed
    gather wiring) was added in rollout step 6; see run_briefing() and
    _build_open_items_section below.

    Best-effort: the call site in run_briefing() wraps this whole function in
    try/except so a DB hiccup can never fail or delay the briefing.
    outcomes.record_flag already guarantees it NEVER raises itself
    (backend/agents/outcomes.py); outcomes.clear_flag does not carry that
    same top-level guard, so it is individually wrapped below, mirroring
    backend/agents/homelab_watch.py's _clear_flag_safe helper.
    """
    from backend.agents import outcomes

    async def _clear(check: str) -> None:
        try:
            await outcomes.clear_flag("briefing", check)
        except Exception as e:
            logger.warning(f"_record_briefing_flags: clear_flag failed for {check!r} (ignored): {e}")

    ha_alerts = context["home_assistant"]["alerts"] or []
    if ha_alerts:
        summary = f"{len(ha_alerts)} Home Assistant alert(s): {'; '.join(str(a) for a in ha_alerts[:5])}"
        await outcomes.record_flag("briefing", "ha_unavailable_entities", summary[:300])
    else:
        await _clear("ha_unavailable_entities")

    array_status = context["unraid"]["array_status"]
    if array_status not in ("started", "unknown"):
        summary = f"Unraid array status is '{array_status}' (expected 'started')."
        await outcomes.record_flag("briefing", "unraid_array", summary[:300])
    else:
        await _clear("unraid_array")

    parity_status = context["unraid"]["parity_status"]
    if parity_status != "unknown" and parity_status not in _UNRAID_PARITY_HEALTHY_STATUSES:
        summary = f"Unraid parity status is '{parity_status}'."
        await outcomes.record_flag("briefing", "unraid_parity", summary[:300])
    else:
        await _clear("unraid_parity")

    stale_prs = context["github"]["stale_prs"] or []
    if stale_prs:
        summary = f"{len(stale_prs)} stale PR(s): {'; '.join(str(p) for p in stale_prs[:5])}"
        await outcomes.record_flag("briefing", "github_stale_prs", summary[:300])
    else:
        await _clear("github_stale_prs")

    new_devices = context["unifi"]["new_devices"] or []
    if new_devices:
        summary = f"{len(new_devices)} new UniFi device(s): {'; '.join(str(d) for d in new_devices[:5])}"
        await outcomes.record_flag("briefing", "unifi_new_devices", summary[:300])
    else:
        await _clear("unifi_new_devices")

    # AdGuard: flag only on a confirmed False reading, clear only on a
    # confirmed True reading. filtering_enabled is coerced to the string
    # "unknown" above (a real read failure -- see that block's comment) when
    # the raw value was None; both None and "unknown" must fall through here
    # untouched (neither flag nor clear) to preserve the 2026-07-26
    # unknown-vs-off fix -- an `is False`/`is True` check does this for free
    # since neither matches a bool.
    filtering_enabled = context["adguard"]["filtering_enabled"]
    if filtering_enabled is False:
        await outcomes.record_flag("briefing", "adguard_filtering_off", "AdGuard filtering is OFF.")
    elif filtering_enabled is True:
        await _clear("adguard_filtering_off")


# 7-day trend baselines (2026-08-21). TrendSnapshot stopped being written when
# the Trends *page* was removed (2026-07-07, Grafana/UptimeKuma cover charting
# externally) -- but BRIEFING_PROMPT's Network Security line still asserts
# "Flag any spike vs 7-day average" with nothing behind it (Brian's own vault
# notes show the LLM correctly, uselessly, saying "No 7-day average available
# for comparison" every day). scheduler.py's daily `record_trend_snapshot` job
# resumed writing rows for exactly the two metrics this file actually asks
# about (AdGuard blocked_pct, Unraid storage_used_gb); these helpers read them
# back. Deliberately NOT reviving the Trends page itself -- out of scope.
def _trend_baseline_avg(source: str, metric: str, min_samples: int = 5) -> float | None:
    """Mean TrendSnapshot.value for source/metric over the trailing 7 days, or
    None when fewer than min_samples rows exist yet (feature just shipped, or
    the daily snapshot job missed a day) -- never builds a "7-day average"
    out of partial history that could misreport a false spike/no-spike. Sync
    (Session) -- call via asyncio.to_thread like every other durable-DB read
    in this file.

    min_samples is 5, not 7: a daily writer over a 7-day window has zero
    margin at 7, so a single missed day (restart, integration outage) blanks
    the baseline entirely. 5 of 7 days still averages honestly.
    """
    from sqlmodel import Session, select
    from backend.database import TrendSnapshot, engine
    cutoff = datetime.utcnow() - timedelta(days=7)
    with Session(engine) as session:
        rows = session.exec(
            select(TrendSnapshot.value)
            .where(TrendSnapshot.source == source)
            .where(TrendSnapshot.metric == metric)
            .where(TrendSnapshot.captured_at >= cutoff)
        ).all()
    if len(rows) < min_samples:
        return None
    return sum(rows) / len(rows)


def _gather_trend_baselines() -> dict:
    """Best-effort 7-day averages for the two metrics BRIEFING_PROMPT/context
    reference. Never raises -- a DB hiccup degrades both to None (same
    "insufficient history" rendering as too few real rows), matching every
    other best-effort fetch in this file."""
    try:
        return {
            "adguard_blocked_pct": _trend_baseline_avg("adguard", "blocked_pct"),
            "unraid_storage_used_gb": _trend_baseline_avg("unraid", "storage_used_gb"),
        }
    except Exception as e:
        logger.warning(f"Trend baseline gather failed: {e}")
        return {"adguard_blocked_pct": None, "unraid_storage_used_gb": None}


BRIEFING_PROMPT = """You are Carl, a direct, high-conviction personal AI assistant, briefing a solo power user starting their day.
Be direct. No filler, no hedging ("try," "hope," "maybe"). Assume high technical literacy. Flag anomalies clearly.
Never say "as of my last update" or similar hedges — this is live data.

DATA SNAPSHOT as of {timestamp}:
{json_context}

KNOWN OPEN ITEMS (already raised with the user — reference them if still relevant,
but do NOT present them as new findings):
{open_items_block}

RECENTLY CLOSED (last 48h — the user already handled these; do NOT re-raise):
{closed_items_block}

{calibration_line}

Produce a morning brief with these exact sections:

## Priority Actions (max 3)
Items requiring action TODAY, ranked by urgency. If nothing urgent, say so.

## Weather
{weather_summary}
[Flag if rain > 50% or temperature extreme]

## System Health
One line per system: Unraid, UniFi, Home Assistant, AdGuard.
For Home Assistant, report unavailable_persistent_over_7d and unavailable_recent_under_1h
from the snapshot (e.g. "12 unavailable >7 days (persistently dead), 3 unavailable <1h
(likely transient)") -- NOT unavailable_total or entity_count as a bare number, that hides
which entities are actually worth acting on.
Docker: {docker_status_line} — state this exactly, verbatim, as its own line. It is
already a computed comparison against a known baseline, so do NOT add any caveat
about verifying whether the count is intentional.
Flag parity check if running. Flag mover if active. Flag new unknown devices on network.

## Network Security
Queries today: {blocked_today} blocked ({blocked_pct}%). {adguard_trend_line}
Filtering: {filtering_enabled}.

## GitHub Pulse
PRs/issues needing attention. Call out any stale PRs explicitly.

## Media
{recording_now}. Notable upcoming in next 24h.
DVR storage: {dvr_used}/{dvr_total} GB.

## Calendar
{calendar_block}

## From Your Vault
Relevant open tasks from Obsidian. Surface anything tagged #today or #urgent.

## Today's Focus
Single paragraph. What should this person focus on and why, given everything above."""


async def run_briefing() -> str:
    from sqlmodel import Session

    from backend.agents.router import (
        close_trace,
        open_trace,
        reset_trace_context,
        set_trace_context,
        sonnet,
    )
    from backend.database import Briefing, engine
    from backend.integrations import (
        adguard,
        channels_dvr,
        github,
        homeassistant,
        obsidian,
        unifi,
        unraid,
        weather,
    )
    from backend.integrations.calendar import get_today_events
    from backend.integrations import protonmail, claude_usage, openrouter
    from backend.agents import calibration, expected_resources, mail_drafts, outcomes

    logger.info("Running morning briefing")

    # Open an AgentTrace (council w-observability) so nested LLM calls made
    # during this run attach TraceSpan rows to it. Mirrors orchestrator's
    # run_task trace wiring, via the generic open_trace/close_trace helper
    # (briefing has no durable task_id -- kind='briefing', task_id=None).
    # trace_id is None on any open failure -- set_trace_context(None) is a
    # safe no-op downstream (router._record_trace_span short-circuits on
    # trace_id is None).
    trace_id = await asyncio.to_thread(open_trace, "briefing", "daily_briefing")
    _trace_token = set_trace_context(trace_id)
    _trace_status = "ok"
    _trace_error = None

    try:
        results = await asyncio.gather(
            homeassistant.fetch(),
            unifi.fetch(),
            unraid.fetch(),
            obsidian.fetch(),
            github.fetch(),
            weather.fetch(),
            channels_dvr.fetch(),
            adguard.fetch(),
            get_today_events(),
            protonmail.list_recent(unread_only=True, limit=25),
            protonmail.list_recent(mailbox="Drafts", limit=10),
            outcomes.open_flags(),
            outcomes.recently_closed(hours=48),
            outcomes.calibration_summary(30),
            calibration.hint_report(30),
            claude_usage.fetch(),
            openrouter.fetch(),
            expected_resources.list_expected(),
            homeassistant.unavailable_report(),
            return_exceptions=True,
        )

        (
            ha, unifi_d, unraid_d, obs, gh, wx, channels, ag, cal_data,
            proton_unread, proton_drafts, open_flags_result, closed_flags_result,
            calibration_result, hint_report_result, claude_usage_result,
            openrouter_result, expected_resources_result, ha_unavail_result,
        ) = results

        # ha_unavailable_report() already never raises on its own (degrades
        # internally to an all-zero dict) -- this guard only covers the
        # unlikely case of asyncio.gather itself handing back an Exception
        # object for this slot (return_exceptions=True).
        ha_unavail_report = ha_unavail_result if isinstance(ha_unavail_result, dict) else {
            "total": 0, "persistent": 0, "recent": 0, "items": [],
        }

        cal_str = cal_data if not isinstance(cal_data, Exception) else "Calendar unavailable"

        # Rollout step 6 read path (spec §4.1) -- both degrade to [] on any
        # fetch exception or unexpected shape (_coerce_flag_list), never
        # blocking the rest of the briefing.
        open_flags_list = _coerce_flag_list(open_flags_result)
        closed_flags_list = _coerce_flag_list(closed_flags_result)
        try:
            from backend.config import get_settings
            flag_cap = int(getattr(get_settings(), "outcome_flag_briefing_max", 10))
        except Exception:
            flag_cap = 10
        open_items_block = _build_flag_prompt_block(open_flags_list, flag_cap)
        closed_items_block = _build_flag_prompt_block(closed_flags_list, flag_cap)

        # Spec §4.4 -- deterministic (no LLM call, AC34) advisory prompt-INPUT
        # line, not a rendered output section (do NOT add to
        # _UNVERIFIED_FACT_SECTIONS): degrades to '(none)' on any gather
        # exception or unexpected shape, same discipline as open/closed above.
        calibration_dict = calibration_result if isinstance(calibration_result, dict) else {}
        hint_report_dict = hint_report_result if isinstance(hint_report_result, dict) else None
        calibration_line = _format_calibration_line(calibration_dict, flag_cap, hint_report_dict)

        try:
            drafted_ids = await asyncio.to_thread(mail_drafts._db_drafted_email_ids)
        except Exception:
            drafted_ids = set()

        trend_baselines = await asyncio.to_thread(_gather_trend_baselines)
        ag_avg = trend_baselines.get("adguard_blocked_pct")
        adguard_trend_line = (
            f"7-day avg: {ag_avg:.1f}%. Flag any spike vs that average."
            if ag_avg is not None
            else "No 7-day average available yet (insufficient history)."
        )

        def safe(obj, attr, default="N/A"):
            if isinstance(obj, Exception):
                return default
            return getattr(obj, attr, default)

        # AdGuard's filtering_enabled is None (not a default True/False) when its
        # own /control/status read failed — a real reading exists but couldn't be
        # taken, distinct from "AdGuard is unreachable" (ag itself would be an
        # Exception then, caught above by safe()). Render that as "unknown", not
        # a bare None, since a bare None reads as a bug in the prompt/JSON either
        # way and used to silently read as "filtering on" instead.
        ag_filtering = safe(ag, "filtering_enabled", None)
        ag_filtering = "unknown" if ag_filtering is None else ag_filtering

        expected_resources_list = expected_resources_result if isinstance(expected_resources_result, list) else []
        docker_status_line = _build_docker_status_line(
            safe(unraid_d, "docker_containers", []), expected_resources_list,
        )

        context = {
            "home_assistant": {
                "entity_count": len(safe(ha, "entities", [])),
                "alerts": safe(ha, "alerts", []),
                # Age-bucketed, not a flat count -- see
                # homeassistant.py::unavailable_report's docstring. Lets the
                # prompt distinguish "just restarted, self-clears in
                # minutes" from "been dead for months" instead of a single
                # number that never goes down and never gets acted on.
                "unavailable_total": ha_unavail_report.get("total", 0),
                "unavailable_persistent_over_7d": ha_unavail_report.get("persistent", 0),
                "unavailable_recent_under_1h": ha_unavail_report.get("recent", 0),
            },
            "unifi": {
                "clients": safe(unifi_d, "client_count", 0),
                "status": safe(unifi_d, "uplink_status", "unknown"),
                "new_devices": safe(unifi_d, "new_devices", []),
            },
            "unraid": {
                "array_status": safe(unraid_d, "array_status", "unknown"),
                "parity_status": safe(unraid_d, "parity_status", "unknown"),
                "mover_running": safe(unraid_d, "mover_running", False),
                "storage_used_gb": safe(unraid_d, "storage_used_gb", 0),
                "storage_used_gb_7day_avg": trend_baselines.get("unraid_storage_used_gb"),
                "storage_total_gb": safe(unraid_d, "storage_total_gb", 0),
                "docker_containers": len(safe(unraid_d, "docker_containers", [])),
                "docker_status": docker_status_line,
            },
            "github": {
                "open_prs": len(safe(gh, "open_prs", [])),
                "assigned_issues": len(safe(gh, "assigned_issues", [])),
                "stale_prs": safe(gh, "stale_prs", []),
            },
            "obsidian": {
                "open_tasks": safe(obs, "open_tasks", []),
            },
            "channels_dvr": {
                "recording_now": safe(channels, "recording_now", []),
                "upcoming": safe(channels, "upcoming", []),
                "storage_used_gb": safe(channels, "storage_used_gb", 0),
                "storage_total_gb": safe(channels, "storage_total_gb", 0),
            },
            "adguard": {
                "queries_today": safe(ag, "queries_today", 0),
                "blocked_today": safe(ag, "blocked_today", 0),
                "blocked_pct": safe(ag, "blocked_pct", 0),
                "blocked_pct_7day_avg": ag_avg,
                "filtering_enabled": ag_filtering,
            },
        }

        wx_data = wx if not isinstance(wx, Exception) else None
        weather_summary = wx_data.summary if wx_data else "Weather data unavailable"
        if wx_data:
            weather_summary = f"{wx_data.summary}. High {wx_data.high_f}°F / Low {wx_data.low_f}°F."

        rec_now = safe(channels, "recording_now", [])
        rec_str = ", ".join([r.get("title", "") for r in rec_now]) if rec_now else "Nothing recording"

        prompt = BRIEFING_PROMPT.format(
            timestamp=datetime.utcnow().isoformat(),
            json_context=json.dumps(context, indent=2),
            open_items_block=open_items_block,
            closed_items_block=closed_items_block,
            calibration_line=calibration_line,
            weather_summary=weather_summary,
            blocked_today=safe(ag, "blocked_today", 0),
            blocked_pct=safe(ag, "blocked_pct", 0),
            adguard_trend_line=adguard_trend_line,
            filtering_enabled=ag_filtering,
            recording_now=rec_str,
            dvr_used=safe(channels, "storage_used_gb", 0),
            dvr_total=safe(channels, "storage_total_gb", 0),
            calendar_block=cal_str,
            docker_status_line=docker_status_line,
        )

        # Added AFTER the prompt is built, on purpose — this must never reach the
        # LLM (see _build_protonmail_section's docstring). It only records what
        # was surfaced, for context_json's own record-keeping.
        proton_section = _build_protonmail_section(proton_unread, proton_drafts, drafted_ids)
        _needing = _needing_reply(proton_unread, drafted_ids)
        _drafts = _parse_email_list(proton_drafts)
        context["protonmail"] = {
            "unread_needing_reply": len(_needing) if _needing is not None else 0,
            "pending_drafts": len(_drafts) if _drafts is not None else 0,
        }

        briefing_text = await sonnet(prompt, label="briefing")
        briefing_text = briefing_text + "\n\n" + proton_section
        logger.info("Briefing generated")

        # Best-effort structured flag write path (spec
        # docs/outcome-tracker-spec.md §2.2-C, rollout step 5) -- write path
        # only, no prompt/read-path change this cycle. Wrapped so a DB
        # hiccup can never fail or delay the briefing; see
        # _record_briefing_flags' own docstring for its internal guards.
        try:
            await _record_briefing_flags(context)
        except Exception as e:
            logger.warning(f"_record_briefing_flags failed (ignored): {e}")

        # Deterministic '## Open Items' section (rollout step 6, spec §4.2) --
        # built from a FRESH read, taken after _record_briefing_flags() above,
        # so today's newly-recorded flags appear. Falls back to the pre-LLM
        # list on a fresh-read failure rather than silently omitting the
        # section. Never fed back through the LLM.
        try:
            fresh_open_flags = await outcomes.open_flags(limit=flag_cap)
            if not isinstance(fresh_open_flags, list):
                fresh_open_flags = open_flags_list
        except Exception as e:
            logger.warning(f"open_items section: fresh read failed, using pre-LLM list: {e}")
            fresh_open_flags = open_flags_list
        briefing_text = briefing_text + "\n\n" + _build_open_items_section(fresh_open_flags)

        # Deterministic '## Claude Usage' section, same "assembled in Python,
        # appended after the LLM call" discipline as Proton Mail/Open Items
        # above -- see _build_claude_usage_section's own docstring.
        briefing_text = briefing_text + "\n\n" + _build_claude_usage_section(claude_usage_result)

        # Deterministic '## OpenRouter' section, same discipline as Claude
        # Usage above -- see _build_openrouter_section's own docstring.
        briefing_text = briefing_text + "\n\n" + _build_openrouter_section(openrouter_result)

        # Fact extraction from briefing content was removed 2026-08-23: briefing
        # facts are unverified and BRIEFING_CONFIDENCE_CAP (0.15) sits below
        # EFFECTIVE_FLOOR (0.2), so they could never be displayed or recalled --
        # a daily Haiku call for rows that were structurally unreachable. See
        # facts.py's effective_confidence docstring. _strip_unverified_sections
        # is retained below for its own tests and as the guard if extraction is
        # ever reinstated (which would need a redesign of the cap, not a toggle).

        # Store in DB
        with Session(engine) as session:
            b = Briefing(content=briefing_text, context_json=json.dumps(context))
            session.add(b)
            session.commit()
            session.refresh(b)
            briefing_id = b.id

        # Write to Obsidian
        try:
            today = datetime.now().strftime("%Y-%m-%d")
            await obsidian.create_note(
                title=today,
                content=f"# Morning Briefing — {today}\n\n{briefing_text}",
                folder="NEXUS/Briefings",
            )
            obsidian_path = f"NEXUS/Briefings/{today}.md"
            with Session(engine) as session:
                b = session.get(Briefing, briefing_id)
                if b:
                    b.obsidian_path = obsidian_path
                    session.commit()
        except Exception as e:
            logger.warning(f"Obsidian write failed: {e}")

        # Deliver via Telegram
        try:
            from backend.integrations.telegram import notify
            delivered = await notify({"type": "briefing", "content": briefing_text, "timestamp": datetime.utcnow().isoformat()})
            if delivered:
                with Session(engine) as session:
                    b = session.get(Briefing, briefing_id)
                    if b:
                        b.delivered = True
                        session.commit()
        except Exception as e:
            logger.warning(f"Telegram delivery failed: {e}")

        # Expected-delivery heartbeat (backend/agents/deliveries.py) — lets
        # watchdog.check_expected_deliveries() page if the morning briefing
        # ever goes silent. In-process call, no HTTP hop needed (unlike
        # brain_organizer.py's subprocess, this runs inside the NEXUS
        # process). Best-effort: never let a heartbeat failure mask the
        # briefing's own success.
        try:
            from backend.agents import deliveries
            await deliveries.record_heartbeat("morning_briefing")
        except Exception as e:
            logger.warning(f"Expected-delivery heartbeat failed: {e}")

        return briefing_text
    except Exception as exc:
        _trace_status = "error"
        _trace_error = str(exc)
        raise
    finally:
        # Best-effort trace close, wrapped so a bookkeeping failure here can
        # never mask the real return value / exception from run_briefing.
        try:
            await asyncio.to_thread(close_trace, trace_id, _trace_status, _trace_error)
        except Exception as e:
            logger.warning(f"trace close failed (non-fatal): {e}")
        reset_trace_context(_trace_token)
