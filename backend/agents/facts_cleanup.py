"""One-time (but safe to re-run) cleanup for pre-fix Fact-table duplicate-
subject noise -- see CLAUDE.md's `facts_digest` note and
tests/test_facts_digest.py::test_facts_digest_enabled_by_default.

Background: `backend.agents.facts._db_upsert_fact` was extended with a
canonical-key fallback (see `facts._canonical_key`) so a NEWLY-extracted fact
whose (subject, predicate) is a wording variant of an existing active fact
("Unraid"/"storage used" vs "unraid"/"current storage") gets SUPERSEDED
instead of accumulating as a near-duplicate row. That fix only applies to
inserts that happen AFTER it shipped -- rows already in the table before then
("Charlie"/"Charlee", "Unraid Array"/"Unraid array"/"Unraid_array", etc.)
were never reconciled. This module is the one-time retroactive pass.

Two independent, purely mechanical passes over ACTIVE facts (superseded_by
IS NULL AND dismissed_at IS NULL) -- deliberately conservative, no fuzzy/
edit-distance matching beyond one hand-verified alias:

  1. SUBJECT RENAME -- collapses pure case/separator spelling variants of the
     same subject ("Unraid Array" / "Unraid array" / "Unraid_array") onto
     whichever spelling appears on the most rows, plus one hand-curated typo
     fix (SUBJECT_ALIASES). This is what actually fixes the digest's
     "one section per subject spelling" noise. Deliberately does NOT attempt
     to merge semantically-close-but-distinct subjects ("Unraid" vs "Unraid
     storage" vs "Unraid system") -- those are plausibly different sub-topics
     and merging them risks conflating facts a human would consider distinct.

  2. PREDICATE MERGE -- reuses `facts._canonical_key` (the exact function the
     live upsert path already uses) to find active rows that are wording
     variants of the SAME (subject, predicate) fact and supersedes all but
     the most-recently-seen one, mirroring the SUPERSEDE semantics
     `_db_upsert_fact` already uses for reinforcement (newest wins, history
     preserved via `superseded_by`, never a hard delete).

Both passes are idempotent: rename is a no-op once every subject is already
canonical; merge only ever looks at rows still active (superseded_by IS
NULL), so an already-merged loser can never be re-merged on a second run.

Nothing is ever deleted. Renames only touch Fact.subject/updated_at on
active rows; merges only set Fact.superseded_by/updated_at on the LOSING
rows of a merge group -- the exact same non-destructive convention already
used everywhere else in facts.py (SUPERSEDE) and facts_digest.py (never
touches raw rows, only a watermark).
"""
import logging
import re
from collections import defaultdict
from datetime import datetime

logger = logging.getLogger(__name__)


# Hand-verified typo fixes NOT caught by mechanical case/separator
# normalization (see _normalize_subject_key). Deliberately a short,
# hand-curated list, not fuzzy/edit-distance matching: an automated
# near-match (e.g. Levenshtein <= 2) would also flag pairs like "Bank of
# America" / "Blazer Auto Pay" through incidental short-string collisions,
# or -- worse -- silently merge two subjects that just happen to be spelled
# similarly. "charlee" -> "Charlie" was confirmed against the real data: both
# rows are a `birthday` predicate created ~6ms apart (same extraction run),
# clearly the same fact with the subject misspelled once.
SUBJECT_ALIASES: dict[str, str] = {
    "charlee": "Charlie",
}


def _normalize_subject_key(subject: str) -> str:
    """Case/separator-insensitive comparison key for a subject string.

    Collapses runs of whitespace/underscore/hyphen to a single space and
    lowercases. Deliberately does NOT sort or drop words the way
    facts._canonical_key does -- this only catches pure FORMATTING variants
    of the same subject ("Unraid Array" / "Unraid array" / "Unraid_array"),
    never semantically-similar-but-distinct subjects.
    """
    return re.sub(r"[\s_-]+", " ", subject.strip().lower())


