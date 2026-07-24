"""Tests for backend/agents/mail_drafts.py — voice profile (Task 4) and the
scheduled auto-draft job (Task 5)."""

import json
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlmodel import Session, SQLModel, create_engine
from sqlalchemy.pool import StaticPool

import backend.database  # noqa: F401,E402 — registers all tables on metadata


def _make_engine():
    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(eng)
    return eng


# ---------------------------------------------------------------------------
# Voice profile (Task 4)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_voice_profile_fresh_cache_short_circuits(monkeypatch):
    eng = _make_engine()
    monkeypatch.setattr("backend.database.engine", eng)
    from backend.database import MailVoiceProfile
    with Session(eng) as s:
        s.add(MailVoiceProfile(id=1, summary="Cached voice.", sample_count=5, updated_at=datetime.utcnow()))
        s.commit()

    with patch("backend.agents.router.sonnet", new_callable=AsyncMock) as mock_sonnet, \
         patch("backend.integrations.protonmail.list_recent", new_callable=AsyncMock) as mock_list:
        from backend.agents.mail_drafts import get_voice_profile
        result = await get_voice_profile()

    assert result == "Cached voice."
    mock_sonnet.assert_not_awaited()
    mock_list.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_voice_profile_stale_triggers_rebuild(monkeypatch):
    eng = _make_engine()
    monkeypatch.setattr("backend.database.engine", eng)
    from backend.database import MailVoiceProfile
    stale_time = datetime.utcnow() - timedelta(days=30)
    with Session(eng) as s:
        s.add(MailVoiceProfile(id=1, summary="Old voice.", sample_count=3, updated_at=stale_time))
        s.commit()

    list_json = json.dumps({"emails": [{"email_id": "1", "subject": "Hi"}]})
    content_json = json.dumps({"emails": [{"body": "Some sent email body long enough to count."}]})

    with patch("backend.integrations.protonmail.list_recent", new_callable=AsyncMock, return_value=list_json), \
         patch("backend.integrations.protonmail.read_email", new_callable=AsyncMock, return_value=content_json) as mock_read, \
         patch("backend.agents.router.sonnet", new_callable=AsyncMock, return_value="New distilled voice.") as mock_sonnet:
        from backend.agents.mail_drafts import get_voice_profile
        result = await get_voice_profile()

    assert result == "New distilled voice."
    mock_sonnet.assert_awaited_once()
    mock_read.assert_awaited_once_with("1", mailbox="Sent")
    _, kwargs = mock_sonnet.call_args
    assert kwargs.get("label") == "mail_voice_distill"

    with Session(eng) as s:
        row = s.get(MailVoiceProfile, 1)
        assert row.summary == "New distilled voice."
        assert row.sample_count == 1


@pytest.mark.asyncio
async def test_get_voice_profile_rebuild_failure_falls_back_to_stale(monkeypatch):
    eng = _make_engine()
    monkeypatch.setattr("backend.database.engine", eng)
    from backend.database import MailVoiceProfile
    stale_time = datetime.utcnow() - timedelta(days=30)
    with Session(eng) as s:
        s.add(MailVoiceProfile(id=1, summary="Stale but usable.", sample_count=2, updated_at=stale_time))
        s.commit()

    with patch("backend.integrations.protonmail.list_recent",
               new_callable=AsyncMock, side_effect=RuntimeError("mcp down")):
        from backend.agents.mail_drafts import get_voice_profile
        result = await get_voice_profile()

    assert result == "Stale but usable."


@pytest.mark.asyncio
async def test_get_voice_profile_no_row_falls_back_to_default(monkeypatch):
    eng = _make_engine()
    monkeypatch.setattr("backend.database.engine", eng)

    with patch("backend.integrations.protonmail.list_recent",
               new_callable=AsyncMock, side_effect=RuntimeError("mcp down")):
        from backend.agents.mail_drafts import get_voice_profile, DEFAULT_VOICE
        result = await get_voice_profile()

    assert result == DEFAULT_VOICE


