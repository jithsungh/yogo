"""Resume import service — PDF/DOCX → Gemini structured extraction → JSON.

PDF: sent directly to Gemini as inline document data (Gemini reads PDFs natively).
DOCX: text extracted locally via python-docx, then sent as plain text.
Both paths converge to the same JSON schema via generate_json().
"""
import re
import uuid
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.llm.gemini_client import GeminiRateLimitError, generate_json

EXTRACTION_PROMPT = """You are a meticulous resume parser. Your job is to extract EVERY piece of
structured data from the resume below with maximum fidelity. Extract ONLY
what is explicitly stated — do NOT infer, estimate, or invent details.

IMPORTANT RULES:
- Read the ENTIRE resume carefully. Resumes use many layouts: columns, tables,
  headers, sidebars. Do NOT skip any section.
- For DATES: Use "YYYY-MM-DD" format. If only month+year is given (e.g.
  "Jan 2022" or "01/2022"), use "YYYY-MM-01" (first of that month). If only
  a year is given (e.g. "2022"), use "YYYY-01-01". If the end date says
  "Present", "Current", "Ongoing", or similar, use the string "present".
- For DESCRIPTIONS: Capture ALL bullet points, responsibilities, and
  achievements verbatim. Join multiple bullets with newline characters (\\n).
  Do NOT summarize or truncate — include every bullet point exactly as written.
- For TECH STACK: Extract individual technology names as separate array items.
  Parse them from descriptions, bullet points, and dedicated "Technologies"
  or "Tech Stack" lines.
- If a field has no data in the resume, OMIT that field entirely from the
  output rather than using empty strings or null.

Return ONLY raw JSON (no markdown fences, no commentary) matching this schema:

{
  "basic_info": {
    "full_name": "Full name of the candidate",
    "email": "Email address",
    "phone": "Phone number (preserve original formatting)",
    "location": "City, State/Country as written",
    "summary": "Professional summary/objective paragraph if present at top of resume",
    "links": {
      "github": "GitHub profile URL",
      "linkedin": "LinkedIn profile URL",
      "portfolio": "Personal website/portfolio URL",
      "leetcode": "LeetCode profile URL",
      "twitter": "Twitter/X profile URL",
      "other": "Any other profile URLs as comma-separated string"
    }
  },
  "education": [
    {
      "degree": "Full degree name (e.g. 'Bachelor of Technology in Computer Science')",
      "institution": "University/college name",
      "start_date": "YYYY-MM-DD (see date rules above)",
      "end_date": "YYYY-MM-DD or 'present'",
      "score": "GPA, CGPA, percentage, or class as written (e.g. '3.8/4.0', '8.5/10', '85%', 'First Class Honours')",
      "highlights": "Relevant coursework, honors, thesis title, dean's list, scholarships — join with newlines"
    }
  ],
  "work_experience": [
    {
      "company": "Company/organization name",
      "role_title": "Exact job title (e.g. 'Software Engineer Intern', 'Senior Backend Developer')",
      "start_date": "YYYY-MM-DD",
      "end_date": "YYYY-MM-DD or 'present'",
      "description": "ALL bullet points and responsibilities, joined with newlines (\\n). Capture EVERY bullet — do NOT skip or summarize any.",
      "tech_stack": ["Individual", "technology", "names", "extracted", "from", "the", "entry"]
    }
  ],
  "projects": [
    {
      "title": "Project name/title",
      "summary": "One sentence: what the project is and does",
      "start_date": "YYYY-MM-DD if a date or duration is given",
      "end_date": "YYYY-MM-DD or 'present'",
      "key_points": ["Each bullet point / achievement as its own item, verbatim"],
      "description": "Full project description — ALL bullet points joined with newlines (\\n)",
      "tech_stack": ["Individual", "technology", "names"],
      "github_url": "GitHub/source code URL if present",
      "live_url": "Deployed/live/demo URL if present",
      "highlights": "Key achievements, metrics, or impact statements from the project"
    }
  ],
  "skills": [
    {
      "name": "Individual skill name (one per entry, e.g. 'Python' not 'Python, Java')",
      "category": "One of: language|framework|cloud|devops|ml|database|tool|soft_skill"
    }
  ],
  "certifications": [
    {
      "title": "Certification name",
      "issuer": "Issuing organization (e.g. 'AWS', 'Google', 'Coursera')",
      "date": "YYYY-MM-DD when issued or earned",
      "url": "Credential verification URL if present"
    }
  ]
}

CATEGORY RULES FOR SKILLS:
- language: Programming/scripting languages (Python, Java, C++, JavaScript, SQL, etc.)
- framework: Libraries and frameworks (React, Django, Spring Boot, TensorFlow, etc.)
- cloud: Cloud platforms and services (AWS, GCP, Azure, S3, EC2, Lambda, etc.)
- devops: DevOps and infrastructure tools (Docker, Kubernetes, Jenkins, Terraform, CI/CD, etc.)
- ml: Machine learning and AI specific (PyTorch, scikit-learn, NLP, Computer Vision, etc.)
- database: Databases and data stores (PostgreSQL, MongoDB, Redis, Elasticsearch, etc.)
- tool: Developer tools, IDEs, version control (Git, VS Code, Jira, Postman, etc.)
- soft_skill: Non-technical skills (Leadership, Agile, Communication, etc.)
"""


