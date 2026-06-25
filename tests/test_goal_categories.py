"""Tests for the goal category vocabulary, normalization, and API filter (Spec §3 category taxonomy).

Covers:
1. normalize_category: canonical vocab values + variants -> canonical; garbage/None -> "other".
2. propose() persists normalized category: STORAGE/whitespace -> "storage"; nonsense -> "other"; omit -> "other".
3. proposer auto-tags goals: item with category="maintenance" -> stored "maintenance"; bogus -> "other".
4. API: GET /categories; GET /?category=storage filter; propose with category passes through normalized.
"""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

import backend.database  # noqa: F401  — registers table metadata


# ---------------------------------------------------------------------------
# Engine fixture (in-memory, isolated per test)
# ---------------------------------------------------------------------------

def make_engine():
    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(eng)
    return eng


@pytest.fixture
def eng(monkeypatch):
    e = make_engine()
    monkeypatch.setattr("backend.database.engine", e)
    return e


# ---------------------------------------------------------------------------
# Pool stub (prevents real task execution)
# ---------------------------------------------------------------------------

def _mock_pool():
    pool = MagicMock()
    pool.enqueue = AsyncMock()
    return pool


# ---------------------------------------------------------------------------
# Proposer test helpers
# ---------------------------------------------------------------------------

def _seed_state(eng, autonomy=True):
    from backend.database import SystemState
    with Session(eng) as s:
        row = s.get(SystemState, 1)
        if row is None:
            row = SystemState(id=1)
            s.add(row)
        row.autonomy_enabled = autonomy
        row.daily_budget_usd = 25.0
        row.per_task_budget_usd = 5.0
        s.commit()


def _all_goals(eng):
    from backend.database import Goal
    with Session(eng) as s:
        return s.exec(select(Goal)).all()


def _fake_fetch():
    return SimpleNamespace(
        entities=[], alerts=[], docker_containers=[],
        array_status="started", storage_used_gb=1.0, storage_total_gb=10.0,
        recording_now=[], blocked_today=0, blocked_pct=0.0,
        filtering_enabled=True, summary="Clear, 70°F",
    )


def _mock_integrations(monkeypatch):
    fake = _fake_fetch()

    async def _fetch(*a, **k):
        return fake

    for mod_path in (
        "backend.integrations.homeassistant.fetch",
        "backend.integrations.unraid.fetch",
        "backend.integrations.channels_dvr.fetch",
        "backend.integrations.adguard.fetch",
        "backend.integrations.weather.fetch",
    ):
        monkeypatch.setattr(mod_path, _fetch)


# ---------------------------------------------------------------------------
# 1. normalize_category — pure function tests
# ---------------------------------------------------------------------------

def test_normalize_category_canonical_values():
    """Each vocab entry maps to itself."""
    from backend.agents.goals import GOAL_CATEGORIES, normalize_category
    for cat in GOAL_CATEGORIES:
        assert normalize_category(cat) == cat, f"Expected {cat!r} -> {cat!r}"


def test_normalize_category_uppercase():
    from backend.agents.goals import normalize_category
    assert normalize_category("STORAGE") == "storage"
    assert normalize_category("NETWORK") == "network"
    assert normalize_category("MAINTENANCE") == "maintenance"


def test_normalize_category_whitespace():
    from backend.agents.goals import normalize_category
    assert normalize_category("  media  ") == "media"
    assert normalize_category("  MONITORING  ") == "monitoring"


def test_normalize_category_mixed_case_whitespace():
    from backend.agents.goals import normalize_category
    assert normalize_category("  Knowledge  ") == "knowledge"


def test_normalize_category_empty_string():
    from backend.agents.goals import normalize_category
    assert normalize_category("") == "other"


def test_normalize_category_none():
    from backend.agents.goals import normalize_category
    assert normalize_category(None) == "other"


def test_normalize_category_bogus():
    from backend.agents.goals import normalize_category
    assert normalize_category("bogus") == "other"
    assert normalize_category("xyz123") == "other"
    assert normalize_category("infrastructure") == "other"


