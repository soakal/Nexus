import pytest
from unittest.mock import AsyncMock, MagicMock, patch


@pytest.mark.asyncio
async def test_obsidian_fetch():
    files_resp = MagicMock(status_code=200)
    files_resp.raise_for_status = MagicMock()
    files_resp.json.return_value = {"files": ["2024-01-01.md", "note1.md", "note2.md"]}
    daily_resp = MagicMock(status_code=200)
    daily_resp.text = "# Daily\n- [ ] Task 1\n- [ ] Task 2\n- [x] Done task"

    with patch("httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value.get = AsyncMock(side_effect=[files_resp, daily_resp])
        mock_cls.return_value = mock_client

        from backend.integrations.obsidian import fetch
        data = await fetch()
        assert len(data.recent_notes) > 0
        assert "2024-01-01.md" in data.recent_notes
        assert len(data.open_tasks) == 2  # only unchecked tasks


@pytest.mark.asyncio
async def test_obsidian_fetch_no_daily_note():
    """When the daily note does not exist, open_tasks should be empty."""
    files_resp = MagicMock(status_code=200)
    files_resp.raise_for_status = MagicMock()
    files_resp.json.return_value = {"files": ["note1.md"]}
    daily_resp = MagicMock(status_code=404)

    with patch("httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value.get = AsyncMock(side_effect=[files_resp, daily_resp])
        mock_cls.return_value = mock_client

        from backend.integrations.obsidian import fetch
        data = await fetch()
        assert data.open_tasks == []
        assert data.daily_note is None


@pytest.mark.asyncio
async def test_obsidian_fetch_truncates_to_10_notes():
    """Only the 10 most recent notes are kept."""
    many_files = [f"note{i:02d}.md" for i in range(20)]
    files_resp = MagicMock(status_code=200)
    files_resp.raise_for_status = MagicMock()
    files_resp.json.return_value = {"files": many_files}
    daily_resp = MagicMock(status_code=404)

    with patch("httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value.get = AsyncMock(side_effect=[files_resp, daily_resp])
        mock_cls.return_value = mock_client

        from backend.integrations.obsidian import fetch
        data = await fetch()
        assert len(data.recent_notes) == 10


@pytest.mark.asyncio
async def test_obsidian_fetch_only_md_files():
    """Non-.md files in the vault listing must be excluded from recent_notes."""
    files_resp = MagicMock(status_code=200)
    files_resp.raise_for_status = MagicMock()
    files_resp.json.return_value = {"files": ["note.md", "image.png", "attachment.pdf"]}
    daily_resp = MagicMock(status_code=404)

    with patch("httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value.get = AsyncMock(side_effect=[files_resp, daily_resp])
        mock_cls.return_value = mock_client

        from backend.integrations.obsidian import fetch
        data = await fetch()
        assert all(f.endswith(".md") for f in data.recent_notes)
        assert len(data.recent_notes) == 1


@pytest.mark.asyncio
async def test_obsidian_health_check_ok():
    with patch("httpx.AsyncClient") as mock_cls:
        mock_resp = MagicMock(status_code=200)
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value.get = AsyncMock(return_value=mock_resp)
        mock_cls.return_value = mock_client
        from backend.integrations.obsidian import health_check
        assert await health_check() is True


@pytest.mark.asyncio
async def test_obsidian_health_check_non_200():
    with patch("httpx.AsyncClient") as mock_cls:
        mock_resp = MagicMock(status_code=503)
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value.get = AsyncMock(return_value=mock_resp)
        mock_cls.return_value = mock_client
        from backend.integrations.obsidian import health_check
        assert await health_check() is False


@pytest.mark.asyncio
async def test_obsidian_health_check_fail():
    with patch("httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value.get = AsyncMock(side_effect=Exception("not running"))
        mock_cls.return_value = mock_client
        from backend.integrations.obsidian import health_check
        assert await health_check() is False


@pytest.mark.asyncio
async def test_obsidian_create_note():
    with patch("httpx.AsyncClient") as mock_cls:
        mock_resp = MagicMock(status_code=200)
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value.put = AsyncMock(return_value=mock_resp)
        mock_cls.return_value = mock_client

        from backend.integrations.obsidian import create_note
        path = await create_note(title="Test Note", content="# Test\nContent here", folder="NEXUS/Test")
        assert path == "NEXUS/Test/Test Note.md"
        mock_client.__aenter__.return_value.put.assert_called_once()


@pytest.mark.asyncio
async def test_obsidian_create_note_default_folder():
    """When no folder is provided the default NEXUS folder is used."""
    with patch("httpx.AsyncClient") as mock_cls:
        mock_resp = MagicMock(status_code=200)
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value.put = AsyncMock(return_value=mock_resp)
        mock_cls.return_value = mock_client

        from backend.integrations.obsidian import create_note
        path = await create_note(title="My Note", content="body")
        assert path == "NEXUS/My Note.md"


@pytest.mark.asyncio
async def test_obsidian_create_note_sanitizes_slashes():
    """Slashes in the title must be replaced with hyphens."""
    with patch("httpx.AsyncClient") as mock_cls:
        mock_resp = MagicMock(status_code=200)
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value.put = AsyncMock(return_value=mock_resp)
        mock_cls.return_value = mock_client

        from backend.integrations.obsidian import create_note
        path = await create_note(title="A/B\\C", content="body")
        assert "/" not in path.split("/", 1)[1]  # title part must have no slash
        assert "A-B-C.md" in path


@pytest.mark.asyncio
async def test_obsidian_write_daily_note():
    with patch("httpx.AsyncClient") as mock_cls:
        mock_resp = MagicMock(status_code=200)
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value.put = AsyncMock(return_value=mock_resp)
        mock_cls.return_value = mock_client

        from backend.integrations.obsidian import write_daily_note
        await write_daily_note("# Daily Note\nContent")
        mock_client.__aenter__.return_value.put.assert_called_once()


@pytest.mark.asyncio
async def test_obsidian_append_to_note():
    with patch("httpx.AsyncClient") as mock_cls:
        mock_resp = MagicMock(status_code=200)
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value.post = AsyncMock(return_value=mock_resp)
        mock_cls.return_value = mock_client

        from backend.integrations.obsidian import append_to_note
        await append_to_note("NEXUS/test.md", "\nAppended line")
        mock_client.__aenter__.return_value.post.assert_called_once()


@pytest.mark.asyncio
async def test_obsidian_complete_task():
    """complete_task reads the note and replaces the open checkbox."""
    get_resp = MagicMock(status_code=200)
    get_resp.text = "# Note\n- [ ] Buy milk\n- [x] Done\n"
    put_resp = MagicMock(status_code=200)

    with patch("httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value.get = AsyncMock(return_value=get_resp)
        mock_client.__aenter__.return_value.put = AsyncMock(return_value=put_resp)
        mock_cls.return_value = mock_client

        from backend.integrations.obsidian import complete_task
        await complete_task("NEXUS/test.md", "Buy milk")
        mock_client.__aenter__.return_value.put.assert_called_once()
        # Verify the content passed to put has the task marked complete
        call_kwargs = mock_client.__aenter__.return_value.put.call_args
        content_bytes = call_kwargs[1].get("content") or call_kwargs[0][1]
        assert b"- [x] Buy milk" in content_bytes


@pytest.mark.asyncio
async def test_obsidian_complete_task_note_not_found():
    """complete_task does nothing if the note returns non-200."""
    get_resp = MagicMock(status_code=404)

    with patch("httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value.get = AsyncMock(return_value=get_resp)
        mock_client.__aenter__.return_value.put = AsyncMock()
        mock_cls.return_value = mock_client

        from backend.integrations.obsidian import complete_task
        await complete_task("NEXUS/missing.md", "Some task")
        mock_client.__aenter__.return_value.put.assert_not_called()


def test_obsidian_data_defaults():
    from backend.integrations.obsidian import ObsidianData
    data = ObsidianData()
    assert data.daily_note is None
    assert data.recent_notes == []
    assert data.open_tasks == []
