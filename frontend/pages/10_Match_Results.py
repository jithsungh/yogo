"""Review and recompute match results for ingested job descriptions."""
import sys
from pathlib import Path

_backend = str(Path(__file__).resolve().parent.parent.parent / "backend")
if _backend not in sys.path:
    sys.path.insert(0, _backend)

import streamlit as st

from app.config import get_settings
from app.db import get_session
from app.services.jd_service import list_job_descriptions
from app.services.match_service import compute_match, get_latest_match
from app.services.role_resolution import assign_role, list_roles

st.set_page_config(page_title="Match Results - CareerOS", page_icon="🎯", layout="wide")
st.title("🎯 Match Results")

user_id = get_settings().default_user_id
if not user_id:
    st.error("DEFAULT_USER_ID is not configured. Bootstrap the default user first.")
    st.stop()

with get_session() as session:
    job_descriptions = list_job_descriptions(session, user_id=user_id)

if not job_descriptions:
    st.info("No job descriptions yet. Add one from the sidebar.")
    st.stop()

rows = []
for jd in job_descriptions:
    with get_session() as session:
        result = get_latest_match(session, user_id=user_id, jd_id=jd.id)
    rows.append({
        "id": jd.id,
        "company": jd.company or "(unknown)",
        "role": jd.role_title or "(unknown)",
        "role_status": "Resolved" if jd.role_id else "Needs review",
        "score": f"{result.score:.0%}" if result else "-",
        "verdict": result.verdict if result else "Not scored",
    })
st.dataframe(
    [{key: value for key, value in row.items() if key != "id"} for row in rows],
    use_container_width=True,
    hide_index=True,
)

labels = [f"{row['company']} - {row['role']}" for row in rows]
selected_index = st.selectbox("Review job description", range(len(rows)), format_func=lambda index: labels[index])
selected = rows[selected_index]
jd = job_descriptions[selected_index]

with get_session() as session:
    result = get_latest_match(session, user_id=user_id, jd_id=jd.id)
    roles = list_roles(session)

if not jd.role_id and roles:
    role_labels = {role_id: f"{canonical_name} ({category})" for role_id, canonical_name, category in roles}
    chosen_role = st.selectbox("Assign canonical role", list(role_labels), format_func=role_labels.get)
    if st.button("Save role assignment"):
        with get_session() as session:
            assign_role(
                session,
                user_id=user_id,
                jd_id=jd.id,
                role_id=chosen_role,
                raw_title=jd.role_title,
            )
        st.success("Role assignment saved.")
        st.rerun()

col1, col2 = st.columns([1, 3])
if result:
    col1.metric("Score", f"{result.score:.0%}")
    col2.subheader(f"Verdict: {result.verdict.title()}")
else:
    col1.metric("Score", "Not scored")
    col2.info("Compute a match to see the fit breakdown.")

if st.button("Recompute match", type="primary"):
    with st.spinner("Comparing profile skills and semantic KB context..."):
        with get_session() as session:
            compute_match(session, user_id=user_id, jd_id=jd.id)
    st.rerun()

if result:
    matched, missing, surplus = st.columns(3)
    matched.subheader("Matched")
    matched.write(result.matched_skills or ["None"])
    missing.subheader("Missing")
    missing.write(result.missing_skills or ["None"])
    surplus.subheader("Surplus")
    surplus.write(result.surplus_skills or ["None"])