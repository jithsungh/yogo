"""Certifications page — list + add/delete certification entries."""
import sys
from pathlib import Path

_backend = str(Path(__file__).resolve().parent.parent.parent / "backend")
if _backend not in sys.path:
    sys.path.insert(0, _backend)

from datetime import date
import streamlit as st
from app.db import get_session
from app.config import get_settings
from app.services.certification_service import (
    list_certifications, create_certification, delete_certification,
)

st.set_page_config(page_title="Certifications — CareerOS", page_icon="📜")
st.title("📜 Certifications")

settings = get_settings()
user_id = settings.default_user_id

# List existing entries
with get_session() as session:
    certs = list_certifications(session, user_id=user_id)

if certs:
    for c in certs:
        with st.expander(f"**{c.title}**" + (f" — {c.issuer}" if c.issuer else "")):
            if c.date:
                st.caption(c.date.isoformat())
            if c.url:
                st.write(f"🔗 [View Certificate]({c.url})")

            if st.button("🗑️ Delete", key=f"del_cert_{c.id}"):
                with get_session() as session:
                    delete_certification(session, certification_id=c.id)
                st.success("Deleted!")
                st.rerun()
else:
    st.info("No certifications yet. Add one below.")

st.markdown("---")
st.subheader("Add Certification")

with st.form("add_certification"):
    title = st.text_input("Title", placeholder="e.g. AWS Solutions Architect Associate")
    issuer = st.text_input("Issuer", placeholder="e.g. Amazon Web Services")
    cert_date = st.date_input("Date", value=None)
    url = st.text_input("URL", placeholder="https://credentials.example.com/...")

    submitted = st.form_submit_button("➕ Add Certification")

if submitted:
    if not title.strip():
        st.error("Title is required.")
        st.stop()

    with st.spinner("Saving and embedding..."):
        with get_session() as session:
            create_certification(
                session,
                user_id=user_id,
                title=title.strip(),
                issuer=issuer.strip() or None,
                date=cert_date,
                url=url.strip() or None,
            )
    st.success("✅ Certification added!")
    st.rerun()