def extract_from_pdf_bytes(pdf_bytes: bytes) -> dict:
    """Send raw PDF bytes to Gemini for structured extraction. Goes through
    the shared wrapper, so quota errors and transient failures are handled
    like every other call (it used to build its own client and bypass both)."""
    prompt = EXTRACTION_PROMPT + "\n\nRESUME CONTENT:\n(attached as PDF)"
    return normalize_extracted(generate_json(prompt, attachments=[(pdf_bytes, "application/pdf")]))


def extract_from_docx_bytes(docx_bytes: bytes) -> dict:
    """Extract text from DOCX using python-docx, then send to Gemini."""
    import io
    from docx import Document

    doc = Document(io.BytesIO(docx_bytes))
    text_parts = []
    for para in doc.paragraphs:
        if para.text.strip():
            text_parts.append(para.text.strip())
    # Also get text from tables (resumes often use tables for layout)
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                if cell.text.strip():
                    text_parts.append(cell.text.strip())

    resume_text = "\n".join(text_parts)
    prompt = EXTRACTION_PROMPT + f"\n\nRESUME CONTENT:\n{resume_text}"
    return normalize_extracted(generate_json(prompt))


def extract_resume(file_bytes: bytes, filename: str) -> dict:
    """Route to PDF or DOCX extraction based on filename extension."""
    lower = filename.lower()
    if lower.endswith(".pdf"):
        return extract_from_pdf_bytes(file_bytes)
    elif lower.endswith(".docx"):
        return extract_from_docx_bytes(file_bytes)
    else:
        raise ValueError(f"Unsupported file type: {filename}. Only .pdf and .docx are supported.")


# ═══════════════════════════════════════════════════════════════════════════
# Normalization - dates were silently lost on import
# ═══════════════════════════════════════════════════════════════════════════

_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
_SEASONS = {"spring": 3, "summer": 6, "fall": 9, "autumn": 9, "winter": 12}
_PRESENT = {"present", "current", "ongoing", "now", "till date", "to date", "today"}


def parse_date(value) -> date | None:
    """Accept what models and humans actually write: 2022, "2022", "2022-06",
    "2022-06-01", "Jun 2022", "June, 2022", "Sept. 2022", "06/2022", "Fall 2022".
    "present" and friends return None (an open-ended range)."""
    if value is None or value == "":
        return None
    if isinstance(value, date):
        return value
    if isinstance(value, (int, float)) and 1950 <= int(value) <= 2100:
        return date(int(value), 1, 1)
    v = str(value).strip().lower().rstrip(".")
    if v in _PRESENT:
        return None
    if m := re.fullmatch(r"(\d{4})-(\d{1,2})(?:-(\d{1,2}))?(?:t.*)?", v):
        return date(int(m[1]), int(m[2]), int(m[3] or 1))
    if m := re.fullmatch(r"(\d{1,2})[/\-.](\d{4})", v):
        return date(int(m[2]), int(m[1]), 1)
    if m := re.fullmatch(r"([a-z]+)\.?,?\s+(\d{4})", v):
        word = m[1]
        if word in _SEASONS:
            return date(int(m[2]), _SEASONS[word], 1)
        if word[:3] in _MONTHS:
            return date(int(m[2]), _MONTHS[word[:3]], 1)
    if m := re.fullmatch(r"(\d{4})", v):
        return date(int(m[1]), 1, 1)
    return None


