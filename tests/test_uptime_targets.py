"""Tier B10 — generic HTTP uptime targets in the 2-min uptime job."""
import pytest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from sqlmodel import Session, SQLModel, create_engine, select
from sqlalchemy.pool import StaticPool


@pytest.fixture
def eng(monkeypatch):
    import backend.database  # noqa: F401 — register models on SQLModel.metadata
    e = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(e)
    monkeypatch.setattr("backend.database.engine", e)
    return e


def _settings_with(targets: str):
    s = MagicMock()
    s.uptime_http_targets = targets
    return s


def test_parse_uptime_http_targets(monkeypatch):
    from backend import scheduler
    monkeypatch.setattr(
        "backend.config.get_settings",
        lambda: _settings_with(
            "glp|http://x:8765|200,\nopenwebui|http://y:3000,"
            "bad-entry-no-pipe,|http://no-name,named|http://z|not-an-int"
        ),
    )
    targets = scheduler._parse_uptime_targets()
    assert ("glp", "http://x:8765", 200) in targets
    assert ("openwebui", "http://y:3000", 200) in targets  # default expect
    assert ("named", "http://z", 200) in targets  # bad int -> default
    assert len(targets) == 3  # malformed entries skipped


def test_parse_uptime_targets_empty(monkeypatch):
    from backend import scheduler
    monkeypatch.setattr("backend.config.get_settings", lambda: _settings_with(""))
    assert scheduler._parse_uptime_targets() == []


@pytest.mark.asyncio
async def test_record_uptime_http_targets(eng, monkeypatch):
    from backend import scheduler
    from backend.database import UptimeSample

    # No integrations: empty sources dict via patching module fetches is heavy;
    # instead patch each integration health_check to a cheap False-free probe.
    async def _ok():
        return True

    for mod in ("adguard", "channels_dvr", "github", "hermes", "homeassistant",
                "obsidian", "openrouter", "proxmox", "unifi", "unraid", "weather"):
        monkeypatch.setattr(
            f"backend.integrations.{mod}.health_check", _ok, raising=False
        )

    monkeypatch.setattr(
        "backend.config.get_settings",
        lambda: _settings_with("up-target|http://fake-up|200,down-target|http://fake-down|200"),
    )

    class _FakeClient:
        def __init__(self, *a, **k):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def get(self, url):
            if "fake-up" in url:
                return SimpleNamespace(status_code=200)
            raise ConnectionError("refused")

    with patch("httpx.AsyncClient", _FakeClient):
        await scheduler._record_uptime()

    with Session(eng) as s:
        rows = {r.source: r for r in s.exec(select(UptimeSample)).all()}
    assert rows["up-target"].ok is True
    assert rows["down-target"].ok is False


@pytest.mark.asyncio
async def test_record_uptime_empty_targets_no_extra_rows(eng, monkeypatch):
    from backend import scheduler
    from backend.database import UptimeSample

    async def _ok():
        return True

    for mod in ("adguard", "channels_dvr", "github", "hermes", "homeassistant",
                "obsidian", "openrouter", "proxmox", "unifi", "unraid", "weather"):
        monkeypatch.setattr(
            f"backend.integrations.{mod}.health_check", _ok, raising=False
        )
    monkeypatch.setattr("backend.config.get_settings", lambda: _settings_with(""))

    await scheduler._record_uptime()

    with Session(eng) as s:
        sources = {r.source for r in s.exec(select(UptimeSample)).all()}
    assert len(sources) == 11  # only the integrations (incl. proxmox), no extras
