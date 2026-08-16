"""Daily digest + end-of-trial verdict for the 2026-08 OpenRouter model-swap
trial (Trial A: Gemini Flash-Lite shadowing Haiku-tier classification calls,
see backend/agents/router.py's _maybe_shadow/_run_shadow_call; Trial B:
Gemini 2.5 Pro replacing Sonnet for the nightly brain_organizer wiki-synthesis
run, see modules/brain-organizer/trial/).

Delivery is a ProtonMail DRAFT, not a Telegram push: protonmail.save_draft is
documented as pure-IMAP, never touches SMTP, LOW-risk/reversible by
construction, and explicitly callable from a scheduled job with no broker
gating -- unlike protonmail.send_email, which is IRREVERSIBLE and hard-
forbidden for any actor but a real human tap. The report/verdict lands in
Drafts every morning; Brian reads it and sends it himself, or doesn't.

Gated by settings.trial_report_enabled -- False by default, so this whole
module is a no-op unless deliberately turned on for the trial window. Meant
to be deleted (this file + its scheduler registration) once the trial is
decided either way.
"""
import json
import logging
import re
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

_RECIPIENT = "claudeai@tbfamily.us"

_SHADOW_LOG = Path("/var/lib/nexus/logs/shadow.jsonl")
_TRIAL_B_NIGHTS_DIR = Path("/var/lib/nexus/brain-trial/nights")
_TRIAL_B_NIGHTS_TOTAL = 5
_VERDICT_DIR = Path("/var/lib/nexus/logs")
_NIGHT_DIRNAME_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _night_dirs() -> list[Path]:
    """Real per-night dirs under _TRIAL_B_NIGHTS_DIR, filtered to actual
    YYYY-MM-DD names -- a stray non-date dir would otherwise raise inside
    date.fromisoformat(d.name) wherever a caller parses the name."""
    if not _TRIAL_B_NIGHTS_DIR.exists():
        return []
    return sorted(
        d for d in _TRIAL_B_NIGHTS_DIR.iterdir()
        if d.is_dir() and _NIGHT_DIRNAME_RE.fullmatch(d.name)
    )

# label -> (real_out, shadow_out) -> bool "the shadow model's disagreement is
# the harmful direction" (would let something through / trash something the
# real model wouldn't have). Duplicated from tools/shadow_diff.py on purpose
# -- tools/ scripts never import backend/, and vice versa isn't done in this
# codebase either, so this is the one place either side is allowed to drift
# from a shared helper. Keep both in sync by hand if the rule ever changes.
_HARMFUL_DIRECTION = {
    "mail_junk_classify": lambda a, b: a.strip().upper() == "KEEP" and b.strip().upper() == "JUNK",
    "mail_reply_classify": lambda a, b: a.strip().upper() == "NO" and b.strip().upper() == "YES",
    "action_judge": lambda a, b: "veto" in a.lower() and "veto" not in b.lower(),
}
_JSON_LABELS = {"facts_extract", "goal_proposer"}


def _parseable(text: str) -> bool:
    try:
        json.loads(text)
        return True
    except Exception:
        return False


def _now() -> datetime:
    from backend.config import get_settings
    try:
        tz = ZoneInfo(get_settings().briefing_timezone)
    except Exception:
        tz = None
    return datetime.now(tz)


async def _run_section(name: str, coro) -> str:
    """Same isolation discipline as homelab_digest.py's _run_section -- one
    broken section must never take the other trial's section down with it."""
    try:
        return await coro
    except Exception as exc:
        logger.warning(f"trial_report section '{name}' error (degraded): {exc}")
        return "error: unavailable"


# --- reading logic (sync, always via asyncio.to_thread) ---------------------

def _read_shadow_rows(day: date) -> list[dict]:
    if not _SHADOW_LOG.exists():
        return []
    rows = []
    day_str = day.isoformat()
    with _SHADOW_LOG.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if str(row.get("ts", "")).startswith(day_str):
                rows.append(row)
    return rows


