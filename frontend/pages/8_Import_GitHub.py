"""GitHub import page — fetch repos → AI-enriched drafts → review/approve into KB.

Features:
- Fetches all public repos with extended metadata (stars, languages, homepage)
- Uses GitHub languages API for comprehensive tech stack detection
- AI-enriches descriptions from README content
- Sort/filter repos by stars, language, name
- Batch approve or individual review
"""
import sys
from pathlib import Path

_backend = str(Path(__file__).resolve().parent.parent.parent / "backend")
if _backend not in sys.path:
    sys.path.insert(0, _backend)

import streamlit as st
from app.db import get_session
from app.config import get_settings
from app.services.github_import_service import (
    fetch_repos, fetch_readme, fetch_languages,
    repo_to_draft_project, suggest_description, ai_enrich_project,
)
from app.services.project_service import create_project

st.set_page_config(page_title="Import GitHub — CareerOS", page_icon="🐙")
st.title("🐙 Import from GitHub")

settings = get_settings()
user_id = settings.default_user_id

# Token status indicator
if settings.github_token:
    st.caption("🔑 GitHub token configured (5,000 requests/hour)")
else:
    st.caption("⚠️ No GitHub token — limited to 60 requests/hour. Set `GITHUB_TOKEN` in `.env` for more.")

st.markdown(
    "Enter your GitHub username to fetch your public repos. "
    "AI will enrich descriptions from READMEs. Review, edit, and approve "
    "the projects you want in your Knowledge Base."
)

# --- Fetch Controls ---
col1, col2 = st.columns([3, 1])
username = col1.text_input("GitHub Username", placeholder="e.g. octocat")
include_forks = col2.checkbox("Include forks", value=False)

col_fetch, col_enrich = st.columns(2)
fetch_clicked = col_fetch.button("🔍 Fetch Repos", disabled=not username.strip())
enrich_with_ai = col_enrich.checkbox("🤖 AI-enrich descriptions", value=True,
                                      help="Use Gemini to generate rich descriptions from READMEs. Takes a bit longer.")

if fetch_clicked and username.strip():
    with st.spinner(f"Fetching repos for {username}..."):
        try:
            repos = fetch_repos(username.strip(), include_forks=include_forks)
            if not repos:
                st.warning(f"No repos found for '{username}'. Check the username and try again.")
                st.stop()

            drafts = []
            progress = st.progress(0, text="Fetching repo details...")
            total = len(repos)

            for i, repo in enumerate(repos):
                progress.progress(
                    (i + 1) / total,
                    text=f"Processing {repo['name']} ({i+1}/{total})..."
                )

                # Fetch languages for richer tech stack
                languages = fetch_languages(username.strip(), repo["name"])

                # Fetch README
                readme = fetch_readme(username.strip(), repo["name"])

                # Build draft
                draft = repo_to_draft_project(repo, readme, languages)

                # AI enrichment (if enabled and README exists)
                if enrich_with_ai and readme and len(readme.strip()) > 50:
                    enriched = ai_enrich_project(draft, readme)
                    if enriched.get("description"):
                        draft["description"] = enriched["description"]
                    if enriched.get("highlights"):
                        draft["highlights"] = enriched["highlights"]
                else:
                    draft["highlights"] = ""

                drafts.append(draft)

            progress.empty()
            st.session_state["github_drafts"] = drafts
            st.session_state["github_username"] = username.strip()
            st.success(f"✅ Found and processed {len(drafts)} repos!")
        except Exception as e:
            st.error(f"Failed to fetch repos: {e}")
            import traceback
            with st.expander("Error details"):
                st.code(traceback.format_exc())

# --- Display Drafts ---
drafts = st.session_state.get("github_drafts", [])
gh_username = st.session_state.get("github_username", "")

