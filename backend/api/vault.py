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


@router.get("/note")
async def get_note(path: str, _: str = Depends(require_api_key)):
    root = _brain_root().resolve()
    target = (root / path).resolve()
    if target.suffix != ".md" or not target.is_relative_to(root):
        raise HTTPException(status_code=400, detail="invalid note path")
    if not target.is_file():
        raise HTTPException(status_code=404, detail=f"note not found: {path}")
    content = await asyncio.to_thread(target.read_text, "utf-8", "ignore")
    return {"path": path, "content": content}


def _build_graph_sync() -> dict:
    catalog = _load_catalog()
    wiki = _brain_root() / "wiki"

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
        try:
            text = (wiki / node["filename"]).read_text(encoding="utf-8", errors="ignore")
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
