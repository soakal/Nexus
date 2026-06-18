import logging
from dataclasses import dataclass, field

import httpx

from backend.cache import async_ttl_cache

logger = logging.getLogger(__name__)


@dataclass
class ObsidianData:
    daily_note: str | None = None
    recent_notes: list = field(default_factory=list)
    open_tasks: list = field(default_factory=list)


async def fetch() -> ObsidianData:
    from backend.config import get_settings
    settings = get_settings()
    try:
        token = settings.obsidian_token
    except Exception:
        raise Exception("OBSIDIAN_TOKEN not configured")

    host = settings.obsidian_host
    headers = {"Authorization": f"Bearer {token}"}

    async with httpx.AsyncClient(timeout=5, verify=False) as client:
        # List vault files
        resp = await client.get(f"{host}/vault/", headers=headers)
        resp.raise_for_status()
        files = resp.json().get("files", [])
        recent_notes = sorted([f for f in files if f.endswith(".md")], reverse=True)[:10]

        # Try to get daily note
        from datetime import date
        today = date.today().strftime("%Y-%m-%d")
        daily_note = None
        open_tasks = []
        try:
            note_resp = await client.get(f"{host}/vault/{today}.md", headers=headers)
            if note_resp.status_code == 200:
                content = note_resp.text
                daily_note = content
                open_tasks = [line.strip() for line in content.splitlines() if line.strip().startswith("- [ ]")]
        except Exception:
            pass

    return ObsidianData(daily_note=daily_note, recent_notes=recent_notes, open_tasks=open_tasks)


@async_ttl_cache(30)
async def health_check() -> bool:
    try:
        from backend.config import get_settings
        settings = get_settings()
        headers = {"Authorization": f"Bearer {settings.obsidian_token}"}
        async with httpx.AsyncClient(timeout=2, verify=False) as client:
            resp = await client.get(f"{settings.obsidian_host}/vault/", headers=headers)
            return resp.status_code == 200
    except Exception:
        return False


async def write_daily_note(content: str) -> None:
    from datetime import date

    from backend.config import get_settings
    settings = get_settings()
    headers = {"Authorization": f"Bearer {settings.obsidian_token}", "Content-Type": "text/markdown"}
    today = date.today().strftime("%Y-%m-%d")
    async with httpx.AsyncClient(timeout=5, verify=False) as client:
        await client.put(f"{settings.obsidian_host}/vault/{today}.md", content=content.encode(), headers=headers)


async def append_to_note(path: str, content: str) -> None:
    from backend.config import get_settings
    settings = get_settings()
    headers = {"Authorization": f"Bearer {settings.obsidian_token}", "Content-Type": "text/markdown"}
    async with httpx.AsyncClient(timeout=5, verify=False) as client:
        await client.post(f"{settings.obsidian_host}/vault/{path}", content=content.encode(), headers=headers)


async def complete_task(note_path: str, task_text: str) -> None:
    from backend.config import get_settings
    settings = get_settings()
    headers = {"Authorization": f"Bearer {settings.obsidian_token}"}
    async with httpx.AsyncClient(timeout=5, verify=False) as client:
        resp = await client.get(f"{settings.obsidian_host}/vault/{note_path}", headers=headers)
        if resp.status_code == 200:
            content = resp.text.replace(f"- [ ] {task_text}", f"- [x] {task_text}")
            headers["Content-Type"] = "text/markdown"
            await client.put(f"{settings.obsidian_host}/vault/{note_path}", content=content.encode(), headers=headers)


async def vault_search(query: str, max_results: int = 10) -> str:
    """Search the Obsidian vault using the Local REST API simple search."""
    from backend.config import get_settings
    settings = get_settings()
    try:
        token = settings.obsidian_token
    except Exception:
        return "Obsidian token not configured."

    host = settings.obsidian_host
    headers = {"Authorization": f"Bearer {token}"}

    try:
        async with httpx.AsyncClient(timeout=10, verify=False) as client:
            # Use the simple search endpoint
            resp = await client.post(
                f"{host}/search/simple/",
                params={"query": query, "contextLength": 200},
                headers=headers,
            )
            if resp.status_code == 200:
                results = resp.json()
                if not results:
                    return f"No notes found matching '{query}'."
                lines = []
                for r in results[:max_results]:
                    path = r.get("filename", "unknown")
                    matches = r.get("matches", [])
                    ctx = " ... ".join(
                        m.get("match", {}).get("text", "") or
                        (m.get("context", "") if isinstance(m.get("context"), str) else "")
                        for m in matches[:2]
                    ).strip()
                    lines.append(f"**{path}**\n{ctx}" if ctx else f"**{path}**")
                return "\n\n".join(lines)
            # Fallback: list files and filter by name
            resp2 = await client.get(f"{host}/vault/", headers=headers)
            if resp2.status_code == 200:
                files = resp2.json().get("files", [])
                q_lower = query.lower()
                matches = [f for f in files if q_lower in f.lower()][:max_results]
                if matches:
                    return "Notes matching by filename:\n" + "\n".join(f"- {f}" for f in matches)
                return f"No notes found matching '{query}'."
    except Exception as e:
        logger.warning(f"Vault search failed: {e}")
        return f"Vault search unavailable: {e}"

    return f"No results for '{query}'."


async def create_note(title: str, content: str, folder: str = "NEXUS") -> str:
    from backend.config import get_settings
    settings = get_settings()
    headers = {"Authorization": f"Bearer {settings.obsidian_token}", "Content-Type": "text/markdown"}
    safe_title = title.replace("/", "-").replace("\\", "-")
    path = f"{folder}/{safe_title}.md"
    async with httpx.AsyncClient(timeout=5, verify=False) as client:
        await client.put(f"{settings.obsidian_host}/vault/{path}", content=content.encode(), headers=headers)
    return path
