"""Resume import page — upload PDF/DOCX → AI extraction → review/approve each item."""
import sys
from pathlib import Path

_backend = str(Path(__file__).resolve().parent.parent.parent / "backend")
if _backend not in sys.path:
    sys.path.insert(0, _backend)

import json
from datetime import date, datetime
import streamlit as st
from app.db import get_session
from app.config import get_settings
from app.services.resume_import_service import extract_resume
from app.services.profile_service import upsert_profile
from app.services.education_service import create_education
from app.services.experience_service import create_experience
from app.services.project_service import create_project
from app.services.skill_service import create_skill
from app.services.certification_service import create_certification

st.set_page_config(page_title="Import Resume — CareerOS", page_icon="📄")
st.title("📄 Import Resume")

settings = get_settings()
user_id = settings.default_user_id


def parse_date(val: str | None) -> date | None:
    """Parse date strings from the AI extraction into Python date objects.

    Handles: 'YYYY-MM-DD', 'YYYY-MM', 'YYYY', 'present'/'current', and None/empty.
    """
    if not val or not isinstance(val, str):
        return None
    val = val.strip().lower()
    if val in ("present", "current", "ongoing", "now", ""):
        return None
    # Try full ISO date first
    for fmt in ("%Y-%m-%d", "%Y-%m", "%Y"):
        try:
            return datetime.strptime(val, fmt).date()
        except ValueError:
            continue
    # Try common formats like "Jan 2022", "January 2022"
    for fmt in ("%b %Y", "%B %Y", "%m/%Y", "%m-%Y"):
        try:
            return datetime.strptime(val, fmt).date()
        except ValueError:
            continue
    return None


st.markdown(
    "Upload your resume (PDF or DOCX). AI will extract structured data for you to "
    "**review and approve** before it enters your Knowledge Base."
)

uploaded = st.file_uploader("Upload Resume", type=["pdf", "docx"])

