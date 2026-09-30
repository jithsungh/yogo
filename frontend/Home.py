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

# One round-trip for every count on the page.
with get_session() as session:
    c = session.execute(
        text("""
            SELECT
              (SELECT count(*) FROM kb_chunk WHERE user_id = :uid)                         AS chunks,
              (SELECT count(*) FROM profile_basic WHERE user_id = :uid)                    AS profile,
              (SELECT count(*) FROM education WHERE user_id = :uid)                        AS education,
              (SELECT count(*) FROM work_experience WHERE user_id = :uid)                  AS experience,
              (SELECT count(*) FROM project WHERE user_id = :uid AND status = 'verified')  AS projects,
              (SELECT count(*) FROM skill WHERE user_id = :uid)                            AS skills,
              (SELECT count(*) FROM certification WHERE user_id = :uid)                    AS certs,
              (SELECT count(*) FROM job_description WHERE user_id = :uid)                  AS jds,
              (SELECT count(*) FROM resume_version WHERE user_id = :uid)                   AS resumes,
              (SELECT count(*) FROM qa_question WHERE user_id = :uid)                      AS qa_questions,
              (SELECT coalesce(sum(a.times_reused), 0) FROM qa_answer a
                 JOIN qa_question q ON q.id = a.question_id WHERE q.user_id = :uid)        AS qa_reuses
        """),
        {"uid": user_id},
    ).mappings().first()

st.subheader("Knowledge base")
col1, col2, col3, col4 = st.columns(4)
col1.metric("🧠 KB Chunks", c["chunks"])
col2.metric("💼 Experiences", c["experience"])
col3.metric("🔧 Projects", c["projects"])
col4.metric("🎯 Skills", c["skills"])

col5, col6, col7 = st.columns(3)
col5.metric("🎓 Education", c["education"])
col6.metric("📜 Certifications", c["certs"])
col7.metric("👤 Profile", "✅" if c["profile"] else "❌")

st.subheader("Applications")
a1, a2, a3, a4 = st.columns(4)
a1.metric("📄 Job descriptions", c["jds"])
a2.metric("📑 Resumes generated", c["resumes"])
a3.metric("💬 Banked questions", c["qa_questions"])
a4.metric("♻️ Answers reused", c["qa_reuses"],
          help="Application questions answered from your bank without calling Gemini.")

st.markdown("---")
st.markdown(
    "Use the **sidebar**: build your knowledge base (pages 1–8), add job descriptions and "
    "check your match (9–10), generate a tailored resume (11), and answer application "
    "questions from your answer bank (12)."
)