@pytest.mark.asyncio
async def test_rebuild_voice_profile_upsert_idempotent(monkeypatch):
    eng = _make_engine()
    monkeypatch.setattr("backend.database.engine", eng)
    from backend.database import MailVoiceProfile

    list_json = json.dumps({"emails": [{"email_id": "1", "subject": "Hi"}]})
    content_json = json.dumps({"emails": [{"body": "Some sent email body long enough to count."}]})

    with patch("backend.integrations.protonmail.list_recent", new_callable=AsyncMock, return_value=list_json), \
         patch("backend.integrations.protonmail.read_email", new_callable=AsyncMock, return_value=content_json), \
         patch("backend.agents.router.sonnet", new_callable=AsyncMock, side_effect=["First.", "Second."]):
        from backend.agents.mail_drafts import _rebuild_voice_profile
        await _rebuild_voice_profile()
        await _rebuild_voice_profile()

    with Session(eng) as s:
        rows = s.exec(__import__("sqlmodel").select(MailVoiceProfile)).all()
        assert len(rows) == 1
        assert rows[0].summary == "Second."


@pytest.mark.asyncio
async def test_compose_reply_prompt_contains_voice_summary():
    with patch("backend.agents.router.sonnet", new_callable=AsyncMock, return_value="Reply body.") as mock_sonnet:
        from backend.agents.mail_drafts import compose_reply
        result = await compose_reply("a@b.com", "Hi", "Can you help?", "Concise and warm.")

    assert result == "Reply body."
    args, kwargs = mock_sonnet.call_args
    assert "Concise and warm." in args[0]
    assert kwargs.get("label") == "mail_reply_draft"


# ---------------------------------------------------------------------------
# Junk profile
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_junk_profile_fresh_cache_short_circuits(monkeypatch):
    eng = _make_engine()
    monkeypatch.setattr("backend.database.engine", eng)
    from backend.database import MailJunkProfile
    with Session(eng) as s:
        s.add(MailJunkProfile(id=1, summary="Cached junk profile.", sample_count=50, updated_at=datetime.utcnow()))
        s.commit()

    with patch("backend.agents.router.sonnet", new_callable=AsyncMock) as mock_sonnet, \
         patch("backend.integrations.protonmail.list_recent", new_callable=AsyncMock) as mock_list:
        from backend.agents.mail_drafts import get_junk_profile
        result = await get_junk_profile()

    assert result == "Cached junk profile."
    mock_sonnet.assert_not_awaited()
    mock_list.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_junk_profile_stale_triggers_rebuild(monkeypatch):
    eng = _make_engine()
    monkeypatch.setattr("backend.database.engine", eng)
    from backend.database import MailJunkProfile
    stale_time = datetime.utcnow() - timedelta(days=60)
    with Session(eng) as s:
        s.add(MailJunkProfile(id=1, summary="Old junk profile.", sample_count=10, updated_at=stale_time))
        s.commit()

    trash_json = json.dumps({"emails": [
        {"sender": "promo@example.com", "subject": "Big sale!"},
        {"sender": "", "subject": "no sender, skipped"},
    ]})

    with patch("backend.integrations.protonmail.list_recent", new_callable=AsyncMock, return_value=trash_json) as mock_list, \
         patch("backend.agents.router.sonnet", new_callable=AsyncMock, return_value="New junk profile.") as mock_sonnet:
        from backend.agents.mail_drafts import get_junk_profile
        result = await get_junk_profile()

    assert result == "New junk profile."
    mock_list.assert_awaited_once_with(mailbox="Trash", limit=50)
    _, kwargs = mock_sonnet.call_args
    assert kwargs.get("label") == "mail_junk_distill"

    with Session(eng) as s:
        row = s.get(MailJunkProfile, 1)
        assert row.summary == "New junk profile."
        assert row.sample_count == 1  # blank-sender line skipped


