"""Generate a JD-tailored resume, and browse every resume generated before."""
import sys
from pathlib import Path

_backend = str(Path(__file__).resolve().parent.parent.parent / "backend")
if _backend not in sys.path:
    sys.path.insert(0, _backend)

import streamlit as st
from sqlalchemy import text

from app.config import get_settings
from app.db import get_session
from app.llm.gemini_client import GeminiRateLimitError
from app.services.overleaf_service import overleaf_form_html
from app.services.pdf_compiler import LatexCompileError, render_preview_images
from app.services.resume_version_service import (
    DEFAULT_MAX_PAGES,
    generate_and_save,
    get_version,
    list_versions,
    restore_version_pdf,
)

st.set_page_config(page_title="Resume Generator - CareerOS", page_icon="📄", layout="wide")

settings = get_settings()
user_id = settings.default_user_id
if not user_id:
    st.error("DEFAULT_USER_ID is not configured. Bootstrap the default user first.")
    st.stop()

st.title("📄 Resume Generator")


def _safe_stem(label: str) -> str:
    return "".join(ch if ch.isalnum() else "_" for ch in label)[:40].strip("_") or "resume"


def render_build_report(report: dict, *, expanded: bool) -> None:
    """The 'why does the resume look like this' panel."""
    with st.expander("How this resume was built", expanded=expanded):
        c1, c2, c3 = st.columns(3)
        c1.metric("Projects on page", len(report.get("selected_projects", [])))
        c2.metric("Projects considered", report.get("projects_available", "—"))
        c3.metric("Duplicates merged", report.get("duplicates_merged", 0))

        if report.get("selected_projects"):
            st.markdown("**Projects chosen, and why**")
            for p in report["selected_projects"]:
                st.markdown(f"- **{p['title']}** — {p.get('reason') or 'best keyword overlap'}")

        if report.get("trimmed_projects"):
            st.info("Dropped to fit the page limit (lowest-ranked first): "
                    + ", ".join(report["trimmed_projects"]))
        if report.get("fit_failed"):
            st.warning("Still over the page limit after trimming. Raise **Max pages**, "
                       "or shorten some source bullets in your KB.")
        if report.get("long_bullets"):
            st.warning("These bullets wrap onto a second line:")
            for problem in report["long_bullets"]:
                st.caption(f"• {problem}")
        if report.get("overfull_boxes"):
            st.warning(f"Text overflows the right margin on {len(report['overfull_boxes'])} "
                       f"line(s): {report['overfull_boxes']} pt over.")
        if report.get("missing_glyphs"):
            st.error("These characters were dropped from the PDF — the font has no glyph "
                     "for them: " + ", ".join(report["missing_glyphs"])
                     + ". Add a mapping in latex_escape._UNICODE_TABLE.")
        if report.get("missing_dates"):
            st.warning("No dates recorded for: " + ", ".join(report["missing_dates"])
                       + ". The date line is left blank rather than guessed — add them "
                         "on the Experience page.")


def render_resume(*, pdf_path: Path | None, latex_source: str, stem: str,
                  key: str, report: dict | None = None, report_expanded: bool = False) -> None:
    """Preview + downloads + Overleaf. Shared by a fresh build and by any
    stored version, so an old resume is exactly as usable as a new one."""
    if report is not None:
        render_build_report(report, expanded=report_expanded)

    # Rasterized server-side rather than embedded as a PDF: Chromium browsers
    # block <iframe src="data:application/pdf;...">  outright ("This page has
    # been blocked by Microsoft Edge"), and st.pdf needs a component that does
    # not load against this Streamlit version. An <img> always works.
    if pdf_path and pdf_path.exists():
        st.markdown("**Preview**")
        pages = render_preview_images(pdf_path)
        if pages:
            for number, png in enumerate(pages, 1):
                st.image(png, width="stretch")
                if len(pages) > 1:
                    st.caption(f"Page {number} of {len(pages)}")
        else:
            st.info("Install `pypdfium2` to preview here (`pip install pypdfium2`). "
                    "The download below still works.")

    d1, d2, d3 = st.columns(3)
    with d1:
        if pdf_path and pdf_path.exists():
            st.download_button("⬇ Download PDF", pdf_path.read_bytes(),
                               file_name=f"{stem}.pdf", mime="application/pdf",
                               width="stretch", key=f"pdf_{key}")
    with d2:
        st.download_button("⬇ Download .tex", latex_source.encode(),
                           file_name=f"{stem}.tex", mime="text/plain",
                           width="stretch", key=f"tex_{key}")
    with d3:
        # A POST form, not a link: Overleaf takes the source in a form field,
        # and the .tex is self-contained so it compiles there as-is. The HTML
        # is built by overleaf_form_html, which escapes the LaTeX into it -
        # st.iframe runs whatever it is given with JavaScript enabled.
        st.iframe(overleaf_form_html(latex_source, file_name=f"{stem}.tex"), height=60)

    with st.expander("LaTeX source"):
        st.code(latex_source, language="latex")


generate_tab, history_tab = st.tabs(["Generate", "Previous resumes"])

