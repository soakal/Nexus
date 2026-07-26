import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timezone, timedelta


@pytest.mark.asyncio
async def test_github_fetch():
    now = datetime.now(timezone.utc)
    items = [
        {"title": "Fix bug", "html_url": "https://github.com/test/repo/pull/1",
         "updated_at": now.isoformat(), "pull_request": {}},
        {"title": "Old PR", "html_url": "https://github.com/test/repo/pull/2",
         "updated_at": (now - timedelta(hours=72)).isoformat(), "pull_request": {}},
    ]
    issues = [
        {"title": "Bug report", "html_url": "https://github.com/test/repo/issues/1"},
    ]
    events = [
        {
            "type": "PushEvent",
            "repo": {"name": "test/repo"},
            "payload": {"commits": [{"message": "Initial commit"}]},
        }
    ]

    with patch("httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        r1 = MagicMock(status_code=200)
        r1.json.return_value = {"items": items}
        r2 = MagicMock(status_code=200)
        r2.json.return_value = issues
        r3 = MagicMock(status_code=200)
        r3.json.return_value = events
        mock_client.__aenter__.return_value.get = AsyncMock(side_effect=[r1, r2, r3])
        mock_cls.return_value = mock_client

        from backend.integrations.github import fetch
        data = await fetch()
        assert len(data.open_prs) == 2
        assert len(data.stale_prs) == 1  # 72h old PR is stale (default 48h)
        assert len(data.assigned_issues) == 1
        assert len(data.recent_commits) == 1


@pytest.mark.asyncio
async def test_github_fetch_no_prs():
    """Fetch returns empty lists when no PRs, issues, or events are returned."""
    with patch("httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        r1 = MagicMock(status_code=200)
        r1.json.return_value = {"items": []}
        r2 = MagicMock(status_code=200)
        r2.json.return_value = []
        r3 = MagicMock(status_code=200)
        r3.json.return_value = []
        mock_client.__aenter__.return_value.get = AsyncMock(side_effect=[r1, r2, r3])
        mock_cls.return_value = mock_client

        from backend.integrations.github import fetch
        data = await fetch()
        assert data.open_prs == []
        assert data.stale_prs == []
        assert data.assigned_issues == []
        assert data.recent_commits == []


@pytest.mark.asyncio
async def test_github_raises_on_non_200():
    """A non-200 response must raise, not silently report an empty list — a
    dead token (401/403) or rate limit used to be indistinguishable from a
    genuinely quiet week, and the briefing rendered the empty list as fact.
    Matches the raise-on-failure convention every other integration already
    uses (unraid.py, proxmox.py, channels_dvr.py)."""
    with patch("httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        r1 = MagicMock(status_code=403)
        r1.json.return_value = {"items": []}
        mock_client.__aenter__.return_value.get = AsyncMock(return_value=r1)
        mock_cls.return_value = mock_client

        from backend.integrations.github import fetch
        with pytest.raises(RuntimeError, match="403"):
            await fetch()


@pytest.mark.asyncio
async def test_github_raises_on_non_200_for_issues_endpoint():
    """The open-PRs call can succeed while a later call (issues) fails —
    that must still raise, not silently blend a success with a failure."""
    with patch("httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        r1 = MagicMock(status_code=200)
        r1.json.return_value = {"items": []}
        r2 = MagicMock(status_code=403)
        r2.json.return_value = []
        mock_client.__aenter__.return_value.get = AsyncMock(side_effect=[r1, r2])
        mock_cls.return_value = mock_client

        from backend.integrations.github import fetch
        with pytest.raises(RuntimeError, match="403"):
            await fetch()


@pytest.mark.asyncio
async def test_github_fetch_filters_pull_request_from_issues():
    """Issues that have a pull_request key must not appear in assigned_issues."""
    with patch("httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        r1 = MagicMock(status_code=200)
        r1.json.return_value = {"items": []}
        # Return one real issue and one PR masquerading as an issue
        r2 = MagicMock(status_code=200)
        r2.json.return_value = [
            {"title": "Real issue", "html_url": "https://github.com/test/repo/issues/2"},
            {"title": "PR as issue", "html_url": "https://github.com/test/repo/pull/3", "pull_request": {}},
        ]
        r3 = MagicMock(status_code=200)
        r3.json.return_value = []
        mock_client.__aenter__.return_value.get = AsyncMock(side_effect=[r1, r2, r3])
        mock_cls.return_value = mock_client

        from backend.integrations.github import fetch
        data = await fetch()
        assert len(data.assigned_issues) == 1
        assert data.assigned_issues[0]["title"] == "Real issue"


@pytest.mark.asyncio
async def test_github_fetch_multiple_commits_per_push():
    """Multiple commits in a single PushEvent are all captured."""
    now = datetime.now(timezone.utc)
    events = [
        {
            "type": "PushEvent",
            "repo": {"name": "test/repo"},
            "payload": {
                "commits": [
                    {"message": "Commit 1"},
                    {"message": "Commit 2"},
                    {"message": "Commit 3"},
                ]
            },
        }
    ]

    with patch("httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        r1 = MagicMock(status_code=200)
        r1.json.return_value = {"items": []}
        r2 = MagicMock(status_code=200)
        r2.json.return_value = []
        r3 = MagicMock(status_code=200)
        r3.json.return_value = events
        mock_client.__aenter__.return_value.get = AsyncMock(side_effect=[r1, r2, r3])
        mock_cls.return_value = mock_client

        from backend.integrations.github import fetch
        data = await fetch()
        # fetch caps at 3 commits per push
        assert len(data.recent_commits) == 3
        assert data.recent_commits[0]["repo"] == "test/repo"


@pytest.mark.asyncio
async def test_github_health_check_ok():
    with patch("httpx.AsyncClient") as mock_cls:
        mock_resp = MagicMock(status_code=200)
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value.get = AsyncMock(return_value=mock_resp)
        mock_cls.return_value = mock_client
        from backend.integrations.github import health_check
        assert await health_check() is True


@pytest.mark.asyncio
async def test_github_health_check_non_200():
    with patch("httpx.AsyncClient") as mock_cls:
        mock_resp = MagicMock(status_code=401)
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value.get = AsyncMock(return_value=mock_resp)
        mock_cls.return_value = mock_client
        from backend.integrations.github import health_check
        assert await health_check() is False


@pytest.mark.asyncio
async def test_github_health_check_fail():
    with patch("httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value.get = AsyncMock(side_effect=Exception("timeout"))
        mock_cls.return_value = mock_client
        from backend.integrations.github import health_check
        assert await health_check() is False


def test_github_data_defaults():
    from backend.integrations.github import GitHubData
    data = GitHubData()
    assert data.open_prs == []
    assert data.stale_prs == []
    assert data.assigned_issues == []
    assert data.recent_commits == []
