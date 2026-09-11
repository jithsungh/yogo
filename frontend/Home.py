"""CareerOS — Personal Job Application Copilot

Home page: dashboard with KB chunk count and quick navigation.
"""
import sys
from pathlib import Path

# Add backend/ to sys.path so Streamlit can import app.*
_backend = str(Path(__file__).resolve().parent.parent / "backend")
if _backend not in sys.path:
    sys.path.insert(0, _backend)

import streamlit as st
from sqlalchemy import text
from app.db import get_session
from app.config import get_settings

st.set_page_config(page_title="CareerOS", page_icon="🚀", layout="wide")

st.title("🚀 CareerOS — Knowledge Base")
st.markdown("Your personal job application copilot. Enter your data once, query it forever.")

settings = get_settings()
user_id = settings.default_user_id

if not user_id:
    st.error(
        "⚠️ DEFAULT_USER_ID not set in .env. "
        "Run `python -m scripts.create_default_user` from `backend/` first, "
        "then paste the UUID into your `.env` file."
    )
    st.stop()

st.markdown("---")

# Show KB stats
with get_session() as session:
    chunk_count = session.execute(
        text("SELECT COUNT(*) FROM kb_chunk WHERE user_id = :uid"),
        {"uid": user_id},
    ).scalar()
    profile_exists = session.execute(
        text("SELECT COUNT(*) FROM profile_basic WHERE user_id = :uid"),
        {"uid": user_id},
    ).scalar()
    edu_count = session.execute(
        text("SELECT COUNT(*) FROM education WHERE user_id = :uid"),
        {"uid": user_id},
    ).scalar()
    exp_count = session.execute(
        text("SELECT COUNT(*) FROM work_experience WHERE user_id = :uid"),
        {"uid": user_id},
    ).scalar()
    proj_count = session.execute(
        text("SELECT COUNT(*) FROM project WHERE user_id = :uid AND status = 'verified'"),
        {"uid": user_id},
    ).scalar()
    skill_count = session.execute(
        text("SELECT COUNT(*) FROM skill WHERE user_id = :uid"),
        {"uid": user_id},
    ).scalar()
    cert_count = session.execute(
        text("SELECT COUNT(*) FROM certification WHERE user_id = :uid"),
        {"uid": user_id},
    ).scalar()

col1, col2, col3, col4 = st.columns(4)
col1.metric("🧠 KB Chunks", chunk_count)
col2.metric("💼 Experiences", exp_count)
col3.metric("🔧 Projects", proj_count)
col4.metric("🎯 Skills", skill_count)

col5, col6, col7 = st.columns(3)
col5.metric("🎓 Education", edu_count)
col6.metric("📜 Certifications", cert_count)
col7.metric("👤 Profile", "✅" if profile_exists else "❌")

st.markdown("---")
st.markdown(
    "Use the **sidebar** to navigate to each section and start building your Knowledge Base. "
    "Every entry you save gets embedded and becomes semantically searchable."
)