def _split_range(value) -> tuple:
    """'2022-2026' or 'Jan 2022 - Present' in a single field."""
    if isinstance(value, str) and (m := re.fullmatch(r"\s*(.+?)\s*(?:-|–|—|to)\s*(.+?)\s*", value)) \
            and parse_date(m[1]) is not None:
        return m[1], m[2]
    return value, None


def normalize_extracted(data: dict) -> dict:
    """Clean an extraction result in place: real dates, lists as lists."""
    if not isinstance(data, dict):
        raise ValueError("Extraction did not return a JSON object.")
    for section in ("education", "work_experience", "projects"):
        for row in data.get(section) or []:
            if not isinstance(row, dict):
                continue
            start, end = row.get("start_date"), row.get("end_date")
            if end is None and isinstance(start, str):
                start, end = _split_range(start)
            row["start_date"], row["end_date"] = parse_date(start), parse_date(end)
            # A single year on an education line is the completion year.
            if section == "education" and row["start_date"] and not row["end_date"] \
                    and str(start).strip().isdigit():
                row["end_date"], row["start_date"] = row["start_date"], None
    for row in data.get("certifications") or []:
        if isinstance(row, dict):
            row["date"] = parse_date(row.get("date"))
    for row in data.get("projects") or []:
        if isinstance(row, dict) and isinstance(row.get("key_points"), str):
            row["key_points"] = [k for k in row["key_points"].split("\n") if k.strip()]
    return data


# ═══════════════════════════════════════════════════════════════════════════
# Saving - upsert on natural keys, so a re-import fills gaps, not duplicates
# ═══════════════════════════════════════════════════════════════════════════

def _key(*parts) -> str:
    return "|".join(re.sub(r"[^a-z0-9]", "", (p or "").lower()) for p in parts)


def _fill_blanks(existing, incoming: dict) -> dict:
    """Only the fields the stored row is missing - never overwrite what the
    user has already written or edited."""
    patch = {}
    for field, value in incoming.items():
        if value in (None, "", [], {}):
            continue
        current = getattr(existing, field, None)
        if current in (None, "", []):
            patch[field] = value
    return patch


