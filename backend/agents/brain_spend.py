"""Ingest Brain Organizer token-usage into the NEXUS spend governor.

The Brain Organizer runs as a SEPARATE subprocess (its own venv) and cannot write
to nexus.db safely, so it appends token-usage as JSON lines to
`modules/brain-organizer/logs/usage.jsonl`. This module is the NEXUS-side ingestor:
a scheduler job atomically claims that file and turns each line into a `SpendLog`
row (label="brain_organizer"), so the organizer's LLM spend counts against the
same daily budget/report as every in-process agent call.

Everything here is SYNCHRONOUS — the scheduler invokes it via asyncio.to_thread so
no Session/ORM crosses an await (Windows event-loop safety). The whole job is
best-effort: it NEVER raises (a metering hiccup must not break the scheduler).
"""

import json
import logging
import os
from datetime import datetime

logger = logging.getLogger(__name__)

# Reuse the SAME module-dir path the API router already resolves, so producer and
# consumer agree on the file location regardless of cwd.
from backend.api.brain_organizer import _MODULE_DIR

_USAGE_FILE = _MODULE_DIR / "logs" / "usage.jsonl"
_CLAIM_FILE = _MODULE_DIR / "logs" / "usage.jsonl.ingest"


def _price_model(model: str, input_tokens: int, output_tokens: int) -> float:
    """USD cost for a usage line using router._PRICE_PER_MTOK.

    Unknown model -> 0.0 (tokens are still recorded on the row). OpenRouter logs a
    model like "anthropic/claude-sonnet-4-6"; strip the provider prefix so those
    rows price against the same table instead of falling through to 0.0.
    """
    from backend.agents.router import _PRICE_PER_MTOK

    price = _PRICE_PER_MTOK.get(model)
    if price is None and "/" in model:
        price = _PRICE_PER_MTOK.get(model.split("/", 1)[1])
    if price is None:
        return 0.0
    return (
        input_tokens / 1e6 * price["input"]
        + output_tokens / 1e6 * price["output"]
    )


def _parse_ts(raw) -> datetime:
    """Parse a producer 'ts' ISO string to a NAIVE UTC datetime (to match
    SpendLog.created_at, which is naive utcnow). Fallback to utcnow on anything odd."""
    try:
        dt = datetime.fromisoformat(str(raw))
        if dt.tzinfo is not None:
            from datetime import timezone
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt
    except Exception:
        return datetime.utcnow()


def _ingest_claim_file() -> int:
    """Parse the claimed .ingest file into SpendLog rows, commit, then delete it.

    Returns the number of rows written. Raises only on a genuine DB/parse-loop
    failure — the public entrypoint wraps this so nothing escapes to the scheduler.
    The .ingest file is deleted ONLY after a successful commit (so a crash mid-way
    leaves it for the next cycle's crash-recovery pass to retry).
    """
    if not _CLAIM_FILE.exists():
        return 0

    from sqlmodel import Session
    from backend.database import SpendLog, engine

    rows: list[SpendLog] = []
    try:
        text = _CLAIM_FILE.read_text(encoding="utf-8")
    except Exception as e:
        logger.warning(f"brain_spend: could not read {_CLAIM_FILE.name}: {e}")
        return 0

    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
            model = str(rec.get("model") or "unknown")
            input_tokens = int(rec.get("input_tokens") or 0)
            output_tokens = int(rec.get("output_tokens") or 0)
        except Exception:
            logger.warning(f"brain_spend: skipping malformed usage line: {line!r}")
            continue
        cost = _price_model(model, input_tokens, output_tokens)
        rows.append(SpendLog(
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost,
            label="brain_organizer",
            task_id=None,
            created_at=_parse_ts(rec.get("ts")),
        ))

    if rows:
        with Session(engine) as session:
            for row in rows:
                session.add(row)
            session.commit()

    # Delete only after the commit succeeded.
    try:
        _CLAIM_FILE.unlink()
    except FileNotFoundError:
        pass

    return len(rows)


def ingest_brain_spend() -> int:
    """Claim usage.jsonl and record its lines as SpendLog rows (sync, best-effort).

    Flow:
      1. ON START, ingest any leftover .ingest file first (crash recovery — a
         previous cycle claimed but died before committing/deleting it).
      2. os.replace(usage.jsonl -> usage.jsonl.ingest) as an ATOMIC claim so the
         producer keeps appending to a fresh usage.jsonl while we consume the
         snapshot. FileNotFoundError -> nothing to do. PermissionError (Windows
         lock race) -> skip this cycle, retry next.
      3. Ingest the freshly-claimed file.

    Returns the total rows written. NEVER raises.
    """
    total = 0
    try:
        # 1. Crash recovery: a leftover claim from a prior interrupted cycle.
        total += _ingest_claim_file()

        # 2. Atomic claim of the live producer file.
        try:
            os.replace(_USAGE_FILE, _CLAIM_FILE)
        except FileNotFoundError:
            return total  # no producer output this cycle
        except PermissionError:
            logger.debug("brain_spend: usage.jsonl locked; skipping this cycle")
            return total

        # 3. Ingest the newly-claimed snapshot.
        total += _ingest_claim_file()
        if total:
            logger.info(f"brain_spend: ingested {total} usage row(s)")
    except Exception as e:
        logger.error(f"brain_spend: ingest failed (best-effort): {e}")
    return total
