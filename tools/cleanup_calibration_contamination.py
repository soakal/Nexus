"""One-off cleanup for contaminated production OutcomeFlag/CalibrationHint rows.

Background: the 2026-08-11 investigation found that `pytest` runs from the
repo root were writing real rows into the live `nexus.db` (cwd-relative
`DB_PATH` in `backend/database.py` — see this repo's own `tests/conftest.py`
`_isolate_test_database` fixture, which fixes the root cause going forward).
This script is the cleanup of the contamination that already happened before
that fix landed: synthetic `homelab_watch:docker:plex*` calibration-loop test
rows.

Deliberately plain stdlib `sqlite3`, NOT `backend.database` — importing the
backend constructs its engine off that same cwd-relative `DB_PATH`, the exact
bug class that caused this contamination in the first place. This script
resolves the DB path from its OWN file location and takes ZERO backend
imports (enforced by tests/test_cleanup_calibration_contamination.py).

Never wired into anything: no scheduler job, no migration shim, no conftest
hook, no import from any backend/ module. Dry-run by default; deletion only
happens with an explicit --confirm flag, and only after a hard count-
verification gate confirms the live DB still matches the 2026-08-11
investigation exactly.

Usage:
    python tools/cleanup_calibration_contamination.py [--db PATH] [--confirm]

Powered by CwiAI
"""

from __future__ import annotations

import argparse
import pathlib
import sqlite3
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
DEFAULT_DB = REPO_ROOT / "nexus.db"

FINGERPRINTS = [
    "homelab_watch:docker:plex",
    "homelab_watch:docker:plex;evil",
    "homelab_watch:docker:plex<b>",
    "homelab_watch:docker:" + "a" * 80,
]
CREATED_MIN = "2026-08-02 00:00:00"
CREATED_MAX = "2026-08-03 00:00:00"
EXPECTED_FLAG_ROWS = 88
EXPECTED_HINT_ROWS = 4
EXPECTED_HINT_IDS = {3, 4, 5, 6}

_FLAG_WHERE = (
    "source = 'homelab_watch' AND fingerprint IN (?,?,?,?) "
    "AND created_at >= ? AND created_at < ?"
)
_HINT_WHERE = "fingerprint IN (?,?,?,?)"


def select_contamination(conn: sqlite3.Connection) -> dict:
    """Query both selections. Returns a dict with 'flags' and 'hints' rows."""
    cur = conn.cursor()

    cur.execute(
        f"SELECT id, fingerprint, created_at, summary FROM outcomeflag WHERE {_FLAG_WHERE}",
        [*FINGERPRINTS, CREATED_MIN, CREATED_MAX],
    )
    flag_rows = cur.fetchall()

    cur.execute(
        f"SELECT id, fingerprint, status, created_at, updated_at FROM calibrationhint WHERE {_HINT_WHERE}",
        FINGERPRINTS,
    )
    hint_rows = cur.fetchall()

    return {"flags": flag_rows, "hints": hint_rows}


def verify_expected(found: dict) -> list[str]:
    """Compare found rows against the EXPECTED_* constants. Empty list = OK."""
    mismatches: list[str] = []

    flag_rows = found["flags"]
    hint_rows = found["hints"]

    total_flags = len(flag_rows)
    if total_flags != EXPECTED_FLAG_ROWS:
        mismatches.append(
            f"MISMATCH: expected {EXPECTED_FLAG_ROWS} total OutcomeFlag rows, found {total_flags}"
        )

    counts_by_fp: dict[str, int] = {fp: 0 for fp in FINGERPRINTS}
    for row in flag_rows:
        fp = row[1]
        counts_by_fp[fp] = counts_by_fp.get(fp, 0) + 1

    for fp in FINGERPRINTS:
        count = counts_by_fp.get(fp, 0)
        if count != 22:
            mismatches.append(
                f"MISMATCH: expected 22 OutcomeFlag rows for fingerprint {fp!r}, found {count}"
            )

    total_hints = len(hint_rows)
    if total_hints != EXPECTED_HINT_ROWS:
        mismatches.append(
            f"MISMATCH: expected {EXPECTED_HINT_ROWS} CalibrationHint rows, found {total_hints}"
        )

    hint_ids = {row[0] for row in hint_rows}
    if hint_ids != EXPECTED_HINT_IDS:
        mismatches.append(
            f"MISMATCH: expected CalibrationHint ids {sorted(EXPECTED_HINT_IDS)}, found {sorted(hint_ids)}"
        )

    return mismatches


