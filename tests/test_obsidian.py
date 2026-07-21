import pytest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch


# ---------------------------------------------------------------------------
# fetch() — filesystem-backed (uses _vault() / pathlib)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_obsidian_fetch(tmp_path, monkeypatch):
    today = date.today().strftime("%Y-%m-%d")
    (tmp_path / "Brain" / "raw").mkdir(parents=True)
    (tmp_path / "Brain" / "raw" / f"{today}.md").write_text(
        "# Daily\n- [ ] Task 1\n- [ ] Task 2\n- [x] Done task", encoding="utf-8"
    )
    (tmp_path / "note1.md").write_text("a", encoding="utf-8")
    (tmp_path / "note2.md").write_text("b", encoding="utf-8")

    monkeypatch.setattr("backend.integrations.obsidian._vault", lambda: tmp_path)

    from backend.integrations.obsidian import fetch
    data = await fetch()

    assert len(data.recent_notes) > 0
    assert any(p.endswith(f"{today}.md") for p in data.recent_notes)
    assert len(data.open_tasks) == 2
    assert data.daily_note is not None


@pytest.mark.asyncio
async def test_obsidian_fetch_no_daily_note(tmp_path, monkeypatch):
    (tmp_path / "note1.md").write_text("hi", encoding="utf-8")

    monkeypatch.setattr("backend.integrations.obsidian._vault", lambda: tmp_path)

    from backend.integrations.obsidian import fetch
    data = await fetch()

    assert data.open_tasks == []
    assert data.daily_note is None
    assert "note1.md" in data.recent_notes


@pytest.mark.asyncio
async def test_obsidian_fetch_truncates_to_10_notes(tmp_path, monkeypatch):
    for i in range(20):
        (tmp_path / f"note{i:02d}.md").write_text("x", encoding="utf-8")

    monkeypatch.setattr("backend.integrations.obsidian._vault", lambda: tmp_path)

    from backend.integrations.obsidian import fetch
    data = await fetch()

    assert len(data.recent_notes) == 10


@pytest.mark.asyncio
async def test_obsidian_fetch_only_md_files(tmp_path, monkeypatch):
    (tmp_path / "note.md").write_text("x", encoding="utf-8")
    (tmp_path / "image.png").write_text("x", encoding="utf-8")
    (tmp_path / "attachment.pdf").write_text("x", encoding="utf-8")

    monkeypatch.setattr("backend.integrations.obsidian._vault", lambda: tmp_path)

    from backend.integrations.obsidian import fetch
    data = await fetch()

    assert all(f.endswith(".md") for f in data.recent_notes)
    assert len(data.recent_notes) == 1


# ---------------------------------------------------------------------------
# health_check() — httpx GET to /health (cached; invalidate before each test)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_obsidian_health_check_ok():
    from backend.integrations.obsidian import health_check
    health_check.invalidate()

    with patch("httpx.AsyncClient") as mock_cls:
        mock_resp = MagicMock(status_code=200)
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value.get = AsyncMock(return_value=mock_resp)
        mock_cls.return_value = mock_client

        assert await health_check() is True


@pytest.mark.asyncio
async def test_obsidian_health_check_non_200():
    from backend.integrations.obsidian import health_check
    health_check.invalidate()

    with patch("httpx.AsyncClient") as mock_cls:
        mock_resp = MagicMock(status_code=503)
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value.get = AsyncMock(return_value=mock_resp)
        mock_cls.return_value = mock_client

        assert await health_check() is False


@pytest.mark.asyncio
async def test_obsidian_health_check_fail():
    from backend.integrations.obsidian import health_check
    health_check.invalidate()

    with patch("httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value.get = AsyncMock(
            side_effect=Exception("not running")
        )
        mock_cls.return_value = mock_client

        assert await health_check() is False


# ---------------------------------------------------------------------------
# create_note() — httpx POST to /raw
# ---------------------------------------------------------------------------

def _make_post_mock():
    mock_resp = MagicMock(status_code=200)
    mock_resp.raise_for_status = MagicMock()
    mock_client = AsyncMock()
    mock_client.__aenter__.return_value.post = AsyncMock(return_value=mock_resp)
    return mock_client


