"""Profile page — basic info (singleton per user)."""
import sys
from pathlib import Path

_backend = str(Path(__file__).resolve().parent.parent.parent / "backend")
if _backend not in sys.path:
    sys.path.insert(0, _backend)

import json
import streamlit as st
from app.db import get_session
from app.config import get_settings
from app.services.profile_service import get_profile, upsert_profile

st.set_page_config(page_title="Profile — CareerOS", page_icon="👤")
st.title("👤 Profile")

settings = get_settings()
user_id = settings.default_user_id

# Load existing profile
with get_session() as session:
    profile = get_profile(session, user_id=user_id)

st.markdown("Your basic info — this is the foundation of your KB.")

with st.form("profile_form"):
    full_name = st.text_input("Full Name", value=profile.full_name if profile else "")
    email = st.text_input("Email", value=profile.email if profile else "")
    phone = st.text_input("Phone", value=profile.phone if profile else "")
    location = st.text_input("Location", value=profile.location if profile else "")
    summary = st.text_area("Professional Summary", value=profile.summary if profile else "", height=150)

    st.markdown("**Links** (JSON format)")
    default_links = json.dumps(profile.links, indent=2) if profile and profile.links else '{\n  "github": "",\n  "linkedin": "",\n  "portfolio": "",\n  "leetcode": ""\n}'
    links_raw = st.text_area("Links", value=default_links, height=120)

    submitted = st.form_submit_button("💾 Save Profile")

if submitted:
    try:
        links = json.loads(links_raw) if links_raw.strip() else {}
    except json.JSONDecodeError:
        st.error("Invalid JSON in Links field. Please fix and retry.")
        st.stop()

    if not full_name.strip():
        st.error("Full Name is required.")
        st.stop()

    with st.spinner("Saving and embedding..."):
        with get_session() as session:
            upsert_profile(
                session,
                user_id=user_id,
                full_name=full_name.strip(),
                email=email.strip() or None,
                phone=phone.strip() or None,
                location=location.strip() or None,
                links=links,
                summary=summary.strip() or None,
            )
    st.success("✅ Profile saved and embedded!")
    st.rerun()
