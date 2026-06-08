import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import httpx

logger = logging.getLogger(__name__)


@dataclass
class GitHubData:
    open_prs: list = field(default_factory=list)
    assigned_issues: list = field(default_factory=list)
    recent_commits: list = field(default_factory=list)
    stale_prs: list = field(default_factory=list)


async def fetch() -> GitHubData:
    from backend.config import get_settings
    settings = get_settings()
    try:
        token = settings.github_token
    except Exception:
        raise Exception("GITHUB_TOKEN not configured")

    headers = {"Authorization": f"token {token}", "Accept": "application/vnd.github.v3+json"}
    username = settings.github_username
    stale_cutoff = datetime.now(UTC) - timedelta(hours=settings.pr_stale_hours)

    async with httpx.AsyncClient(timeout=5) as client:
        # Open PRs
        resp = await client.get("https://api.github.com/search/issues", headers=headers,
                                params={"q": f"is:pr is:open author:{username}", "per_page": 20})
        open_prs = []
        stale_prs = []
        if resp.status_code == 200:
            items = resp.json().get("items", [])
            for pr in items:
                updated = datetime.fromisoformat(pr["updated_at"].replace("Z", "+00:00"))
                pr_data = {"title": pr["title"], "url": pr["html_url"], "updated_at": pr["updated_at"]}
                open_prs.append(pr_data)
                if updated < stale_cutoff:
                    stale_prs.append(pr_data)

        # Assigned issues
        resp2 = await client.get("https://api.github.com/issues", headers=headers,
                                 params={"assignee": username, "state": "open", "per_page": 20})
        assigned_issues = []
        if resp2.status_code == 200:
            for issue in resp2.json():
                if "pull_request" not in issue:
                    assigned_issues.append({"title": issue["title"], "url": issue["html_url"]})

        # Recent commits
        resp3 = await client.get(f"https://api.github.com/users/{username}/events", headers=headers, params={"per_page": 10})
        recent_commits = []
        if resp3.status_code == 200:
            for event in resp3.json():
                if event.get("type") == "PushEvent":
                    for commit in event.get("payload", {}).get("commits", [])[:3]:
                        recent_commits.append({"message": commit.get("message", ""), "repo": event.get("repo", {}).get("name", "")})

    return GitHubData(open_prs=open_prs, assigned_issues=assigned_issues, recent_commits=recent_commits, stale_prs=stale_prs)


async def health_check() -> bool:
    try:
        from backend.config import get_settings
        settings = get_settings()
        headers = {"Authorization": f"token {settings.github_token}"}
        async with httpx.AsyncClient(timeout=2) as client:
            resp = await client.get("https://api.github.com/user", headers=headers)
            return resp.status_code == 200
    except Exception:
        return False