def test_normalize_category_other_is_canonical():
    from backend.agents.goals import normalize_category
    assert normalize_category("other") == "other"
    assert normalize_category("OTHER") == "other"


# ---------------------------------------------------------------------------
# 2. propose() persists normalized category
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_propose_normalized_category_storage(eng):
    """propose(..., category='STORAGE ') -> stored goal has category='storage'."""
    from backend.agents import goals

    result = await goals.propose(
        "Expand NAS storage",
        "Add more drives to the Unraid array.",
        category="STORAGE ",
    )
    assert result["status"] == "proposed"
    assert result["goal"]["category"] == "storage"

    from backend.database import Goal
    with Session(eng) as s:
        rows = s.exec(select(Goal)).all()
    assert len(rows) == 1
    assert rows[0].category == "storage"


@pytest.mark.asyncio
async def test_propose_nonsense_category_becomes_other(eng):
    """propose(..., category='nonsense') -> stored goal has category='other'."""
    from backend.agents import goals

    result = await goals.propose(
        "Some goal",
        "Do something unclassifiable.",
        category="nonsense",
    )
    assert result["status"] == "proposed"
    assert result["goal"]["category"] == "other"


@pytest.mark.asyncio
async def test_propose_no_category_defaults_to_other(eng):
    """propose() with no category argument -> stored goal has category='other'."""
    from backend.agents import goals

    result = await goals.propose(
        "Another goal",
        "Yet another goal with no category.",
    )
    assert result["status"] == "proposed"
    assert result["goal"]["category"] == "other"


@pytest.mark.asyncio
async def test_propose_none_category_defaults_to_other(eng):
    """propose(..., category=None) -> stored goal has category='other'."""
    from backend.agents import goals

    result = await goals.propose(
        "No category goal",
        "Category is explicitly None.",
        category=None,
    )
    assert result["status"] == "proposed"
    assert result["goal"]["category"] == "other"


# ---------------------------------------------------------------------------
# 3. Proposer auto-tags goals with category
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_proposer_tags_maintenance_category(eng, monkeypatch):
    """Opus returns category='maintenance' -> stored goal.category == 'maintenance'."""
    _seed_state(eng, autonomy=True)
    _mock_integrations(monkeypatch)

    opus_response = json.dumps([
        {
            "title": "Check system health",
            "description": "Run a full system diagnostics pass on all nodes.",
            "risk": "low",
            "reversibility": "reversible",
            "confidence": 0.8,
            "category": "maintenance",
        }
    ])

    with patch("backend.agents.router.sonnet", new=AsyncMock(return_value=opus_response)):
        with patch("backend.config.get_settings") as mock_settings:
            s = MagicMock()
            s.proposer_max_per_tick = 3
            s.goal_ttl_seconds = 86400
            s.goal_debounce_seconds = 3600
            s.auto_approve_low_risk = False
            mock_settings.return_value = s

            from backend.agents.proposer import propose_goals_tick
            result = await propose_goals_tick()

    assert result["status"] == "ok"
    assert result["count_proposed"] == 1

    goals_rows = _all_goals(eng)
    assert len(goals_rows) == 1
    assert goals_rows[0].category == "maintenance"


@pytest.mark.asyncio
async def test_proposer_bogus_category_becomes_other(eng, monkeypatch):
    """Opus returns a bogus category -> stored goal.category == 'other'."""
    _seed_state(eng, autonomy=True)
    _mock_integrations(monkeypatch)

    opus_response = json.dumps([
        {
            "title": "Random task",
            "description": "Do some unclassified thing on the homelab.",
            "risk": "low",
            "reversibility": "reversible",
            "confidence": 0.7,
            "category": "infrastructure",  # not in the vocabulary
        }
    ])

    with patch("backend.agents.router.sonnet", new=AsyncMock(return_value=opus_response)):
        with patch("backend.config.get_settings") as mock_settings:
            s = MagicMock()
            s.proposer_max_per_tick = 3
            s.goal_ttl_seconds = 86400
            s.goal_debounce_seconds = 3600
            s.auto_approve_low_risk = False
            mock_settings.return_value = s

            from backend.agents.proposer import propose_goals_tick
            result = await propose_goals_tick()

    assert result["status"] == "ok"
    goals_rows = _all_goals(eng)
    assert len(goals_rows) == 1
    assert goals_rows[0].category == "other"


