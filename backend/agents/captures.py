"""Quill capture tasks: archived captures -> extracted items -> rendered views.

Flow (one scheduler job, `run_capture_extract`): scan `Brain/captures/*/*.md`
into `Capture` rows, ask Haiku for explicit commitments in each pending one,
then `refresh_views()` -- sync `[x]` ticks from the vault views into the DB
(complete-only), then re-render `views/{work,personal}/{today,master}.md`
through the Brain MCP server's `PUT /views`. Render always follows sync under
one lock, and the PUT carries the sha of the file the sync read, so a tick made
in between is never overwritten (409 -> re-sync, retry once).

Shape of obligations.py: sync `_db_*` helpers open their own Session (engine
re-imported per call so tests can monkeypatch it); public async functions use
asyncio.to_thread.
"""
import asyncio
import hashlib
import html
import json
import logging
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

MAX_ITEMS = 8
MAX_ATTEMPTS = 3
KINDS = ("task", "followup", "decision", "idea")
CATEGORIES = ("work", "personal")
VIEW_NAMES = ("today", "master")
_BATCH = 20  # extractions per run; the rest wait for the next tick
_VIEWS_LOCK = asyncio.Lock()
_FP_PAT = re.compile(r'^fingerprint: "([0-9a-f]{64})"$', re.M)
_TICK_PAT = re.compile(r"^- \[x\] .* %%nx:(\d+)%%\s*$", re.M | re.I)
_WORD_PAT = re.compile(r"[a-z0-9']+")
_STOP = frozenset("the and for with that this from have will need about your would could should "
                  "just into then them they what when where which".split())

_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": list(KINDS)},
                    "title": {"type": "string"},
                    "notes": {"type": "string"},
                    "due_date": {"type": "string"},
                },
                "required": ["kind", "title", "notes", "due_date"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["items"],
    "additionalProperties": False,
}


def _brain() -> Path:
    from backend.config import get_settings
    return Path(get_settings().obsidian_vault_path) / "Brain"


def _local_now() -> datetime:
    from backend.config import get_settings
    return datetime.now(ZoneInfo(get_settings().briefing_timezone))


def _item_dict(i, path: str | None = None) -> dict:
    iso = lambda d: d.isoformat() if d else None  # noqa: E731
    return {
        "id": i.id, "capture_id": i.capture_id, "category": i.category, "kind": i.kind,
        "title": i.title, "notes": i.notes, "due_at": iso(i.due_at), "status": i.status,
        "snoozed_until": iso(i.snoozed_until), "resurfaced_at": iso(i.resurfaced_at), "similar_to_id": i.similar_to_id,
        "done_at": iso(i.done_at), "done_by": i.done_by, "created_at": iso(i.created_at),
        "source_path": path,
    }


def _clean(t: str) -> str:
    """One line, no `%%`, no leading list marker: a title can never forge a
    `- [x] ... %%nx:N%%` tick line or a marker for another item."""
    t = re.sub(r"\s+", " ", str(t)).replace("%%", "").strip()
    return re.sub(r"^[-*+]\s*\[", "[", t)


def _sig_key(title: str) -> frozenset:
    """First 6 significant words of a title. # ponytail: lexical only (no
    embeddings); catches near-verbatim repeats, misses paraphrases."""
    words = [w for w in _WORD_PAT.findall(title.lower()) if len(w) > 3 and w not in _STOP]
    return frozenset(words[:6]) if len(words) >= 3 else frozenset()


# ---------------------------------------------------------------------------
# Sync DB helpers
# ---------------------------------------------------------------------------

