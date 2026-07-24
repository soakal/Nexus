import pytest
from unittest.mock import AsyncMock, patch

from backend.config import get_settings

# Tests assert against whatever account is actually configured (real value via
# the gitignored .env on this machine, or the placeholder default otherwise) —
# never hardcode the real account name in this public repo's test source.
_ACCOUNT = get_settings().protonmail_account


@pytest.mark.asyncio
async def test_health_check_true_when_account_present():
    with patch(
        "backend.integrations.protonmail._call_tool",
        AsyncMock(return_value=f'{{"result": [{{"account_name": "{_ACCOUNT}"}}]}}'),
    ) as mock_call:
        from backend.integrations.protonmail import health_check
        assert await health_check() is True
        mock_call.assert_awaited_once_with("list_available_accounts", {})


@pytest.mark.asyncio
async def test_health_check_false_on_raise():
    with patch(
        "backend.integrations.protonmail._call_tool",
        AsyncMock(side_effect=RuntimeError("transport down")),
    ):
        from backend.integrations.protonmail import health_check
        assert await health_check() is False


@pytest.mark.asyncio
async def test_list_recent_unread_only_passes_seen_false():
    with patch(
        "backend.integrations.protonmail._call_tool", AsyncMock(return_value="[]")
    ) as mock_call:
        from backend.integrations.protonmail import list_recent
        await list_recent(unread_only=True, limit=5)
        name, args = mock_call.call_args.args
        assert name == "list_emails_metadata"
        assert args["seen"] is False
        assert args["page_size"] == 5
        assert args["account_name"] == _ACCOUNT
        assert args["order"] == "desc"


@pytest.mark.asyncio
async def test_list_recent_defaults_do_not_set_seen():
    with patch(
        "backend.integrations.protonmail._call_tool", AsyncMock(return_value="[]")
    ) as mock_call:
        from backend.integrations.protonmail import list_recent
        await list_recent()
        _, args = mock_call.call_args.args
        assert "seen" not in args


@pytest.mark.asyncio
async def test_read_email_passes_email_id_and_page():
    with patch(
        "backend.integrations.protonmail._call_tool", AsyncMock(return_value="body text")
    ) as mock_call:
        from backend.integrations.protonmail import read_email
        result = await read_email("95", page=2)
        assert result == "body text"
        name, args = mock_call.call_args.args
        assert name == "get_emails_content"
        assert args["email_ids"] == ["95"]
        assert args["page"] == 2
        assert args["account_name"] == _ACCOUNT


@pytest.mark.asyncio
async def test_read_email_mailbox_passthrough():
    with patch(
        "backend.integrations.protonmail._call_tool", AsyncMock(return_value="{}")
    ) as mock_call:
        from backend.integrations.protonmail import read_email
        await read_email("1", mailbox="Sent")
        _, args = mock_call.call_args.args
        assert args["mailbox"] == "Sent"


@pytest.mark.asyncio
async def test_read_email_default_omits_mailbox():
    with patch(
        "backend.integrations.protonmail._call_tool", AsyncMock(return_value="{}")
    ) as mock_call:
        from backend.integrations.protonmail import read_email
        await read_email("1")
        _, args = mock_call.call_args.args
        assert "mailbox" not in args


@pytest.mark.asyncio
async def test_send_email_success_threads_reply_fields():
    with patch(
        "backend.integrations.protonmail._call_tool", AsyncMock(return_value="sent")
    ) as mock_call:
        from backend.integrations.protonmail import send_email
        result = await send_email(
            ["a@example.com"], "Re: hi", "body",
            in_reply_to="<msgid>", references="<msgid>",
        )
        assert result == {"ok": True, "detail": "sent"}
        name, args = mock_call.call_args.args
        assert name == "send_email"
        assert args["recipients"] == ["a@example.com"]
        assert args["subject"] == "Re: hi"
        assert args["in_reply_to"] == "<msgid>"
        assert args["references"] == "<msgid>"
        assert args["account_name"] == _ACCOUNT


@pytest.mark.asyncio
async def test_send_email_raises_integration_error_on_tool_error():
    from backend.integrations.protonmail import IntegrationError
    with patch(
        "backend.integrations.protonmail._call_tool",
        AsyncMock(side_effect=IntegrationError("send failed")),
    ):
        from backend.integrations.protonmail import send_email
        with pytest.raises(IntegrationError):
            await send_email(["a@example.com"], "s", "b")


@pytest.mark.asyncio
async def test_list_recent_mailbox_passthrough():
    with patch(
        "backend.integrations.protonmail._call_tool", AsyncMock(return_value="[]")
    ) as mock_call:
        from backend.integrations.protonmail import list_recent
        await list_recent(mailbox="Sent", limit=25)
        _, args = mock_call.call_args.args
        assert args["mailbox"] == "Sent"