@pytest.mark.asyncio
async def test_proposer_missing_category_becomes_other(eng, monkeypatch):
    """Opus returns no 'category' key -> stored goal.category == 'other'."""
    _seed_state(eng, autonomy=True)
    _mock_integrations(monkeypatch)

    opus_response = json.dumps([
        {
            "title": "Orphan task",
            "description": "Task with no category field at all.",
            "risk": "low",
            "reversibility": "reversible",
            "confidence": 0.65,
            # no "category" key
        }
    ])

    with patch("backend.agents.router.sonnet", new=AsyncMock(return_value=opus_response)):
        with patch("backend.config.get_settings") as mock_settings:
            s = MagicMock()
            s.proposer_max_per_tick = 3
            s.goal_ttl_seconds = 86400
            s.goal_debounce_seconds = 3600
            s.auto_approve_low_risk = False
            mock_settings.return_value = s

            from backend.agents.proposer import propose_goals_tick
            result = await propose_goals_tick()

    assert result["status"] == "ok"
    goals_rows = _all_goals(eng)
    assert len(goals_rows) == 1
    assert goals_rows[0].category == "other"


# ---------------------------------------------------------------------------
# 4. API: GET /categories, GET /?category=, propose with category
# ---------------------------------------------------------------------------

@pytest.fixture
def goals_client(tmp_path, monkeypatch):
    vault_key = tmp_path / ".vault.key"
    vault_file = tmp_path / "nexus.vault"
    vault_key.write_bytes(b"A" * 32)
    vault_file.write_text("{}")
    monkeypatch.chdir(tmp_path)

    test_engine = make_engine()
    monkeypatch.setattr("backend.database.engine", test_engine)

    from backend.database import get_session

    def override_session():
        with Session(test_engine) as session:
            yield session

    with patch("backend.database.create_db_and_tables"), \
         patch("backend.scheduler.setup_scheduler"), \
         patch("backend.scheduler.scheduler") as sched, \
         patch("backend.agents.memo_watcher.start_watcher_blocking"), \
         patch("backend.agents.memo_watcher.stop_watcher", new_callable=AsyncMock):
        sched.running = False
        from backend.main import app
        app.dependency_overrides[get_session] = override_session
        with TestClient(app) as c:
            c._engine = test_engine
            yield c
        app.dependency_overrides.clear()