def delete_contamination(conn: sqlite3.Connection) -> tuple[int, int]:
    """Execute both DELETEs (same WHERE clauses) inside a single transaction, commit.

    Returns (flags_deleted, hints_deleted).
    """
    cur = conn.cursor()

    cur.execute(
        f"DELETE FROM outcomeflag WHERE {_FLAG_WHERE}",
        [*FINGERPRINTS, CREATED_MIN, CREATED_MAX],
    )
    flags_deleted = cur.rowcount

    cur.execute(
        f"DELETE FROM calibrationhint WHERE {_HINT_WHERE}",
        FINGERPRINTS,
    )
    hints_deleted = cur.rowcount

    conn.commit()
    return flags_deleted, hints_deleted


def _print_report(found: dict) -> None:
    flag_rows = found["flags"]
    hint_rows = found["hints"]

    print("=== OutcomeFlag contamination ===")
    by_fp: dict[str, list] = {fp: [] for fp in FINGERPRINTS}
    for row in flag_rows:
        by_fp.setdefault(row[1], []).append(row)

    for fp in FINGERPRINTS:
        rows = by_fp.get(fp, [])
        ids = [r[0] for r in rows]
        created_ats = [r[2] for r in rows]
        print(f"  fingerprint={fp!r}  count={len(rows)}")
        if ids:
            print(f"    id range: {min(ids)}-{max(ids)}")
            print(f"    created_at range: {min(created_ats)} .. {max(created_ats)}")
        for r in rows[:3]:
            summary = (r[3] or "")[:80]
            print(f"    sample: id={r[0]} created_at={r[2]} summary={summary!r}")

    print(f"  TOTAL: {len(flag_rows)}")

    print("=== CalibrationHint contamination ===")
    for row in hint_rows:
        hid, fp, status, created_at, updated_at = row
        print(
            f"  id={hid} fingerprint={fp!r} status={status} "
            f"created_at={created_at} updated_at={updated_at}"
        )
    print(f"  TOTAL: {len(hint_rows)}")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Clean up contaminated production OutcomeFlag/CalibrationHint rows "
        "from the 2026-08-11 calibration-loop test-data leak. Dry run by default."
    )
    parser.add_argument("--db", type=pathlib.Path, default=DEFAULT_DB)
    parser.add_argument("--confirm", action="store_true")
    args = parser.parse_args(argv)

    db_path: pathlib.Path = args.db

    if not db_path.exists():
        print(f"ERROR: database file does not exist: {db_path}", file=sys.stderr)
        print("Powered by CwiAI")
        return 2

    conn = sqlite3.connect(str(db_path), timeout=30)
    try:
        conn.execute("PRAGMA busy_timeout=30000")

        found = select_contamination(conn)
        _print_report(found)

        mismatches = verify_expected(found)
        if mismatches:
            for line in mismatches:
                print(line)
            print(
                "Refusing to proceed — the live DB no longer matches the "
                "2026-08-11 investigation. Re-verify manually."
            )
            print("Powered by CwiAI")
            return 2

        if not args.confirm:
            print("DRY RUN — no changes made.")
            print(
                f"Suggested first: Copy-Item {db_path.name} "
                f"{db_path.name}.bak-cleanup-YYYY-MM-DD"
            )
            print("Re-run with --confirm to delete the rows listed above.")
            print("Powered by CwiAI")
            return 0

        flags_deleted, hints_deleted = delete_contamination(conn)

        cur = conn.cursor()
        cur.execute(
            f"SELECT COUNT(*) FROM outcomeflag WHERE {_FLAG_WHERE}",
            [*FINGERPRINTS, CREATED_MIN, CREATED_MAX],
        )
        remaining_flags = cur.fetchone()[0]
        cur.execute(
            f"SELECT COUNT(*) FROM calibrationhint WHERE {_HINT_WHERE}",
            FINGERPRINTS,
        )
        remaining_hints = cur.fetchone()[0]

        print(
            f"Deleted {flags_deleted} OutcomeFlag rows, {hints_deleted} CalibrationHint rows."
        )
        print(
            f"Post-delete verification: {remaining_flags} OutcomeFlag / "
            f"{remaining_hints} CalibrationHint rows remain matching the criteria."
        )

        if remaining_flags != 0 or remaining_hints != 0:
            print(
                "ERROR: post-delete recount is nonzero — something is deeply "
                "wrong, human look now.",
                file=sys.stderr,
            )
            print("Powered by CwiAI")
            return 1

        print("Powered by CwiAI")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
