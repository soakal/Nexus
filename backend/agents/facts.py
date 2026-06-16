"""Tier 2.3c — durable Entity/Fact store with confidence-decay + supersede.

Facts are extracted from chat messages by Haiku, stored in the `fact` table,
and the most relevant active facts are injected into the CHAT memory block.

Confidence decays exponentially with age (half-life HALF_LIFE_DAYS). When a
new value is observed for an existing (subject, predicate) pair the old row is
SUPERSEDED (superseded_by set to the new row id) so history is preserved.

All DB helpers are SYNC and must only be called via asyncio.to_thread — no
Session/ORM must ever cross an await. All public async functions are best-effort
and NEVER raise.
"""

import asyncio
import json
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module constants
# ---------------------------------------------------------------------------

HALF_LIFE_DAYS: float = 30.0       # confidence halves every 30 days
EFFECTIVE_FLOOR: float = 0.2       # facts below this effective confidence are hidden
RECALL_LIMIT: int = 8              # max facts injected into the memory block
MAX_EXTRACT: int = 5               # max facts extracted per message
CONFIRM_BUMP: float = 0.1          # confidence bump on reinforcement


# ---------------------------------------------------------------------------
# Pure helpers (no I/O)
# ---------------------------------------------------------------------------

def effective_confidence(confidence: float, age_days: float) -> float:
    """Exponential decay: conf * 0.5^(age_days / HALF_LIFE_DAYS).

    age_days is clamped to >= 0 so future-dated rows don't gain confidence.
    """
    return confidence * (0.5 ** (max(0.0, age_days) / HALF_LIFE_DAYS))


def _age_days(created_at: datetime, now: datetime) -> float:
    """Return age in fractional days, clamped to >= 0."""
    return max(0.0, (now - created_at).total_seconds() / 86400.0)


# ---------------------------------------------------------------------------
# SYNC DB helpers — call only via asyncio.to_thread
# ---------------------------------------------------------------------------

def _db_active_facts() -> list[dict]:
    """Return all active (superseded_by IS NULL) facts as plain dicts.

    Opens its own Session; never raises (returns [] on any error).
    """
    try:
        from sqlmodel import Session, select
        from backend.database import Fact, engine

        with Session(engine) as session:
            stmt = select(Fact).where(Fact.superseded_by == None)  # noqa: E711
            rows = session.exec(stmt).all()
            return [
                {
                    "id": r.id,
                    "subject": r.subject,
                    "predicate": r.predicate,
                    "value": r.value,
                    "confidence": r.confidence,
                    "created_at": r.created_at.isoformat(),
                    "source": r.source,
                }
                for r in rows
            ]
    except Exception as exc:
        logger.debug(f"_db_active_facts: error (ignored): {exc}")
        return []


def _db_upsert_fact(
    subject: str,
    predicate: str,
    value: str,
    confidence: float,
    source: str,
    conversation_id: int | None,
) -> None:
    """SUPERSEDE / REINFORCE / INSERT a fact.

    Matching is case-insensitive on both subject and predicate.

    - REINFORCE: existing active fact with same (subject, predicate, value)
      → bump confidence (capped at 1.0), refresh last_seen_at + updated_at.
    - SUPERSEDE: existing active fact with same (subject, predicate) but
      DIFFERENT value → insert new Fact, then set old.superseded_by = new.id.
    - INSERT: no existing active fact for (subject, predicate) → insert new.

    Opens its own Session. Raises on unhandled DB errors (caller wraps in
    to_thread; the async caller swallows exceptions).
    """
    from sqlmodel import Session, select
    from backend.database import Fact, engine

    subj_lower = subject.strip().lower()
    pred_lower = predicate.strip().lower()
    val_stripped = value.strip()

    with Session(engine) as session:
        # Load active facts for this subject, then filter in Python to avoid
        # SQLite func.lower() portability concerns.
        stmt = select(Fact).where(Fact.superseded_by == None)  # noqa: E711
        candidates = session.exec(stmt).all()
        existing = next(
            (
                f for f in candidates
                if f.subject.strip().lower() == subj_lower
                and f.predicate.strip().lower() == pred_lower
            ),
            None,
        )

        now = datetime.utcnow()

        if existing is not None:
            if existing.value.strip().lower() == val_stripped.lower():
                # REINFORCE — same value, bump confidence
                existing.confidence = min(1.0, existing.confidence + CONFIRM_BUMP)
                existing.last_seen_at = now
                existing.updated_at = now
                session.add(existing)
                session.commit()
            else:
                # SUPERSEDE — new value for same predicate
                new_fact = Fact(
                    subject=subject.strip(),
                    predicate=predicate.strip(),
                    value=val_stripped,
                    confidence=float(confidence),
                    source=source,
                    conversation_id=conversation_id,
                    created_at=now,
                    updated_at=now,
                    last_seen_at=now,
                )
                session.add(new_fact)
                session.flush()  # get new_fact.id
                existing.superseded_by = new_fact.id
                existing.updated_at = now
                session.add(existing)
                session.commit()
        else:
            # INSERT — brand new fact
            new_fact = Fact(
                subject=subject.strip(),
                predicate=predicate.strip(),
                value=val_stripped,
                confidence=float(confidence),
                source=source,
                conversation_id=conversation_id,
                created_at=now,
                updated_at=now,
                last_seen_at=now,
            )
            session.add(new_fact)
            session.commit()


