"""Generate, fit, cache and persist tailored resumes.

Two things happen here that are not in the tailor or the compiler:

REUSE. Generating a resume costs two Gemini calls and a Tectonic run. Almost
every "regenerate" click happens with nothing about the inputs changed - same
KB, same match result, same JD, same template. So every resume is stored with
a fingerprint of all of those, and an identical fingerprint returns the stored
PDF instead of rebuilding it. Editing the template or bumping the prompt
version changes the fingerprint, so a real change is never masked by the cache.

PAGE FITTING. The model is asked for a fixed number of projects, but how many
actually fit depends on how long the bullets came out. Rather than guess, the
resume is compiled and the page count read back; if it overflows, the
lowest-ranked project is dropped and it is compiled again. Projects arrive
rank-ordered, so the thing dropped is always the least relevant to this JD.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.services.pdf_compiler import CompileResult, compile_to_pdf
from app.services.resume_renderer import render_latex, template_fingerprint
from app.services.resume_tailor import (
    MAX_PROJECTS,
    TAILOR_VERSION,
    build_tailored_resume_data,
    collect_source_data,
)

# How many pages the resume may occupy. One, to match the hand-tuned
# base_resume.tex this format was derived from; raise it for a longer CV.
DEFAULT_MAX_PAGES = 1
# Each retry costs one more Tectonic run (~2s) and no Gemini call, because
# trimming only slices the already-generated, rank-ordered project list.
MAX_FIT_ATTEMPTS = 4


def version_pdf_path(version_id, stored_path: str | None) -> Path | None:
    """The PDF for this version, or None if there isn't a trustworthy one.

    A stored path only counts when the file exists AND is named for this
    version. Rows written before filenames were per-version all point at a
    single "<user>_<jd>.pdf" that later builds overwrote, so that file holds
    some other version's content. Returning it would silently show the wrong
    resume, which is worse than showing none - None sends the UI down the
    "rebuild from stored LaTeX" path instead, which reproduces the real one.
    """
    if not stored_path:
        return None
    path = Path(stored_path)
    if path.name != f"resume_{version_id}.pdf":
        return None
    return path if path.exists() else None


def compute_content_hash(source: dict) -> str:
    """Fingerprint every input the resume derives from.

    Includes the template and class files and the prompt version, so a layout
    fix or a prompt change invalidates the cache - otherwise the improvement
    would silently not reach any JD already generated.
    """
    payload = {
        "tailor_version": TAILOR_VERSION,
        "template": template_fingerprint(),
        "source": source,
    }
    blob = json.dumps(payload, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def find_reusable_version(
    session: Session, *, user_id: uuid.UUID, jd_id: uuid.UUID, content_hash: str,
) -> dict | None:
    """The newest stored resume for this JD whose inputs are unchanged AND
    whose PDF is still on disk. A row pointing at a deleted file is not a
    cache hit - the user would get a download button for nothing."""
    row = session.execute(
        text("""
            SELECT id, latex_source, pdf_path, pages, build_report, generated_at, notes
            FROM resume_version
            WHERE user_id = :uid AND jd_id = :jd AND content_hash = :hash
            ORDER BY generated_at DESC
            LIMIT 1
        """),
        {"uid": user_id, "jd": jd_id, "hash": content_hash},
    ).mappings().first()
    if not row:
        return None

    pdf_path = version_pdf_path(row["id"], row["pdf_path"])
    if not pdf_path:
        return None

    return {
        "version_id": row["id"],
        "pdf_path": pdf_path,
        "tex_path": pdf_path.with_suffix(".tex"),
        "latex_source": row["latex_source"],
        "pages": row["pages"],
        "report": row["build_report"] or {},
        "generated_at": row["generated_at"],
        "notes": row["notes"],
        "reused": True,
    }


def generate_and_save(
    session: Session,
    *,
    user_id: uuid.UUID,
    jd_id: uuid.UUID,
    notes: str | None = None,
    force: bool = False,
    max_pages: int = DEFAULT_MAX_PAGES,
) -> dict:
    """Full pipeline: fingerprint -> reuse or (tailor -> render -> fit -> compile) -> save.

    Pass force=True to rebuild even when the inputs are unchanged - the only
    reason to do that is to get a different draft out of the model.
    """
    source = collect_source_data(session, user_id=user_id, jd_id=jd_id)
    content_hash = compute_content_hash(source)

    if not force:
        cached = find_reusable_version(
            session, user_id=user_id, jd_id=jd_id, content_hash=content_hash
        )
        if cached:
            return cached

    data, report = build_tailored_resume_data(
        session, user_id=user_id, jd_id=jd_id, source=source, max_projects=MAX_PROJECTS
    )

    # The id is minted before compiling so it can name the output files. A
    # stem of "<user>_<jd>" made every regeneration for the same JD overwrite
    # the same PDF, so every stored version pointed at the newest one and the
    # history was a lie.
    version_id = uuid.uuid4()
    latex_source, compiled, report = _compile_to_fit(
        data, report, stem=f"resume_{version_id}", max_pages=max_pages
    )

    report["pages"] = compiled.pages
    report["overfull_boxes"] = [round(pt, 1) for pt in compiled.overfull if pt >= 1.0]
    report["missing_glyphs"] = compiled.missing_glyphs

    session.execute(
        text("""
            INSERT INTO resume_version
                (id, user_id, jd_id, latex_source, pdf_path, notes, content_hash, pages, build_report)
            VALUES
                (:id, :user_id, :jd_id, :latex_source, :pdf_path, :notes, :hash, :pages,
                 CAST(:report AS JSONB))
        """),
        {
            "id": version_id, "user_id": user_id, "jd_id": jd_id,
            "latex_source": latex_source, "pdf_path": str(compiled.pdf_path),
            "notes": notes, "hash": content_hash, "pages": compiled.pages,
            "report": json.dumps(report, default=str),
        },
    )
    session.commit()

    return {
        "version_id": version_id,
        "pdf_path": compiled.pdf_path,
        "tex_path": compiled.tex_path,
        "latex_source": latex_source,
        "pages": compiled.pages,
        "report": report,
        "reused": False,
    }


def _compile_to_fit(
    data: dict, report: dict, *, stem: str, max_pages: int,
) -> tuple[str, CompileResult, dict]:
    """Compile, and if it runs long, drop the lowest-ranked project and retry.

    Trimming never re-runs the model: `data["projects"]` is already ordered
    best-first, so slicing the tail removes exactly the content least relevant
    to this JD.
    """
    trimmed: list[str] = []

    for _ in range(MAX_FIT_ATTEMPTS):
        latex_source = render_latex(data)
        compiled = compile_to_pdf(latex_source, output_stem=stem)

        if compiled.pages <= max_pages or len(data["projects"]) <= 1:
            report["trimmed_projects"] = trimmed
            report["fit_attempts"] = len(trimmed) + 1
            return latex_source, compiled, report

        dropped = data["projects"][-1]
        trimmed.append(dropped["title"])
        data["projects"] = data["projects"][:-1]

    # Out of attempts. Ship the most-trimmed version - it is the closest to
    # fitting - rather than failing outright. An overlong resume is still
    # usable, and the report says what happened.
    latex_source = render_latex(data)
    compiled = compile_to_pdf(latex_source, output_stem=stem)
    report["trimmed_projects"] = trimmed
    report["fit_attempts"] = MAX_FIT_ATTEMPTS
    report["fit_failed"] = True
    return latex_source, compiled, report


def get_version(session: Session, *, user_id: uuid.UUID, version_id: uuid.UUID) -> dict | None:
    """One stored resume, with everything the UI needs to show it again."""
    row = session.execute(
        text("""
            SELECT rv.id, rv.jd_id, rv.latex_source, rv.pdf_path, rv.pages,
                   rv.notes, rv.build_report, rv.generated_at,
                   jd.company, jd.role_title
            FROM resume_version rv
            JOIN job_description jd ON jd.id = rv.jd_id
            WHERE rv.id = :id AND rv.user_id = :uid
        """),
        {"id": version_id, "uid": user_id},
    ).mappings().first()
    if not row:
        return None

    pdf_path = version_pdf_path(row["id"], row["pdf_path"])
    return {
        "version_id": row["id"],
        "jd_id": row["jd_id"],
        "latex_source": row["latex_source"],
        "pdf_path": pdf_path,
        "pdf_exists": pdf_path is not None,
        "pages": row["pages"],
        "notes": row["notes"],
        "report": row["build_report"] or {},
        "generated_at": row["generated_at"],
        "company": row["company"],
        "role_title": row["role_title"],
    }


def restore_version_pdf(session: Session, *, user_id: uuid.UUID,
                        version_id: uuid.UUID) -> dict:
    """Recompile a stored version from its saved LaTeX.

    The .tex is kept in the database, so a deleted or overwritten PDF can be
    rebuilt exactly - no Gemini call, because the tailoring decisions are
    already baked into the stored source. This is also the repair path for
    rows written before filenames were per-version, which all point at one
    file.
    """
    version = get_version(session, user_id=user_id, version_id=version_id)
    if not version:
        raise ValueError(f"No resume version {version_id} for this user")

    compiled = compile_to_pdf(version["latex_source"], output_stem=f"resume_{version_id}")
    session.execute(
        text("UPDATE resume_version SET pdf_path = :path, pages = :pages WHERE id = :id"),
        {"path": str(compiled.pdf_path), "pages": compiled.pages, "id": version_id},
    )
    session.commit()

    version.update(pdf_path=compiled.pdf_path, pdf_exists=True, pages=compiled.pages)
    return version


def list_versions(session: Session, *, user_id: uuid.UUID, jd_id: uuid.UUID | None = None,
                  limit: int = 20) -> list[dict]:
    """Recent resumes, for the history panel in the UI."""
    clause = "AND jd_id = :jd" if jd_id else ""
    rows = session.execute(
        text(f"""
            SELECT rv.id, rv.jd_id, rv.pdf_path, rv.pages, rv.notes, rv.generated_at,
                   rv.content_hash, jd.company, jd.role_title
            FROM resume_version rv
            JOIN job_description jd ON jd.id = rv.jd_id
            WHERE rv.user_id = :uid {clause}
            ORDER BY rv.generated_at DESC
            LIMIT :limit
        """),
        {"uid": user_id, "jd": jd_id, "limit": limit},
    ).mappings().fetchall()
    return [{**dict(r), "pdf_exists": version_pdf_path(r["id"], r["pdf_path"]) is not None}
            for r in rows]
