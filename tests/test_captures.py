"""Quill capture tasks (backend/agents/captures.py + backend/api/captures.py)."""
import hashlib
import json
from datetime import datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

import backend.database  # noqa: F401 -- registers table metadata
from backend.agents import captures


@pytest.fixture
def eng(monkeypatch):
    e = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(e)
    monkeypatch.setattr("backend.database.engine", e)
    return e


@pytest.fixture
def brain(tmp_path, monkeypatch):
    monkeypatch.setattr(captures, "_brain", lambda: tmp_path)
    return tmp_path


def _capture(brain, name="Quill-1.md", category="work", body="I need to email Bob the invoice by Friday"):
    d = brain / "captures" / category
    d.mkdir(parents=True, exist_ok=True)
    fp = hashlib.sha256((category + body + name).encode()).hexdigest()
    (d / name).write_text(f'---\ncategory: "{category}"\nfingerprint: "{fp}"\n---\n\n{body}', encoding="utf-8")
    return fp


def _haiku(items):
    return AsyncMock(return_value=json.dumps({"items": items}))


def _it(title="Email Bob the invoice", kind="task", due=""):
    return {"kind": kind, "title": title, "notes": "", "due_date": due}


@pytest.fixture
def haiku(monkeypatch):
    m = _haiku([_it()])
    monkeypatch.setattr("backend.agents.router.haiku", m)
    return m


async def _run():
    return await captures.run_capture_extract()


async def test_extract_once_per_fingerprint(eng, brain, haiku, monkeypatch):
    monkeypatch.setattr(captures, "refresh_views", AsyncMock(return_value=True))
    _capture(brain)
    r1 = await _run()
    r2 = await _run()
    assert r1["extracted"] == 1 and r2["extracted"] == 0 and haiku.await_count == 1
    with Session(eng) as s:
        assert len(s.exec(select(backend.database.CaptureItem)).all()) == 1
        assert len(s.exec(select(backend.database.Capture)).all()) == 1


async def test_failure_retries_then_stops_at_three(eng, brain, monkeypatch):
    monkeypatch.setattr(captures, "refresh_views", AsyncMock(return_value=True))
    m = AsyncMock(return_value="not json")
    monkeypatch.setattr("backend.agents.router.haiku", m)
    _capture(brain)
    for _ in range(5):
        await _run()
    assert m.await_count == 3
    with Session(eng) as s:
        c = s.exec(select(backend.database.Capture)).one()
        assert c.extracted_at is None and c.attempts == 3 and c.error


async def test_items_and_extracted_at_commit_together(eng, brain, haiku, monkeypatch):
    monkeypatch.setattr(captures, "refresh_views", AsyncMock(return_value=True))
    monkeypatch.setattr(captures, "_sig_key", lambda t: (_ for _ in ()).throw(RuntimeError("boom")))
    # seed an open item so the similarity pass actually calls _sig_key inside the transaction
    _capture(brain)
    captures._db_scan_new()
    with Session(eng) as s:
        s.add(backend.database.CaptureItem(capture_id=99, item_index=0, category="work", kind="task", title="x"))
        s.commit()
    await _run()
    with Session(eng) as s:
        assert s.exec(select(backend.database.Capture)).one().extracted_at is None
        assert len(s.exec(select(backend.database.CaptureItem)).all()) == 1  # only the seed row


async def test_item_cap_and_bad_kind(eng, brain, monkeypatch):
    monkeypatch.setattr(captures, "refresh_views", AsyncMock(return_value=True))
    monkeypatch.setattr("backend.agents.router.haiku", _haiku([_it(f"Task number {n}") for n in range(12)]))
    _capture(brain)
    await _run()
    with Session(eng) as s:
        assert len(s.exec(select(backend.database.CaptureItem)).all()) == 8
    with pytest.raises(ValueError):
        captures._parse_items(json.dumps({"items": [_it(kind="bogus")]}), "")


def test_unique_capture_item_index(eng):
    from sqlalchemy.exc import IntegrityError
    with Session(eng) as s:
        s.add(backend.database.CaptureItem(capture_id=1, item_index=0, category="work", kind="task", title="a"))
        s.add(backend.database.CaptureItem(capture_id=1, item_index=0, category="work", kind="task", title="b"))
        with pytest.raises(IntegrityError):
            s.commit()


async def test_similarity_flags_but_keeps(eng, brain, monkeypatch):
    monkeypatch.setattr(captures, "refresh_views", AsyncMock(return_value=True))
    monkeypatch.setattr("backend.agents.router.haiku", _haiku([_it("Email Bob about the quarterly invoice")]))
    _capture(brain, "a.md", body="one")
    _capture(brain, "b.md", body="two")
    await _run()
    with Session(eng) as s:
        rows = s.exec(select(backend.database.CaptureItem).order_by(backend.database.CaptureItem.id)).all()
        assert len(rows) == 2 and rows[0].similar_to_id is None and rows[1].similar_to_id == rows[0].id


def _item(eng, **kw):
    with Session(eng) as s:
        row = backend.database.CaptureItem(**{"capture_id": 1, "item_index": 0, "category": "work", "kind": "task", "title": "T", **kw})
        s.add(row)
        s.commit()
        s.refresh(row)
        return row.id


