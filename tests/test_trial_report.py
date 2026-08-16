import json
from datetime import date, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.agents import trial_report


def _settings(**overrides):
    s = MagicMock()
    s.trial_report_enabled = True
    s.shadow_model = "google/gemini-2.5-flash-lite"
    s.shadow_until = "2026-09-01"
    s.briefing_timezone = "America/Detroit"
    for k, v in overrides.items():
        setattr(s, k, v)
    return s


@pytest.mark.asyncio
async def test_missing_shadow_log_degrades_not_raises(tmp_path, monkeypatch):
    """No shadow.jsonl yet (trial just started, or Trial A never ran) must
    produce a readable 'no data yet' line, never a crash."""
    monkeypatch.setattr(trial_report, "_SHADOW_LOG", tmp_path / "shadow.jsonl")
    monkeypatch.setattr(trial_report, "_TRIAL_B_NIGHTS_DIR", tmp_path / "nights")
    with patch("backend.config.get_settings", return_value=_settings()):
        text = await trial_report.build_trial_report_text()
    assert "No shadow calls logged" in text
    assert "No trial run recorded" in text


@pytest.mark.asyncio
async def test_malformed_jsonl_line_is_skipped_not_fatal(tmp_path, monkeypatch):
    log = tmp_path / "shadow.jsonl"
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    good_row = {
        "ts": f"{yesterday}T01:00:00", "label": "mail_junk_classify",
        "agree": True, "out_a": "KEEP", "out_b": "KEEP",
    }
    log.write_text(f"not json at all\n{json.dumps(good_row)}\n\n", encoding="utf-8")
    monkeypatch.setattr(trial_report, "_SHADOW_LOG", log)
    monkeypatch.setattr(trial_report, "_TRIAL_B_NIGHTS_DIR", tmp_path / "nights")

    settings = _settings()
    with patch("backend.config.get_settings", return_value=settings), \
         patch("backend.safety.governor.spend_report", return_value={"by_label": []}):
        text = await trial_report.build_trial_report_text()

    assert "1 calls" in text
    assert "0 disagreed" in text


def test_verdict_payload_a_bounds_sample_size(tmp_path, monkeypatch):
    """The verdict prompt must never balloon with every disagreement ever
    logged -- harmful-direction rows always included, capped at 20 total."""
    log = tmp_path / "shadow.jsonl"
    rows = []
    for i in range(30):
        rows.append({
            "ts": f"2026-08-{(i % 28) + 1:02d}T01:00:00",
            "label": "mail_junk_classify",
            "agree": False,
            "out_a": "KEEP",
            "out_b": "JUNK",  # harmful direction every time
        })
    log.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    monkeypatch.setattr(trial_report, "_SHADOW_LOG", log)

    with patch("backend.safety.governor.spend_report", return_value={"by_label": []}):
        payload = trial_report._build_verdict_payload_a()

    assert payload is not None
    aggregate_block, sample_block, cost_line = payload
    assert "mail_junk_classify: 30 calls, 30 disagreed" in aggregate_block
    assert sample_block.count("[HARMFUL]") <= 20
    assert sample_block.count("[HARMFUL]") == 20  # capped, not all 30


@pytest.mark.asyncio
async def test_verdict_not_resent_when_marker_exists(tmp_path, monkeypatch):
    monkeypatch.setattr(trial_report, "_VERDICT_DIR", tmp_path)
    marker = tmp_path / "verdict-A.md"
    marker.write_text("already sent", encoding="utf-8")

    with patch("backend.agents.router.opus", new_callable=AsyncMock) as mock_opus:
        result = await trial_report.send_trial_verdict("A")

    assert result == {"delivered": False, "reason": "already sent"}
    mock_opus.assert_not_called()