def _db_scan_new() -> int:
    from sqlalchemy.exc import IntegrityError
    from sqlmodel import Session, select
    from backend.database import Capture, engine

    brain = _brain()
    added = 0
    for f in sorted(brain.glob("captures/*/*.md")):
        try:
            text = f.read_text(encoding="utf-8")
        except OSError:
            continue
        m = _FP_PAT.search(text[:2000])
        fp = m.group(1) if m else hashlib.sha256(text.encode()).hexdigest()
        rid = re.search(r'^request_id: "([^"]*)"$', text[:2000], re.M)
        cat = re.search(r'^captured_at: "([^"]*)"$', text[:2000], re.M)
        try:
            captured = datetime.fromisoformat(cat.group(1)).astimezone(UTC).replace(tzinfo=None) if cat and cat.group(1) else None
        except ValueError:
            captured = None
        with Session(engine) as s:
            if s.exec(select(Capture.id).where(Capture.fingerprint == fp)).first():
                continue
            s.add(Capture(path=str(f.relative_to(brain)), fingerprint=fp, category=f.parent.name,
                          request_id=rid.group(1) if rid else None, captured_at=captured))
            try:
                s.commit()
                added += 1
            except IntegrityError:
                s.rollback()
    return added


def _db_pending() -> list[dict]:
    from sqlmodel import Session, select
    from backend.database import Capture, engine

    with Session(engine) as s:
        rows = s.exec(
            select(Capture)
            .where(Capture.extracted_at.is_(None), Capture.attempts < MAX_ATTEMPTS)  # type: ignore[union-attr]
            .order_by(Capture.id).limit(_BATCH)
        ).all()
        return [{"id": c.id, "path": c.path, "category": c.category} for c in rows]


def _db_fail(capture_id: int, error: str) -> None:
    from sqlmodel import Session
    from backend.database import Capture, engine

    with Session(engine) as s:
        c = s.get(Capture, capture_id)
        if c:
            c.attempts += 1
            c.error = error[:500]
            s.add(c)
            s.commit()


def _db_commit_extraction(capture_id: int, category: str, items: list[dict]) -> int:
    """Insert items and stamp extracted_at in ONE transaction. Similarity only
    sets similar_to_id (same category, open, other capture) -- never discards."""
    from sqlmodel import Session, select
    from backend.database import Capture, CaptureItem, engine

    with Session(engine) as s:
        existing = [(i.id, _sig_key(i.title)) for i in s.exec(
            select(CaptureItem).where(CaptureItem.category == category, CaptureItem.status == "open",
                                      CaptureItem.capture_id != capture_id)).all()]
        for n, it in enumerate(items):
            key = _sig_key(it["title"])
            sim = next((i for i, k in existing if key and k == key), None)
            s.add(CaptureItem(capture_id=capture_id, item_index=n, category=category, kind=it["kind"],
                              title=it["title"], notes=it["notes"], due_at=it["due_at"], similar_to_id=sim))
        c = s.get(Capture, capture_id)
        c.extracted_at = datetime.utcnow()
        c.error = None
        s.add(c)
        s.commit()
    return len(items)


def _db_items(category: str | None = None, status: str | None = None) -> list[dict]:
    from sqlmodel import Session, select
    from backend.database import Capture, CaptureItem, engine

    with Session(engine) as s:
        q = select(CaptureItem, Capture.path).join(Capture, Capture.id == CaptureItem.capture_id, isouter=True)  # type: ignore[arg-type]
        if category:
            q = q.where(CaptureItem.category == category)
        if status:
            q = q.where(CaptureItem.status == status)
        return [_item_dict(i, p) for i, p in s.exec(q.order_by(CaptureItem.id)).all()]  # type: ignore[arg-type]


def _db_unsnooze() -> None:
    from sqlalchemy import text
    from backend.database import engine

    with engine.begin() as c:
        c.execute(text("UPDATE captureitem SET status='open', snoozed_until=NULL, resurfaced_at=:now "
                       "WHERE status='snoozed' AND snoozed_until <= :now"), {"now": datetime.utcnow()})


def _db_set_status(item_id: int, status: str, by: str, days: int) -> str:
    """Atomic transition via a guarded UPDATE; rowcount 0 -> why it didn't apply."""
    from sqlmodel import Session
    from sqlalchemy import update
    from backend.database import CaptureItem, engine

    now = datetime.utcnow()
    allowed = {"done": ("open", "snoozed"), "open": ("done", "snoozed"), "snoozed": ("open",)}[status]
    vals: dict = {"status": status, "updated_at": now}
    if status == "done":
        vals.update(done_at=now, done_by=by)
    elif status == "open":
        vals.update(done_at=None, done_by=None, snoozed_until=None)
    else:
        vals.update(snoozed_until=now + timedelta(days=days))
    with Session(engine) as s:
        res = s.exec(update(CaptureItem).where(CaptureItem.id == item_id, CaptureItem.status.in_(allowed)).values(**vals))  # type: ignore[attr-defined]
        s.commit()
        if res.rowcount:
            return "ok"
        row = s.get(CaptureItem, item_id)
    if row is None:
        return "not_found"
    return f"already_{'open' if row.status == 'open' else row.status}"


