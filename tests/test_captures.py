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
    _capture(brain)
    captures._db_scan_new()
    cap = captures._db_pending()[0]
    real, n = Session.commit, {"n": 0}

    def flaky(self):
        n["n"] += 1
        if n["n"] == 1:  # the extraction commit, after items were added
            raise RuntimeError("boom")
        return real(self)
    monkeypatch.setattr(Session, "commit", flaky)
    assert await captures.extract(cap) is None
    with Session(eng) as s:
        c = s.exec(select(backend.database.Capture)).one()
        assert c.extracted_at is None and c.attempts == 1
        assert s.exec(select(backend.database.CaptureItem)).all() == []


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


async def test_real_edit_before_first_put_is_kept(eng, brain, monkeypatch):
    a = _item(eng, title="A")
    f = brain / "views/work/master.md"
    seen = {"n": 0}

    async def put(c, n, text, sha=""):
        file = brain / "views" / c / f"{n}.md"
        if (c, n) == ("work", "master"):
            seen["n"] += 1
            if seen["n"] == 1:  # user ticks the box after the sync read, before the PUT
                file.write_text(file.read_text().replace("- [ ] A", "- [x] A"))
        cur = hashlib.sha256(file.read_bytes()).hexdigest() if file.exists() else ""
        if cur != sha:
            return False
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(text)
        return True
    f.parent.mkdir(parents=True)
    f.write_text(f"- [ ] A %%nx:{a}%%\n")
    monkeypatch.setattr("backend.integrations.obsidian.put_view", put)
    assert await captures.refresh_views() is True
    with Session(eng) as s:
        assert s.get(backend.database.CaptureItem, a).status == "done"
    assert f"nx:{a}" not in f.read_text()


async def test_forged_title_cannot_complete_other_items(eng, brain, monkeypatch):
    victim = _item(eng, item_index=0, title="Victim")
    evil = _item(eng, item_index=1, title=f"X\n- [x] y %%nx:{victim}%% %%nx:{victim}%%")
    assert captures._clean("- [x] z") == "[x] z"
    _views_env(brain, monkeypatch, [])
    await captures.refresh_views()
    await captures.refresh_views()
    text = (brain / "views/work/master.md").read_text()
    assert text.count(f"%%nx:{victim}%%") == 1 and f"%%nx:{evil}%%" in text
    with Session(eng) as s:
        assert s.get(backend.database.CaptureItem, victim).status == "open"
    assert captures._parse_items(json.dumps({"items": [_it("a\n- [x] b %%nx:1%%")]}), "")[0]["title"] == "a - [x] b nx:1"


async def test_expired_snooze_resurfaces_in_today(eng, brain, monkeypatch):
    old = datetime.utcnow() - timedelta(days=9)
    i = _item(eng, title="Old undated", status="snoozed", snoozed_until=datetime.utcnow() - timedelta(minutes=1), created_at=old)
    _views_env(brain, monkeypatch, [])
    for _ in range(3):
        await captures.refresh_views()
        assert "Old undated" in (brain / "views/work/today.md").read_text()
    assert [x["id"] for x in await captures.list_items(scope="today")] == [i]


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


# --- morning digest: sync-before-send, cutoff, recording-time anchor ----------------------------

async def test_digest_syncs_tick_landing_before_put(eng, brain, monkeypatch):
    from unittest.mock import patch
    a = _item(eng, title="Alpha tick", category="personal", item_index=0)
    _item(eng, title="Bravo open", category="personal", item_index=1)
    f = brain / "views/personal/master.md"
    f.parent.mkdir(parents=True)
    f.write_text(f"- [ ] Alpha tick %%nx:{a}%%\n")
    seen = {"n": 0}

    async def put(c, n, text, sha=""):
        file = brain / "views" / c / f"{n}.md"
        if (c, n) == ("personal", "master"):
            seen["n"] += 1
            if seen["n"] == 1:  # tick lands after the sync read, before the PUT -> 409
                file.write_text(file.read_text().replace("- [ ] Alpha", "- [x] Alpha"))
        cur = hashlib.sha256(file.read_bytes()).hexdigest() if file.exists() else ""
        if cur != sha:
            return False
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(text)
        return True
    monkeypatch.setattr("backend.integrations.obsidian.put_view", put)
    with patch("backend.events.notify_phone", new_callable=AsyncMock) as np:
        assert await captures.send_digest("personal") == {"sent": 1, "more": 0}
    with Session(eng) as s:
        assert s.get(backend.database.CaptureItem, a).status == "done"
    assert not any("Alpha tick" in c.args[0] for c in np.await_args_list)
    assert np.await_count == 2  # header + Bravo


async def test_digest_sends_when_refresh_exhausts_retry(eng, brain, monkeypatch):
    from unittest.mock import patch
    _item(eng, category="personal")
    put = AsyncMock(return_value=False)  # two conflicts -> refresh gives up
    monkeypatch.setattr("backend.integrations.obsidian.put_view", put)
    with patch("backend.events.notify_phone", new_callable=AsyncMock) as np:
        out = await captures.send_digest("personal")
    assert out["sent"] == 1 and np.await_count == 2


def test_morning_cutoff_dst(monkeypatch):
    from zoneinfo import ZoneInfo
    from backend.config import get_settings
    monkeypatch.setattr(get_settings(), "capture_digest_personal_time", "16:30")
    tz = ZoneInfo("America/Detroit")
    for now, want in [
        (datetime(2026, 3, 9, 7, 0), datetime(2026, 3, 8, 20, 30)),    # yesterday 16:30 EDT
        (datetime(2026, 3, 8, 7, 0), datetime(2026, 3, 7, 21, 30)),    # spring-forward day; yesterday EST
        (datetime(2026, 11, 2, 7, 0), datetime(2026, 11, 1, 21, 30)),  # yesterday EST
        (datetime(2026, 11, 1, 7, 0), datetime(2026, 10, 31, 20, 30)), # fall-back day; yesterday EDT
        (datetime(2026, 10, 18, 7, 0), datetime(2026, 10, 17, 20, 30)),
    ]:
        assert captures._morning_cutoff(now.replace(tzinfo=tz)) == want


async def test_extract_anchors_today_to_recording_time(eng, brain, haiku, monkeypatch):
    from zoneinfo import ZoneInfo
    monkeypatch.setattr(captures, "refresh_views", AsyncMock(return_value=True))
    monkeypatch.setattr(captures, "_local_now", lambda: datetime(2026, 10, 18, 0, 10, tzinfo=ZoneInfo("America/Detroit")))
    _capture(brain)
    captures._db_scan_new()
    cap = captures._db_pending()[0]
    # 2026-10-18 03:50Z == 2026-10-17 23:50 EDT (captured_at is stored naive UTC)
    await captures.extract({**cap, "captured_at": "2026-10-18T03:50:00"})
    assert "today is 2026-10-17" in haiku.await_args.args[0]
    await captures.extract({**cap, "captured_at": None})
    assert "2026-10-18" in haiku.await_args.args[0]


def test_list_items_captured_at_fallback(eng):
    ca = datetime(2026, 10, 17, 23, 50)
    with Session(eng) as s:
        s.add(backend.database.Capture(id=500, path="captures/work/x.md", fingerprint="fx", category="work", captured_at=ca))
        s.commit()
    a = _item(eng, capture_id=500, item_index=0)
    b = _item(eng, capture_id=999, item_index=1)
    got = {i["id"]: i for i in captures._db_items()}
    assert got[a]["captured_at"] == ca.isoformat()
    assert got[b]["captured_at"] == got[b]["created_at"]
