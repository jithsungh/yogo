"""GitHub import page — fetch repos → review/edit drafts → approve into KB."""
import sys
from pathlib import Path

_backend = str(Path(__file__).resolve().parent.parent.parent / "backend")
if _backend not in sys.path:
    sys.path.insert(0, _backend)

import streamlit as st
from app.db import get_session
from app.config import get_settings
from app.services.github_import_service import fetch_repos, fetch_readme, repo_to_draft_project, suggest_description
from app.services.project_service import create_project

st.set_page_config(page_title="Import GitHub — CareerOS", page_icon="🐙")
st.title("🐙 Import from GitHub")

settings = get_settings()
user_id = settings.default_user_id

st.markdown(
    "Enter your GitHub username to fetch your public repos. "
    "Each repo becomes a **draft** project — review, edit, and approve "
    "the ones you want in your KB."
)

col1, col2 = st.columns([3, 1])
username = col1.text_input("GitHub Username", placeholder="e.g. octocat")
include_forks = col2.checkbox("Include forks", value=False)

if st.button("🔍 Fetch Repos") and username.strip():
    with st.spinner(f"Fetching repos for {username}..."):
        try:
            repos = fetch_repos(username.strip(), include_forks=include_forks)
            # Fetch READMEs
            drafts = []
            progress = st.progress(0)
            for i, repo in enumerate(repos):
                readme = fetch_readme(username.strip(), repo["name"])
                drafts.append(repo_to_draft_project(repo, readme))
                progress.progress((i + 1) / len(repos))
            st.session_state["github_drafts"] = drafts
            st.session_state["github_username"] = username.strip()
            st.success(f"Found {len(drafts)} repos!")
        except Exception as e:
            st.error(f"Failed to fetch repos: {e}")

# Display drafts for review
drafts = st.session_state.get("github_drafts", [])
gh_username = st.session_state.get("github_username", "")

if drafts:
    st.markdown("---")
    st.subheader(f"Repos from {gh_username}")
    st.caption(f"{len(drafts)} repos found. Approve the ones you want in your KB.")

    for i, draft in enumerate(drafts):
        with st.expander(f"📦 {draft['title']}" + (f" — {draft['description'][:60]}..." if draft['description'] else "")):
            with st.form(f"approve_gh_{i}"):
                title = st.text_input("Title", value=draft["title"], key=f"gh_title_{i}")
                description = st.text_area(
                    "Description",
                    value=draft["description"],
                    height=100,
                    key=f"gh_desc_{i}",
                )
                tech_raw = st.text_input(
                    "Tech Stack",
                    value=", ".join(draft["tech_stack"]),
                    key=f"gh_tech_{i}",
                )
                github_url = st.text_input("GitHub URL", value=draft.get("github_url", ""), key=f"gh_url_{i}")
                highlights = st.text_area("Highlights", value="", key=f"gh_hi_{i}", height=80)

                col1, col2 = st.columns(2)
                approve = col1.form_submit_button("✅ Approve & Save")
                skip = col2.form_submit_button("⏭️ Skip")

            # Suggest description button (outside form to avoid nested form issues)
            if st.button("💡 Suggest Description", key=f"suggest_{i}"):
                with st.spinner("Generating description..."):
                    suggested = suggest_description(
                        {"name": draft["title"], "description": draft["description"],
                         "language": "", "topics": draft["tech_stack"]},
                        draft.get("_readme"),
                    )
                    st.info(f"**Suggested:** {suggested}")
                    st.caption("Copy this into the Description field above if you like it, then Approve.")

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
                                highlights=highlights.strip() or None,
                                source="github_import",
                                status="verified",  # approved = verified, goes to kb_chunk
                            )
                    st.success(f"✅ '{title}' approved and added to KB!")
                else:
                    st.error("Title and Description are required.")
