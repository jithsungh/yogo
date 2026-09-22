"""Add job descriptions from pasted text or a best-effort URL extraction."""
import sys
from pathlib import Path

_backend = str(Path(__file__).resolve().parent.parent.parent / "backend")
if _backend not in sys.path:
    sys.path.insert(0, _backend)

import streamlit as st

from app.config import get_settings
from app.db import get_session
from app.llm.gemini_client import GeminiRateLimitError
from app.services.jd_service import fetch_jd_from_url, ingest_job_description

st.set_page_config(page_title="Add Job Description - CareerOS", page_icon="📄", layout="wide")
st.title("📄 Add Job Description")
st.caption("Paste text is the reliable path. URL extraction is best-effort and can fall back cleanly.")

user_id = get_settings().default_user_id
if not user_id:
    st.error("DEFAULT_USER_ID is not configured. Bootstrap the default user first.")
    st.stop()

mode = st.radio("Input", ["Paste text", "Paste a URL"], horizontal=True)
source_url = None
raw_text = ""
if mode == "Paste text":
    raw_text = st.text_area("Job description", height=360, placeholder="Paste the full job description here...")
else:
    source_url = st.text_input("Job description URL", placeholder="https://example.com/jobs/role")
    if source_url and st.button("Extract page text"):
        with st.spinner("Fetching and extracting..."):
            extracted = fetch_jd_from_url(source_url.strip())
        if extracted:
            st.session_state["jd_extracted_text"] = extracted
            st.success("Page text extracted. Review it before ingesting.")
        else:
            st.warning("Could not extract a usable job description from this page. Paste the text instead.")
    raw_text = st.text_area(
        "Extracted or pasted job description",
        value=st.session_state.get("jd_extracted_text", ""),
        height=360,
    )

if st.button("Parse and save job description", type="primary"):
    if len(raw_text.strip()) < 50:
        st.error("Add a fuller job description before parsing.")
        st.stop()
    with st.spinner("Extracting requirements, resolving role, and embedding..."):
        try:
            with get_session() as session:
                jd = ingest_job_description(
                    session,
                    user_id=user_id,
                    raw_text=raw_text,
                    source_url=source_url,
                )
            st.success(f"Saved {jd.company or 'job description'} - {jd.role_title or 'role not detected'}.")
            if jd.role_id is None:
                st.warning("Role not recognized. Review it below in Match Results and assign a canonical role.")
            st.session_state.pop("jd_extracted_text", None)
        except GeminiRateLimitError as exc:
            st.error(str(exc))
            st.info("No job description was saved. Wait briefly, then try again.")
        except (ValueError, RuntimeError) as exc:
            st.error(str(exc))