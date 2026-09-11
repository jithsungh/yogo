"""GitHub import service — fetch repos → draft projects.

Uses GitHub REST API. Optional GITHUB_TOKEN for higher rate limits.
"""
from __future__ import annotations

import httpx

from app.config import get_settings
from app.llm.gemini_client import generate_text


def _headers() -> dict[str, str]:
    settings = get_settings()
    h = {"Accept": "application/vnd.github+json"}
    if settings.github_token:
        h["Authorization"] = f"Bearer {settings.github_token}"
    return h


def fetch_repos(username: str, *, include_forks: bool = False) -> list[dict]:
    """Fetch public repos for a GitHub user, paginated."""
    repos = []
    page = 1
    while True:
        resp = httpx.get(
            f"https://api.github.com/users/{username}/repos",
            params={"sort": "updated", "per_page": 100, "page": page},
            headers=_headers(),
            timeout=30,
        )
        resp.raise_for_status()
        batch = resp.json()
        if not batch:
            break
        for r in batch:
            if not include_forks and r.get("fork", False):
                continue
            repos.append({
                "name": r["name"],
                "description": r.get("description") or "",
                "language": r.get("language") or "",
                "topics": r.get("topics", []),
                "html_url": r["html_url"],
                "fork": r.get("fork", False),
            })
        page += 1
    return repos


def fetch_readme(owner: str, repo: str) -> str | None:
    """Best-effort fetch of the repo's README content. Returns None on 404."""
    try:
        resp = httpx.get(
            f"https://api.github.com/repos/{owner}/{repo}/readme",
            headers={**_headers(), "Accept": "application/vnd.github.raw"},
            timeout=30,
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.text
    except httpx.HTTPError:
        return None


def repo_to_draft_project(repo: dict, readme: str | None = None) -> dict:
    """Convert a GitHub API repo dict to a draft project dict for the DB."""
    tech_stack = []
    if repo.get("language"):
        tech_stack.append(repo["language"])
    tech_stack.extend(repo.get("topics", []))
    # Deduplicate while preserving order
    seen = set()
    unique_stack = []
    for t in tech_stack:
        lower = t.lower()
        if lower not in seen:
            seen.add(lower)
            unique_stack.append(t)

    return {
        "title": repo["name"],
        "description": repo.get("description") or "",
        "tech_stack": unique_stack,
        "github_url": repo["html_url"],
        "source": "github_import",
        "status": "draft",
        "_readme": readme,  # carried along for the suggest-description prompt, not stored in DB
    }


def suggest_description(repo: dict, readme: str | None) -> str:
    """Ask Gemini to write a project description from repo metadata + README.
    Explicitly instructed NOT to invent impact/metrics not present."""
    parts = [
        f"Repository: {repo['name']}",
        f"Description: {repo.get('description', '')}",
        f"Language: {repo.get('language', '')}",
        f"Topics: {', '.join(repo.get('topics', []))}",
    ]
    if readme:
        # Truncate very long READMEs to stay within prompt limits
        truncated = readme[:4000]
        parts.append(f"README:\n{truncated}")

    prompt = (
        "Write a concise project description (2-4 sentences) for a resume/portfolio "
        "based on the following GitHub repository information. "
        "Extract and summarize ONLY what is explicitly stated. "
        "Do NOT invent impact metrics, user counts, performance numbers, or any "
        "claims not directly present in the source material. "
        "If the README lacks impact details, describe what the project does and "
        "its tech stack — do not fabricate impact.\n\n"
        + "\n".join(parts)
    )
    return generate_text(prompt)