@pytest.mark.asyncio
async def test_get_junk_profile_rebuild_failure_falls_back_to_stale(monkeypatch):
    eng = _make_engine()
    monkeypatch.setattr("backend.database.engine", eng)
    from backend.database import MailJunkProfile
    stale_time = datetime.utcnow() - timedelta(days=60)
    with Session(eng) as s:
        s.add(MailJunkProfile(id=1, summary="Stale but usable.", sample_count=20, updated_at=stale_time))
        s.commit()

    with patch("backend.integrations.protonmail.list_recent",
               new_callable=AsyncMock, side_effect=RuntimeError("mcp down")):
        from backend.agents.mail_drafts import get_junk_profile
        result = await get_junk_profile()

    assert result == "Stale but usable."


@pytest.mark.asyncio
async def test_get_junk_profile_no_row_returns_none(monkeypatch):
    eng = _make_engine()
    monkeypatch.setattr("backend.database.engine", eng)

    with patch("backend.integrations.protonmail.list_recent",
               new_callable=AsyncMock, side_effect=RuntimeError("mcp down")):
        from backend.agents.mail_drafts import get_junk_profile
        result = await get_junk_profile()

    assert result is None


@pytest.mark.asyncio
async def test_rebuild_junk_profile_upsert_idempotent(monkeypatch):
    eng = _make_engine()
    monkeypatch.setattr("backend.database.engine", eng)
    from backend.database import MailJunkProfile

    trash_json = json.dumps({"emails": [{"sender": "promo@example.com", "subject": "Sale"}]})

    with patch("backend.integrations.protonmail.list_recent", new_callable=AsyncMock, return_value=trash_json), \
         patch("backend.agents.router.sonnet", new_callable=AsyncMock, side_effect=["First.", "Second."]):
        from backend.agents.mail_drafts import _rebuild_junk_profile
        await _rebuild_junk_profile()
        await _rebuild_junk_profile()

    with Session(eng) as s:
        rows = s.exec(__import__("sqlmodel").select(MailJunkProfile)).all()
        assert len(rows) == 1
        assert rows[0].summary == "Second."


def test_db_drafted_email_ids_returns_only_drafted_within_window(monkeypatch):
    eng = _make_engine()
    monkeypatch.setattr("backend.database.engine", eng)
    from backend.database import ProcessedMailId

    now = datetime.utcnow()
    with Session(eng) as s:
        s.add(ProcessedMailId(email_id="drafted-fresh", drafted=True, processed_at=now))
        s.add(ProcessedMailId(email_id="drafted-stale", drafted=True, processed_at=now - timedelta(days=30)))
        s.add(ProcessedMailId(email_id="skipped", drafted=False, processed_at=now))
        s.commit()

    from backend.agents.mail_drafts import _db_drafted_email_ids
    result = _db_drafted_email_ids(within_days=14)
    assert result == {"drafted-fresh"}


def test_db_drafted_email_ids_empty_when_no_rows(monkeypatch):
    eng = _make_engine()
    monkeypatch.setattr("backend.database.engine", eng)
    from backend.agents.mail_drafts import _db_drafted_email_ids
    assert _db_drafted_email_ids() == set()


# ---------------------------------------------------------------------------
# Pre-filter + classifier (Task 5)
# ---------------------------------------------------------------------------

def test_is_automated_sender_positive_cases():
    from backend.agents.mail_drafts import _is_automated_sender
    assert _is_automated_sender("no-reply@notify.proton.me")
    assert _is_automated_sender('"CVS ExtraCare" <extracare_x@simplelogin.co>')
    assert _is_automated_sender("newsletter@example.com")


def test_is_automated_sender_negative_cases():
    from backend.agents.mail_drafts import _is_automated_sender
    assert not _is_automated_sender('"Jane Doe" <jane@example.com>')
    assert not _is_automated_sender("bob@company.com")


@pytest.mark.asyncio
async def test_warrants_reply_default_deny_on_garbage():
    with patch("backend.agents.router.haiku", new_callable=AsyncMock, return_value="whatever nonsense"):
        from backend.agents.mail_drafts import _warrants_reply
        assert await _warrants_reply("a@b.com", "hi", "body") is False