async def test_set_status_transitions(eng):
    i = _item(eng)
    assert await captures.set_status(i, "done", "api") == "ok"
    assert await captures.set_status(i, "done", "api") == "already_done"
    assert await captures.set_status(i, "snoozed", "api") == "already_done"
    assert await captures.set_status(i, "open", "api") == "ok"
    assert await captures.set_status(i, "open", "api") == "already_open"
    assert await captures.set_status(i, "snoozed", "api") == "ok"
    assert await captures.set_status(i, "snoozed", "api") == "already_snoozed"
    assert await captures.set_status(999, "done", "api") == "not_found"


async def test_expired_snooze_returns_open(eng):
    i = _item(eng, status="snoozed", snoozed_until=datetime.utcnow() - timedelta(minutes=1))
    j = _item(eng, item_index=1, status="snoozed", snoozed_until=datetime.utcnow() + timedelta(days=1))
    captures._db_unsnooze()
    with Session(eng) as s:
        assert s.get(backend.database.CaptureItem, i).status == "open"
        assert s.get(backend.database.CaptureItem, j).status == "snoozed"


def _views_env(brain, monkeypatch, puts):
    async def put(c, n, text, sha=""):
        puts.append((c, n, text, sha))
        f = brain / "views" / c / f"{n}.md"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text, encoding="utf-8")
        return True
    monkeypatch.setattr("backend.integrations.obsidian.put_view", put)


async def test_render_links_and_skip_unchanged(eng, brain, monkeypatch):
    with Session(eng) as s:
        s.add(backend.database.Capture(id=1, path="captures/work/Quill-1.md", fingerprint="f", category="work"))
        s.commit()
    i = _item(eng, title="Email Bob")
    puts: list = []
    _views_env(brain, monkeypatch, puts)
    assert await captures.refresh_views()
    text = (brain / "views/work/today.md").read_text()
    assert f"%%nx:{i}%%" in text and "[[captures/work/Quill-1|source]]" in text
    n = len(puts)
    assert await captures.refresh_views()
    assert len(puts) == n  # nothing changed -> no PUT


async def test_sync_marks_x_done_ignores_unticked(eng, brain, monkeypatch):
    a, b = _item(eng, title="A"), _item(eng, item_index=1, title="B")
    puts: list = []
    _views_env(brain, monkeypatch, puts)
    f = brain / "views/work/master.md"
    f.parent.mkdir(parents=True)
    f.write_text(f"- [x] A %%nx:{a}%%\n- [ ] B %%nx:{b}%%\n")
    await captures.refresh_views()
    with Session(eng) as s:
        assert s.get(backend.database.CaptureItem, a).status == "done"
        assert s.get(backend.database.CaptureItem, a).done_by == "obsidian"
        assert s.get(backend.database.CaptureItem, b).status == "open"
    assert "%%nx:%d%%" % a not in (brain / "views/work/master.md").read_text()


async def test_conflict_resyncs_and_retries_once(eng, brain, monkeypatch):
    _item(eng)
    calls = []

    async def put(c, n, text, sha=""):
        calls.append(sha)
        return False
    monkeypatch.setattr("backend.integrations.obsidian.put_view", put)
    assert await captures.refresh_views() is False
    assert len(calls) == 2  # one try + one retry, then give up


async def test_snoozed_hidden_from_views(eng, brain, monkeypatch):
    _item(eng, title="Hidden", status="snoozed", snoozed_until=datetime.utcnow() + timedelta(days=1))
    _views_env(brain, monkeypatch, [])
    await captures.refresh_views()
    assert "Hidden" not in (brain / "views/work/master.md").read_text()


# --- API -------------------------------------------------------------------

@pytest.fixture
def client(eng):
    from backend.api import captures as api
    app = FastAPI()
    app.include_router(api.router, prefix="/api/captures")
    return TestClient(app)


@pytest.mark.parametrize("method,path", [
    ("get", "/api/captures/items"), ("post", "/api/captures/items/1/done"),
    ("post", "/api/captures/items/1/reopen"), ("post", "/api/captures/items/1/snooze"),
    ("patch", "/api/captures/items/1"),
])
def test_api_requires_key(client, method, path):
    assert getattr(client, method)(path).status_code == 401


def test_api_done_flow(client, eng, auth_headers):
    i = _item(eng)
    r = client.post(f"/api/captures/items/{i}/done", headers=auth_headers)
    assert r.status_code == 200 and r.json() == {"id": i, "status": "done", "done_by": "api"}
    assert client.post(f"/api/captures/items/{i}/done", headers=auth_headers).status_code == 409
    assert client.post("/api/captures/items/999/done", headers=auth_headers).status_code == 404
    assert client.get("/api/captures/items?status=done", headers=auth_headers).json()[0]["id"] == i


def test_api_patch_and_snooze(client, eng, auth_headers):
    i = _item(eng)
    r = client.patch(f"/api/captures/items/{i}", json={"title": "New", "due_at": "2026-10-20T00:00:00"}, headers=auth_headers)
    assert r.status_code == 200 and r.json()["title"] == "New" and r.json()["due_at"].startswith("2026-10-20")
    assert client.patch(f"/api/captures/items/{i}", json={"kind": "bogus"}, headers=auth_headers).status_code == 422
    assert client.post(f"/api/captures/items/{i}/snooze", json={"days": 2}, headers=auth_headers).status_code == 200
    assert client.post(f"/api/captures/items/{i}/snooze", headers=auth_headers).status_code == 409