def _shadow_first_ts() -> str | None:
    if not _SHADOW_LOG.exists():
        return None
    with _SHADOW_LOG.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                return json.loads(line).get("ts")
            except json.JSONDecodeError:
                continue
    return None


def _read_trial_b_night(night: date) -> dict:
    night_dir = _TRIAL_B_NIGHTS_DIR / night.isoformat()
    if not night_dir.exists():
        return {"exists": False}

    result: dict = {"exists": True}

    # run-trial.sh's own $run_rc is the authoritative signal -- a grep for
    # "Traceback" in the log tail always found run-trial.sh's own trailing
    # "exited N" line first (that line always lands last), never the actual
    # traceback, so status must come from the rc file, not a log substring.
    rc_path = night_dir / "rc"
    log_path = night_dir / "organizer.log"
    tail = log_path.read_text(encoding="utf-8", errors="replace")[-2000:] if log_path.exists() else ""

    if rc_path.exists():
        try:
            rc = int(rc_path.read_text(encoding="utf-8").strip())
        except ValueError:
            rc = None
        if rc == 0:
            result["status"] = "OK"
        else:
            result["status"] = "FAILED"
            traceback_lines = [ln for ln in tail.splitlines() if "Error" in ln or ln.startswith("Traceback")]
            result["detail"] = traceback_lines[-1][:300] if traceback_lines else f"exited {rc}"
    elif tail:
        result["status"] = "unknown (still running or truncated log)"
    else:
        result["status"] = "no log"

    diff_path = night_dir / "diff.txt"
    if diff_path.exists():
        added = removed = files_changed = 0
        for line in diff_path.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("+++") or line.startswith("Only in"):
                files_changed += 1
            elif line.startswith("+") and not line.startswith("+++"):
                added += 1
            elif line.startswith("-") and not line.startswith("---"):
                removed += 1
        result["diff"] = {"files_changed": files_changed, "added": added, "removed": removed}

    census_path = night_dir / "census.md"
    if census_path.exists():
        import re
        text = census_path.read_text(encoding="utf-8", errors="replace")
        m = re.search(r"\*\*Overall status:\*\*\s*(PASS|FAIL)", text)
        result["census_status"] = m.group(1) if m else None
        result["census_text"] = text[:2000]

    return result


def _trial_a_day_range(settings) -> tuple[int, int]:
    """(day_n, total_days) -- day_n from the first logged shadow row, total
    from shadow_until. Both derived, nothing new to keep in sync."""
    first_ts = _shadow_first_ts()
    until = getattr(settings, "shadow_until", "") or ""
    if not first_ts or not until:
        return (0, 0)
    try:
        start = datetime.fromisoformat(first_ts).date()
        end = date.fromisoformat(until)
        total = max(1, (end - start).days + 1)
        day_n = min(total, (date.today() - start).days + 1)
        return (max(1, day_n), total)
    except Exception:
        return (0, 0)


# --- section builders ---------------------------------------------------