@pytest.mark.asyncio
async def test_warrants_reply_default_deny_on_exception():
    with patch("backend.agents.router.haiku", new_callable=AsyncMock, side_effect=RuntimeError("down")):
        from backend.agents.mail_drafts import _warrants_reply
        assert await _warrants_reply("a@b.com", "hi", "body") is False


@pytest.mark.asyncio
async def test_warrants_reply_true_on_exact_reply_token():
    with patch("backend.agents.router.haiku", new_callable=AsyncMock, return_value="REPLY"):
        from backend.agents.mail_drafts import _warrants_reply
        assert await _warrants_reply("a@b.com", "hi", "body") is True


@pytest.mark.asyncio
async def test_is_junk_default_deny_on_garbage():
    with patch("backend.agents.router.haiku", new_callable=AsyncMock, return_value="whatever nonsense"):
        from backend.agents.mail_drafts import _is_junk
        assert await _is_junk("promo@example.com", "Sale!", "profile text") is False


@pytest.mark.asyncio
async def test_is_junk_default_deny_on_exception():
    with patch("backend.agents.router.haiku", new_callable=AsyncMock, side_effect=RuntimeError("down")):
        from backend.agents.mail_drafts import _is_junk
        assert await _is_junk("promo@example.com", "Sale!", "profile text") is False


@pytest.mark.asyncio
async def test_is_junk_true_on_trash_token():
    with patch("backend.agents.router.haiku", new_callable=AsyncMock, return_value="TRASH"):
        from backend.agents.mail_drafts import _is_junk
        assert await _is_junk("promo@example.com", "Sale!", "profile text") is True


# ---------------------------------------------------------------------------
# Full tick integration
# ---------------------------------------------------------------------------

_INBOX_JSON = json.dumps({
    "emails": [
        {"email_id": "1", "subject": "$4 Coupon!", "sender": '"CVS" <cvs@simplelogin.co>'},
        {"email_id": "2", "subject": "Can you help with the report?", "sender": '"Jane Doe" <jane@example.com>'},
    ],
})


@pytest.mark.asyncio
async def test_autodraft_tick_drafts_real_correspondence_skips_promo(monkeypatch):
    eng = _make_engine()
    monkeypatch.setattr("backend.database.engine", eng)

    content_json = json.dumps({"emails": [{"body": "Can you send the report?", "message_id": "<m1>"}]})

    with patch("backend.integrations.protonmail.list_recent", new_callable=AsyncMock, return_value=_INBOX_JSON), \
         patch("backend.integrations.protonmail.read_email", new_callable=AsyncMock, return_value=content_json), \
         patch("backend.integrations.protonmail.save_draft", new_callable=AsyncMock, return_value={"ok": True}) as mock_save, \
         patch("backend.integrations.protonmail.send_email", new_callable=AsyncMock) as mock_send, \
         patch("backend.agents.mail_drafts._warrants_reply", new_callable=AsyncMock, return_value=True) as mock_warrants, \
         patch("backend.agents.mail_drafts.get_voice_profile", new_callable=AsyncMock, return_value="Voice."), \
         patch("backend.agents.mail_drafts.compose_reply", new_callable=AsyncMock, return_value="A reply."), \
         patch("backend.agents.mail_drafts.get_junk_profile", new_callable=AsyncMock, return_value=None), \
         patch("backend.events.notify_phone", new_callable=AsyncMock, return_value=True) as mock_notify:
        from backend.agents.mail_drafts import autodraft_tick
        await autodraft_tick()

    # Only the real-correspondence email (id=2) is classified/drafted; CVS promo is filtered pre-LLM.
    mock_warrants.assert_awaited_once()
    mock_save.assert_awaited_once()
    _, kwargs = mock_save.call_args
    assert kwargs["recipients"] == ["jane@example.com"]
    assert kwargs["in_reply_to"] == "<m1>"
    mock_notify.assert_awaited_once()
    _, notify_kwargs = mock_notify.call_args
    assert notify_kwargs.get("kind") == "mail_draft_created"
    mock_send.assert_not_awaited()

    from backend.database import ProcessedMailId
    with Session(eng) as s:
        rows = {r.email_id: r for r in s.exec(__import__("sqlmodel").select(ProcessedMailId)).all()}
        assert rows["1"].drafted is False   # CVS promo: claimed, never drafted
        assert rows["2"].drafted is True    # real correspondence: drafted


