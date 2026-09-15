"""GitHub import service — fetch repos → draft projects.

Uses GitHub REST API. Optional GITHUB_TOKEN for higher rate limits.
Fetches repo metadata, languages, and READMEs to build rich project drafts.
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


def _get_json(url: str, **params) -> dict | list | None:
    """Helper for GitHub API GETs with error handling."""
    try:
        resp = httpx.get(url, params=params, headers=_headers(), timeout=30)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPError:
        return None


def fetch_repos(username: str, *, include_forks: bool = False) -> list[dict]:
    """Fetch public repos for a GitHub user, paginated.

    Captures extended metadata: stars, homepage, license, created/updated dates.
    """
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
                "stargazers_count": r.get("stargazers_count", 0),
                "homepage": r.get("homepage") or "",
                "license": (r.get("license") or {}).get("spdx_id", ""),
                "created_at": r.get("created_at", ""),
                "updated_at": r.get("updated_at", ""),
                "size": r.get("size", 0),  # in KB
            })
        page += 1
    return repos


def fetch_languages(owner: str, repo: str) -> list[str]:
    """Fetch all languages used in a repo (returns sorted by bytes, descending)."""
    data = _get_json(f"https://api.github.com/repos/{owner}/{repo}/languages")
    if not data or not isinstance(data, dict):
        return []
    # Sort by bytes used (descending) and return language names
    return [lang for lang, _ in sorted(data.items(), key=lambda x: x[1], reverse=True)]


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


def repo_to_draft_project(repo: dict, readme: str | None = None, languages: list[str] | None = None) -> dict:
    """Convert a GitHub API repo dict to a draft project dict.

    Enriches tech_stack from topics + languages endpoint, captures homepage as live_url.
    """
    # Build tech stack from primary language + all languages + topics
    tech_stack = []
    if languages:
        tech_stack.extend(languages)
    elif repo.get("language"):
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

    # Build a richer description from available metadata
    desc_parts = []
    if repo.get("description"):
        desc_parts.append(repo["description"])

    return {
        "title": repo["name"],
        "description": ". ".join(desc_parts) if desc_parts else "",
        "tech_stack": unique_stack,
        "github_url": repo["html_url"],
        "live_url": repo.get("homepage") or "",
        "stars": repo.get("stargazers_count", 0),
        "license": repo.get("license", ""),
        "source": "github_import",
        "status": "draft",
        "_readme": readme,  # carried along for suggest-description, not stored in DB
    }


def suggest_description(repo: dict, readme: str | None) -> str:
    """Ask Gemini to write a project description from repo metadata + README.
    Explicitly instructed NOT to invent impact/metrics not present."""
    parts = [
        f"Repository: {repo.get('name', repo.get('title', ''))}",
        f"Short description: {repo.get('description', '')}",
    ]
    if repo.get("language"):
        parts.append(f"Primary language: {repo['language']}")
    if repo.get("topics"):
        parts.append(f"Topics/tags: {', '.join(repo['topics'])}")
    if repo.get("stars"):
        parts.append(f"Stars: {repo['stars']}")
    if readme:
        # Truncate very long READMEs to stay within prompt limits
        truncated = readme[:6000]
        parts.append(f"README:\n{truncated}")

    prompt = (
        "Write a concise project description (3-5 sentences) suitable for a "
        "technical resume or portfolio, based on the following GitHub repository "
        "information.\n\n"
        "RULES:\n"
        "- Describe what the project does, its architecture, and key features.\n"
        "- Mention the specific technologies and frameworks used.\n"
        "- Extract and summarize ONLY what is explicitly stated in the source material.\n"
        "- Do NOT invent impact metrics, user counts, performance numbers, or any "
        "claims not directly present in the README or description.\n"
        "- If the README lacks impact details, describe what the project does and "
        "its tech stack — do not fabricate impact.\n"
        "- Use active voice and technical language appropriate for a software "
        "engineering resume.\n"
        "- Output ONLY the description text, no headers or labels.\n\n"
        + "\n".join(parts)
    )
    return generate_text(prompt)


def ai_enrich_project(repo: dict, readme: str | None) -> dict:
    """Use Gemini to generate a full project description + highlights from repo data.

    Returns a dict with 'description' and 'highlights' keys.
    Falls back gracefully if the AI call fails.
    """
    parts = [
        f"Repository: {repo.get('name', repo.get('title', ''))}",
        f"Short description: {repo.get('description', '')}",
    ]
    if repo.get("language"):
        parts.append(f"Primary language: {repo['language']}")
    tech = repo.get("tech_stack", [])
    if tech:
        parts.append(f"Tech stack: {', '.join(tech)}")
    if repo.get("stars"):
        parts.append(f"GitHub stars: {repo['stars']}")
    if readme:
        truncated = readme[:6000]
        parts.append(f"README:\n{truncated}")

    prompt = (
        "Based on the following GitHub repository information, generate TWO things:\n\n"
        "1. DESCRIPTION: A concise project description (3-5 sentences) suitable for a "
        "technical resume. Describe what the project does, its architecture, and key "
        "technologies used. Use active voice.\n\n"
        "2. HIGHLIGHTS: 2-4 bullet points of notable technical achievements, features, "
        "or design decisions. Each bullet should be one sentence.\n\n"
        "RULES:\n"
        "- Extract and summarize ONLY what is explicitly stated.\n"
        "- Do NOT invent metrics, user counts, or performance claims.\n"
        "- Output in this exact format (no markdown fences):\n"
        "DESCRIPTION: <your description>\n"
        "HIGHLIGHTS:\n"
        "- <bullet 1>\n"
        "- <bullet 2>\n\n"
        + "\n".join(parts)
    )

    try:
        raw = generate_text(prompt)
        result = {"description": "", "highlights": ""}

        # Parse the response
        lines = raw.strip().split("\n")
        in_highlights = False
        desc_parts = []
        highlight_parts = []

        for line in lines:
            stripped = line.strip()
            if stripped.upper().startswith("DESCRIPTION:"):
                desc_parts.append(stripped[len("DESCRIPTION:"):].strip())
                in_highlights = False
            elif stripped.upper().startswith("HIGHLIGHTS:"):
                in_highlights = True
            elif in_highlights and stripped.startswith("- "):
                highlight_parts.append(stripped[2:].strip())
            elif in_highlights and stripped.startswith("• "):
                highlight_parts.append(stripped[2:].strip())
            elif not in_highlights and desc_parts:
                # Continuation of description
                desc_parts.append(stripped)

        result["description"] = " ".join(desc_parts).strip()
        result["highlights"] = "\n".join(f"• {h}" for h in highlight_parts) if highlight_parts else ""

        return result
    except Exception:
        return {"description": repo.get("description", ""), "highlights": ""}
