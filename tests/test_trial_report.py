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


# --- P2: semantic comparator for decision-shaped labels ---------------------

def _row(label, out_a, out_b, agree=False, day=None):
    ts = f"{day or (date.today() - timedelta(days=1)).isoformat()}T01:00:00"
    return {"ts": ts, "label": label, "agree": agree, "out_a": out_a, "out_b": out_b}


def _judge(allow, reason):
    return json.dumps({"allow": allow, "confidence": 0.9, "reason": reason})


def test_shadow_agree_ignores_the_free_text_reason():
    """The bug: action_judge's two models never phrase `reason` identically, so
    raw text equality reported ~100% disagreement on a label where both models
    actually agreed. Agreement is the decision, not the prose."""
    from backend.agents.router import shadow_agree

    assert shadow_agree("action_judge", _judge(True, "no automation owns it"),
                        _judge(True, "nothing else manages this entity")) is True
    assert shadow_agree("action_judge", _judge(True, "fine"), _judge(False, "fine")) is False
    assert shadow_agree("goal_criteria_eval", json.dumps({"met": True, "why": "a"}),
                        json.dumps({"met": True, "why": "b"})) is True
    # A fenced response is still a decision -- find("{")/rfind("}") skips the fence.
    assert shadow_agree("action_judge", "```json\n" + _judge(True, "x") + "\n```",
                        _judge(True, "y")) is True
    # Unparseable shadow output is NOT agreement, and is not silently a "no".
    assert shadow_agree("action_judge", _judge(True, "x"), "I'm sorry, I can't") is False
    # Text-comparator labels are untouched by any of this.
    assert shadow_agree("mail_junk_classify", "keep", "KEEP") is True
    assert shadow_agree("mail_junk_classify", "KEEP", "JUNK") is False


def test_action_judge_harmful_direction_reads_allow_not_the_word_veto():
    """The old check grepped for the literal string "veto", which the judge
    never emits -- so it scored zero harmful rows no matter what happened."""
    check = trial_report._HARMFUL_DIRECTION["action_judge"]
    # Real vetoed, shadow would have let it through: the harmful direction.
    assert check(_judge(False, "flip-flopping"), _judge(True, "looks fine")) is True
    # The safe direction (shadow more conservative) is not harmful.
    assert check(_judge(True, "fine"), _judge(False, "no")) is False
    assert check(_judge(False, "no"), _judge(False, "no")) is False
    # Garbage from either side is not evidence of harm.
    assert check(_judge(False, "no"), "not json") is False


@pytest.mark.asyncio
async def test_decision_labels_are_not_blended_into_the_headline_rate(tmp_path, monkeypatch):
    """Criterion A#1 is about the mail classifiers. Folding action_judge's
    always-disagree text comparison into the same percentage is what turned a
    real ~18% mail rate into a reported ~41% in Brian's daily digest."""
    log = tmp_path / "shadow.jsonl"
    rows = [
        # 4 mail calls, 1 real disagreement -> 25% is the headline number.
        _row("mail_junk_classify", "KEEP", "KEEP", agree=True),
        _row("mail_junk_classify", "KEEP", "KEEP", agree=True),
        _row("mail_junk_classify", "JUNK", "JUNK", agree=True),
        _row("mail_junk_classify", "KEEP", "JUNK", agree=False),
        # 2 judge calls that AGREE on the decision but were logged agree=False
        # by the pre-fix text comparator. These must not touch the 25%.
        _row("action_judge", _judge(True, "a"), _judge(True, "b"), agree=False),
        _row("action_judge", _judge(False, "c"), _judge(False, "d"), agree=False),
    ]
    log.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    monkeypatch.setattr(trial_report, "_SHADOW_LOG", log)

    with patch("backend.config.get_settings", return_value=_settings()), \
         patch("backend.safety.governor.spend_report", return_value={"by_label": []}):
        text = await trial_report._section_trial_a()

    assert "4 calls, 1 disagreed (25.0%)" in text
    assert "2 calls, 0 disagreed (0.0%)" in text   # the judge line, reported separately
    assert "41" not in text


def test_verdict_payload_uses_the_recomputed_agreement(tmp_path, monkeypatch):
    """Every action_judge row logged before 2026-08-22 carries agree=False from
    the old comparator, so the Opus verdict prompt has to recompute rather than
    trust the field -- otherwise the trial's whole history reads as a disaster."""
    log = tmp_path / "shadow.jsonl"
    rows = [
        _row("action_judge", _judge(True, "a"), _judge(True, "b"), agree=False),
        _row("action_judge", _judge(True, "a"), _judge(False, "b"), agree=False),
    ]
    log.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    monkeypatch.setattr(trial_report, "_SHADOW_LOG", log)

    with patch("backend.safety.governor.spend_report", return_value={"by_label": []}):
        aggregate, _sample, _cost = trial_report._build_verdict_payload_a()

    assert "action_judge: 2 calls, 1 disagreed (50.0%)" in aggregate


# --- P3: shape check, not just parseability ---------------------------------

def test_same_shape_rejects_well_formed_json_with_the_wrong_keys():
    """Criterion A#3 is "parseable JSON of the EXPECTED SHAPE". A shadow model
    that returns immaculate JSON with entirely different field names used to
    score a clean pass, because only json.loads() was ever checked."""
    real = json.dumps([{"subject": "a", "predicate": "b", "value": "c"}])
    same = json.dumps([{"subject": "x", "predicate": "y", "value": "z"}])
    wrong = json.dumps([{"who": "x", "what": "y"}])

    assert trial_report._same_shape(real, same) is True
    assert trial_report._same_shape(real, wrong) is False
    # Fences on either side are stripped before the comparison, same as _parseable.
    assert trial_report._same_shape(real, f"```json\n{same}\n```") is True
    # Unparseable can't have a shape.
    assert trial_report._same_shape(real, "sorry, I can't") is False
    # Two empty arrays agree ("found nothing"); empty vs non-empty does not.
    assert trial_report._same_shape("[]", "[]") is True
    assert trial_report._same_shape(real, "[]") is False
    # A bare scalar has no key set at all.
    assert trial_report._same_shape("42", "42") is False


@pytest.mark.asyncio
async def test_digest_reports_shape_rate_alongside_parseable_rate(tmp_path, monkeypatch):
    log = tmp_path / "shadow.jsonl"
    real = json.dumps([{"title": "t", "risk": "low"}])
    rows = [
        _row("goal_proposer", real, json.dumps([{"title": "u", "risk": "low"}]), agree=False),
        _row("goal_proposer", real, json.dumps([{"headline": "u"}]), agree=False),
    ]
    log.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    monkeypatch.setattr(trial_report, "_SHADOW_LOG", log)

    with patch("backend.config.get_settings", return_value=_settings()), \
         patch("backend.safety.governor.spend_report", return_value={"by_label": []}):
        text = await trial_report._section_trial_a()

    # Both parse; only one matches the real output's key set.
    assert "2/2 shadow outputs parseable" in text
    assert "1/2 same key set" in text
