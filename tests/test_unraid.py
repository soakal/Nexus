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
async def test_unraid_fetch_populates_cpu_ram_from_metrics():
    """cpu_pct/ram_pct are wired from the live-verified GraphQL
    metrics.cpu.percentTotal / metrics.memory.percentTotal fields."""
    data = {
        "array": {"state": "started", "disks": []},
        "docker": {"containers": []},
        "metrics": {
            "cpu": {"percentTotal": 12.345},
            "memory": {"percentTotal": 54.321},
        },
    }
    with patch("httpx.AsyncClient") as mock_cls:
        mock_cls.return_value = _post_client(_gql_response(data))
        from backend.integrations.unraid import fetch
        result = await fetch()
        assert result.cpu_pct == 12.3
        assert result.ram_pct == 54.3


@pytest.mark.asyncio
async def test_unraid_fetch_gql_query_requests_metrics_field():
    """Pins the query string so cpu/ram can't silently fall out of the
    request the way `parities` once did (see the sibling parities-pinning
    test above)."""
    data = {"array": {"state": "started", "disks": []}, "docker": {"containers": []}}
    with patch("httpx.AsyncClient") as mock_cls:
        client = _post_client(_gql_response(data))
        mock_cls.return_value = client
        from backend.integrations.unraid import fetch
        await fetch()
        sent_query = client.__aenter__.return_value.post.call_args.kwargs["json"]["query"]
        assert "metrics" in sent_query
        assert "percentTotal" in sent_query


@pytest.mark.asyncio
async def test_unraid_fetch_missing_metrics_section_degrades_to_zero():
    """An older/degraded response omitting `metrics` entirely must not raise --
    same graceful-degrade contract as the missing-docker-section case."""
    data = {"array": {"state": "started", "disks": []}, "docker": {"containers": []}}
    with patch("httpx.AsyncClient") as mock_cls:
        mock_cls.return_value = _post_client(_gql_response(data))
        from backend.integrations.unraid import fetch
        result = await fetch()
        assert result.cpu_pct == 0.0
        assert result.ram_pct == 0.0


@pytest.mark.asyncio
async def test_unraid_fetch_null_metrics_leaves_degrade_to_zero():
    """A `metrics` section that's present but whose leaves are explicit GraphQL
    `null` (not just missing) must not raise -- `.get(key, 0.0)` doesn't catch
    a `null` value the way `.get(key) or 0.0` does; a regression here would
    make round(None, 1) blow up and get misreported as a full Unraid outage."""
    data = {
        "array": {"state": "started", "disks": []},
        "docker": {"containers": []},
        "metrics": {"cpu": {"percentTotal": None}, "memory": {"percentTotal": None}},
    }
    with patch("httpx.AsyncClient") as mock_cls:
        mock_cls.return_value = _post_client(_gql_response(data))
        from backend.integrations.unraid import fetch
        result = await fetch()
        assert result.cpu_pct == 0.0
        assert result.ram_pct == 0.0


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
async def test_unraid_gql_query_requests_parities_field():
    """Pins the actual query string sent to Unraid — without this, the
    parsing-level tests above would all keep passing even if `parities` were
    accidentally removed from the query again (proven: that's exactly what
    happened for the entire life of this integration until the contract
    canary caught it — the parsing code was 'correct' against a shape the
    query never actually requested)."""
    data = {"array": {"state": "started", "disks": [], "parities": []}, "docker": {"containers": []}}
    with patch("httpx.AsyncClient") as mock_cls:
        client = _post_client(_gql_response(data))
        mock_cls.return_value = client
        from backend.integrations.unraid import fetch
        await fetch()
        sent_query = client.__aenter__.return_value.post.call_args.kwargs["json"]["query"]
        assert "parities" in sent_query


@pytest.mark.asyncio
async def test_unraid_fetch_parity_status_from_parities_field():
    """Regression guard: Unraid's real GraphQL schema puts parity drives in
    their own top-level `array.parities` list, never inside `disks` — a
    `disks[].type == "PARITY"` filter is permanently vacuous against a real
    server (confirmed via live schema introspection) and left parity_status
    stuck at its 'unknown' default forever, silently, until the integration
    contract canary caught it. This must read from `parities`."""
    data = {
        "array": {
            "state": "started",
            "disks": [
                {"name": "disk1", "type": "DATA", "size": 2000 * 1048576, "fsUsed": 500 * 1048576},
            ],
            "parities": [
                {"name": "parity", "status": "DISK_OK"},
            ],
        },
        "docker": {"containers": []},
    }
    with patch("httpx.AsyncClient") as mock_cls:
        mock_cls.return_value = _post_client(_gql_response(data))
        from backend.integrations.unraid import fetch
        result = await fetch()
        assert result.parity_status == "disk_ok"