@pytest.mark.asyncio
async def test_obsidian_create_note(tmp_path, monkeypatch):
    monkeypatch.setattr("backend.integrations.obsidian._vault", lambda: tmp_path)

    with patch("httpx.AsyncClient") as mock_cls:
        mock_client = _make_post_mock()
        mock_cls.return_value = mock_client

        from backend.integrations.obsidian import create_note
        path = await create_note(title="Test Note", content="# Test\nContent here", folder="NEXUS/Test")

    assert path.replace("\\", "/") == "NEXUS/Test/Test Note.md"
    mock_client.__aenter__.return_value.post.assert_called_once()
    call = mock_client.__aenter__.return_value.post.call_args
    assert call.kwargs["json"]["filename"] == "NEXUS/Test/Test Note.md"
    assert call.kwargs["json"]["content"] == "# Test\nContent here"


@pytest.mark.asyncio
async def test_obsidian_create_note_default_folder(tmp_path, monkeypatch):
    monkeypatch.setattr("backend.integrations.obsidian._vault", lambda: tmp_path)

    with patch("httpx.AsyncClient") as mock_cls:
        mock_client = _make_post_mock()
        mock_cls.return_value = mock_client

        from backend.integrations.obsidian import create_note
        path = await create_note(title="My Note", content="body")

    assert path.replace("\\", "/") == "NEXUS/My Note.md"


@pytest.mark.asyncio
async def test_obsidian_create_note_sanitizes_slashes(tmp_path, monkeypatch):
    monkeypatch.setattr("backend.integrations.obsidian._vault", lambda: tmp_path)

    with patch("httpx.AsyncClient") as mock_cls:
        mock_client = _make_post_mock()
        mock_cls.return_value = mock_client

        from backend.integrations.obsidian import create_note
        path = await create_note(title="A/B\\C", content="body")

    assert "A-B-C.md" in path
    assert path.replace("\\", "/").rsplit("/", 1)[-1] == "A-B-C.md"


# ---------------------------------------------------------------------------
# write_daily_note() — httpx POST to /raw
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_obsidian_write_daily_note():
    with patch("httpx.AsyncClient") as mock_cls:
        mock_resp = MagicMock(status_code=200)
        mock_resp.raise_for_status = MagicMock()
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value.post = AsyncMock(return_value=mock_resp)
        mock_cls.return_value = mock_client

        from backend.integrations.obsidian import write_daily_note
        await write_daily_note("# Daily Note\nContent")

    mock_client.__aenter__.return_value.post.assert_called_once()
    today = date.today().strftime("%Y-%m-%d")
    call = mock_client.__aenter__.return_value.post.call_args
    assert call.kwargs["json"]["filename"] == f"{today}.md"
    assert call.kwargs["json"]["content"] == "# Daily Note\nContent"


# ---------------------------------------------------------------------------
# complete_task() — direct filesystem (pathlib), no httpx
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_obsidian_complete_task(tmp_path, monkeypatch):
    monkeypatch.setattr("backend.integrations.obsidian._vault", lambda: tmp_path)

    (tmp_path / "NEXUS").mkdir()
    (tmp_path / "NEXUS" / "test.md").write_text(
        "# Note\n- [ ] Buy milk\n- [x] Done\n", encoding="utf-8"
    )

    from backend.integrations.obsidian import complete_task
    await complete_task("NEXUS/test.md", "Buy milk")

    result = (tmp_path / "NEXUS" / "test.md").read_text(encoding="utf-8")
    assert "- [x] Buy milk" in result
    assert "- [ ] Buy milk" not in result
    assert "- [x] Done" in result


@pytest.mark.asyncio
async def test_obsidian_complete_task_note_not_found(tmp_path, monkeypatch):
    monkeypatch.setattr("backend.integrations.obsidian._vault", lambda: tmp_path)

    from backend.integrations.obsidian import complete_task
    await complete_task("NEXUS/missing.md", "Some task")

    assert not (tmp_path / "NEXUS" / "missing.md").exists()


@pytest.mark.asyncio
async def test_obsidian_complete_task_rejects_path_traversal(tmp_path, monkeypatch):
    """note_path is an LLM tool-call arg -- '../' (or an absolute path, which
    Path's own '/' operator lets replace the vault root entirely) must never
    let a write happen outside the vault."""
    monkeypatch.setattr("backend.integrations.obsidian._vault", lambda: tmp_path)

    outside = tmp_path.parent / "outside.md"
    outside.write_text("- [ ] Buy milk\n", encoding="utf-8")

    from backend.integrations.obsidian import complete_task
    await complete_task("../outside.md", "Buy milk")

    assert outside.read_text(encoding="utf-8") == "- [ ] Buy milk\n", (
        "a file outside the vault must never be modified"
    )