def _db_patch(item_id: int, fields: dict) -> dict | None:
    from sqlmodel import Session
    from backend.database import CaptureItem, engine

    with Session(engine) as s:
        row = s.get(CaptureItem, item_id)
        if row is None:
            return None
        for k, v in fields.items():
            setattr(row, k, _clean(v)[:200] if k == "title" else v)
        row.updated_at = datetime.utcnow()
        s.add(row)
        s.commit()
        s.refresh(row)
        return _item_dict(row)


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------

def _parse_items(raw: str, today: str) -> list[dict]:
    start, end = raw.find("{"), raw.rfind("}") + 1
    data = json.loads(raw[start:end])
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        raise ValueError("response missing items list")
    out = []
    for it in data["items"][:MAX_ITEMS]:
        if not isinstance(it, dict) or it.get("kind") not in KINDS:
            raise ValueError("bad item kind")
        title = _clean(it.get("title") or "")[:200]
        if not title:
            continue
        try:
            due = datetime.strptime(str(it.get("due_date") or ""), "%Y-%m-%d")
        except ValueError:
            due = None
        out.append({"kind": it["kind"], "title": title, "notes": _clean(it.get("notes") or "")[:1000] or None, "due_at": due})
    return out


async def extract(capture: dict) -> int | None:
    """Extract one capture. Returns item count, or None on failure (attempts+=1)."""
    from backend.agents import router
    try:
        text = await asyncio.to_thread((_brain() / capture["path"]).read_text, "utf-8")
        body = text.split("\n---\n", 1)[-1][:20000]
        today = _local_now().strftime("%Y-%m-%d (%A)")
        prompt = (
            "Extract ONLY explicit commitments from this voice-note transcript: things the speaker "
            "clearly said they will do, must do, or are waiting on someone for. Do NOT infer tasks, "
            "and do not turn observations or musings into tasks. kind: task (something to do), "
            "followup (chase/check on someone or something), decision (a decision made), idea "
            f"(an idea worth keeping). At most {MAX_ITEMS} items; an empty list is a valid answer. "
            "title: short imperative. notes: one line of context or empty string. due_date: "
            f"YYYY-MM-DD only if a date was stated or clearly implied (today is {today}), else empty string.\n\n"
            f"Transcript:\n{body}"
        )
        raw = await router.haiku(prompt, response_schema=_SCHEMA, label="capture_extract")
        items = _parse_items(raw, today)
        return await asyncio.to_thread(_db_commit_extraction, capture["id"], capture["category"], items)
    except Exception as e:
        logger.warning("capture extract failed for %s: %s", capture["path"], type(e).__name__)
        await asyncio.to_thread(_db_fail, capture["id"], f"{type(e).__name__}: {e}")
        return None


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------

def _line(i: dict) -> str:
    due = f" (due {i['due_at'][:10]})" if i["due_at"] else ""
    src = f" · [[{i['source_path'][:-3]}|source]]" if i["source_path"] else ""
    sim = f" (similar to #{i['similar_to_id']})" if i["similar_to_id"] else ""
    return f"- [ ] {_clean(i['title'])}{due}{src}{sim} %%nx:{i['id']}%%"


