"""Job-description ingestion, editing, and best-effort URL extraction."""
import hashlib
import re
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
to their common form ("ReactJS" -> "React", "Postgres" -> "PostgreSQL"), but
never invent a skill that is not mentioned.

REQUIRED vs NICE-TO-HAVE follows the posting's own section headers
("Requirements", "Must have", "Minimum qualifications" -> required;
"Preferred", "Nice to have", "Bonus", "Plus" -> nice_to_have).

WEIGHT - how central the skill is to THIS role. Use the full range:
  1.0  in the job title, the core of the main responsibility, or repeated
  0.8  listed under the required / minimum qualifications
  0.5  preferred / nice-to-have / bonus
  0.3  mentioned only in passing ("exposure to", "familiarity with", "etc.")
When a posting lists alternatives ("React or Vue", "Java, Go, or Python"),
emit ONE entry named with the alternatives joined by " / " (e.g. "React / Vue")
instead of one entry per option - the candidate needs one of them, not all.

Return ONLY raw JSON (no markdown fences or commentary) matching exactly:
{{
  "company": "",
  "role_title": "",
  "seniority": "",
  "years_experience_min": null,
  "required_skills": [{{"name": "Go", "weight": 1.0}}, {{"name": "PostgreSQL", "weight": 0.8}}],
  "nice_to_have_skills": [{{"name": "Kubernetes", "weight": 0.5}}],
  "responsibilities": [""]
}}

JOB DESCRIPTION:
<<<{jd_text}>>>
"""

_BOILERPLATE = re.compile(
    r"^(company logo for.*|about the job|show more|show less|.*people clicked apply.*|"
    r"your profile and resume match.*|apply|save|easy apply|promoted|actively recruiting)$",
    re.IGNORECASE,
)


def jd_content_hash(raw: str) -> str:
    """Identity of a JD's text, insensitive to whitespace, case and job-board
    chrome. Kept in step with migration a8d4e7b2c915."""
    lines = [l.strip() for l in (raw or "").splitlines()]
    kept = [l for l in lines if l and not _BOILERPLATE.match(l)]
    norm = re.sub(r"\s+", " ", " ".join(kept).lower()).strip()
    return hashlib.sha256(norm.encode()).hexdigest()


class DuplicateJobDescription(ValueError):
    """The same JD text is already stored. Carries the existing row's id."""

    def __init__(self, existing_id, company, role_title):
        self.existing_id = existing_id
        super().__init__(f"This job description is already saved ({company or '?'} - "
                         f"{role_title or '?'}). Open it from the Stored tab.")


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
    allow_duplicate: bool = False,
) -> JobDescription:
    """Extract, resolve, embed, and persist one JD and its skill mentions."""
    clean_text = raw_text.strip()
    if not clean_text:
        raise ValueError("Job description text is required")

    # Checked BEFORE the extraction call: a re-paste costs nothing now.
    digest = jd_content_hash(clean_text)
    if not allow_duplicate:
        dup = session.execute(
            select(JobDescription).where(JobDescription.user_id == user_id,
                                         JobDescription.content_hash == digest)
        ).scalars().first()
        if dup:
            raise DuplicateJobDescription(dup.id, dup.company, dup.role_title)

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
        content_hash=digest,
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
        # Clamp: the model occasionally returns 2 or 10 on a 0-1 scale.
        entries.append({"skill_name": str(item["name"]).strip(), "weight": min(max(weight, 0.1), 1.0)})
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
                   length(jd.raw_text)     AS raw_length,
                   (SELECT count(*) FROM job_description d2
                     WHERE d2.user_id = jd.user_id AND d2.content_hash = jd.content_hash
                       AND d2.id <> jd.id)  AS duplicate_count
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


# ── Editing ──────────────────────────────────────────────────────────────────

def update_job_description(session: Session, *, user_id, jd_id, company: str | None = None,
                           role_title: str | None = None, role_id=None,
                           source_url: str | None = None) -> None:
    """Correct the header fields. Changing role_id teaches the role resolver
    this title (it is added as an alias), so the next similar JD resolves
    right on its own."""
    from app.services.role_resolution import add_role_alias

    jd = session.get(JobDescription, uuid.UUID(str(jd_id)))
    if jd is None or str(jd.user_id) != str(user_id):
        raise ValueError("Job description not found.")
    if company is not None:
        jd.company = company.strip() or None
    if role_title is not None:
        jd.role_title = role_title.strip() or None
    if source_url is not None:
        jd.source_url = source_url.strip() or None
    if role_id is not None and str(role_id) != str(jd.role_id):
        jd.role_id = uuid.UUID(str(role_id))
        if jd.role_title:
            add_role_alias(session, role_id=jd.role_id, raw_title=jd.role_title)
    session.commit()


def set_skill_mentions(session: Session, *, user_id, jd_id, mentions: list[dict]) -> int:
    """Replace a JD's extracted skills with the user's edited list:
    [{"skill_name", "weight", "is_required"}]. The match result becomes stale
    automatically (its inputs hash changes). Returns the number saved."""
    jd = session.get(JobDescription, uuid.UUID(str(jd_id)))
    if jd is None or str(jd.user_id) != str(user_id):
        raise ValueError("Job description not found.")
    clean, seen = [], set()
    for m in mentions:
        name = re.sub(r"\s+", " ", str(m.get("skill_name") or "")).strip()
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        try:
            weight = min(max(float(m.get("weight") if m.get("weight") is not None else 1.0), 0.1), 1.0)
        except (TypeError, ValueError):
            weight = 1.0
        clean.append(ExtractedSkillMention(jd_id=jd.id, skill_name=name, weight=weight,
                                           is_required=bool(m.get("is_required", True))))
    session.execute(text("DELETE FROM extracted_skill_mention WHERE jd_id = :id"), {"id": jd.id})
    session.add_all(clean)
    # Keep parsed_requirements consistent with what matching now uses.
    reqs = dict(jd.parsed_requirements or {})
    reqs["required_skills"] = [{"name": c.skill_name, "weight": c.weight} for c in clean if c.is_required]
    reqs["nice_to_have_skills"] = [{"name": c.skill_name, "weight": c.weight} for c in clean if not c.is_required]
    jd.parsed_requirements = reqs
    session.commit()
    return len(clean)


def reextract_job_description(session: Session, *, user_id, jd_id) -> JobDescription:
    """Run extraction again IN PLACE (keeps the id, so match results and
    resumes stay attached). One Gemini call."""
    jd = session.get(JobDescription, uuid.UUID(str(jd_id)))
    if jd is None or str(jd.user_id) != str(user_id):
        raise ValueError("Job description not found.")
    parsed = generate_json(EXTRACTION_PROMPT.format(jd_text=jd.raw_text))
    if not isinstance(parsed, dict):
        raise ValueError("Extraction did not return a JSON object")
    jd.parsed_requirements = parsed
    jd.company = parsed.get("company") or jd.company
    jd.role_title = parsed.get("role_title") or jd.role_title
    session.execute(text("DELETE FROM extracted_skill_mention WHERE jd_id = :id"), {"id": jd.id})
    for skill in _skill_entries(parsed.get("required_skills"), default_weight=1.0):
        session.add(ExtractedSkillMention(jd_id=jd.id, is_required=True, **skill))
    for skill in _skill_entries(parsed.get("nice_to_have_skills"), default_weight=0.5):
        session.add(ExtractedSkillMention(jd_id=jd.id, is_required=False, **skill))
    session.commit()
    return jd
