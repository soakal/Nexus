"""Tests for tools/cleanup_calibration_contamination.py.

Everything here runs against a throwaway tmp_path SQLite file — NEVER the
real DB, NEVER the script's own DEFAULT_DB path. The script itself takes
zero backend imports (see test_script_imports_nothing_from_backend below),
so these tests don't go through tests/conftest.py's database isolation at
all; they build their own minimal schema directly.
"""

import importlib.util
import pathlib
import sqlite3
import sys

import pytest

_SCRIPT_PATH = (
    pathlib.Path(__file__).resolve().parents[1]
    / "tools"
    / "cleanup_calibration_contamination.py"
)

_spec = importlib.util.spec_from_file_location(
    "cleanup_calibration_contamination", _SCRIPT_PATH
)
cleanup = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cleanup)


FINGERPRINTS = cleanup.FINGERPRINTS


def _make_db(tmp_path: pathlib.Path) -> pathlib.Path:
    db_path = tmp_path / "test_nexus.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        CREATE TABLE outcomeflag (
            id INTEGER PRIMARY KEY,
            source VARCHAR,
            fingerprint VARCHAR,
            created_at VARCHAR,
            summary VARCHAR
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE calibrationhint (
            id INTEGER PRIMARY KEY,
            fingerprint VARCHAR,
            status VARCHAR,
            created_at VARCHAR,
            updated_at VARCHAR
        )
        """
    )
    conn.commit()
    conn.close()
    return db_path


def _seed_contamination(conn: sqlite3.Connection, *, skip_one_flag: bool = False) -> None:
    """Seed 88 contamination OutcomeFlag rows (22 per fingerprint) + 4 CalibrationHint
    rows (ids 3-6) + decoys that must survive any delete."""
    next_id = 1
    for fp in FINGERPRINTS:
        count = 22
        if skip_one_flag and fp == FINGERPRINTS[0]:
            count = 21
        for i in range(count):
            conn.execute(
                "INSERT INTO outcomeflag (id, source, fingerprint, created_at, summary) "
                "VALUES (?, 'homelab_watch', ?, ?, ?)",
                (next_id, fp, f"2026-08-02 0{i % 9}:00:00.000000", f"contamination row {next_id}"),
            )
            next_id += 1

    # CalibrationHint rows, ids 3-6, one per fingerprint (order matches FINGERPRINTS).
    hint_id = 3
    for fp in FINGERPRINTS:
        conn.execute(
            "INSERT INTO calibrationhint (id, fingerprint, status, created_at, updated_at) "
            "VALUES (?, ?, 'expired', '2026-08-02 07:50:00', '2026-08-11 07:50:00')",
            (hint_id, fp),
        )
        hint_id += 1

    # Decoys that MUST survive every code path below.
    # 1. Same fingerprint as a real contamination row, but OUTSIDE the date window
    #    (a genuine later real Plex alert).
    conn.execute(
        "INSERT INTO outcomeflag (id, source, fingerprint, created_at, summary) "
        "VALUES (9000, 'homelab_watch', ?, '2026-08-05 12:00:00.000000', 'real later plex alert')",
        (FINGERPRINTS[0],),
    )
    # 2. Different fingerprint, inside the window.
    conn.execute(
        "INSERT INTO outcomeflag (id, source, fingerprint, created_at, summary) "
        "VALUES (9001, 'homelab_watch', 'homelab_watch:garage_open', '2026-08-02 12:00:00.000000', 'unrelated')"
    )
    # 3. Unrelated CalibrationHint fingerprint.
    conn.execute(
        "INSERT INTO calibrationhint (id, fingerprint, status, created_at, updated_at) "
        "VALUES (9002, 'homelab_watch:garage_open', 'active', '2026-08-02 07:50:00', '2026-08-11 07:50:00')"
    )
    conn.commit()


@pytest.fixture
def contaminated_db(tmp_path):
    db_path = _make_db(tmp_path)
    conn = sqlite3.connect(str(db_path))
    _seed_contamination(conn)
    conn.close()
    return db_path


def _counts(db_path: pathlib.Path) -> tuple[int, int]:
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM outcomeflag")
    flags = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM calibrationhint")
    hints = cur.fetchone()[0]
    conn.close()
    return flags, hints


def test_dry_run_deletes_nothing(contaminated_db, capsys):
    before_flags, before_hints = _counts(contaminated_db)

    rc = cleanup.main(["--db", str(contaminated_db)])

    assert rc == 0
    out = capsys.readouterr().out
    assert "DRY RUN — no changes made." in out
    assert "Powered by CwiAI" in out
    after_flags, after_hints = _counts(contaminated_db)
    assert (after_flags, after_hints) == (before_flags, before_hints)


def test_confirm_deletes_exactly_matching_rows(contaminated_db, capsys):
    rc = cleanup.main(["--db", str(contaminated_db), "--confirm"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "Deleted 88 OutcomeFlag rows, 4 CalibrationHint rows." in out
    assert "Post-delete verification: 0 OutcomeFlag / 0 CalibrationHint rows remain matching the criteria." in out
    assert "Powered by CwiAI" in out

    conn = sqlite3.connect(str(contaminated_db))
    cur = conn.cursor()

    # The 88+4 contamination rows are gone.
    cur.execute("SELECT COUNT(*) FROM outcomeflag WHERE source='homelab_watch'")
    remaining_homelab_flags = cur.fetchone()[0]
    # Only decoy 9001 (different fingerprint, in-window) should remain among homelab_watch rows,
    # plus decoy 9000 (real Plex fingerprint, out of window).
    assert remaining_homelab_flags == 2

    # Decoys survive.
    cur.execute("SELECT id FROM outcomeflag WHERE id = 9000")
    assert cur.fetchone() is not None
    cur.execute("SELECT id FROM outcomeflag WHERE id = 9001")
    assert cur.fetchone() is not None
    cur.execute("SELECT id FROM calibrationhint WHERE id = 9002")
    assert cur.fetchone() is not None

    # Original contamination hint ids are gone.
    cur.execute("SELECT id FROM calibrationhint WHERE id IN (3,4,5,6)")
    assert cur.fetchall() == []

    conn.close()


def test_count_mismatch_refuses(tmp_path, capsys):
    db_path = _make_db(tmp_path)
    conn = sqlite3.connect(str(db_path))
    _seed_contamination(conn, skip_one_flag=True)
    conn.close()

    before_flags, before_hints = _counts(db_path)
    assert before_flags == 88 - 1 + 2  # 87 contamination + 2 flag decoys
    assert before_hints == 4 + 1  # 4 contamination hints + 1 decoy hint

    rc = cleanup.main(["--db", str(db_path), "--confirm"])

    assert rc == 2
    out = capsys.readouterr().out
    assert "MISMATCH:" in out
    assert (
        "Refusing to proceed — the live DB no longer matches the 2026-08-11 investigation. "
        "Re-verify manually." in out
    )

    after_flags, after_hints = _counts(db_path)
    assert (after_flags, after_hints) == (before_flags, before_hints)


def test_hint_id_mismatch_refuses(tmp_path, capsys):
    db_path = _make_db(tmp_path)
    conn = sqlite3.connect(str(db_path))
    _seed_contamination(conn)
    # Mutate hint id 6 -> 7, breaking the expected {3,4,5,6} id set.
    conn.execute("UPDATE calibrationhint SET id = 7 WHERE id = 6")
    conn.commit()
    conn.close()

    before_flags, before_hints = _counts(db_path)

    rc = cleanup.main(["--db", str(db_path), "--confirm"])

    assert rc == 2
    out = capsys.readouterr().out
    assert "MISMATCH:" in out

    after_flags, after_hints = _counts(db_path)
    assert (after_flags, after_hints) == (before_flags, before_hints)


def test_missing_db_refuses(tmp_path, capsys):
    missing = tmp_path / "does_not_exist.db"

    rc = cleanup.main(["--db", str(missing)])

    assert rc == 2
    captured = capsys.readouterr()
    assert "does not exist" in (captured.out + captured.err)


def test_script_imports_nothing_from_backend():
    text = _SCRIPT_PATH.read_text(encoding="utf-8")
    for line in text.splitlines():
        stripped = line.strip()
        assert not stripped.startswith("import backend"), line
        assert not stripped.startswith("from backend"), line
