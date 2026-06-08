import pytest
from unittest.mock import AsyncMock, MagicMock, patch


@pytest.mark.asyncio
async def test_unraid_fetch():
    array_data = {
        "state": "started",
        "disks": [
            {"name": "disk1", "temp": 35, "status": "healthy", "type": "Data",
             "size": 1024**3 * 4000, "fsUsed": 1024**3 * 1500},
        ],
    }
    containers = [
        {"id": "abc123", "name": "plex", "status": "running"},
        {"id": "def456", "name": "sonarr", "status": "running"},
    ]

    with patch("httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        r1 = MagicMock(status_code=200)
        r1.json.return_value = array_data
        r2 = MagicMock(status_code=200)
        r2.json.return_value = containers
        mock_client.__aenter__.return_value.get = AsyncMock(side_effect=[r1, r2])
        mock_cls.return_value = mock_client

        from backend.integrations.unraid import fetch
        data = await fetch()
        assert data.array_status == "started"
        assert len(data.docker_containers) == 2
        assert data.storage_total_gb == 4000.0
        assert data.storage_used_gb == 1500.0


@pytest.mark.asyncio
async def test_unraid_fetch_multiple_data_disks():
    """Storage totals must sum across all Data-type disks."""
    array_data = {
        "state": "started",
        "disks": [
            {"name": "disk1", "type": "Data", "size": 1024**3 * 2000, "fsUsed": 1024**3 * 500},
            {"name": "disk2", "type": "Data", "size": 1024**3 * 2000, "fsUsed": 1024**3 * 1000},
            {"name": "parity", "type": "Parity", "size": 1024**3 * 4000, "fsUsed": 0},
        ],
    }

    with patch("httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        r1 = MagicMock(status_code=200)
        r1.json.return_value = array_data
        r2 = MagicMock(status_code=200)
        r2.json.return_value = []
        mock_client.__aenter__.return_value.get = AsyncMock(side_effect=[r1, r2])
        mock_cls.return_value = mock_client

        from backend.integrations.unraid import fetch
        data = await fetch()
        assert data.storage_total_gb == 4000.0
        assert data.storage_used_gb == 1500.0


@pytest.mark.asyncio
async def test_unraid_fetch_array_api_fails_gracefully():
    """If the array endpoint raises, docker containers are still fetched."""
    containers = [{"id": "abc", "name": "nginx", "status": "running"}]

    with patch("httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        r1 = MagicMock(status_code=500)
        r1.json.return_value = {}
        r2 = MagicMock(status_code=200)
        r2.json.return_value = containers
        mock_client.__aenter__.return_value.get = AsyncMock(side_effect=[r1, r2])
        mock_cls.return_value = mock_client

        from backend.integrations.unraid import fetch
        data = await fetch()
        # Array status stays at default when API returns non-200
        assert data.array_status == "unknown"
        assert len(data.docker_containers) == 1


@pytest.mark.asyncio
async def test_unraid_fetch_docker_api_fails_gracefully():
    """If the docker endpoint raises, array data is still returned."""
    array_data = {"state": "started", "disks": []}

    with patch("httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        r1 = MagicMock(status_code=200)
        r1.json.return_value = array_data
        r2 = MagicMock(status_code=500)
        r2.json.return_value = []
        mock_client.__aenter__.return_value.get = AsyncMock(side_effect=[r1, r2])
        mock_cls.return_value = mock_client

        from backend.integrations.unraid import fetch
        data = await fetch()
        assert data.array_status == "started"
        assert data.docker_containers == []


@pytest.mark.asyncio
async def test_unraid_fetch_disk_health_fields():
    """disk_health list must contain name, temp and status for each disk."""
    array_data = {
        "state": "started",
        "disks": [
            {"name": "disk1", "temp": 40, "status": "healthy", "type": "Data", "size": 0, "fsUsed": 0},
        ],
    }

    with patch("httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        r1 = MagicMock(status_code=200)
        r1.json.return_value = array_data
        r2 = MagicMock(status_code=200)
        r2.json.return_value = []
        mock_client.__aenter__.return_value.get = AsyncMock(side_effect=[r1, r2])
        mock_cls.return_value = mock_client

        from backend.integrations.unraid import fetch
        data = await fetch()
        assert len(data.disk_health) == 1
        disk = data.disk_health[0]
        assert disk["name"] == "disk1"
        assert disk["temp"] == 40
        assert disk["status"] == "healthy"


@pytest.mark.asyncio
async def test_unraid_health_check_ok():
    with patch("httpx.AsyncClient") as mock_cls:
        mock_resp = MagicMock(status_code=200)
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value.get = AsyncMock(return_value=mock_resp)
        mock_cls.return_value = mock_client
        from backend.integrations.unraid import health_check
        assert await health_check() is True


@pytest.mark.asyncio
async def test_unraid_health_check_non_200():
    with patch("httpx.AsyncClient") as mock_cls:
        mock_resp = MagicMock(status_code=401)
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value.get = AsyncMock(return_value=mock_resp)
        mock_cls.return_value = mock_client
        from backend.integrations.unraid import health_check
        assert await health_check() is False


@pytest.mark.asyncio
async def test_unraid_health_check_fail():
    with patch("httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value.get = AsyncMock(side_effect=Exception("connection refused"))
        mock_cls.return_value = mock_client
        from backend.integrations.unraid import health_check
        assert await health_check() is False


@pytest.mark.asyncio
async def test_unraid_restart_docker_success():
    with patch("httpx.AsyncClient") as mock_cls:
        mock_resp = MagicMock(status_code=200)
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value.post = AsyncMock(return_value=mock_resp)
        mock_cls.return_value = mock_client

        from backend.integrations.unraid import restart_docker
        assert await restart_docker("abc123") is True


@pytest.mark.asyncio
async def test_unraid_restart_docker_204():
    """restart_docker also returns True for 204 No Content."""
    with patch("httpx.AsyncClient") as mock_cls:
        mock_resp = MagicMock(status_code=204)
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value.post = AsyncMock(return_value=mock_resp)
        mock_cls.return_value = mock_client

        from backend.integrations.unraid import restart_docker
        assert await restart_docker("def456") is True


@pytest.mark.asyncio
async def test_unraid_restart_docker_server_error():
    """restart_docker returns False for 5xx responses."""
    with patch("httpx.AsyncClient") as mock_cls:
        mock_resp = MagicMock(status_code=500)
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value.post = AsyncMock(return_value=mock_resp)
        mock_cls.return_value = mock_client

        from backend.integrations.unraid import restart_docker
        assert await restart_docker("abc123") is False


@pytest.mark.asyncio
async def test_unraid_restart_docker_fail():
    with patch("httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value.post = AsyncMock(side_effect=Exception("fail"))
        mock_cls.return_value = mock_client

        from backend.integrations.unraid import restart_docker
        assert await restart_docker("abc123") is False


def test_unraid_data_defaults():
    from backend.integrations.unraid import UnraidData
    data = UnraidData()
    assert data.array_status == "unknown"
    assert data.docker_containers == []
    assert data.storage_total_gb == 0.0
    assert data.storage_used_gb == 0.0