async def _section_trial_a() -> str:
    import asyncio
    from backend.config import get_settings
    settings = get_settings()

    day_n, total = await asyncio.to_thread(_trial_a_day_range, settings)
    yesterday = date.today().fromordinal(date.today().toordinal() - 1)
    all_rows = await asyncio.to_thread(_read_shadow_rows, yesterday)

    header = f"Trial A · Flash-Lite shadow · day {day_n} of {total}" if total else "Trial A · Flash-Lite shadow"

    # JSON-emitting labels (facts_extract, goal_proposer) compared by text
    # equality would read as a near-0% "agreement" rate on free-form
    # generation -- meaningless. Judge them by JSON-parseability instead,
    # same split tools/shadow_diff.py already makes.
    json_rows = [r for r in all_rows if r.get("label") in _JSON_LABELS]
    rows = [r for r in all_rows if r.get("label") not in _JSON_LABELS]

    if not rows and not json_rows:
        return f"{header}\n  No shadow calls logged for {yesterday.isoformat()} yet."

    lines = [header]

    if rows:
        n = len(rows)
        disagreements = [r for r in rows if not r.get("agree", True)]
        harmful = []
        for r in disagreements:
            check = _HARMFUL_DIRECTION.get(r.get("label", ""))
            if check and check(r.get("out_a", ""), r.get("out_b", "")):
                harmful.append(r)
        pct = len(disagreements) / n * 100 if n else 0.0
        lines.append(
            f"  Yesterday: {n} calls, {len(disagreements)} disagreed ({pct:.1f}%), "
            f"{len(harmful)} in the harmful direction."
        )
        if harmful:
            worst = harmful[0]
            lines.append(
                f"  Worst: {worst.get('label')} — real said {worst.get('out_a', '')[:40]!r}, "
                f"shadow said {worst.get('out_b', '')[:40]!r}."
            )
    if json_rows:
        ok = sum(1 for r in json_rows if _parseable(r.get("out_b", "")))
        lines.append(f"  JSON labels: {ok}/{len(json_rows)} shadow outputs parseable.")

    from backend.safety import governor
    report = await asyncio.to_thread(governor.spend_report, 30)
    shadow_cost = sum(e["cost_usd"] for e in report.get("by_label", []) if e["label"].startswith("shadow:"))
    days_running = max(1, day_n)
    projected = shadow_cost / days_running * 30
    lines.append(f"  Cost: ${shadow_cost:.3f} so far → ~${projected:.2f}/month if kept on.")
    return "\n".join(lines)


async def _section_trial_b() -> str:
    import asyncio
    yesterday = date.today().fromordinal(date.today().toordinal() - 1)
    info = await asyncio.to_thread(_read_trial_b_night, yesterday)

    nights_done = len(await asyncio.to_thread(_night_dirs))
    header = f"Trial B · Gemini 2.5 Pro brain organizer · night {nights_done} of {_TRIAL_B_NIGHTS_TOTAL}"

    if not info.get("exists"):
        return f"{header}\n  No trial run recorded for {yesterday.isoformat()} yet."

    lines = [header]
    status = info.get("status", "unknown")
    if status == "FAILED":
        lines.append(f"  Last night: FAILED — {info.get('detail', 'see organizer.log')}")
        lines.append("  No diff or census for this night.")
        return "\n".join(lines)

    lines.append(f"  Last night: {status}.")
    if "diff" in info:
        d = info["diff"]
        lines.append(f"  Wiki diff: {d['files_changed']} files, +{d['added']} / -{d['removed']} lines.")
    if "census_status" in info:
        lines.append(f"  Census: {info.get('census_status') or 'unreadable'}.")
    lines.append("  Cost: check OpenRouter credits — not metered in SpendLog.")
    return "\n".join(lines)


async def build_trial_report_text() -> str:
    a = await _run_section("trial_a", _section_trial_a())
    b = await _run_section("trial_b", _section_trial_b())
    now = _now()
    return f"NEXUS trial report — {now:%Y-%m-%d}\n\n{a}\n\n{b}"


# --- top-level entry point + end-of-trial verdict trigger --------------------

def _trial_a_over(settings) -> bool:
    until = getattr(settings, "shadow_until", "") or ""
    if not until:
        return False
    try:
        return date.today() > date.fromisoformat(until)
    except Exception:
        return False


def _trial_b_over() -> bool:
    return len(_night_dirs()) >= _TRIAL_B_NIGHTS_TOTAL


async def run_trial_report() -> dict:
    """Top-level entry point called by the scheduler once a day. NEVER raises."""
    try:
        from backend.config import get_settings
        settings = get_settings()
        if not getattr(settings, "trial_report_enabled", False):
            return {"skipped": True}

        text = await build_trial_report_text()

        from backend.integrations import protonmail
        now = _now()
        try:
            await protonmail.save_draft(
                recipients=[_RECIPIENT],
                subject=f"NEXUS Trial Report — {now:%Y-%m-%d}",
                body=text,
            )
            delivered = True
        except Exception as e:
            logger.warning(f"trial report draft failed (non-fatal): {e}")
            delivered = False

        for trial, over in (("A", _trial_a_over(settings)), ("B", _trial_b_over())):
            marker = _VERDICT_DIR / f"verdict-{trial}.md"
            if over and not marker.exists():
                await send_trial_verdict(trial)

        return {"delivered": delivered}
    except Exception as exc:
        logger.error(f"run_trial_report error (ignored): {exc}")
        return {"delivered": False}