def save_import(session: Session, *, user_id, data: dict,
                sections: set[str] | None = None) -> dict:
    """Write an (edited) extraction into the KB.

    Each row is matched to an existing one first - education by
    (institution, degree), experience by (company, role), projects by repo
    URL then title, certifications by title, skills by name - and only blank
    fields are filled in. Re-importing an updated resume, or double-clicking
    Save, no longer duplicates anything.

    Returns {"created": {section: n}, "updated": {...}, "skipped": {...},
    "errors": [str], "stopped": str | None}. A Gemini quota error stops the
    run (every later row would fail the same way) and is reported, rather
    than being swallowed while the page says "Saved!".
    """
    from app.models.models import Certification, Education, Project, Skill, WorkExperience
    from app.services import kb_service as kb
    from app.services.profile_service import get_profile, upsert_profile

    sections = sections or {"basic_info", "education", "work_experience", "projects",
                            "skills", "certifications"}
    report = {"created": {}, "updated": {}, "skipped": {}, "errors": [], "stopped": None}
    uid = uuid.UUID(str(user_id))

    def bump(kind, section):
        report[kind][section] = report[kind].get(section, 0) + 1

    def upsert(section, kind, model, key_fn, rows, prepare):
        existing = {key_fn(r): r for r in session.execute(
            select(model).where(model.user_id == uid)).scalars().all()}
        for raw in rows or []:
            if not isinstance(raw, dict):
                continue
            try:
                values = prepare(raw)
                if not values:
                    bump("skipped", section)
                    continue
                match = existing.get(key_fn(type("R", (), values)))
                if match is None and kind == "project":
                    rk = kb.repo_key(values.get("github_url")) or kb.repo_key(values.get("live_url"))
                    match = next((r for r in existing.values() if rk and r.repo_key == rk), None)
                if match is not None:
                    patch = _fill_blanks(match, values)
                    if patch:
                        kb.update_item(session, kind, user_id=uid, item_id=match.id, patch=patch)
                        bump("updated", section)
                    else:
                        bump("skipped", section)
                else:
                    item, _ = kb.create_item(session, kind, user_id=uid, data=values)
                    existing[key_fn(type("R", (), values))] = session.get(model, item.id)
                    bump("created", section)
            except GeminiRateLimitError as exc:
                raise
            except Exception as exc:  # noqa: BLE001 - reported per row
                session.rollback()
                report["errors"].append(f"{section}: {raw.get('title') or raw.get('name') or raw.get('company') or raw.get('degree') or '?'} - {exc}")

    def clean(d: dict, fields: tuple) -> dict:
        return {f: d.get(f) for f in fields if d.get(f) not in (None, "", [])}

    try:
        if "basic_info" in sections and isinstance(data.get("basic_info"), dict):
            bi = data["basic_info"]
            current = get_profile(session, user_id=uid)
            merged = current.model_dump() if current else {}
            for field in ("full_name", "email", "phone", "location", "summary"):
                if bi.get(field) and not merged.get(field):
                    merged[field] = bi[field]
            links = dict(merged.get("links") or {})
            for k, v in (bi.get("links") or {}).items():
                if v and k != "other" and k not in links:
                    links[k] = v
            merged["links"] = links
            if merged.get("full_name"):
                upsert_profile(session, user_id=uid, **{k: merged.get(k) for k in
                               ("full_name", "email", "phone", "location", "summary", "links")})
                bump("updated" if current else "created", "basic_info")

        if "education" in sections:
            upsert("education", "education", Education,
                   lambda r: _key(getattr(r, "institution", ""), getattr(r, "degree", "")),
                   data.get("education"),
                   lambda d: clean(d, ("degree", "institution", "start_date", "end_date", "score", "highlights"))
                   if d.get("degree") and d.get("institution") else None)
        if "work_experience" in sections:
            upsert("work_experience", "experience", WorkExperience,
                   lambda r: _key(getattr(r, "company", ""), getattr(r, "role_title", "")),
                   data.get("work_experience"),
                   lambda d: clean(d, ("company", "role_title", "start_date", "end_date", "description", "tech_stack"))
                   if d.get("company") and d.get("role_title") else None)
        if "projects" in sections:
            upsert("projects", "project", Project,
                   lambda r: _key(getattr(r, "title", "")),
                   data.get("projects"),
                   lambda d: {**clean(d, ("title", "summary", "description", "tech_stack", "github_url",
                                          "live_url", "key_points", "start_date", "end_date")),
                              "description": d.get("description") or ""}
                   if d.get("title") else None)
        if "certifications" in sections:
            upsert("certifications", "certification", Certification,
                   lambda r: _key(getattr(r, "title", "")),
                   data.get("certifications"),
                   lambda d: clean(d, ("title", "issuer", "date", "url")) if d.get("title") else None)
        if "skills" in sections:
            have = {n.lower() for (n,) in session.execute(
                select(func.lower(Skill.name)).where(Skill.user_id == uid)).all()}
            for s in data.get("skills") or []:
                name = (s or {}).get("name", "").strip() if isinstance(s, dict) else ""
                if not name:
                    continue
                if name.lower() in have:
                    bump("skipped", "skills")
                    continue
                category = s.get("category") if s.get("category") in (
                    "language", "framework", "cloud", "devops", "ml", "database", "tool", "soft_skill") else "tool"
                try:
                    kb.create_item(session, "skill", user_id=uid, data={"name": name, "category": category})
                    have.add(name.lower())
                    bump("created", "skills")
                except GeminiRateLimitError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    session.rollback()
                    report["errors"].append(f"skills: {name} - {exc}")
    except GeminiRateLimitError as exc:
        report["stopped"] = str(exc)
    return report