def _render(items: list[dict], category: str, name: str, today, day_start: datetime) -> str:
    mine = [i for i in items if i["category"] == category and i["status"] == "open"]
    tasks = [i for i in mine if i["kind"] in ("task", "followup")]
    made_today = lambda i: datetime.fromisoformat(i["created_at"]) >= day_start  # noqa: E731
    back_today = lambda i: i["resurfaced_at"] and datetime.fromisoformat(i["resurfaced_at"]) >= day_start  # noqa: E731
    due_today = lambda i: i["due_at"] and datetime.fromisoformat(i["due_at"]).date() <= today  # noqa: E731
    if name == "today":
        tasks = [i for i in tasks if made_today(i) or due_today(i) or back_today(i)]
    out = [f"# {category.title()} - {name.title()}", "", "<!-- generated by NEXUS; tick [x] to complete (complete-only: ticked items vanish on next refresh); unsorted captures appear via API only -->", ""]
    out += [_line(i) for i in tasks] or ["_Nothing open._"]
    if name == "today":
        notes = [i for i in mine if i["kind"] in ("idea", "decision") and made_today(i)]
        if notes:
            out += ["", "## Ideas & decisions", ""]
            out += [f"- ({i['kind']}) {_clean(i['title'])}" for i in notes]
    return "\n".join(out) + "\n"


def _read_views() -> dict:
    """{(category, name): (sha-of-bytes or "", text)} for the 4 view files."""
    res = {}
    for c in CATEGORIES:
        for n in VIEW_NAMES:
            try:
                b = (_brain() / "views" / c / f"{n}.md").read_bytes()
                res[(c, n)] = (hashlib.sha256(b).hexdigest(), b.decode("utf-8", "replace"))
            except OSError:
                res[(c, n)] = ("", "")
    return res


def _sync_from_vault() -> dict:
    """Complete-only: a ticked `[x] ... %%nx:ID%%` marks the item done. `[ ]` is ignored."""
    views = _read_views()
    for _sha, text in views.values():
        for m in _TICK_PAT.finditer(text):
            _db_set_status(int(m.group(1)), "done", "obsidian", 1)
    return views


async def refresh_views() -> bool:
    """Un-snooze expired, sync ticks from the vault, render, PUT. The only render path."""
    from backend.integrations import obsidian
    async with _VIEWS_LOCK:
        await asyncio.to_thread(_db_unsnooze)
        for _attempt in range(2):
            views = await asyncio.to_thread(_sync_from_vault)
            items = await asyncio.to_thread(_db_items)
            now = _local_now()
            start = now.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC).replace(tzinfo=None)
            conflict = False
            for (c, n), (sha, current) in views.items():
                text = _render(items, c, n, now.date(), start)
                if text != current and not await obsidian.put_view(c, n, text, sha):
                    conflict = True
                    break
            if not conflict:
                return True
        logger.warning("capture views still conflicting after retry; next tick")
        return False


# ---------------------------------------------------------------------------
# Public async API
# ---------------------------------------------------------------------------

async def set_status(item_id: int, status: str, by: str, days: int = 1) -> str:
    """-> ok | not_found | already_done | already_open | already_snoozed."""
    return await asyncio.to_thread(_db_set_status, item_id, status, by, days)


async def get_item(item_id: int) -> dict | None:
    return next((i for i in await asyncio.to_thread(_db_items) if i["id"] == item_id), None)


def _local_str(iso: str) -> str:
    """Naive-UTC isoformat -> 'Mon Oct 19 08:00' in the scheduler timezone."""
    return datetime.fromisoformat(iso).replace(tzinfo=UTC).astimezone(_local_now().tzinfo).strftime("%a %b %d %H:%M")


async def detail_text(item_id: int) -> str:
    """Popup text for the Open button (Telegram alert caps ~200 chars)."""
    i = await get_item(item_id)
    if i is None:
        return "Task not found."
    bits = [i["title"], f"{i['category']} {i['kind']} - {i['status']}"]
    if i["due_at"]:
        bits.append(f"due {i['due_at'][:10]}")
    if i["snoozed_until"]:
        bits.append(f"snoozed until {_local_str(i['snoozed_until'])}")
    if i["notes"]:
        bits.append(i["notes"])
    return "\n".join(bits)[:190]


_refresh_tasks: set = set()


async def _refresh_quiet() -> None:
    try:
        await refresh_views()
    except Exception as e:
        logger.warning("capture view refresh failed: %s", type(e).__name__)


def schedule_refresh() -> None:
    """Fire-and-forget refresh_views() (strong ref held; never raises, never blocks the caller)."""
    try:
        t = asyncio.get_running_loop().create_task(_refresh_quiet())
        _refresh_tasks.add(t)
        t.add_done_callback(_refresh_tasks.discard)
    except Exception as e:
        logger.warning("capture view refresh not scheduled: %s", type(e).__name__)


