import logging
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import httpx

from backend.cache import async_ttl_cache

logger = logging.getLogger(__name__)


def _vault() -> Path:
    from backend.config import get_settings
    return Path(get_settings().obsidian_vault_path)


def _mcp_url() -> str:
    from backend.config import get_settings
    return get_settings().brain_mcp_url.rstrip("/")


def _mcp_headers() -> dict:
    from backend.config import get_settings
    token = get_settings().brain_mcp_token
    return {"Authorization": f"Bearer {token}"} if token else {}


@dataclass
class ObsidianData:
    daily_note: str | None = None
    recent_notes: list = field(default_factory=list)
    open_tasks: list = field(default_factory=list)


async def fetch() -> ObsidianData:
    vault = _vault()
    if not vault.exists():
        raise Exception(f"Obsidian vault not found at {vault}")

    md_files = sorted(vault.rglob("*.md"), key=lambda f: f.stat().st_mtime, reverse=True)
    recent_notes = [str(f.relative_to(vault)) for f in md_files[:10]]

    today = date.today().strftime("%Y-%m-%d")
    daily_note = None
    open_tasks = []
    for candidate in [vault / "Brain" / "raw" / f"{today}.md", vault / f"{today}.md"]:
        if candidate.exists():
            content = candidate.read_text(encoding="utf-8")
            daily_note = content
            open_tasks = [ln.strip() for ln in content.splitlines() if ln.strip().startswith("- [ ]")]
            break

    return ObsidianData(daily_note=daily_note, recent_notes=recent_notes, open_tasks=open_tasks)


@async_ttl_cache(30)
async def health_check() -> bool:
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            resp = await client.get(f"{_mcp_url()}/health")
            return resp.status_code == 200
    except Exception:
        return False


async def write_daily_note(content: str) -> None:
    today = date.today().strftime("%Y-%m-%d")
    await _post_raw(content, filename=f"{today}.md")


async def append_to_note(path: str, content: str) -> None:
    note_path = _vault() / path
    note_path.parent.mkdir(parents=True, exist_ok=True)
    with note_path.open("a", encoding="utf-8") as f:
        f.write(content)


async def complete_task(note_path: str, task_text: str) -> None:
    path = _vault() / note_path
    if path.exists():
        content = path.read_text(encoding="utf-8")
        updated = content.replace(f"- [ ] {task_text}", f"- [x] {task_text}")
        path.write_text(updated, encoding="utf-8")


async def vault_search(query: str, max_results: int = 10) -> str:
    vault = _vault()
    if not vault.exists():
        return f"Obsidian vault not found at {vault}."

    query_lower = query.lower()
    results = []
    try:
        for md_file in vault.rglob("*.md"):
            try:
                text = md_file.read_text(encoding="utf-8", errors="ignore")
                if query_lower in text.lower() or query_lower in md_file.name.lower():
                    ctx_lines = [ln for ln in text.splitlines() if query_lower in ln.lower()][:2]
                    ctx = " ... ".join(ctx_lines).strip()
                    rel = str(md_file.relative_to(vault))
                    results.append(f"**{rel}**\n{ctx}" if ctx else f"**{rel}**")
                    if len(results) >= max_results:
                        break
            except Exception:
                continue
    except Exception as e:
        logger.warning(f"Vault search failed: {e}")
        return f"Vault search unavailable: {e}"

    if not results:
        return f"No notes found matching '{query}'."
    return "\n\n".join(results)


async def create_note(title: str, content: str, folder: str = "NEXUS") -> str:
    safe_title = title.replace("/", "-").replace("\\", "-")
    filename = f"{safe_title}.md"
    await _post_raw(content, filename=filename)
    return str(Path(folder) / filename)


async def _post_raw(content: str, filename: str) -> None:
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            f"{_mcp_url()}/raw",
            json={"content": content, "filename": filename},
            headers=_mcp_headers(),
        )
        resp.raise_for_status()