def test_api_get_categories(goals_client, auth_headers):
    """GET /api/goals/categories returns the 7-item vocabulary list."""
    from backend.agents.goals import GOAL_CATEGORIES
    resp = goals_client.get("/api/goals/categories", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert "categories" in data
    cats = data["categories"]
    assert len(cats) == 7
    assert cats == GOAL_CATEGORIES
    assert "other" in cats


def test_api_categories_requires_auth(goals_client):
    """GET /api/goals/categories requires Bearer auth."""
    resp = goals_client.get("/api/goals/categories")
    assert resp.status_code == 401


def test_api_list_goals_category_filter(goals_client, auth_headers, monkeypatch):
    """GET /api/goals/?category=storage returns only storage goals."""
    pool = _mock_pool()
    monkeypatch.setattr("backend.agents.goals.get_pool", lambda: pool)

    # Seed 2 storage goals + 1 network goal via the API.
    goals_client.post(
        "/api/goals/propose", headers=auth_headers,
        json={"title": "Add NAS drives", "description": "Expand Unraid array.", "category": "storage"},
    )
    goals_client.post(
        "/api/goals/propose", headers=auth_headers,
        json={"title": "Archive old data", "description": "Move data to cold storage.", "category": "storage"},
    )
    goals_client.post(
        "/api/goals/propose", headers=auth_headers,
        json={"title": "Check VLANs", "description": "Review network VLAN config.", "category": "network"},
    )

    resp = goals_client.get("/api/goals/?category=storage", headers=auth_headers)
    assert resp.status_code == 200
    items = resp.json()
    assert len(items) == 2
    for item in items:
        assert item["category"] == "storage"


def test_api_list_goals_category_filter_network(goals_client, auth_headers, monkeypatch):
    """GET /api/goals/?category=network returns only network goals."""
    pool = _mock_pool()
    monkeypatch.setattr("backend.agents.goals.get_pool", lambda: pool)

    goals_client.post(
        "/api/goals/propose", headers=auth_headers,
        json={"title": "Add NAS drives", "description": "Expand Unraid array.", "category": "storage"},
    )
    goals_client.post(
        "/api/goals/propose", headers=auth_headers,
        json={"title": "Check VLANs", "description": "Review network VLAN config.", "category": "network"},
    )

    resp = goals_client.get("/api/goals/?category=network", headers=auth_headers)
    assert resp.status_code == 200
    items = resp.json()
    assert len(items) == 1
    assert items[0]["category"] == "network"


def test_api_list_goals_no_filter_returns_all(goals_client, auth_headers, monkeypatch):
    """GET /api/goals/ with no category filter returns all goals."""
    pool = _mock_pool()
    monkeypatch.setattr("backend.agents.goals.get_pool", lambda: pool)

    goals_client.post(
        "/api/goals/propose", headers=auth_headers,
        json={"title": "Storage goal", "description": "A storage task.", "category": "storage"},
    )
    goals_client.post(
        "/api/goals/propose", headers=auth_headers,
        json={"title": "Network goal", "description": "A network task.", "category": "network"},
    )

    resp = goals_client.get("/api/goals/", headers=auth_headers)
    assert resp.status_code == 200
    items = resp.json()
    assert len(items) == 2


def test_api_category_filter_case_insensitive(goals_client, auth_headers, monkeypatch):
    """GET /api/goals/?category=STORAGE normalizes and returns storage goals."""
    pool = _mock_pool()
    monkeypatch.setattr("backend.agents.goals.get_pool", lambda: pool)

    goals_client.post(
        "/api/goals/propose", headers=auth_headers,
        json={"title": "Storage goal", "description": "A storage task.", "category": "storage"},
    )

    resp = goals_client.get("/api/goals/?category=STORAGE", headers=auth_headers)
    assert resp.status_code == 200
    items = resp.json()
    assert len(items) == 1
    assert items[0]["category"] == "storage"


def test_api_propose_with_category_normalizes(goals_client, auth_headers, monkeypatch):
    """Proposing via the API with category='NETWORK ' stores 'network'."""
    pool = _mock_pool()
    monkeypatch.setattr("backend.agents.goals.get_pool", lambda: pool)

    resp = goals_client.post(
        "/api/goals/propose", headers=auth_headers,
        json={
            "title": "Review UniFi config",
            "description": "Audit the UniFi network controller settings.",
            "category": "NETWORK ",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "proposed"
    assert body["goal"]["category"] == "network"


def test_api_propose_bogus_category_normalizes_to_other(goals_client, auth_headers, monkeypatch):
    """Proposing via the API with a bogus category stores 'other'."""
    pool = _mock_pool()
    monkeypatch.setattr("backend.agents.goals.get_pool", lambda: pool)

    resp = goals_client.post(
        "/api/goals/propose", headers=auth_headers,
        json={
            "title": "Misc task",
            "description": "An unclassifiable task.",
            "category": "infrastructure",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "proposed"
    assert body["goal"]["category"] == "other"


def test_api_propose_no_category_defaults_to_other(goals_client, auth_headers, monkeypatch):
    """Proposing via the API with no category field stores 'other'."""
    pool = _mock_pool()
    monkeypatch.setattr("backend.agents.goals.get_pool", lambda: pool)

    resp = goals_client.post(
        "/api/goals/propose", headers=auth_headers,
        json={"title": "Generic task", "description": "A task without a category."},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "proposed"
    assert body["goal"]["category"] == "other"