async def apply_status(item_id: int, status: str, by: str, days: int = 1) -> str:
    """set_status + human reply text + best-effort view refresh. Shared by Telegram buttons and commands."""
    r = await set_status(item_id, status, by, days)
    if r == "ok":
        schedule_refresh()
        return {"done": "Done.", "open": "Reopened.", "snoozed": f"Snoozed {days} day{'s' if days != 1 else ''}."}[status]
    if r == "already_snoozed":
        i = await get_item(item_id)
        return f"Already snoozed until {_local_str(i['snoozed_until'])}." if i and i["snoozed_until"] else "Already snoozed."
    return "Task not found." if r == "not_found" else f"Already {r.removeprefix('already_')}."


async def send_digest(category: str) -> dict:
    """Header + up to MAX_ITEMS one-message-per-task pushes with Complete/Snooze/Open buttons.
    Skips (sends nothing) when no task is open -- an empty digest is noise."""
    from backend.events import notify_phone
    await asyncio.to_thread(_db_unsnooze)
    tasks = [i for i in await list_items(category, "open") if i["kind"] in ("task", "followup")]
    if not tasks:
        return {"skipped": "nothing open"}
    tasks.sort(key=lambda i: (i["due_at"] is None, i["due_at"] or "", i["id"]))
    shown = tasks[:MAX_ITEMS]
    more = len(tasks) - len(shown)
    head = f"{category.title()} tasks: {len(tasks)} open" + (f" (showing {len(shown)}, /open {category} for the rest)" if more else "")
    await notify_phone(head, kind="capture_digest")
    for i in shown:
        due = f" (due {i['due_at'][:10]})" if i["due_at"] else ""
        await notify_phone(f"{html.escape(i['title'])}{due}", kind="capture_digest", buttons=[
            {"text": "\u2713 Complete", "callback_data": f"ct:done:{i['id']}"},
            {"text": "\u23f0 Snooze", "callback_data": f"ct:snooze:{i['id']}"},
            {"text": "\u2139 Open", "callback_data": f"ct:open:{i['id']}"}])
    return {"sent": len(shown), "more": more}


async def run_capture_digest(category: str) -> dict:
    """Scheduler entry. Never raises."""
    from backend.config import get_settings
    if not getattr(get_settings(), "capture_digest_enabled", True):
        return {"skipped": "disabled"}
    try:
        return await send_digest(category)
    except Exception as e:
        logger.error("capture digest error: %s", type(e).__name__)
        return {"error": type(e).__name__}


async def list_items(category: str | None = None, status: str | None = None, scope: str | None = None) -> list[dict]:
    items = await asyncio.to_thread(_db_items, category, status)
    if scope in ("today", "open"):
        items = [i for i in items if i["status"] == "open"]
    if scope == "today":
        now = _local_now()
        start = now.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC).replace(tzinfo=None)
        items = [i for i in items if datetime.fromisoformat(i["created_at"]) >= start
                 or (i["resurfaced_at"] and datetime.fromisoformat(i["resurfaced_at"]) >= start)
                 or (i["due_at"] and datetime.fromisoformat(i["due_at"]).date() <= now.date())]
    return items


async def patch_item(item_id: int, fields: dict) -> dict | None:
    return await asyncio.to_thread(_db_patch, item_id, fields)


async def run_capture_extract() -> dict:
    """Scheduler entry. Never raises."""
    from backend.config import get_settings
    if not getattr(get_settings(), "capture_extract_enabled", True):
        return {"skipped": "disabled"}
    out = {"added": 0, "extracted": 0, "failed": 0, "views": False}
    try:
        out["added"] = await asyncio.to_thread(_db_scan_new)
        for cap in await asyncio.to_thread(_db_pending):
            if await extract(cap) is None:
                out["failed"] += 1
            else:
                out["extracted"] += 1
        out["views"] = await refresh_views()
    except Exception as e:
        logger.error("capture run error: %s", e)
        out["error"] = type(e).__name__
    return out
