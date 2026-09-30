"""Add job descriptions, and inspect what was parsed out of the stored ones."""
import sys
from pathlib import Path

_backend = str(Path(__file__).resolve().parent.parent.parent / "backend")
if _backend not in sys.path:
    sys.path.insert(0, _backend)

import streamlit as st

from app.config import get_settings
from app.db import get_session
from app.llm.gemini_client import GeminiRateLimitError
from app.services.jd_service import (
    delete_job_description,
    fetch_jd_from_url,
    get_job_description_detail,
    ingest_job_description,
    list_job_description_summaries,
)

st.set_page_config(page_title="Job Descriptions - CareerOS", page_icon="📄", layout="wide")
st.title("📄 Job Descriptions")

user_id = get_settings().default_user_id
if not user_id:
    st.error("DEFAULT_USER_ID is not configured. Bootstrap the default user first.")
    st.stop()

add_tab, stored_tab = st.tabs(["Add", "Stored"])

# ══════════════════════════════════════════════════════════════════════════════
# Add
# ══════════════════════════════════════════════════════════════════════════════
with add_tab:
    st.caption("Paste text is the reliable path. URL extraction is best-effort "
               "and can fall back cleanly.")

    mode = st.radio("Input", ["Paste text", "Paste a URL"], horizontal=True)
    source_url = None
    raw_text = ""

    if mode == "Paste text":
        raw_text = st.text_area("Job description", height=360,
                                placeholder="Paste the full job description here...")
    else:
        source_url = st.text_input("Job description URL",
                                   placeholder="https://example.com/jobs/role")
        if source_url and st.button("Extract page text"):
            with st.spinner("Fetching and extracting..."):
                extracted = fetch_jd_from_url(source_url.strip())
            if extracted:
                st.session_state["jd_extracted_text"] = extracted
                st.success("Page text extracted. Review it before ingesting.")
            else:
                st.warning("Could not extract a usable job description from this page. "
                           "Paste the text instead.")
        raw_text = st.text_area(
            "Extracted or pasted job description",
            value=st.session_state.get("jd_extracted_text", ""),
            height=360,
        )

    # No st.stop() here: it would halt the whole script and leave the Stored
    # tab blank for the rest of this run.
    if st.button("Parse and save job description", type="primary"):
        if len(raw_text.strip()) < 50:
            st.error("Add a fuller job description before parsing.")
        else:
            with st.spinner("Extracting requirements, resolving role, and embedding..."):
                try:
                    with get_session() as session:
                        jd = ingest_job_description(
                            session,
                            user_id=user_id,
                            raw_text=raw_text,
                            source_url=source_url,
                        )
                        saved_company = jd.company or "job description"
                        saved_role = jd.role_title or "role not detected"
                        role_missing = jd.role_id is None
                    st.success(f"Saved {saved_company} - {saved_role}. "
                               "See the **Stored** tab.")
                    if role_missing:
                        st.warning("Role not recognized. Review it in Match Results "
                                   "and assign a canonical role.")
                    st.session_state.pop("jd_extracted_text", None)
                except GeminiRateLimitError as exc:
                    st.error(str(exc))
                    st.info("No job description was saved. Wait briefly, then try again.")
                except (ValueError, RuntimeError) as exc:
                    st.error(str(exc))