@pytest.mark.asyncio
async def test_autodraft_tick_dedup_on_second_tick(monkeypatch):
    eng = _make_engine()
    monkeypatch.setattr("backend.database.engine", eng)

    content_json = json.dumps({"emails": [{"body": "Can you send the report?", "message_id": "<m1>"}]})

    with patch("backend.integrations.protonmail.list_recent", new_callable=AsyncMock, return_value=_INBOX_JSON), \
         patch("backend.integrations.protonmail.read_email", new_callable=AsyncMock, return_value=content_json) as mock_read, \
         patch("backend.integrations.protonmail.save_draft", new_callable=AsyncMock, return_value={"ok": True}), \
         patch("backend.agents.mail_drafts._warrants_reply", new_callable=AsyncMock, return_value=True), \
         patch("backend.agents.mail_drafts.get_voice_profile", new_callable=AsyncMock, return_value="Voice."), \
         patch("backend.agents.mail_drafts.compose_reply", new_callable=AsyncMock, return_value="A reply."), \
         patch("backend.agents.mail_drafts.get_junk_profile", new_callable=AsyncMock, return_value=None), \
         patch("backend.events.notify_phone", new_callable=AsyncMock, return_value=True):
        from backend.agents.mail_drafts import autodraft_tick
        await autodraft_tick()
        await autodraft_tick()

    # Second tick: both emails already processed -> read_email never called again.
    assert mock_read.await_count == 1


@pytest.mark.asyncio
async def test_autodraft_tick_per_email_fault_isolation(monkeypatch):
    eng = _make_engine()
    monkeypatch.setattr("backend.database.engine", eng)

    two_real = json.dumps({
        "emails": [
            {"email_id": "10", "subject": "Question one", "sender": '"A" <a@example.com>'},
            {"email_id": "11", "subject": "Question two", "sender": '"B" <b@example.com>'},
        ],
    })
    content_json = json.dumps({"emails": [{"body": "body", "message_id": "<m>"}]})

    with patch("backend.integrations.protonmail.list_recent", new_callable=AsyncMock, return_value=two_real), \
         patch("backend.integrations.protonmail.read_email",
               new_callable=AsyncMock, side_effect=[RuntimeError("boom"), content_json]), \
         patch("backend.integrations.protonmail.save_draft", new_callable=AsyncMock, return_value={"ok": True}) as mock_save, \
         patch("backend.agents.mail_drafts._warrants_reply", new_callable=AsyncMock, return_value=True), \
         patch("backend.agents.mail_drafts.get_voice_profile", new_callable=AsyncMock, return_value="Voice."), \
         patch("backend.agents.mail_drafts.compose_reply", new_callable=AsyncMock, return_value="A reply."), \
         patch("backend.events.notify_phone", new_callable=AsyncMock, return_value=True):
        from backend.agents.mail_drafts import autodraft_tick
        await autodraft_tick()

    # First email's read_email blew up -- second email still gets processed.
    mock_save.assert_awaited_once()


@pytest.mark.asyncio
async def test_autodraft_tick_skips_when_kill_switch_off(monkeypatch):
    eng = _make_engine()
    monkeypatch.setattr("backend.database.engine", eng)
    from backend.database import SystemState
    with Session(eng) as s:
        s.add(SystemState(id=1, autonomy_enabled=False))
        s.commit()

    with patch("backend.integrations.protonmail.list_recent", new_callable=AsyncMock) as mock_list:
        from backend.agents.mail_drafts import autodraft_tick
        await autodraft_tick()

    mock_list.assert_not_awaited()


