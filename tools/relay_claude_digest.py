"""Relay the daily Claude-features digest (written by a cloud routine into
digests/claude-features/*.md) into the Brain vault + a Telegram notify.

Run daily by a Windows Scheduled Task shortly after the cloud routine's
09:00 America/New_York run. Assumes the repo has already been `git pull`ed
(the .cmd wrapper does that before invoking this).
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DIGEST_DIR = REPO_ROOT / "digests" / "claude-features"
STATE_FILE = DIGEST_DIR / ".relay_state.json"
_DATED_DIGEST = re.compile(r"^\d{4}-\d{2}-\d{2}\.md$")

sys.path.insert(0, str(REPO_ROOT))


def _load_relayed() -> set[str]:
    if STATE_FILE.exists():
        return set(json.loads(STATE_FILE.read_text(encoding="utf-8")))
    return set()


def _save_relayed(names: set[str]) -> None:
    STATE_FILE.write_text(json.dumps(sorted(names), indent=2), encoding="utf-8")


async def _push_to_brain(filename: str, content: str) -> bool:
    import httpx
    from backend.config import get_settings

    settings = get_settings()
    url = f"{settings.brain_mcp_url.rstrip('/')}/raw"
    headers = {}
    try:
        if settings.brain_mcp_token:
            headers["Authorization"] = f"Bearer {settings.brain_mcp_token}"
    except Exception:
        pass
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(url, json={"content": content, "filename": filename}, headers=headers)
        resp.raise_for_status()
        return True


async def _notify_telegram(date_str: str, content: str) -> bool:
    from backend.integrations import hermes

    body = content if len(content) <= 3500 else content[:3500] + "\n\n...(truncated, full digest saved to the Brain vault)"
    payload = {
        "type": "claude_features_digest",
        "content": f"Claude + AI Digest — {date_str}\n\n{body}",
    }
    return await hermes.notify(payload)


async def main() -> int:
    if not DIGEST_DIR.exists():
        print("no digests/claude-features/ dir yet — nothing to relay")
        return 0

    relayed = _load_relayed()
    files = sorted(
        p for p in DIGEST_DIR.glob("*.md")
        if _DATED_DIGEST.match(p.name) and p.name not in relayed
    )
    if not files:
        print("nothing new to relay")
        return 0

    any_failed = False
    for f in files:
        content = f.read_text(encoding="utf-8")
        date_str = f.stem
        try:
            await _push_to_brain(f"claude-features-digest-{date_str}.md", content)
            ok = await _notify_telegram(date_str, content)
            relayed.add(f.name)
            if ok:
                print(f"relayed {f.name}")
            else:
                print(f"relayed {f.name} to Brain vault but TELEGRAM NOTIFY FAILED (check Hermes)")
                any_failed = True
        except Exception as e:
            print(f"FAILED to relay {f.name}: {e}")
            any_failed = True

    _save_relayed(relayed)
    return 1 if any_failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