@pytest.mark.asyncio
async def test_verdict_happy_path_writes_marker_and_drafts_once(tmp_path, monkeypatch):
    log = tmp_path / "shadow.jsonl"
    row = {"ts": "2026-08-15T01:00:00", "label": "mail_junk_classify", "agree": True, "out_a": "KEEP", "out_b": "KEEP"}
    log.write_text(json.dumps(row) + "\n", encoding="utf-8")
    monkeypatch.setattr(trial_report, "_SHADOW_LOG", log)
    monkeypatch.setattr(trial_report, "_VERDICT_DIR", tmp_path / "logs")

    with patch("backend.safety.governor.spend_report", return_value={"by_label": []}), \
         patch("backend.agents.router.opus", new_callable=AsyncMock, return_value="VERDICT: GO\nWHY: fine.") as mock_opus, \
         patch("backend.integrations.protonmail.save_draft", new_callable=AsyncMock, return_value={"ok": True}) as mock_draft:
        first = await trial_report.send_trial_verdict("A")
        second = await trial_report.send_trial_verdict("A")

    assert first == {"delivered": True}
    mock_opus.assert_awaited_once()
    mock_draft.assert_awaited_once()
    marker = tmp_path / "logs" / "verdict-A.md"
    assert marker.exists()
    assert "VERDICT: GO" in marker.read_text(encoding="utf-8")

    assert second == {"delivered": False, "reason": "already sent"}
    mock_opus.assert_awaited_once()  # still just once -- second call was a no-op


@pytest.mark.asyncio
async def test_verdict_budget_exceeded_defers_without_marker(tmp_path, monkeypatch):
    """The money-relevant case: a BudgetExceeded call must NOT write the
    marker, so send_trial_verdict retries tomorrow instead of losing the
    verdict for the rest of the trial."""
    from backend.safety.governor import BudgetExceeded

    log = tmp_path / "shadow.jsonl"
    row = {"ts": "2026-08-15T01:00:00", "label": "mail_junk_classify", "agree": True, "out_a": "KEEP", "out_b": "KEEP"}
    log.write_text(json.dumps(row) + "\n", encoding="utf-8")
    monkeypatch.setattr(trial_report, "_SHADOW_LOG", log)
    verdict_dir = tmp_path / "logs"
    monkeypatch.setattr(trial_report, "_VERDICT_DIR", verdict_dir)

    with patch("backend.safety.governor.spend_report", return_value={"by_label": []}), \
         patch("backend.agents.router.opus", new_callable=AsyncMock, side_effect=BudgetExceeded("daily", 25.0, 25.0)), \
         patch("backend.events.notify_phone", new_callable=AsyncMock) as mock_notify:
        result = await trial_report.send_trial_verdict("A")

    assert result == {"delivered": False, "deferred": True}
    mock_notify.assert_awaited_once()
    assert not (verdict_dir / "verdict-A.md").exists()


@pytest.mark.asyncio
async def test_run_trial_report_skips_when_disabled(monkeypatch):
    with patch("backend.config.get_settings", return_value=_settings(trial_report_enabled=False)), \
         patch("backend.integrations.protonmail.save_draft", new_callable=AsyncMock) as mock_draft:
        result = await trial_report.run_trial_report()
    assert result == {"skipped": True}
    mock_draft.assert_not_called()


@pytest.mark.asyncio
async def test_run_trial_report_drafts_via_protonmail_not_send(tmp_path, monkeypatch):
    """The whole point of Trial 7's 'auto-draft, you send' decision: this
    must call save_draft, and must never touch a send path."""
    monkeypatch.setattr(trial_report, "_SHADOW_LOG", tmp_path / "shadow.jsonl")
    monkeypatch.setattr(trial_report, "_TRIAL_B_NIGHTS_DIR", tmp_path / "nights")
    monkeypatch.setattr(trial_report, "_VERDICT_DIR", tmp_path / "logs")

    # Far-future shadow_until so this test's behavior doesn't quietly change
    # once the real 2026-09-01 trial-end date passes -- a live date fuse.
    with patch("backend.config.get_settings", return_value=_settings(shadow_until="2099-01-01")), \
         patch("backend.integrations.protonmail.save_draft", new_callable=AsyncMock, return_value={"ok": True}) as mock_draft, \
         patch("backend.integrations.protonmail.send_email", new_callable=AsyncMock) as mock_send:
        result = await trial_report.run_trial_report()

    assert result["delivered"] is True
    mock_draft.assert_awaited_once()
    assert mock_draft.call_args.kwargs["recipients"] == [trial_report._RECIPIENT]
    mock_send.assert_not_called()
