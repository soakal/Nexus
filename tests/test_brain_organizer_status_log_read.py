"""Regression test for backend/api/brain_organizer.py::brain_organizer_status
(spec #2 SS6.5, acceptance criterion 50): NONE of the three read_text() calls
in this function (organizer.log tail, processed.json, config.json) may run
directly on the event loop -- each must be offloaded via asyncio.to_thread --
while the function still returns correct data derived from those reads.
"""
import asyncio
import json

import pytest

from backend.api import brain_organizer as boapi


@pytest.mark.asyncio
async def test_status_log_tail_reads_via_to_thread(monkeypatch, tmp_path):
    log_file = tmp_path / "organizer.log"
    log_file.write_text(
        "2026-08-02 00:00:00 [INFO] first line\n"
        "2026-08-02 00:00:01 [WARNING] a warning\n"
        "2026-08-02 00:00:02 [ERROR] boom\n",
        encoding="utf-8",
    )

    # processed.json and config.json also get real, valid content so their
    # read_text() branches actually execute (criterion 50 covers all three
    # reads in the function, not just the log tail).
    processed_file = tmp_path / "processed.json"
    processed_file.write_text(
        json.dumps({"note-1": {"status": "ok", "timestamp": "2026-08-02T00:00:00"}}),
        encoding="utf-8",
    )
    config_file = tmp_path / "config.json"
    config_file.write_text(
        json.dumps(
            {
                "vault_path": str(tmp_path),
                "raw_folder": "raw",
                "backup_folder": "backup",
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(boapi, "_LOG", log_file)
    monkeypatch.setattr(boapi, "_PROCESSED", processed_file)
    monkeypatch.setattr(boapi, "_CONFIG", config_file)

    calls = []
    real_to_thread = asyncio.to_thread

    async def spy_to_thread(func, *args, **kwargs):
        calls.append((func, args, kwargs))
        return await real_to_thread(func, *args, **kwargs)

    monkeypatch.setattr(boapi.asyncio, "to_thread", spy_to_thread)

    result = await boapi.brain_organizer_status()

    # asyncio.to_thread must actually be invoked to perform each read (the
    # regression this test guards against: a direct blocking read_text() for
    # any of the three reads). Filter by the bound read_text method rather
    # than pinning the total call count, since other unrelated to_thread
    # calls could legitimately be added later.
    def to_thread_calls_for(path):
        return [
            c for c in calls
            if c[0] == path.read_text and c[2].get("encoding") == "utf-8"
        ]

    log_calls = to_thread_calls_for(log_file)
    processed_calls = to_thread_calls_for(processed_file)
    config_calls = to_thread_calls_for(config_file)

    assert len(log_calls) == 1, calls
    assert len(processed_calls) == 1, calls
    assert len(config_calls) == 1, calls

    # And the read content must still surface correctly in the response.
    assert result["log_tail"] == [
        "2026-08-02 00:00:00 [INFO] first line",
        "2026-08-02 00:00:01 [WARNING] a warning",
        "2026-08-02 00:00:02 [ERROR] boom",
    ]
    assert result["succeeded"] == 1
    assert result["failed"] == 0
    assert result["last_run"] == "2026-08-02T00:00:00"