@pytest.mark.asyncio
async def test_autodraft_tick_runs_when_kill_switch_on(monkeypatch):
    eng = _make_engine()
    monkeypatch.setattr("backend.database.engine", eng)
    from backend.database import SystemState
    with Session(eng) as s:
        s.add(SystemState(id=1, autonomy_enabled=True))
        s.commit()

    with patch("backend.integrations.protonmail.list_recent",
               new_callable=AsyncMock, return_value='{"emails": []}') as mock_list:
        from backend.agents.mail_drafts import autodraft_tick
        await autodraft_tick()

    mock_list.assert_awaited_once()


# ---------------------------------------------------------------------------
# Full tick integration — learned auto-junk-cleanup
# ---------------------------------------------------------------------------

_JUNK_AND_HUMAN_JSON = json.dumps({
    "emails": [
        {"email_id": "j1", "subject": "Big Sale!", "sender": '"Promo" <promo@simplelogin.co>'},
        {"email_id": "h1", "subject": "Can you help?", "sender": '"Jane Doe" <jane@example.com>'},
    ],
})


@pytest.mark.asyncio
async def test_autodraft_tick_trashes_profiled_junk(monkeypatch):
    eng = _make_engine()
    monkeypatch.setattr("backend.database.engine", eng)
    from backend.safety.broker import Decision

    content_json = json.dumps({"emails": [{"body": "Can you send the report?", "message_id": "<m1>"}]})
    executed_res = MagicMock(decision=Decision.EXECUTED)

    with patch("backend.integrations.protonmail.list_recent", new_callable=AsyncMock, return_value=_JUNK_AND_HUMAN_JSON), \
         patch("backend.integrations.protonmail.read_email", new_callable=AsyncMock, return_value=content_json), \
         patch("backend.integrations.protonmail.save_draft", new_callable=AsyncMock, return_value={"ok": True}), \
         patch("backend.agents.mail_drafts._warrants_reply", new_callable=AsyncMock, return_value=True), \
         patch("backend.agents.mail_drafts.get_voice_profile", new_callable=AsyncMock, return_value="Voice."), \
         patch("backend.agents.mail_drafts.compose_reply", new_callable=AsyncMock, return_value="A reply."), \
         patch("backend.agents.mail_drafts.get_junk_profile", new_callable=AsyncMock, return_value="Junk profile."), \
         patch("backend.agents.mail_drafts._is_junk", new_callable=AsyncMock, return_value=True), \
         patch("backend.safety.broker.execute_action", new_callable=AsyncMock, return_value=executed_res) as mock_exec, \
         patch("backend.events.notify_phone", new_callable=AsyncMock, return_value=True):
        from backend.agents.mail_drafts import autodraft_tick
        await autodraft_tick()

    mock_exec.assert_awaited_once()
    _, kwargs = mock_exec.call_args
    assert kwargs["actor"] == "autonomous"
    assert kwargs["kind"] == "protonmail_delete"
    assert kwargs["payload"] == {"email_id": "j1"}

    from backend.database import ProcessedMailId
    with Session(eng) as s:
        rows = {r.email_id: r for r in s.exec(__import__("sqlmodel").select(ProcessedMailId)).all()}
        assert rows["j1"].trashed is True
        assert rows["j1"].drafted is False
        assert rows["h1"].drafted is True   # human sender: unaffected, still drafts
        assert rows["h1"].trashed is False