# --- end-of-trial Opus verdict ------------------------------------------

VERDICT_PROMPT = """You are reviewing a completed model-substitution trial run inside NEXUS,
a personal AI OS. You are being asked for one decision, not an essay.

TRIAL: {trial_name}
REAL MEASURED COST: {cost_line}

GO/NO-GO CRITERIA, defined before the trial started (this is the bar — do not
invent new ones, do not soften these):
{criteria_block}

AGGREGATE RESULTS (computed from the full dataset, not a sample):
{aggregate_block}

SAMPLE OF THE ACTUAL DIFFERING CONTENT (bounded sample, not the full set —
{sample_note}):
{sample_block}

Answer in this exact shape, under 250 words total:

VERDICT: GO or NO-GO or EXTEND
WHY: two sentences, citing the specific criterion that decided it.
WHAT WOULD CHANGE YOUR MIND: one sentence.
IF GO, THE ONE THING TO WATCH: one sentence.
"""

_CRITERIA_A = """1. mail_junk_classify + mail_reply_classify overall disagreement <= 2%.
2. Zero cases in the harmful direction (shadow model would trash something the real
   model would have kept) that a human would judge as "the real model was right".
3. facts_extract/goal_proposer produce parseable JSON of the expected shape >= 99% of calls."""

_CRITERIA_B = """1. Zero truncated/failed nights across the 5 nights.
2. wikilink_census broken-link count not worse than the real run's.
3. Preferred or indifferent on >= 4 of 5 nights (Brian's own judgment, recorded manually).
4. Measured OpenRouter spend extrapolates to <= $18.84/month (the current real Sonnet cost)."""


def _build_verdict_payload_a() -> tuple[str, str, str] | None:
    """Returns (aggregate_block, sample_block, cost_line) or None if there's
    nothing to judge yet."""
    if not _SHADOW_LOG.exists():
        return None

    from collections import defaultdict
    by_label: dict[str, list[dict]] = defaultdict(list)
    with _SHADOW_LOG.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            by_label[row.get("label", "")].append(row)

    if not by_label:
        return None

    agg_lines = []
    sample_rows: list[tuple[str, dict, bool]] = []
    harmful_rows: list[tuple[str, dict, bool]] = []

    for label, rows in sorted(by_label.items()):
        n = len(rows)
        if label in _JSON_LABELS:
            ok = sum(1 for r in rows if _parseable(r.get("out_b", "")))
            agg_lines.append(f"{label}: {n} calls, {ok}/{n} shadow outputs parseable JSON")
            continue
        disagreements = [r for r in rows if not r.get("agree", True)]
        harmful = [
            r for r in disagreements
            if (_HARMFUL_DIRECTION.get(label) or (lambda a, b: False))(r.get("out_a", ""), r.get("out_b", ""))
        ]
        pct = len(disagreements) / n * 100 if n else 0.0
        agg_lines.append(f"{label}: {n} calls, {len(disagreements)} disagreed ({pct:.1f}%), {len(harmful)} harmful")

        quota = 4
        for r in disagreements[:quota]:
            is_harmful = r in harmful
            sample_rows.append((label, r, is_harmful))
        for r in harmful:
            if not any(r is existing for _, existing, _ in sample_rows):
                harmful_rows.append((label, r, True))

    sample_rows = (harmful_rows + sample_rows)[:20]

    sample_lines = []
    for label, r, is_harmful in sample_rows:
        tag = "HARMFUL" if is_harmful else "disagreement"
        sample_lines.append(
            f"[{tag}] {label}\n  prompt: {r.get('prompt', '')[:600]!r}\n"
            f"  real: {r.get('out_a', '')[:200]!r}\n  shadow: {r.get('out_b', '')[:200]!r}"
        )

    from backend.safety import governor
    report = governor.spend_report(30)
    shadow_cost = sum(e["cost_usd"] for e in report.get("by_label", []) if e["label"].startswith("shadow:"))
    cost_line = f"${shadow_cost:.3f} in shadow spend (real Haiku spend unaffected/unchanged)"

    return ("\n".join(agg_lines), "\n\n".join(sample_lines), cost_line)


