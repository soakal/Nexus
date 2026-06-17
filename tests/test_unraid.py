import pytest
from unittest.mock import AsyncMock, MagicMock, patch


def _gql_response(data: dict, status_code: int = 200):
    """Mock the single GraphQL POST response that fetch/health_check issue.
    `data` is the value of the GraphQL top-level 'data' field."""
    resp = MagicMock(status_code=status_code)
    resp.json.return_value = {"data": data}
    return resp


def _post_client(resp):
    client = AsyncMock()
    client.__aenter__.return_value.post = AsyncMock(return_value=resp)
    return client


@pytest.mark.asyncio
async def test_unraid_fetch():
    # size/fsUsed are in KB; the source converts KB -> GB by /1048576.
    data = {
        "array": {
            "state": "started",
            "disks": [
                {"name": "disk1", "temp": 35, "status": "healthy", "type": "DATA",
                 "size": 4000 * 1048576, "fsUsed": 1500 * 1048576},
            ],
        },
        "docker": {"containers": [
            {"id": "abc123def456", "names": ["/plex"], "state": "running", "status": "Up"},
            {"id": "def456", "names": ["/sonarr"], "state": "running", "status": "Up"},
        ]},
    }
    with patch("httpx.AsyncClient") as mock_cls:
        mock_cls.return_value = _post_client(_gql_response(data))
        from backend.integrations.unraid import fetch
        result = await fetch()
        assert result.array_status == "started"
        assert len(result.docker_containers) == 2
        assert result.docker_containers[0]["name"] == "plex"
        assert result.storage_total_gb == 4000.0
        assert result.storage_used_gb == 1500.0


@pytest.mark.asyncio
async def test_unraid_fetch_multiple_data_disks():
    """Storage totals must sum across all DATA-type disks (parity excluded)."""
    data = {
        "array": {
            "state": "started",
            "disks": [
                {"name": "disk1", "type": "DATA", "size": 2000 * 1048576, "fsUsed": 500 * 1048576},
                {"name": "disk2", "type": "DATA", "size": 2000 * 1048576, "fsUsed": 1000 * 1048576},
                {"name": "parity", "type": "PARITY", "size": 4000 * 1048576, "fsUsed": 0},
            ],
        },
        "docker": {"containers": []},
    }
    with patch("httpx.AsyncClient") as mock_cls:
        mock_cls.return_value = _post_client(_gql_response(data))
        from backend.integrations.unraid import fetch
        result = await fetch()
        assert result.storage_total_gb == 4000.0
        assert result.storage_used_gb == 1500.0


@pytest.mark.asyncio
async def test_unraid_fetch_http_error_raises():
    """A non-2xx GraphQL response (raise_for_status raises) must RAISE — NOT return
    zero-filled defaults. Zeros look like catastrophic data loss to the briefing/
    trends/proposer; a raise makes downstream report Unraid 'unavailable' instead."""
    resp = _gql_response({}, status_code=500)
    resp.raise_for_status.side_effect = Exception("HTTP 500")
    with patch("httpx.AsyncClient") as mock_cls:
        mock_cls.return_value = _post_client(resp)
        from backend.integrations.unraid import fetch
        with pytest.raises(Exception):
            await fetch()


@pytest.mark.asyncio
async def test_unraid_fetch_connection_error_raises():
    """A connection failure (post raises) must propagate as unavailable, not zeros."""
    with patch("httpx.AsyncClient") as mock_cls:
        client = AsyncMock()
        client.__aenter__.return_value.post = AsyncMock(side_effect=Exception("connection refused"))
        mock_cls.return_value = client
        from backend.integrations.unraid import fetch
        with pytest.raises(Exception):
            await fetch()


@pytest.mark.asyncio
async def test_unraid_fetch_missing_docker_section():
    """If the GraphQL payload omits the docker section, array data still parses."""
    data = {"array": {"state": "started", "disks": []}}  # no 'docker' key
    with patch("httpx.AsyncClient") as mock_cls:
        mock_cls.return_value = _post_client(_gql_response(data))
        from backend.integrations.unraid import fetch
        result = await fetch()
        assert result.array_status == "started"
        assert result.docker_containers == []


@pytest.mark.asyncio
async def test_unraid_fetch_disk_health_fields():
    """disk_health must carry name, temp and status for each disk."""
    data = {
        "array": {
            "state": "started",
            "disks": [
                {"name": "disk1", "temp": 40, "status": "healthy", "type": "DATA", "size": 0, "fsUsed": 0},
            ],
        },
        "docker": {"containers": []},
    }
    with patch("httpx.AsyncClient") as mock_cls:
        mock_cls.return_value = _post_client(_gql_response(data))
        from backend.integrations.unraid import fetch
        result = await fetch()
        assert len(result.disk_health) == 1
        disk = result.disk_health[0]
        assert disk["name"] == "disk1"
        assert disk["temp"] == 40
        assert disk["status"] == "healthy"


@pytest.mark.asyncio
async def test_unraid_health_check_ok():
    with patch("httpx.AsyncClient") as mock_cls:
        mock_cls.return_value = _post_client(_gql_response({"array": {"state": "started"}}))
        from backend.integrations.unraid import health_check
        assert await health_check() is True


@pytest.mark.asyncio
async def test_unraid_health_check_non_200():
    with patch("httpx.AsyncClient") as mock_cls:
        mock_cls.return_value = _post_client(_gql_response({"array": {}}, status_code=401))
        from backend.integrations.unraid import health_check
        assert await health_check() is False


@pytest.mark.asyncio
async def test_unraid_health_check_fail():
    with patch("httpx.AsyncClient") as mock_cls:
        client = AsyncMock()
        client.__aenter__.return_value.post = AsyncMock(side_effect=Exception("connection refused"))
        mock_cls.return_value = client
        from backend.integrations.unraid import health_check
        assert await health_check() is False


@pytest.mark.asyncio
async def test_unraid_restart_docker_success():
    with patch("httpx.AsyncClient") as mock_cls:
        mock_cls.return_value = _post_client(MagicMock(status_code=200))
        from backend.integrations.unraid import restart_docker
        assert await restart_docker("abc123") is True


@pytest.mark.asyncio
async def test_unraid_restart_docker_invalidates_cache():
    """A successful restart busts the fetch cache so the next poll shows new state."""
    with patch("httpx.AsyncClient") as mock_cls:
        mock_cls.return_value = _post_client(MagicMock(status_code=200))
        from backend.integrations import unraid
        with patch.object(unraid.fetch, "invalidate") as mock_inv:
            assert await unraid.restart_docker("abc123") is True
            mock_inv.assert_called_once()


@pytest.mark.asyncio
async def test_unraid_restart_docker_server_error():
    """restart_docker returns False for 5xx responses."""
    with patch("httpx.AsyncClient") as mock_cls:
        mock_cls.return_value = _post_client(MagicMock(status_code=500))
        from backend.integrations.unraid import restart_docker
        assert await restart_docker("abc123") is False


@pytest.mark.asyncio
async def test_unraid_restart_docker_fail():
    with patch("httpx.AsyncClient") as mock_cls:
        client = AsyncMock()
        client.__aenter__.return_value.post = AsyncMock(side_effect=Exception("fail"))
        mock_cls.return_value = client
        from backend.integrations.unraid import restart_docker
        assert await restart_docker("abc123") is False


def test_unraid_data_defaults():
    from backend.integrations.unraid import UnraidData
    data = UnraidData()
    assert data.array_status == "unknown"
    assert data.docker_containers == []
    assert data.storage_total_gb == 0.0
    assert data.storage_used_gb == 0.0