if uploaded is not None:
    # Extract
    if "resume_data" not in st.session_state or st.session_state.get("resume_filename") != uploaded.name:
        with st.spinner("🤖 Extracting data with Gemini..."):
            try:
                data = extract_resume(uploaded.read(), uploaded.name)
                st.session_state["resume_data"] = data
                st.session_state["resume_filename"] = uploaded.name
            except ValueError as e:
                st.error(f"Extraction failed: {e}")
                with st.expander("Raw model output"):
                    st.code(str(e))
                st.stop()

    data = st.session_state["resume_data"]

    # --- Basic Info ---
    basic = data.get("basic_info", {})
    if basic:
        st.subheader("👤 Basic Info")
        with st.form("approve_basic"):
            full_name = st.text_input("Full Name", value=basic.get("full_name", ""))
            email = st.text_input("Email", value=basic.get("email", ""))
            phone = st.text_input("Phone", value=basic.get("phone", ""))
            location = st.text_input("Location", value=basic.get("location", ""))
            summary = st.text_area(
                "Professional Summary",
                value=basic.get("summary", ""),
                height=120,
                help="Extracted from the summary/objective section of your resume",
            )
            links = basic.get("links", {})
            # Show links as editable JSON
            st.markdown("**Links**")
            links_json = st.text_area(
                "Links (JSON)",
                value=json.dumps(links, indent=2) if links else '{\n  "github": "",\n  "linkedin": "",\n  "portfolio": ""\n}',
                height=100,
                key="basic_links",
            )
            if st.form_submit_button("✅ Approve & Save Basic Info"):
                if full_name.strip():
                    try:
                        parsed_links = json.loads(links_json) if links_json.strip() else {}
                    except json.JSONDecodeError:
                        parsed_links = links
                    with get_session() as session:
                        upsert_profile(
                            session, user_id=user_id, full_name=full_name.strip(),
                            email=email.strip() or None, phone=phone.strip() or None,
                            location=location.strip() or None, links=parsed_links,
                            summary=summary.strip() or None,
                        )
                    st.success("Basic info saved!")
                else:
                    st.error("Full name is required.")

    # --- Education ---
    education_items = data.get("education", [])
    if education_items:
        st.subheader("🎓 Education")
        for i, edu in enumerate(education_items):
            date_hint = ""
            if edu.get("start_date") or edu.get("end_date"):
                s = edu.get("start_date", "?")
                e = edu.get("end_date", "present")
                date_hint = f" ({s} → {e})"
            with st.form(f"approve_edu_{i}"):
                st.markdown(f"**{edu.get('degree', '')}** — {edu.get('institution', '')}{date_hint}")
                degree = st.text_input("Degree", value=edu.get("degree", ""), key=f"edu_deg_{i}")
                institution = st.text_input("Institution", value=edu.get("institution", ""), key=f"edu_inst_{i}")
                col1, col2 = st.columns(2)
                start_date = col1.date_input(
                    "Start Date", value=parse_date(edu.get("start_date")), key=f"edu_sd_{i}",
                )
                end_date = col2.date_input(
                    "End Date", value=parse_date(edu.get("end_date")), key=f"edu_ed_{i}",
                )
                score = st.text_input("Score", value=edu.get("score", ""), key=f"edu_score_{i}")
                highlights = st.text_area(
                    "Highlights",
                    value=edu.get("highlights", ""),
                    key=f"edu_hi_{i}",
                    height=100,
                    help="Coursework, honors, achievements",
                )
                if st.form_submit_button(f"✅ Approve Education #{i+1}"):
                    if degree.strip() and institution.strip():
                        with get_session() as session:
                            create_education(
                                session, user_id=user_id,
                                degree=degree.strip(), institution=institution.strip(),
                                start_date=start_date, end_date=end_date,
                                score=score.strip() or None, highlights=highlights.strip() or None,
                            )
                        st.success(f"Education #{i+1} saved!")
                    else:
                        st.error("Degree and Institution are required.")

    # --- Work Experience ---
    exp_items = data.get("work_experience", [])
    if exp_items:
        st.subheader("💼 Work Experience")
        for i, exp in enumerate(exp_items):
            date_hint = ""
            if exp.get("start_date") or exp.get("end_date"):
                s = exp.get("start_date", "?")
                e = exp.get("end_date", "present")
                date_hint = f" ({s} → {e})"
            with st.form(f"approve_exp_{i}"):
                st.markdown(f"**{exp.get('role_title', '')}** at {exp.get('company', '')}{date_hint}")
                company = st.text_input("Company", value=exp.get("company", ""), key=f"exp_co_{i}")
                role_title = st.text_input("Role", value=exp.get("role_title", ""), key=f"exp_role_{i}")
                col1, col2 = st.columns(2)
                start_date = col1.date_input(
                    "Start Date", value=parse_date(exp.get("start_date")), key=f"exp_sd_{i}",
                )
                end_date = col2.date_input(
                    "End Date (leave empty if current)",
                    value=parse_date(exp.get("end_date")),
                    key=f"exp_ed_{i}",
                )
                description = st.text_area(
                    "Description",
                    value=exp.get("description", ""),
                    key=f"exp_desc_{i}",
                    height=200,
                    help="All bullet points and responsibilities from the resume",
                )
                tech_raw = st.text_input(
                    "Tech Stack (comma-separated)",
                    value=", ".join(exp.get("tech_stack", [])),
                    key=f"exp_tech_{i}",
                )
                if st.form_submit_button(f"✅ Approve Experience #{i+1}"):
                    if company.strip() and role_title.strip() and description.strip():
                        tech = [t.strip() for t in tech_raw.split(",") if t.strip()]
                        with get_session() as session:
                            create_experience(
                                session, user_id=user_id,
                                company=company.strip(), role_title=role_title.strip(),
                                description=description.strip(), tech_stack=tech,
                                start_date=start_date, end_date=end_date,
                            )
                        st.success(f"Experience #{i+1} saved!")
                    else:
                        st.error("Company, Role, and Description are required.")

    # --- Projects ---
    proj_items = data.get("projects", [])
    if proj_items:
        st.subheader("🔧 Projects")
        for i, proj in enumerate(proj_items):
            with st.form(f"approve_proj_{i}"):
                st.markdown(f"**{proj.get('title', '')}**")
                title = st.text_input("Title", value=proj.get("title", ""), key=f"proj_title_{i}")
                description = st.text_area(
                    "Description",
                    value=proj.get("description", ""),
                    key=f"proj_desc_{i}",
                    height=150,
                )
                tech_raw = st.text_input(
                    "Tech Stack (comma-separated)",
                    value=", ".join(proj.get("tech_stack", [])),
                    key=f"proj_tech_{i}",
                )
                github_url = st.text_input("GitHub URL", value=proj.get("github_url", ""), key=f"proj_gh_{i}")
                live_url = st.text_input("Live/Demo URL", value=proj.get("live_url", ""), key=f"proj_live_{i}")
                highlights = st.text_area(
                    "Highlights",
                    value=proj.get("highlights", ""),
                    key=f"proj_hi_{i}",
                    height=80,
                )
                if st.form_submit_button(f"✅ Approve Project #{i+1}"):
                    if title.strip() and description.strip():
                        tech = [t.strip() for t in tech_raw.split(",") if t.strip()]
                        with get_session() as session:
                            create_project(
                                session, user_id=user_id,
                                title=title.strip(), description=description.strip(),
                                tech_stack=tech, github_url=github_url.strip() or None,
                                live_url=live_url.strip() or None,
                                highlights=highlights.strip() or None,
                            )
                        st.success(f"Project #{i+1} saved!")
                    else:
                        st.error("Title and Description are required.")

    # --- Skills ---
    skill_items = data.get("skills", [])
    if skill_items:
        st.subheader("🎯 Skills")
        with st.form("approve_skills"):
            selected = []
            for i, sk in enumerate(skill_items):
                if st.checkbox(f"{sk.get('name', '')} ({sk.get('category', '')})", value=True, key=f"sk_{i}"):
                    selected.append(sk)
            if st.form_submit_button("✅ Approve Selected Skills"):
                saved = 0
                for sk in selected:
                    if sk.get("name"):
                        cat = sk.get("category", "tool")
                        valid_cats = ["language", "framework", "cloud", "devops", "ml", "database", "tool", "soft_skill"]
                        if cat not in valid_cats:
                            cat = "tool"
                        try:
                            with get_session() as session:
                                create_skill(session, user_id=user_id, name=sk["name"], category=cat)
                            saved += 1
                        except Exception:
                            pass  # duplicate skill, skip silently
                st.success(f"Saved {saved} skills!")

    # --- Certifications ---
    cert_items = data.get("certifications", [])
    if cert_items:
        st.subheader("📜 Certifications")
        for i, cert in enumerate(cert_items):
            with st.form(f"approve_cert_{i}"):
                st.markdown(f"**{cert.get('title', '')}**")
                title = st.text_input("Title", value=cert.get("title", ""), key=f"cert_title_{i}")
                issuer = st.text_input("Issuer", value=cert.get("issuer", ""), key=f"cert_issuer_{i}")
                cert_date = st.date_input(
                    "Date",
                    value=parse_date(cert.get("date")),
                    key=f"cert_date_{i}",
                )
                url = st.text_input("URL", value=cert.get("url", ""), key=f"cert_url_{i}")
                if st.form_submit_button(f"✅ Approve Certification #{i+1}"):
                    if title.strip():
                        with get_session() as session:
                            create_certification(
                                session, user_id=user_id,
                                title=title.strip(), issuer=issuer.strip() or None,
                                date=cert_date, url=url.strip() or None,
                            )
                        st.success(f"Certification #{i+1} saved!")
                    else:
                        st.error("Title is required.")

    # --- Raw Extraction Debug ---
    with st.expander("🔍 Raw Extraction Output (debug)"):
        st.json(data)
