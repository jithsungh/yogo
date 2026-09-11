"""Education page — list + add/edit/delete education entries."""
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
from app.services.education_service import (
    list_education, create_education, update_education, delete_education,
)

st.set_page_config(page_title="Education — CareerOS", page_icon="🎓")
st.title("🎓 Education")

settings = get_settings()
user_id = settings.default_user_id

# List existing entries
with get_session() as session:
    entries = list_education(session, user_id=user_id)

if entries:
    for entry in entries:
        with st.expander(f"**{entry.degree}** — {entry.institution}"):
            date_str = ""
            if entry.start_date:
                date_str = entry.start_date.isoformat()
            if entry.end_date:
                date_str += f" → {entry.end_date.isoformat()}"
            if date_str:
                st.caption(date_str)
            if entry.score:
                st.write(f"**Score:** {entry.score}")
            if entry.highlights:
                st.write(entry.highlights)

            col1, col2 = st.columns(2)
            if col2.button("🗑️ Delete", key=f"del_edu_{entry.id}"):
                with get_session() as session:
                    delete_education(session, education_id=entry.id)
                st.success("Deleted!")
                st.rerun()
else:
    st.info("No education entries yet. Add one below.")

st.markdown("---")
st.subheader("Add Education")

with st.form("add_education"):
    degree = st.text_input("Degree / Program", placeholder="e.g. B.Tech in Computer Science")
    institution = st.text_input("Institution", placeholder="e.g. IIT Madras")
    col1, col2 = st.columns(2)
    start_date = col1.date_input("Start Date", value=None)
    end_date = col2.date_input("End Date", value=None)
    score = st.text_input("Score (GPA/Percentage)", placeholder="e.g. 8.5/10 or 85%")
    highlights = st.text_area("Highlights", placeholder="Relevant coursework, achievements, etc.", height=100)

    submitted = st.form_submit_button("➕ Add Education")

if submitted:
    if not degree.strip() or not institution.strip():
        st.error("Degree and Institution are required.")
        st.stop()

    with st.spinner("Saving and embedding..."):
        with get_session() as session:
            create_education(
                session,
                user_id=user_id,
                degree=degree.strip(),
                institution=institution.strip(),
                start_date=start_date,
                end_date=end_date,
                score=score.strip() or None,
                highlights=highlights.strip() or None,
            )
    st.success("✅ Education entry added!")
    st.rerun()