@pytest.mark.asyncio
async def test_unraid_fetch_parity_status_unknown_when_no_parity_configured():
    """A genuinely parity-less array (no redundancy) legitimately has no
    parities entries — must degrade to the 'unknown' default, not raise."""
    data = {
        "array": {
            "state": "started",
            "disks": [{"name": "disk1", "type": "DATA", "size": 0, "fsUsed": 0}],
            "parities": [],
        },
        "docker": {"containers": []},
    }
    with patch("httpx.AsyncClient") as mock_cls:
        mock_cls.return_value = _post_client(_gql_response(data))
        from backend.integrations.unraid import fetch
        result = await fetch()
        assert result.parity_status == "unknown"


@pytest.mark.asyncio
async def test_unraid_fetch_parity_status_missing_parities_key():
    """Older/degraded GraphQL responses omitting `parities` entirely must not
    raise — same graceful-degrade contract as the missing-docker-section case."""
    data = {
        "array": {"state": "started", "disks": []},
        "docker": {"containers": []},
    }
    with patch("httpx.AsyncClient") as mock_cls:
        mock_cls.return_value = _post_client(_gql_response(data))
        from backend.integrations.unraid import fetch
        result = await fetch()
        assert result.parity_status == "unknown"


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


# ---------------------------------------------------------------------------
# Phase 7c — resolve_container_id + restart_docker (stop-then-start, since no
# restartContainer mutation exists in Unraid's GraphQL schema; confirmed live
# 2026-07-27)
# ---------------------------------------------------------------------------

def _containers_data(containers):
    from backend.integrations.unraid import UnraidData
    return UnraidData(docker_containers=containers)


@pytest.mark.asyncio
async def test_resolve_container_id_passthrough_for_real_id():
    """A real PrefixedID (contains ':') is used as-is -- fetch() never called."""
    from backend.integrations.unraid import resolve_container_id
    with patch("backend.integrations.unraid.fetch", new_callable=AsyncMock) as mock_fetch:
        result = await resolve_container_id("abcdef0123456789:fedcba9876543210")
    assert result == "abcdef0123456789:fedcba9876543210"
    mock_fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_resolve_container_id_resolves_by_name():
    from backend.integrations.unraid import resolve_container_id
    data = _containers_data([
        {"id": "hash1:hash2", "name": "plex", "status": "Up", "state": "running"},
        {"id": "hash3:hash4", "name": "sonarr", "status": "Up", "state": "running"},
    ])
    with patch("backend.integrations.unraid.fetch", new_callable=AsyncMock, return_value=data):
        result = await resolve_container_id("plex")
    assert result == "hash1:hash2"


@pytest.mark.asyncio
async def test_resolve_container_id_unknown_name_raises():
    from backend.integrations.unraid import resolve_container_id
    data = _containers_data([{"id": "hash1:hash2", "name": "plex", "status": "Up", "state": "running"}])
    with patch("backend.integrations.unraid.fetch", new_callable=AsyncMock, return_value=data):
        with pytest.raises(ValueError, match="no container found"):
            await resolve_container_id("nonexistent")


@pytest.mark.asyncio
async def test_resolve_container_id_ambiguous_name_raises():
    from backend.integrations.unraid import resolve_container_id
    data = _containers_data([
        {"id": "hash1:hash2", "name": "app", "status": "Up", "state": "running"},
        {"id": "hash3:hash4", "name": "app", "status": "Up", "state": "running"},
    ])
    with patch("backend.integrations.unraid.fetch", new_callable=AsyncMock, return_value=data):
        with pytest.raises(ValueError, match="ambiguous"):
            await resolve_container_id("app")


@pytest.mark.asyncio
async def test_resolve_container_id_rejects_unsafe_input():
    """Rejected BEFORE any fetch()/HTTP call, regardless of what the server
    would do with it -- ids/names are spliced into a GraphQL mutation via an
    f-string (no query variables)."""
    from backend.integrations.unraid import resolve_container_id
    with patch("backend.integrations.unraid.fetch", new_callable=AsyncMock) as mock_fetch:
        with pytest.raises(ValueError, match="unsafe or empty"):
            await resolve_container_id('abc" }) mutation evil { x')
        with pytest.raises(ValueError, match="unsafe or empty"):
            await resolve_container_id("")
        mock_fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_unraid_restart_docker_success():
    """Happy path: resolve -> stop -> poll shows stopped -> start. Both
    mutations issued in order, cache invalidated on success."""
    from backend.integrations import unraid
    data = _containers_data([{"id": "hash1:hash2", "name": "plex", "status": "Up", "state": "EXITED"}])
    with patch("backend.integrations.unraid.resolve_container_id", new_callable=AsyncMock, return_value="hash1:hash2"), \
         patch("backend.integrations.unraid.fetch", new_callable=AsyncMock, return_value=data) as mock_fetch, \
         patch("backend.integrations.unraid._docker_mutation", new_callable=AsyncMock, side_effect=[(True, ""), (True, "")]) as mock_mut, \
         patch("asyncio.sleep", new_callable=AsyncMock):
        mock_fetch.invalidate = MagicMock()
        result = await unraid.restart_docker("plex")

    assert result == {"success": True}
    assert mock_mut.await_args_list[0].args == ("stop", "hash1:hash2")
    assert mock_mut.await_args_list[1].args == ("start", "hash1:hash2")
    mock_fetch.invalidate.assert_called()


