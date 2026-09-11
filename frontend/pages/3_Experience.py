"""Experience page — list + add/edit/delete work experience entries."""
import sys
from pathlib import Path

_backend = str(Path(__file__).resolve().parent.parent.parent / "backend")
if _backend not in sys.path:
    sys.path.insert(0, _backend)

import uuid
from datetime import date
import streamlit as st
from app.db import get_session
from app.config import get_settings
from app.services.experience_service import (
    list_experiences, create_experience, update_experience, delete_experience,
)

st.set_page_config(page_title="Experience — CareerOS", page_icon="💼")
st.title("💼 Work Experience")

settings = get_settings()
user_id = settings.default_user_id

# List existing entries
with get_session() as session:
    entries = list_experiences(session, user_id=user_id)

if entries:
    for entry in entries:
        with st.expander(f"**{entry.role_title}** at {entry.company}"):
            date_str = ""
            if entry.start_date:
                date_str = entry.start_date.isoformat()
            if entry.end_date:
                date_str += f" → {entry.end_date.isoformat()}"
            else:
                date_str += " → present"
            if date_str:
                st.caption(date_str)
            st.write(entry.description)
            if entry.tech_stack:
                st.write("**Tech:** " + ", ".join(entry.tech_stack))

            col1, col2 = st.columns(2)
            if col2.button("🗑️ Delete", key=f"del_exp_{entry.id}"):
                with get_session() as session:
                    delete_experience(session, experience_id=entry.id)
                st.success("Deleted!")
                st.rerun()
else:
    st.info("No work experience entries yet. Add one below.")

st.markdown("---")
st.subheader("Add Experience")

with st.form("add_experience"):
    company = st.text_input("Company", placeholder="e.g. Google")
    role_title = st.text_input("Role Title", placeholder="e.g. Software Engineer")
    col1, col2 = st.columns(2)
    start_date = col1.date_input("Start Date", value=None)
    end_date = col2.date_input("End Date (leave empty if current)", value=None)
    description = st.text_area(
        "Description",
        placeholder="What you did, impact, responsibilities...",
        height=200,
    )
    tech_stack_raw = st.text_input(
        "Tech Stack (comma-separated)",
        placeholder="e.g. Python, Kubernetes, Terraform, AWS",
    )

    submitted = st.form_submit_button("➕ Add Experience")

if submitted:
    if not company.strip() or not role_title.strip() or not description.strip():
        st.error("Company, Role Title, and Description are required.")
        st.stop()

    tech_stack = [t.strip() for t in tech_stack_raw.split(",") if t.strip()] if tech_stack_raw else []

    with st.spinner("Saving and embedding..."):
        with get_session() as session:
            create_experience(
                session,
                user_id=user_id,
                company=company.strip(),
                role_title=role_title.strip(),
                description=description.strip(),
                tech_stack=tech_stack,
                start_date=start_date,
                end_date=end_date,
            )
    st.success("✅ Experience added!")
    st.rerun()