def _build_verdict_payload_b() -> tuple[str, str, str] | None:
    night_dirs = _night_dirs()
    if not night_dirs:
        return None

    agg_lines = []
    sample_lines = []
    for d in night_dirs:
        info = _read_trial_b_night(date.fromisoformat(d.name))
        status = info.get("status", "unknown")
        diff = info.get("diff", {})
        agg_lines.append(
            f"{d.name}: {status}, "
            f"{diff.get('files_changed', '?')} files changed, "
            f"+{diff.get('added', '?')}/-{diff.get('removed', '?')} lines, "
            f"census={info.get('census_status', '?')}"
        )
        diff_path = d / "diff.txt"
        if diff_path.exists() and status == "OK":
            text = diff_path.read_text(encoding="utf-8", errors="replace")
            hunk = text[:1500]
            sample_lines.append(f"{d.name} (first hunk):\n{hunk}")

    cost_line = "see OpenRouter GET /api/v1/credits delta for the trial window (not metered in SpendLog)"
    return ("\n".join(agg_lines), "\n\n".join(sample_lines[:2]), cost_line)


async def send_trial_verdict(trial: str) -> dict:
    """The one Opus call per trial. Writes the verdict to a marker file FIRST
    (archive + idempotency marker), then drafts it. A budget-exceeded call
    defers cleanly and retries the next day (marker not written on failure)."""
    try:
        marker = _VERDICT_DIR / f"verdict-{trial}.md"
        if marker.exists():
            return {"delivered": False, "reason": "already sent"}

        import asyncio
        builder = _build_verdict_payload_a if trial == "A" else _build_verdict_payload_b
        payload = await asyncio.to_thread(builder)
        if payload is None:
            logger.info(f"trial {trial} verdict skipped: no data yet")
            return {"delivered": False, "reason": "no data"}

        aggregate_block, sample_block, cost_line = payload
        prompt = VERDICT_PROMPT.format(
            trial_name=f"Trial {trial}",
            cost_line=cost_line,
            criteria_block=_CRITERIA_A if trial == "A" else _CRITERIA_B,
            aggregate_block=aggregate_block,
            sample_note="stratified, harmful-direction rows always included" if trial == "A"
                        else "first hunk of the largest changed file per night",
            sample_block=sample_block or "(no differing content to sample)",
        )

        from backend.agents import router
        from backend.safety.governor import BudgetExceeded

        try:
            verdict_text = await router.opus(prompt, label="trial_verdict")
        except BudgetExceeded as e:
            from backend import events
            await events.notify_phone(
                f"Trial {trial} verdict deferred — daily budget hit "
                f"(${e.spend:.2f}/${e.cap:.2f}). Retrying tomorrow.",
                kind="trial_verdict",
            )
            return {"delivered": False, "deferred": True}

        _VERDICT_DIR.mkdir(parents=True, exist_ok=True)
        marker.write_text(
            f"# Trial {trial} verdict — {_now():%Y-%m-%d}\n\n{verdict_text}\n", encoding="utf-8",
        )

        from backend.integrations import protonmail
        try:
            await protonmail.save_draft(
                recipients=[_RECIPIENT],
                subject=f"NEXUS Trial {trial} — FINAL VERDICT",
                body=verdict_text,
            )
            delivered = True
        except Exception as e:
            logger.warning(f"trial {trial} verdict draft failed (non-fatal, verdict archived at {marker}): {e}")
            delivered = False

        return {"delivered": delivered}
    except Exception as e:  # never let a verdict-layer bug touch anything else
        logger.error(f"send_trial_verdict({trial!r}) failed (non-fatal): {e}")
        return {"delivered": False}
