"""Projects page — list + add/edit/delete project entries."""
import sys
from pathlib import Path

_backend = str(Path(__file__).resolve().parent.parent.parent / "backend")
if _backend not in sys.path:
    sys.path.insert(0, _backend)

import streamlit as st
from app.db import get_session
from app.config import get_settings
from app.services.project_service import (
    list_projects, create_project, update_project, delete_project,
)

st.set_page_config(page_title="Projects — CareerOS", page_icon="🔧")
st.title("🔧 Projects")

settings = get_settings()
user_id = settings.default_user_id

# List existing entries (verified and draft)
with get_session() as session:
    projects = list_projects(session, user_id=user_id)

if projects:
    for p in projects:
        status_badge = "✅" if p.status == "verified" else "📝 draft"
        with st.expander(f"**{p.title}** {status_badge}"):
            st.write(p.description)
            if p.tech_stack:
                st.write("**Tech:** " + ", ".join(p.tech_stack))
            if p.github_url:
                st.write(f"🔗 [GitHub]({p.github_url})")
            if p.live_url:
                st.write(f"🌐 [Live]({p.live_url})")
            if p.highlights:
                st.write(f"**Highlights:** {p.highlights}")
            st.caption(f"Source: {p.source} | Status: {p.status}")

            col1, col2 = st.columns(2)
            if col2.button("🗑️ Delete", key=f"del_proj_{p.id}"):
                with get_session() as session:
                    delete_project(session, project_id=p.id)
                st.success("Deleted!")
                st.rerun()
else:
    st.info("No projects yet. Add one below, or import from GitHub.")

st.markdown("---")
st.subheader("Add Project")

with st.form("add_project"):
    title = st.text_input("Project Title", placeholder="e.g. CareerOS")
    description = st.text_area(
        "Description",
        placeholder="What it does, how it works, what you built...",
        height=150,
    )
    tech_stack_raw = st.text_input(
        "Tech Stack (comma-separated)",
        placeholder="e.g. Python, FastAPI, PostgreSQL, Gemini",
    )
    col1, col2 = st.columns(2)
    github_url = col1.text_input("GitHub URL", placeholder="https://github.com/you/repo")
    live_url = col2.text_input("Live URL", placeholder="https://your-app.com")
    highlights = st.text_area("Highlights", placeholder="Key impact, metrics, achievements...", height=80)

    submitted = st.form_submit_button("➕ Add Project")

if submitted:
    if not title.strip() or not description.strip():
        st.error("Title and Description are required.")
        st.stop()

    tech_stack = [t.strip() for t in tech_stack_raw.split(",") if t.strip()] if tech_stack_raw else []

    with st.spinner("Saving and embedding..."):
        with get_session() as session:
            create_project(
                session,
                user_id=user_id,
                title=title.strip(),
                description=description.strip(),
                tech_stack=tech_stack,
                github_url=github_url.strip() or None,
                live_url=live_url.strip() or None,
                highlights=highlights.strip() or None,
            )
    st.success("✅ Project added!")
    st.rerun()