@pytest.mark.asyncio
async def test_list_recent_default_omits_mailbox():
    with patch(
        "backend.integrations.protonmail._call_tool", AsyncMock(return_value="[]")
    ) as mock_call:
        from backend.integrations.protonmail import list_recent
        await list_recent()
        _, args = mock_call.call_args.args
        assert "mailbox" not in args


@pytest.mark.asyncio
async def test_save_draft_success_threads_reply_fields():
    with patch(
        "backend.integrations.protonmail._call_tool", AsyncMock(return_value="saved")
    ) as mock_call:
        from backend.integrations.protonmail import save_draft
        result = await save_draft(
            ["a@example.com"], "Re: hi", "body",
            in_reply_to="<msgid>", references="<msgid>",
        )
        assert result == {"ok": True, "detail": "saved"}
        name, args = mock_call.call_args.args
        assert name == "save_to_mailbox"
        assert args["recipients"] == ["a@example.com"]
        assert args["subject"] == "Re: hi"
        assert args["mailbox"] == "Drafts"
        assert args["in_reply_to"] == "<msgid>"
        assert args["references"] == "<msgid>"
        assert args["account_name"] == _ACCOUNT


@pytest.mark.asyncio
async def test_save_draft_raises_integration_error_on_tool_error():
    from backend.integrations.protonmail import IntegrationError
    with patch(
        "backend.integrations.protonmail._call_tool",
        AsyncMock(side_effect=IntegrationError("save failed")),
    ):
        from backend.integrations.protonmail import save_draft
        with pytest.raises(IntegrationError):
            await save_draft(["a@example.com"], "s", "b")


@pytest.mark.asyncio
async def test_archive_email_success():
    with patch(
        "backend.integrations.protonmail._call_tool", AsyncMock(return_value="archived")
    ) as mock_call:
        from backend.integrations.protonmail import archive_email
        result = await archive_email("1")
        assert result == {"ok": True, "detail": "archived"}
        name, args = mock_call.call_args.args
        assert name == "archive_emails"
        assert args["email_ids"] == ["1"]
        assert args["account_name"] == _ACCOUNT
        assert "mailbox" not in args


@pytest.mark.asyncio
async def test_archive_email_mailbox_passthrough():
    with patch(
        "backend.integrations.protonmail._call_tool", AsyncMock(return_value="archived")
    ) as mock_call:
        from backend.integrations.protonmail import archive_email
        await archive_email("1", mailbox="Drafts")
        _, args = mock_call.call_args.args
        assert args["mailbox"] == "Drafts"


@pytest.mark.asyncio
async def test_archive_email_raises_integration_error_on_tool_error():
    from backend.integrations.protonmail import IntegrationError
    with patch(
        "backend.integrations.protonmail._call_tool",
        AsyncMock(side_effect=IntegrationError("archive failed")),
    ):
        from backend.integrations.protonmail import archive_email
        with pytest.raises(IntegrationError):
            await archive_email("1")


@pytest.mark.asyncio
async def test_trash_email_calls_move_emails_with_trash_destination():
    with patch(
        "backend.integrations.protonmail._call_tool", AsyncMock(return_value="moved")
    ) as mock_call:
        from backend.integrations.protonmail import trash_email
        result = await trash_email("1")
        assert result == {"ok": True, "detail": "moved"}
        name, args = mock_call.call_args.args
        assert name == "move_emails"
        assert args["email_ids"] == ["1"]
        assert args["account_name"] == _ACCOUNT
        assert args["destination_mailbox"] == "Trash"
        assert "source_mailbox" not in args


@pytest.mark.asyncio
async def test_trash_email_mailbox_maps_to_source_mailbox():
    with patch(
        "backend.integrations.protonmail._call_tool", AsyncMock(return_value="moved")
    ) as mock_call:
        from backend.integrations.protonmail import trash_email
        await trash_email("1", mailbox="Archive")
        _, args = mock_call.call_args.args
        assert args["source_mailbox"] == "Archive"
        assert args["destination_mailbox"] == "Trash"


@pytest.mark.asyncio
async def test_trash_email_raises_integration_error_on_tool_error():
    from backend.integrations.protonmail import IntegrationError
    with patch(
        "backend.integrations.protonmail._call_tool",
        AsyncMock(side_effect=IntegrationError("move failed")),
    ):
        from backend.integrations.protonmail import trash_email
        with pytest.raises(IntegrationError):
            await trash_email("1")


def test_protonmail_module_never_calls_delete_emails():
    """Invariant: the hard-expunge MCP tool must never be called again — a test
    email deleted via it was verified (2026-07-23) to never reach the real
    Trash folder, unlike Brian's actual delete behavior. trash_email/move_emails
    is the only sanctioned 'delete' path."""
    import inspect
    from backend.integrations import protonmail
    src = inspect.getsource(protonmail)
    assert "delete_emails" not in src
    assert not hasattr(protonmail, "delete_email")
