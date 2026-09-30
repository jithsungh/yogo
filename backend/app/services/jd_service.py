"""Job-description ingestion and best-effort URL extraction."""
import uuid

from sqlalchemy import select, text
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

# ── Read-side helpers for the Job Descriptions UI ─────────────────────────────
# Raw SQL rather than the ORM: these join across job_description, role,
# match_result and resume_version, and the UI wants flat rows, not graphs.

def list_job_description_summaries(session: Session, *, user_id: uuid.UUID) -> list[dict]:
    """One row per stored JD, with its latest match verdict and resume count."""
    rows = session.execute(
        text("""
            SELECT jd.id, jd.company, jd.role_title, jd.source_url, jd.created_at,
                   r.canonical_name        AS canonical_role,
                   mr.score, mr.verdict, mr.created_at AS matched_at,
                   COALESCE(rc.n, 0)       AS resume_count,
                   length(jd.raw_text)     AS raw_length
            FROM job_description jd
            LEFT JOIN role r ON r.id = jd.role_id
            LEFT JOIN LATERAL (
                SELECT score, verdict, created_at FROM match_result
                WHERE jd_id = jd.id AND user_id = :uid
                ORDER BY created_at DESC LIMIT 1
            ) mr ON true
            LEFT JOIN LATERAL (
                SELECT count(*) AS n FROM resume_version
                WHERE jd_id = jd.id AND user_id = :uid
            ) rc ON true
            WHERE jd.user_id = :uid
            ORDER BY jd.created_at DESC
        """),
        {"uid": user_id},
    ).mappings().fetchall()
    return [dict(r) for r in rows]


def get_job_description_detail(session: Session, *, user_id: uuid.UUID,
                               jd_id: uuid.UUID) -> dict | None:
    """Everything stored about one JD: what was parsed out of it, the skill
    mentions that drive matching, the latest match result, and the raw text."""
    row = session.execute(
        text("""
            SELECT jd.id, jd.company, jd.role_title, jd.source_url, jd.created_at,
                   jd.parsed_requirements, jd.raw_text, jd.role_id,
                   r.canonical_name AS canonical_role, r.category AS role_category
            FROM job_description jd
            LEFT JOIN role r ON r.id = jd.role_id
            WHERE jd.id = :id AND jd.user_id = :uid
        """),
        {"id": jd_id, "uid": user_id},
    ).mappings().first()
    if not row:
        return None

    mentions = session.execute(
        text("""
            SELECT skill_name, weight, is_required FROM extracted_skill_mention
            WHERE jd_id = :id
            ORDER BY is_required DESC, weight DESC, skill_name
        """),
        {"id": jd_id},
    ).mappings().fetchall()

    match = session.execute(
        text("""
            SELECT score, verdict, matched_skills, missing_skills, surplus_skills, created_at
            FROM match_result WHERE jd_id = :id AND user_id = :uid
            ORDER BY created_at DESC LIMIT 1
        """),
        {"id": jd_id, "uid": user_id},
    ).mappings().first()

    return {
        **dict(row),
        "parsed_requirements": row["parsed_requirements"] or {},
        "skill_mentions": [dict(m) for m in mentions],
        "match": dict(match) if match else None,
    }


def delete_job_description(session: Session, *, user_id: uuid.UUID, jd_id: uuid.UUID) -> bool:
    """Remove a JD. Skill mentions, match results and resume versions cascade."""
    result = session.execute(
        text("DELETE FROM job_description WHERE id = :id AND user_id = :uid"),
        {"id": jd_id, "uid": user_id},
    )
    session.commit()
    return result.rowcount > 0