@pytest.mark.asyncio
async def test_autodraft_tick_keeps_automated_nonjunk(monkeypatch):
    eng = _make_engine()
    monkeypatch.setattr("backend.database.engine", eng)

    with patch("backend.integrations.protonmail.list_recent", new_callable=AsyncMock, return_value=_JUNK_AND_HUMAN_JSON), \
         patch("backend.integrations.protonmail.read_email", new_callable=AsyncMock, return_value=json.dumps({"emails": [{"body": "b"}]})), \
         patch("backend.agents.mail_drafts._warrants_reply", new_callable=AsyncMock, return_value=False), \
         patch("backend.agents.mail_drafts.get_junk_profile", new_callable=AsyncMock, return_value="Junk profile."), \
         patch("backend.agents.mail_drafts._is_junk", new_callable=AsyncMock, return_value=False), \
         patch("backend.safety.broker.execute_action", new_callable=AsyncMock) as mock_exec:
        from backend.agents.mail_drafts import autodraft_tick
        await autodraft_tick()

    mock_exec.assert_not_awaited()
    from backend.database import ProcessedMailId
    with Session(eng) as s:
        row = s.exec(__import__("sqlmodel").select(ProcessedMailId).where(ProcessedMailId.email_id == "j1")).first()
        assert row.trashed is False


@pytest.mark.asyncio
async def test_autodraft_tick_no_profile_no_trash(monkeypatch):
    eng = _make_engine()
    monkeypatch.setattr("backend.database.engine", eng)

    with patch("backend.integrations.protonmail.list_recent", new_callable=AsyncMock, return_value=_JUNK_AND_HUMAN_JSON), \
         patch("backend.integrations.protonmail.read_email", new_callable=AsyncMock, return_value=json.dumps({"emails": [{"body": "b"}]})), \
         patch("backend.agents.mail_drafts._warrants_reply", new_callable=AsyncMock, return_value=False), \
         patch("backend.agents.mail_drafts.get_junk_profile", new_callable=AsyncMock, return_value=None), \
         patch("backend.agents.mail_drafts._is_junk", new_callable=AsyncMock) as mock_is_junk, \
         patch("backend.safety.broker.execute_action", new_callable=AsyncMock) as mock_exec:
        from backend.agents.mail_drafts import autodraft_tick
        await autodraft_tick()

    mock_is_junk.assert_not_awaited()
    mock_exec.assert_not_awaited()
    from backend.database import ProcessedMailId
    with Session(eng) as s:
        row = s.exec(__import__("sqlmodel").select(ProcessedMailId).where(ProcessedMailId.email_id == "j1")).first()
        assert row.trashed is False


@pytest.mark.asyncio
async def test_autodraft_tick_autotrash_disabled_flag(monkeypatch):
    eng = _make_engine()
    monkeypatch.setattr("backend.database.engine", eng)
    from backend.config import Settings
    fake_settings = Settings(mail_autotrash_enabled=False)
    monkeypatch.setattr("backend.config.get_settings", lambda: fake_settings)

    with patch("backend.integrations.protonmail.list_recent", new_callable=AsyncMock, return_value=_JUNK_AND_HUMAN_JSON), \
         patch("backend.integrations.protonmail.read_email", new_callable=AsyncMock, return_value=json.dumps({"emails": [{"body": "b"}]})), \
         patch("backend.agents.mail_drafts._warrants_reply", new_callable=AsyncMock, return_value=False), \
         patch("backend.agents.mail_drafts.get_junk_profile", new_callable=AsyncMock) as mock_get_profile, \
         patch("backend.safety.broker.execute_action", new_callable=AsyncMock) as mock_exec:
        from backend.agents.mail_drafts import autodraft_tick
        await autodraft_tick()

    mock_get_profile.assert_not_awaited()
    mock_exec.assert_not_awaited()


