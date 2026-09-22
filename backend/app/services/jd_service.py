"""Job-description ingestion and best-effort URL extraction."""
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.llm.gemini_client import embed_text, generate_json
from app.models.models import ExtractedSkillMention, JobDescription
from app.services.role_resolution import resolve_role

MIN_VIABLE_LENGTH = 200

EXTRACTION_PROMPT = """
You are extracting structured requirements from a job description. Extract
ONLY what is explicitly stated. Do not infer seniority, years of experience,
or requirements that are not directly written. Normalize skill and tool names
to their common form, but never invent a skill that is not mentioned.

Return ONLY raw JSON (no markdown fences or commentary) matching exactly:
{{
  "company": "",
  "role_title": "",
  "seniority": "",
  "years_experience_min": null,
    "required_skills": [{{"name": "", "weight": 1.0}}],
    "nice_to_have_skills": [{{"name": "", "weight": 0.5}}],
  "responsibilities": [""]
}}

JOB DESCRIPTION:
<<<{jd_text}>>>
"""


def fetch_jd_from_url(url: str) -> str | None:
    """Fetch and extract readable page content; return None for blocked/poor pages."""
    try:
        import httpx
        import trafilatura
    except ImportError:
        return None
    try:
        response = httpx.get(
            url,
            headers={"User-Agent": "CareerOS/1.0"},
            timeout=10,
            follow_redirects=True,
        )
        response.raise_for_status()
        extracted = trafilatura.extract(response.text)
    except (httpx.HTTPError, ValueError):
        return None
    return extracted.strip() if extracted and len(extracted.strip()) >= MIN_VIABLE_LENGTH else None


def ingest_job_description(
    session: Session,
    *,
    user_id: uuid.UUID,
    raw_text: str,
    source_url: str | None = None,
) -> JobDescription:
    """Extract, resolve, embed, and persist one JD and its skill mentions."""
    clean_text = raw_text.strip()
    if not clean_text:
        raise ValueError("Job description text is required")

    parsed = generate_json(EXTRACTION_PROMPT.format(jd_text=clean_text))
    if not isinstance(parsed, dict):
        raise ValueError("Job description extraction did not return a JSON object")

    role_id = resolve_role(session, parsed.get("role_title"))
    row = JobDescription(
        user_id=user_id,
        raw_text=clean_text,
        source_url=source_url or None,
        company=parsed.get("company") or None,
        role_title=parsed.get("role_title") or None,
        role_id=role_id,
        parsed_requirements=parsed,
        embedding=embed_text(clean_text),
    )
    session.add(row)
    session.flush()

    for skill in _skill_entries(parsed.get("required_skills"), default_weight=1.0):
        session.add(ExtractedSkillMention(jd_id=row.id, is_required=True, **skill))
    for skill in _skill_entries(parsed.get("nice_to_have_skills"), default_weight=0.5):
        session.add(ExtractedSkillMention(jd_id=row.id, is_required=False, **skill))

    session.commit()
    return row


def _skill_entries(value: object, *, default_weight: float) -> list[dict]:
    if not isinstance(value, list):
        return []
    entries = []
    for item in value:
        if not isinstance(item, dict) or not str(item.get("name", "")).strip():
            continue
        try:
            weight = float(item.get("weight", default_weight))
        except (TypeError, ValueError):
            weight = default_weight
        entries.append({"skill_name": str(item["name"]).strip(), "weight": max(weight, 0.0)})
    return entries


def list_job_descriptions(session: Session, *, user_id: uuid.UUID) -> list[JobDescription]:
    return list(session.execute(
        select(JobDescription)
        .where(JobDescription.user_id == user_id)
        .order_by(JobDescription.created_at.desc())
    ).scalars().all())