if drafts:
    st.markdown("---")
    st.subheader(f"Repos from {gh_username}")

    # --- Filtering & Sorting ---
    col_filter, col_sort = st.columns(2)

    # Get unique languages across all drafts
    all_languages = sorted({t for d in drafts for t in d.get("tech_stack", [])})
    filter_lang = col_filter.selectbox(
        "Filter by language/tech",
        options=["All"] + all_languages,
        index=0,
    )

    sort_by = col_sort.selectbox(
        "Sort by",
        options=["Stars (high → low)", "Stars (low → high)", "Name (A → Z)", "Name (Z → A)"],
        index=0,
    )

    # Apply filter
    filtered = drafts
    if filter_lang != "All":
        filtered = [d for d in drafts if filter_lang in d.get("tech_stack", [])]

    # Apply sort
    if sort_by == "Stars (high → low)":
        filtered = sorted(filtered, key=lambda d: d.get("stars", 0), reverse=True)
    elif sort_by == "Stars (low → high)":
        filtered = sorted(filtered, key=lambda d: d.get("stars", 0))
    elif sort_by == "Name (A → Z)":
        filtered = sorted(filtered, key=lambda d: d.get("title", "").lower())
    elif sort_by == "Name (Z → A)":
        filtered = sorted(filtered, key=lambda d: d.get("title", "").lower(), reverse=True)

    st.caption(f"Showing {len(filtered)} of {len(drafts)} repos. Approve the ones you want in your KB.")

    # --- Individual Repo Cards ---
    for i, draft in enumerate(filtered):
        # Build header with metadata badges
        stars = draft.get("stars", 0)
        star_str = f" ⭐ {stars}" if stars else ""
        lang_str = ""
        if draft.get("tech_stack"):
            top_langs = draft["tech_stack"][:3]
            lang_str = f" · {', '.join(top_langs)}"

        header = f"📦 **{draft['title']}**{star_str}{lang_str}"
        desc_preview = draft.get("description", "")
        if desc_preview and len(desc_preview) > 80:
            desc_preview = desc_preview[:80] + "..."

        with st.expander(f"{header}" + (f"\n{desc_preview}" if desc_preview else "")):
            with st.form(f"approve_gh_{draft['title']}_{i}"):
                title = st.text_input("Project Title", value=draft["title"], key=f"gh_title_{i}")
                description = st.text_area(
                    "Description",
                    value=draft.get("description", ""),
                    height=150,
                    key=f"gh_desc_{i}",
                    help="AI-generated from README if enrichment was enabled",
                )
                tech_raw = st.text_input(
                    "Tech Stack (comma-separated)",
                    value=", ".join(draft.get("tech_stack", [])),
                    key=f"gh_tech_{i}",
                )
                col_url1, col_url2 = st.columns(2)
                github_url = col_url1.text_input(
                    "GitHub URL",
                    value=draft.get("github_url", ""),
                    key=f"gh_url_{i}",
                )
                live_url = col_url2.text_input(
                    "Live/Demo URL",
                    value=draft.get("live_url", ""),
                    key=f"gh_live_{i}",
                )
                highlights = st.text_area(
                    "Highlights",
                    value=draft.get("highlights", ""),
                    key=f"gh_hi_{i}",
                    height=100,
                    help="Key features, achievements, or design decisions",
                )

                # Show metadata
                meta_parts = []
                if draft.get("license"):
                    meta_parts.append(f"📄 {draft['license']}")
                if stars:
                    meta_parts.append(f"⭐ {stars} stars")
                if meta_parts:
                    st.caption(" · ".join(meta_parts))

                col1, col2, col3 = st.columns(3)
                approve = col1.form_submit_button("✅ Approve & Save")
                regenerate = col2.form_submit_button("🔄 Regenerate Description")
                skip = col3.form_submit_button("⏭️ Skip")

            if approve:
                if title.strip() and description.strip():
                    tech = [t.strip() for t in tech_raw.split(",") if t.strip()]
                    with st.spinner("Saving and embedding..."):
                        with get_session() as session:
                            create_project(
                                session,
                                user_id=user_id,
                                title=title.strip(),
                                description=description.strip(),
                                tech_stack=tech,
                                github_url=github_url.strip() or None,
                                live_url=live_url.strip() or None,
                                highlights=highlights.strip() or None,
                                source="github_import",
                                status="verified",
                            )
                    st.success(f"✅ '{title}' approved and added to KB!")
                else:
                    st.error("Title and Description are required.")

            if regenerate:
                with st.spinner("🤖 Regenerating description with AI..."):
                    suggested = suggest_description(
                        {"name": draft["title"], "description": draft.get("description", ""),
                         "language": draft.get("tech_stack", [""])[0] if draft.get("tech_stack") else "",
                         "topics": draft.get("tech_stack", []),
                         "stars": draft.get("stars", 0)},
                        draft.get("_readme"),
                    )
                    st.info(f"**Suggested description:**\n\n{suggested}")
                    st.caption("Copy this into the Description field above, then click Approve.")
