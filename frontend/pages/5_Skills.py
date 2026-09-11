"""Skills page — list + add/delete skills."""
import sys
from pathlib import Path

_backend = str(Path(__file__).resolve().parent.parent.parent / "backend")
if _backend not in sys.path:
    sys.path.insert(0, _backend)

import streamlit as st
from app.db import get_session
from app.config import get_settings
from app.services.skill_service import list_skills, create_skill, delete_skill

st.set_page_config(page_title="Skills — CareerOS", page_icon="🎯")
st.title("🎯 Skills")

CATEGORIES = [
    "language", "framework", "cloud", "devops", "ml", "database", "tool", "soft_skill",
]

settings = get_settings()
user_id = settings.default_user_id

# List existing skills grouped by category
with get_session() as session:
    skills = list_skills(session, user_id=user_id)

if skills:
    # Group by category
    by_cat = {}
    for s in skills:
        by_cat.setdefault(s.category, []).append(s)

    for cat in CATEGORIES:
        cat_skills = by_cat.get(cat, [])
        if cat_skills:
            st.subheader(cat.replace("_", " ").title())
            cols = st.columns(4)
            for i, s in enumerate(cat_skills):
                with cols[i % 4]:
                    prof_str = f" ({s.proficiency}/5)" if s.proficiency else ""
                    st.write(f"**{s.name}**{prof_str}")
                    if st.button("❌", key=f"del_skill_{s.id}"):
                        with get_session() as session:
                            delete_skill(session, skill_id=s.id)
                        st.rerun()
else:
    st.info("No skills yet. Add some below.")

st.markdown("---")
st.subheader("Add Skill")

with st.form("add_skill"):
    col1, col2, col3 = st.columns(3)
    name = col1.text_input("Skill Name", placeholder="e.g. Kubernetes")
    category = col2.selectbox("Category", CATEGORIES)
    proficiency = col3.slider("Proficiency", 1, 5, 3)

    submitted = st.form_submit_button("➕ Add Skill")

if submitted:
    if not name.strip():
        st.error("Skill name is required.")
        st.stop()

    with st.spinner("Saving and embedding..."):
        try:
            with get_session() as session:
                create_skill(
                    session,
                    user_id=user_id,
                    name=name.strip(),
                    category=category,
                    proficiency=proficiency,
                )
            st.success(f"✅ Skill '{name}' added!")
        except Exception as e:
            if "unique" in str(e).lower() or "duplicate" in str(e).lower():
                st.error(f"Skill '{name}' already exists.")
            else:
                raise
    st.rerun()