@pytest.mark.asyncio
async def test_autodraft_tick_trash_cap_leaves_unclaimed(monkeypatch):
    eng = _make_engine()
    monkeypatch.setattr("backend.database.engine", eng)
    from backend.safety.broker import Decision

    six_junk = json.dumps({
        "emails": [
            {"email_id": f"j{i}", "subject": "Sale", "sender": '"Promo" <promo@simplelogin.co>'}
            for i in range(6)
        ],
    })
    executed_res = MagicMock(decision=Decision.EXECUTED)

    with patch("backend.integrations.protonmail.list_recent", new_callable=AsyncMock, return_value=six_junk), \
         patch("backend.agents.mail_drafts.get_junk_profile", new_callable=AsyncMock, return_value="Junk profile."), \
         patch("backend.agents.mail_drafts._is_junk", new_callable=AsyncMock, return_value=True), \
         patch("backend.safety.broker.execute_action", new_callable=AsyncMock, return_value=executed_res) as mock_exec:
        from backend.agents.mail_drafts import autodraft_tick
        await autodraft_tick()

    assert mock_exec.await_count == 5  # MAX_TRASHED_PER_TICK
    from backend.database import ProcessedMailId
    with Session(eng) as s:
        rows = {r.email_id for r in s.exec(__import__("sqlmodel").select(ProcessedMailId)).all()}
        assert "j5" not in rows  # 6th candidate left unclaimed for next tick


@pytest.mark.asyncio
async def test_autodraft_tick_trash_dispatch_failure_leaves_mail(monkeypatch):
    eng = _make_engine()
    monkeypatch.setattr("backend.database.engine", eng)
    from backend.safety.broker import Decision

    failed_res = MagicMock(decision=Decision.FAILED)

    with patch("backend.integrations.protonmail.list_recent", new_callable=AsyncMock, return_value=_JUNK_AND_HUMAN_JSON), \
         patch("backend.integrations.protonmail.read_email", new_callable=AsyncMock, return_value=json.dumps({"emails": [{"body": "b"}]})), \
         patch("backend.agents.mail_drafts._warrants_reply", new_callable=AsyncMock, return_value=False), \
         patch("backend.agents.mail_drafts.get_junk_profile", new_callable=AsyncMock, return_value="Junk profile."), \
         patch("backend.agents.mail_drafts._is_junk", new_callable=AsyncMock, return_value=True), \
         patch("backend.safety.broker.execute_action", new_callable=AsyncMock, return_value=failed_res):
        from backend.agents.mail_drafts import autodraft_tick
        await autodraft_tick()  # must not raise

    from backend.database import ProcessedMailId
    with Session(eng) as s:
        row = s.exec(__import__("sqlmodel").select(ProcessedMailId).where(ProcessedMailId.email_id == "j1")).first()
        assert row is not None
        assert row.trashed is False


# ---------------------------------------------------------------------------
# Scheduler registration
# ---------------------------------------------------------------------------

def test_mail_autodraft_job_registered_when_enabled(monkeypatch):
    from backend.config import Settings
    fake_settings = Settings(mail_autodraft_enabled=True, mail_autodraft_interval_minutes=15)
    monkeypatch.setattr("backend.config.get_settings", lambda: fake_settings)

    import backend.scheduler as scheduler_mod
    mock_sched = MagicMock()
    monkeypatch.setattr(scheduler_mod, "scheduler", mock_sched)

    scheduler_mod.setup_scheduler("07:00", "America/Detroit")

    job_ids = [c.kwargs.get("id") for c in mock_sched.add_job.call_args_list]
    assert "mail_autodraft" in job_ids


def test_mail_drafts_module_never_references_send_email():
    """Hard safety invariant: this module drafts, never sends."""
    import inspect
    from backend.agents import mail_drafts
    src = inspect.getsource(mail_drafts)
    assert "send_email" not in src


def test_mail_autodraft_job_absent_when_disabled(monkeypatch):
    from backend.config import Settings
    fake_settings = Settings(mail_autodraft_enabled=False)
    monkeypatch.setattr("backend.config.get_settings", lambda: fake_settings)

    import backend.scheduler as scheduler_mod
    mock_sched = MagicMock()
    monkeypatch.setattr(scheduler_mod, "scheduler", mock_sched)

    scheduler_mod.setup_scheduler("07:00", "America/Detroit")

    job_ids = [c.kwargs.get("id") for c in mock_sched.add_job.call_args_list]
    assert "mail_autodraft" not in job_ids
