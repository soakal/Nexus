"""Vault browser API — catalog, search, note read, wikilink graph.

GET /api/vault/catalog   — precomputed wiki catalog (nightly brain_organizer output)
GET /api/vault/search?q= — reuses integrations.obsidian.vault_search (Brain/-scoped)
GET /api/vault/note?path=— one note, path relative to Brain/
GET /api/vault/graph     — nodes (wiki pages) + links (resolved wikilinks)

All endpoints require Bearer auth. Read-only.

The graph resolver deliberately RE-IMPLEMENTS (never imports) the deterministic
rungs of modules/brain-organizer/brain_organizer.py::_defuse_unknown_wikilinks --
backend must never import brain_organizer (separate venv; subprocess-only
precedent in backend/api/brain_organizer.py). Rung order mirrored exactly:
exact stem -> exact title -> case-insensitive stem -> case-insensitive title.
The fuzzy rung (find_similar_page) is intentionally dropped: at read time fuzzy
matching would fabricate edges, and the nightly defuser already rewrote every
fuzzy-resolvable link into filename-stem space at write time -- anything still
unresolved here is a genuinely broken link, not an edge.
"""

import asyncio
import json
import re
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException

from backend.auth import require_api_key
from backend.cache import async_ttl_cache
from backend.integrations.obsidian import vault_search

router = APIRouter()

# Mirrors WIKILINK_PAT, modules/brain-organizer/brain_organizer.py:1878.
WIKILINK_PAT = re.compile(r"(?<!\!)\[\[([^\]|#]+)(?:#([^\]|]*))?(?:\|([^\]]*))?\]\]")


def _brain_root() -> Path:
    """Monkeypatch seam for tests -- same style as obsidian._vault()."""
    from backend.config import get_settings
    return Path(get_settings().obsidian_vault_path) / "Brain"


def _load_catalog() -> dict:
    path = _brain_root() / "_meta" / "wiki-catalog.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise HTTPException(status_code=503, detail=f"wiki catalog unavailable: {e}")


@router.get("/catalog")
async def get_catalog(_: str = Depends(require_api_key)):
    data = await asyncio.to_thread(_load_catalog)
    # path_str deliberately dropped -- no absolute server paths to the client.
    pages = [
        {k: p.get(k) for k in ("title", "filename", "headers", "summary", "tags")}
        for p in data.get("pages", [])
    ]
    return {"built_at": data.get("built_at"), "pages": pages}


@router.get("/search")
async def search(q: str, max_results: int = 20, _: str = Depends(require_api_key)):
    max_results = max(1, min(max_results, 50))
    result = await vault_search(q, max_results)  # already runs off-loop via to_thread
    return {"query": q, "result": result}


def read_note_text(path: str) -> str:
    """Guarded Brain/-relative note read. Raises ValueError on a bad path
    (including a malformed path Path.resolve() itself rejects, e.g. an
    embedded null byte) and FileNotFoundError when absent. Shared by GET
    /note and the vault_read_note chat tool (backend/agents/tools.py) --
    the path-traversal guard must exist in exactly one place."""
    root = _brain_root().resolve()
    target = (root / path).resolve()
    if target.suffix != ".md" or not target.is_relative_to(root):
        raise ValueError("invalid note path")
    if not target.is_file():
        raise FileNotFoundError(path)
    return target.read_text("utf-8", "ignore")


def resolve_note_candidates(name: str) -> list[str]:
    """Resolve a bare title/stem to Brain/-relative wiki paths via the
    catalog. Same rung ORDER as _build_graph_sync's resolve() (exact stem ->
    exact title -> case-insensitive stem -> case-insensitive title, mirroring
    _defuse_unknown_wikilinks), but returns ALL matches at the first rung
    that hits, not just one -- the chat tool must surface ambiguity to the
    model rather than silently first-winning like the graph's resolve() (that
    function feeds edges where a wrong-but-plausible guess is harmless noise;
    this one feeds an answer, where it's a wrong fact). Fuzzy rung
    deliberately absent, same reasoning as the module docstring.
    """
    pages = _load_catalog().get("pages", [])
    name_lower = name.lower()

    def _matches(pred) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for p in pages:
            filename = p.get("filename")
            if filename and pred(p) and filename not in seen:
                seen.add(filename)
                out.append(f"wiki/{filename}")
        return out

    for pred in (
        lambda p: Path(p["filename"]).stem == name,
        lambda p: p.get("title") == name,
        lambda p: Path(p["filename"]).stem.lower() == name_lower,
        lambda p: (p.get("title") or "").lower() == name_lower,
    ):
        hits = _matches(pred)
        if hits:
            return hits
    return []


@router.get("/note")
async def get_note(path: str, _: str = Depends(require_api_key)):
    try:
        content = await asyncio.to_thread(read_note_text, path)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid note path")
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"note not found: {path}")
    return {"path": path, "content": content}


def _build_graph_sync() -> dict:
    catalog = _load_catalog()
    wiki = (_brain_root() / "wiki").resolve()

    nodes: list[dict] = []
    by_stem: dict[str, str] = {}
    by_title: dict[str, str] = {}
    for p in catalog.get("pages", []):
        filename = p.get("filename")
        if not filename:
            continue
        stem = Path(filename).stem
        nodes.append({"id": stem, "title": p.get("title") or stem, "filename": filename})
        by_stem.setdefault(stem, stem)
        title = p.get("title")
        if title:
            by_title.setdefault(title, stem)
    by_stem_ci: dict[str, str] = {}
    for stem in by_stem:
        by_stem_ci.setdefault(stem.lower(), stem)
    by_title_ci: dict[str, str] = {}
    for title, stem in by_title.items():
        by_title_ci.setdefault(title.lower(), stem)

    def resolve(target: str) -> str | None:
        # Rung order mirrors _defuse_unknown_wikilinks (see module docstring).
        if target in by_stem:
            return target
        stem = by_title.get(target)
        if stem is None:
            stem = by_stem_ci.get(target.lower())
        if stem is None:
            stem = by_title_ci.get(target.lower())
        return stem

    edges: set[tuple[str, str]] = set()
    for node in nodes:
        # Same containment guard as read_note_text -- the catalog is trusted
        # (nightly job output, never user/LLM input) so this is defense in
        # depth, not a live exploit path, but it costs nothing to match the
        # rest of this file's convention instead of being the one exception.
        candidate = (wiki / node["filename"]).resolve()
        if not candidate.is_relative_to(wiki):
            continue
        try:
            text = candidate.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue  # catalog is nightly -- a file deleted since then is not an error
        for m in WIKILINK_PAT.finditer(text):
            dest = resolve(m.group(1).strip())
            if dest is not None and dest != node["id"]:
                edges.add((node["id"], dest))

    # "links" (not "edges") -- feeds force-graph's graphData() unchanged.
    return {"nodes": nodes, "links": [{"source": s, "target": t} for s, t in sorted(edges)]}


@async_ttl_cache(60)
async def _graph_data() -> dict:
    return await asyncio.to_thread(_build_graph_sync)


@router.get("/graph")
async def get_graph(_: str = Depends(require_api_key)):
    return await _graph_data()