def plan_subject_renames(rows: list[dict]) -> dict[str, str]:
    """Pure planner (no I/O). Map every distinct subject spelling seen in
    `rows` to its canonical spelling.

    rows: dicts with at least "subject" (str) and "created_at" (datetime).

    Stage 1 -- SUBJECT_ALIASES (hand-curated typo fixes) applied first.
    Stage 2 -- subjects whose alias-applied spelling normalizes to the same
    _normalize_subject_key are merged onto whichever raw spelling appears on
    the most rows (tie-break: most recently created, then alphabetical --
    both purely for determinism so replanning always agrees with itself).

    Returns {original_subject: canonical_subject} for every distinct ORIGINAL
    subject in rows, including identity entries for subjects needing no
    change -- callers only need `!=` to know whether an UPDATE is needed.
    """
    post_alias = {
        r["subject"]: SUBJECT_ALIASES.get(r["subject"].strip().lower(), r["subject"])
        for r in rows
    }

    norm_groups: dict[str, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
    for r in rows:
        spelling = post_alias[r["subject"]]
        norm_groups[_normalize_subject_key(spelling)][spelling].append(r)

    canonical_for_spelling: dict[str, str] = {}
    for spellings in norm_groups.values():
        def _sort_key(item, _spellings=spellings):
            spelling, spelling_rows = item
            latest = max(row["created_at"] for row in spelling_rows)
            return (-len(spelling_rows), -latest.timestamp(), spelling)

        winner = sorted(spellings.items(), key=_sort_key)[0][0]
        for spelling in spellings:
            canonical_for_spelling[spelling] = winner

    return {orig: canonical_for_spelling[post_alias[orig]] for orig in post_alias}


def plan_predicate_merges(rows: list[dict]) -> list[dict]:
    """Pure planner (no I/O). Group rows by facts._canonical_key(subject,
    predicate) -- the SAME function the live `_db_upsert_fact` fallback
    already uses -- and, for any group with more than one row, pick a winner
    (most recent last_seen_at, falling back to created_at, tie-break highest
    id) and mark the rest as its losers.

    rows: dicts with "id" (int), "subject", "predicate", "created_at"
    (datetime), "last_seen_at" (datetime | None).

    Returns [{"winner_id": int, "loser_ids": [int, ...]}, ...] -- one entry
    per colliding group. Groups of size 1 are omitted.
    """
    from backend.agents.facts import _canonical_key

    groups: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        groups[_canonical_key(r["subject"], r["predicate"])].append(r)

    merges = []
    for group_rows in groups.values():
        if len(group_rows) < 2:
            continue

        def _sort_key(r):
            ts = r.get("last_seen_at") or r["created_at"]
            return (ts, r["id"])

        ordered = sorted(group_rows, key=_sort_key, reverse=True)
        winner, losers = ordered[0], ordered[1:]
        merges.append({"winner_id": winner["id"], "loser_ids": [r["id"] for r in losers]})
    return merges


# ---------------------------------------------------------------------------
# SYNC DB helper — call directly from a one-off script (never from inside a
# live request/event-loop path). Opens its own short-lived Session so it
# never holds a long transaction against a concurrently-running app.
# ---------------------------------------------------------------------------

def run_cleanup(dry_run: bool = True) -> dict:
    """Plan (and, unless dry_run, apply) the subject-rename + predicate-merge
    passes over the live Fact table.

    Returns a report dict:
      {
        "subject_renames": [{"id", "old", "new"}, ...],
        "merges": [{"winner_id", "loser_ids"}, ...],
        "active_before": int,
        "active_after": int,   # what active count IS (applied) or WOULD BE (dry_run)
        "dry_run": bool,
      }

    Never raises on its own logic; a DB error propagates (this is an
    operator-invoked maintenance tool, not a best-effort background job --
    a silent failure here would be worse than a loud one).
    """
    from sqlmodel import Session, select
    from backend.database import Fact, engine

    now = datetime.utcnow()

    with Session(engine) as session:
        stmt = (
            select(Fact)
            .where(Fact.superseded_by == None)  # noqa: E711
            .where(Fact.dismissed_at == None)   # noqa: E711
        )
        active = session.exec(stmt).all()
        active_before = len(active)
        by_id = {f.id: f for f in active}

        rows = [
            {
                "id": f.id,
                "subject": f.subject,
                "predicate": f.predicate,
                "created_at": f.created_at,
                "last_seen_at": f.last_seen_at,
            }
            for f in active
        ]

        rename_plan = plan_subject_renames(rows)
        subject_renames = []
        for r in rows:
            canonical = rename_plan[r["subject"]]
            if canonical != r["subject"]:
                subject_renames.append({"id": r["id"], "old": r["subject"], "new": canonical})
                r["subject"] = canonical  # keep in-memory rows consistent for the merge plan below
                if not dry_run:
                    by_id[r["id"]].subject = canonical
                    by_id[r["id"]].updated_at = now

        merge_plan = plan_predicate_merges(rows)
        if not dry_run:
            for m in merge_plan:
                for loser_id in m["loser_ids"]:
                    by_id[loser_id].superseded_by = m["winner_id"]
                    by_id[loser_id].updated_at = now
            for f in by_id.values():
                session.add(f)
            session.commit()

        active_after = active_before - sum(len(m["loser_ids"]) for m in merge_plan)

        logger.info(
            f"facts_cleanup: dry_run={dry_run} renames={len(subject_renames)} "
            f"merges={len(merge_plan)} active {active_before}->{active_after}"
        )

        return {
            "subject_renames": subject_renames,
            "merges": merge_plan,
            "active_before": active_before,
            "active_after": active_after,
            "dry_run": dry_run,
        }