# ══════════════════════════════════════════════════════════════════════════════
# Stored
# ══════════════════════════════════════════════════════════════════════════════
with stored_tab:
    with get_session() as session:
        summaries = list_job_description_summaries(session, user_id=user_id)

    if not summaries:
        st.info("Nothing stored yet. Add a job description on the **Add** tab.")
    else:
        st.caption(f"{len(summaries)} stored.")
        st.dataframe(
            [
                {
                    "Added": s["created_at"].strftime("%d %b %Y"),
                    "Company": s["company"] or "—",
                    "Role (as written)": s["role_title"] or "—",
                    "Canonical role": s["canonical_role"] or "unresolved",
                    "Match": (f"{s['score']:.0%} {s['verdict']}"
                              if s["score"] is not None else "not matched"),
                    "Resumes": s["resume_count"],
                }
                for s in summaries
            ],
            width="stretch",
            hide_index=True,
        )

        st.divider()

        def _label(s: dict) -> str:
            stamp = s["created_at"].strftime("%d %b %Y")
            head = f"{s['company'] or 'Unknown'} — {s['role_title'] or 'Unknown role'}"
            verdict = (f"  ·  {s['verdict']} ({s['score']:.0%})"
                       if s["score"] is not None else "  ·  not matched")
            return f"{stamp} — {head}{verdict}"

        options = {_label(s): str(s["id"]) for s in summaries}
        chosen = st.selectbox("Inspect a job description", list(options))
        jd_id = options[chosen]

        with get_session() as session:
            detail = get_job_description_detail(session, user_id=user_id, jd_id=jd_id)

    if summaries and not detail:
        st.error("That job description no longer exists.")
    elif summaries:
        parsed = detail["parsed_requirements"]

        # ── What was parsed out of the text ──
        st.subheader("Parsed details")
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Company", parsed.get("company") or detail["company"] or "—")
        c2.metric("Seniority", parsed.get("seniority") or "not stated")
        years = parsed.get("years_experience_min")
        c3.metric("Min experience", f"{years} yr" if years is not None else "not stated")
        c4.metric("Canonical role", detail["canonical_role"] or "unresolved")

        if not detail["canonical_role"]:
            st.warning("No canonical role resolved. Matching falls back to skill "
                       "overlap only, which scores less accurately.")
        if detail["source_url"]:
            st.caption(f"Source: {detail['source_url']}")

        # ── Skills, as the matcher sees them ──
        required = [m for m in detail["skill_mentions"] if m["is_required"]]
        nice = [m for m in detail["skill_mentions"] if not m["is_required"]]

        st.subheader(f"Skills extracted ({len(detail['skill_mentions'])})")
        st.caption("These rows are what the matcher scores against — not the raw text.")
        s1, s2 = st.columns(2)
        for column, title, rows in ((s1, "Required", required), (s2, "Nice to have", nice)):
            with column:
                st.markdown(f"**{title} ({len(rows)})**")
                if rows:
                    st.dataframe(
                        [{"Skill": m["skill_name"], "Weight": round(m["weight"], 2)}
                         for m in rows],
                        width="stretch", hide_index=True,
                    )
                else:
                    st.caption("None extracted.")

        # ── Responsibilities ──
        responsibilities = parsed.get("responsibilities") or []
        if responsibilities:
            st.subheader(f"Responsibilities ({len(responsibilities)})")
            for line in responsibilities:
                st.markdown(f"- {line}")

        # ── Latest match ──
        match = detail["match"]
        st.subheader("Latest match result")
        if not match:
            st.info("Not matched yet. Run it from the Match Results page.")
        else:
            m1, m2 = st.columns([1, 3])
            m1.metric("Score", f"{match['score']:.0%}", match["verdict"])
            m2.caption(f"Matched {match['created_at']:%d %b %Y %H:%M}")
            for title, key, help_text in [
                ("✅ Matched", "matched_skills", "You have these and the job wants them."),
                ("❌ Missing", "missing_skills", "The job wants these and you do not have them."),
                ("➕ Surplus", "surplus_skills", "You have these; this job did not ask."),
            ]:
                names = match[key] or []
                with st.expander(f"{title} ({len(names)})"):
                    st.caption(help_text)
                    st.write(", ".join(names) if names else "None.")

        # ── Raw material ──
        with st.expander("Raw parsed JSON"):
            st.json(parsed)
        with st.expander(f"Original text ({len(detail['raw_text']):,} chars)"):
            st.text(detail["raw_text"])

        # ── Delete ──
        st.divider()
        with st.expander("Delete this job description"):
            st.caption("Also deletes its match results and every resume version "
                       "generated for it. The PDFs on disk are left alone.")
            if st.checkbox("I understand", key=f"del_ack_{jd_id}"):
                if st.button("Delete permanently"):
                    with get_session() as session:
                        delete_job_description(session, user_id=user_id, jd_id=jd_id)
                    st.success("Deleted.")
                    st.rerun()