# ---------------------------------------------------------------------------
# vault_search() — filesystem (pathlib rglob), no httpx
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_obsidian_vault_search_match(tmp_path, monkeypatch):
    monkeypatch.setattr("backend.integrations.obsidian._vault", lambda: tmp_path)

    (tmp_path / "meeting.md").write_text(
        "Project kickoff with the budget team", encoding="utf-8"
    )
    (tmp_path / "other.md").write_text("unrelated content", encoding="utf-8")

    from backend.integrations.obsidian import vault_search
    result = await vault_search("budget")

    assert "meeting.md" in result
    assert "No notes found" not in result
    assert "other.md" not in result


@pytest.mark.asyncio
async def test_obsidian_vault_search_no_match(tmp_path, monkeypatch):
    monkeypatch.setattr("backend.integrations.obsidian._vault", lambda: tmp_path)

    (tmp_path / "note.md").write_text("hello world", encoding="utf-8")

    from backend.integrations.obsidian import vault_search
    result = await vault_search("zzznotpresent")

    assert result == "No notes found matching 'zzznotpresent'."


# ---------------------------------------------------------------------------
# ObsidianData dataclass defaults
# ---------------------------------------------------------------------------

def test_obsidian_data_defaults():
    from backend.integrations.obsidian import ObsidianData
    data = ObsidianData()
    assert data.daily_note is None
    assert data.recent_notes == []
    assert data.open_tasks == []


# ---------------------------------------------------------------------------
# _format_event() -- pure formatter, no I/O
# ---------------------------------------------------------------------------

def test_format_event_exact_template_and_filename():
    from backend.integrations.obsidian import _format_event

    when = datetime(2026, 7, 21, 14, 2, 33, tzinfo=timezone.utc)
    content, filename = _format_event(
        "goal.approved",
        "Goal approved: Ship it",
        "Goal 42 approved by user.",
        when=when,
    )

    assert content == (
        "# Event: Goal approved: Ship it\n"
        "\n"
        "- Source: nexus\n"
        "- Type: goal.approved\n"
        "- When: 2026-07-21T14:02:33Z\n"
        "\n"
        "Goal 42 approved by user.\n"
        "\n"
        "Powered by CwiAI"
    )
    assert filename == "event-nexus-goal-approved-20260721T140233Z.md"


def test_format_event_defaults_when_to_now(monkeypatch):
    from backend.integrations import obsidian

    fixed = datetime(2026, 1, 5, 3, 4, 5, tzinfo=timezone.utc)

    class _FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed

    monkeypatch.setattr(obsidian, "datetime", _FixedDatetime)

    content, filename = obsidian._format_event("goal.failed", "Goal failed", "It failed.")

    assert "- When: 2026-01-05T03:04:05Z\n" in content
    assert filename == "event-nexus-goal-failed-20260105T030405Z.md"


# ---------------------------------------------------------------------------
# emit_event() -- best-effort POST via _post_raw, never raises
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_emit_event_calls_post_raw_with_formatted_content_and_short_timeout():
    from backend.integrations import obsidian

    mock_post_raw = AsyncMock()
    with patch.object(obsidian, "_post_raw", mock_post_raw):
        await obsidian.emit_event("goal.approved", "Goal approved: X", "body text")

    mock_post_raw.assert_called_once()
    args, kwargs = mock_post_raw.call_args
    assert args[0].startswith("# Event: Goal approved: X\n")
    assert args[1].startswith("event-nexus-goal-approved-")
    assert kwargs["timeout"] == 5


@pytest.mark.asyncio
async def test_emit_event_swallows_raising_post_raw(caplog):
    from backend.integrations import obsidian

    with patch.object(obsidian, "_post_raw", AsyncMock(side_effect=Exception("boom"))):
        with caplog.at_level("WARNING"):
            result = await obsidian.emit_event("goal.failed", "Goal failed: X", "it broke")

    assert result is None
    assert "boom" in caplog.text
    assert "goal.failed" in caplog.text