@pytest.mark.asyncio
async def test_unraid_restart_docker_stop_fails_start_never_issued():
    from backend.integrations import unraid
    with patch("backend.integrations.unraid.resolve_container_id", new_callable=AsyncMock, return_value="hash1:hash2"), \
         patch("backend.integrations.unraid._docker_mutation", new_callable=AsyncMock, return_value=(False, "no such container")) as mock_mut:
        result = await unraid.restart_docker("plex")

    assert result["success"] is False
    assert "stopped" not in result
    assert "stop failed" in result["error"]
    mock_mut.assert_awaited_once()  # start never attempted


@pytest.mark.asyncio
async def test_unraid_restart_docker_start_fails_after_stop_succeeds():
    """The critical case: stop succeeds, start fails -- container is DOWN.
    Must be distinguishable ('stopped': True) from a routine failure."""
    from backend.integrations import unraid
    data = _containers_data([{"id": "hash1:hash2", "name": "plex", "status": "Exited", "state": "EXITED"}])
    with patch("backend.integrations.unraid.resolve_container_id", new_callable=AsyncMock, return_value="hash1:hash2"), \
         patch("backend.integrations.unraid.fetch", new_callable=AsyncMock, return_value=data) as mock_fetch, \
         patch("backend.integrations.unraid._docker_mutation", new_callable=AsyncMock, side_effect=[(True, ""), (False, "connection reset")]), \
         patch("asyncio.sleep", new_callable=AsyncMock):
        mock_fetch.invalidate = MagicMock()
        result = await unraid.restart_docker("plex")

    assert result["success"] is False
    assert result["stopped"] is True
    assert "stopped but failed to restart" in result["error"]


@pytest.mark.asyncio
async def test_unraid_restart_docker_polls_until_stopped():
    """Fires start only once fetch() reports the container is no longer
    RUNNING -- not immediately after the stop call returns."""
    from backend.integrations import unraid
    still_running = _containers_data([{"id": "hash1:hash2", "name": "plex", "status": "Up", "state": "RUNNING"}])
    now_stopped = _containers_data([{"id": "hash1:hash2", "name": "plex", "status": "Exited", "state": "EXITED"}])
    with patch("backend.integrations.unraid.resolve_container_id", new_callable=AsyncMock, return_value="hash1:hash2"), \
         patch("backend.integrations.unraid.fetch", new_callable=AsyncMock, side_effect=[still_running, still_running, now_stopped]) as mock_fetch, \
         patch("backend.integrations.unraid._docker_mutation", new_callable=AsyncMock, side_effect=[(True, ""), (True, "")]), \
         patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        mock_fetch.invalidate = MagicMock()
        result = await unraid.restart_docker("plex")

    assert result == {"success": True}
    assert mock_fetch.await_count == 3
    assert mock_sleep.await_count == 2  # slept between the 2 "still running" polls


@pytest.mark.asyncio
async def test_unraid_restart_docker_unknown_name_returns_error_no_mutation_attempted():
    from backend.integrations import unraid
    with patch("backend.integrations.unraid.resolve_container_id", new_callable=AsyncMock, side_effect=ValueError("no container found with name 'ghost'")), \
         patch("backend.integrations.unraid._docker_mutation", new_callable=AsyncMock) as mock_mut:
        result = await unraid.restart_docker("ghost")

    assert result == {"success": False, "error": "no container found with name 'ghost'"}
    mock_mut.assert_not_awaited()


@pytest.mark.asyncio
async def test_docker_mutation_gql_errors_returns_false():
    """HTTP 200 with a GraphQL 'errors' array is a failed mutation, not a success."""
    resp = MagicMock(status_code=200)
    resp.json.return_value = {"errors": [{"message": "no such container"}]}
    with patch("httpx.AsyncClient") as mock_cls:
        mock_cls.return_value = _post_client(resp)
        from backend.integrations.unraid import _docker_mutation
        ok, err = await _docker_mutation("stop", "hash1:hash2")
    assert ok is False
    assert "no such container" in err


@pytest.mark.asyncio
async def test_docker_mutation_server_error_returns_false():
    with patch("httpx.AsyncClient") as mock_cls:
        mock_cls.return_value = _post_client(MagicMock(status_code=500))
        from backend.integrations.unraid import _docker_mutation
        ok, err = await _docker_mutation("start", "hash1:hash2")
    assert ok is False
    assert "500" in err


@pytest.mark.asyncio
async def test_docker_mutation_transport_error_returns_false():
    with patch("httpx.AsyncClient") as mock_cls:
        client = AsyncMock()
        client.__aenter__.return_value.post = AsyncMock(side_effect=Exception("connection refused"))
        mock_cls.return_value = client
        from backend.integrations.unraid import _docker_mutation
        ok, err = await _docker_mutation("stop", "hash1:hash2")
    assert ok is False
    assert "connection refused" in err


def test_unraid_data_defaults():
    from backend.integrations.unraid import UnraidData
    data = UnraidData()
    assert data.array_status == "unknown"
    assert data.docker_containers == []
    assert data.storage_total_gb == 0.0
    assert data.storage_used_gb == 0.0