# ---------------------------------------------------------------------------
# Async public API
# ---------------------------------------------------------------------------

async def facts_recall(query: str, limit: int = RECALL_LIMIT) -> str:
    """Return a formatted string of the most relevant active facts.

    Ranks by keyword overlap with the query (primary) then effective confidence
    (secondary). Drops facts whose effective_confidence < EFFECTIVE_FLOOR.
    Best-effort: returns "" on any error.
    """
    try:
        active = await asyncio.to_thread(_db_active_facts)
        if not active:
            return ""

        now = datetime.utcnow()
        query_tokens = {t for t in query.lower().split() if len(t) > 2}

        scored: list[tuple[int, float, dict]] = []
        for f in active:
            created_at = datetime.fromisoformat(f["created_at"])
            age = _age_days(created_at, now)
            eff = effective_confidence(f["confidence"], age)
            if eff < EFFECTIVE_FLOOR:
                continue
            if query_tokens:
                text = f"{f['subject']} {f['predicate']} {f['value']}".lower()
                overlap = sum(1 for t in query_tokens if t in text)
            else:
                overlap = 0
            scored.append((overlap, eff, f))

        if not scored:
            return ""

        # Sort: overlap desc, eff desc
        scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
        top = scored[:limit]

        lines = [
            f"- {item[2]['subject']} {item[2]['predicate']} {item[2]['value']}"
            f" (conf {item[1]:.2f})"
            for item in top
        ]
        return "\n".join(lines)

    except Exception as exc:
        logger.debug(f"facts_recall: error (ignored): {exc}")
        return ""


async def extract_and_store(user_message: str, conversation_id: int | None) -> None:
    """Extract durable facts from the user message and persist them.

    Uses Haiku to extract a JSON array of facts. Best-effort: NEVER raises,
    swallows BudgetExceeded and all other errors. Logs failures at WARNING.
    """
    try:
        from backend.agents.router import haiku  # lazy import avoids circular at module load

        extract_prompt = (
            "Extract DURABLE facts from the following user message. "
            "Return a JSON array only (no prose). Each element: "
            "{\"subject\": str, \"predicate\": str, \"value\": str, \"confidence\": float 0-1}. "
            "Only stable facts: names, preferences, configurations, locations, relationships, decisions. "
            "NOT questions, NOT transient state, NOT one-off requests. "
            "Return [] if none.\n\n"
            f"User message: \"{user_message}\""
        )

        raw = await haiku(extract_prompt)

        # Defensive JSON parse: find first '[' / last ']'
        start = raw.find("[")
        end = raw.rfind("]") + 1
        if start < 0 or end <= start:
            return

        try:
            items = json.loads(raw[start:end])
        except Exception:
            return

        if not isinstance(items, list):
            return

        for item in items[:MAX_EXTRACT]:
            try:
                subj = str(item.get("subject") or "").strip()
                pred = str(item.get("predicate") or "").strip()
                val = str(item.get("value") or "").strip()
                if not subj or not pred or not val:
                    continue
                conf = float(item.get("confidence") or 0.6)
                await asyncio.to_thread(
                    _db_upsert_fact, subj, pred, val, conf, "chat", conversation_id
                )
            except Exception as item_exc:
                logger.warning(f"extract_and_store: skipping item {item!r}: {item_exc}")

    except Exception as exc:
        logger.warning(f"extract_and_store: error (ignored): {exc}")