# ══════════════════════════════════════════════════════════════════════════════
# Generate
# ══════════════════════════════════════════════════════════════════════════════
with generate_tab:
    st.caption("Picks the projects that fit this job, rewrites bullets against the "
               "matched skills, and compiles a PDF.")

    with get_session() as session:
        rows = session.execute(
            text("""
                SELECT jd.id, jd.company, jd.role_title, mr.score, mr.verdict
                FROM job_description jd
                LEFT JOIN LATERAL (
                    SELECT score, verdict FROM match_result
                    WHERE jd_id = jd.id AND user_id = :uid
                    ORDER BY created_at DESC LIMIT 1
                ) mr ON true
                WHERE jd.user_id = :uid
                ORDER BY jd.created_at DESC
            """),
            {"uid": user_id},
        ).fetchall()
        jds = [{"id": str(r[0]), "company": r[1], "role_title": r[2],
                "score": r[3], "verdict": r[4]} for r in rows]

    if not jds:
        st.info("No job descriptions yet. Add one on the Job Description page first.")
    else:
        def _label(jd: dict) -> str:
            head = f"{jd['company'] or 'Unknown company'} — {jd['role_title'] or 'Unknown role'}"
            if jd["score"] is None:
                return f"{head}  ·  not matched yet"
            return f"{head}  ·  {jd['verdict'] or '?'} ({jd['score']:.0%})"

        labels = {_label(jd): jd["id"] for jd in jds}
        selected_label = st.selectbox("Job description", list(labels))
        selected_jd_id = labels[selected_label]

        col_a, col_b, col_c = st.columns([2, 1, 1])
        with col_a:
            notes = st.text_input("Note for this version (optional)",
                                  placeholder="e.g. referral via Priya")
        with col_b:
            max_pages = st.number_input("Max pages", 1, 3, DEFAULT_MAX_PAGES)
        with col_c:
            force = st.checkbox(
                "Force regenerate",
                help="Off, an unchanged resume is reused instead of calling Gemini "
                     "again. Turn on only to get a different draft out of the model.",
            )

        if st.button("Generate tailored resume", type="primary"):
            with st.spinner("Rebuilding…" if force else "Checking for a reusable resume…"):
                try:
                    with get_session() as session:
                        result = generate_and_save(
                            session, user_id=user_id, jd_id=selected_jd_id,
                            notes=notes or None, force=force, max_pages=int(max_pages),
                        )
                    st.session_state["resume_result"] = result
                    st.session_state["resume_jd_label"] = selected_label
                except GeminiRateLimitError as exc:
                    st.error(f"Gemini quota reached — wait and try again.\n\n{exc}")
                except LatexCompileError as exc:
                    st.error("LaTeX compilation failed. Tectonic's output is below.")
                    st.code(str(exc), language="text")
                except Exception as exc:  # noqa: BLE001 - surface anything else
                    st.exception(exc)

        result = st.session_state.get("resume_result")
        if result:
            st.divider()
            if result.get("reused"):
                st.success(
                    f"Reused the resume built {result['generated_at']:%d %b %Y %H:%M} — "
                    "nothing in your profile, the match result or the template has "
                    "changed since. Tick **Force regenerate** for a fresh draft."
                )
            else:
                st.success(f"Generated · {result.get('pages')} page(s)")

            render_resume(
                pdf_path=Path(result["pdf_path"]) if result.get("pdf_path") else None,
                latex_source=result["latex_source"],
                stem=_safe_stem(st.session_state.get("resume_jd_label", "resume")),
                key="current",
                report=result.get("report") or {},
                report_expanded=not result.get("reused"),
            )

# ══════════════════════════════════════════════════════════════════════════════
# Previous resumes
# ══════════════════════════════════════════════════════════════════════════════
with history_tab:
    with get_session() as session:
        history = list_versions(session, user_id=user_id, limit=50)

    if not history:
        st.caption("No resumes generated yet.")
    else:
        st.caption(f"{len(history)} stored. Every version keeps its own LaTeX, so one "
                   "can be reopened or rebuilt without calling Gemini again.")
        st.dataframe(
            [
                {
                    "Generated": v["generated_at"].strftime("%d %b %Y %H:%M"),
                    "Company": v["company"],
                    "Role": v["role_title"],
                    "Pages": v["pages"],
                    "Note": v["notes"] or "",
                    "PDF": "on disk" if v["pdf_exists"] else "needs rebuild",
                }
                for v in history
            ],
            width="stretch",
            hide_index=True,
        )

        st.divider()

        def _version_label(v: dict) -> str:
            stamp = v["generated_at"].strftime("%d %b %Y %H:%M")
            note = f" · {v['notes']}" if v["notes"] else ""
            missing = "" if v["pdf_exists"] else "  ⚠ PDF missing"
            return f"{stamp} — {v['company']} / {v['role_title']}{note}{missing}"

        options = {_version_label(v): str(v["id"]) for v in history}
        chosen = st.selectbox("Open a version", list(options))
        version_id = options[chosen]

        with get_session() as session:
            version = get_version(session, user_id=user_id, version_id=version_id)

        if not version:
            st.error("That version no longer exists.")
        else:
            meta = st.columns(3)
            meta[0].metric("Generated", version["generated_at"].strftime("%d %b %Y"))
            meta[1].metric("Pages", version["pages"] or "—")
            meta[2].metric("Target", version["company"] or "—")

            if not version["pdf_exists"]:
                st.warning(
                    "No trustworthy PDF on disk for this version — either the file "
                    "is gone, or it predates per-version filenames and was "
                    "overwritten by a later build. The LaTeX is stored, so it "
                    "rebuilds exactly, with no Gemini call."
                )
                if st.button("Rebuild PDF from stored LaTeX", type="primary"):
                    with st.spinner("Compiling…"):
                        try:
                            with get_session() as session:
                                version = restore_version_pdf(
                                    session, user_id=user_id, version_id=version_id)
                            st.success("Rebuilt.")
                        except LatexCompileError as exc:
                            st.error("Compilation failed.")
                            st.code(str(exc), language="text")

            render_resume(
                pdf_path=version["pdf_path"] if version["pdf_exists"] else None,
                latex_source=version["latex_source"],
                stem=_safe_stem(f"{version['company']}_{version['role_title']}"),
                key=f"v_{version_id}",
                report=version["report"],
                report_expanded=False,
            )
