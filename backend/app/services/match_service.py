"""Explainable two-tier JD matching: trigram first, semantic retrieval second."""
import uuid

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.config import get_settings
from app.llm.gemini_client import embed_text
from app.models.models import JobDescription, MatchResult

STRING_MATCH_THRESHOLD = 0.45
SEMANTIC_MATCH_THRESHOLD = 0.65


def compute_match(session: Session, *, user_id: uuid.UUID, jd_id: uuid.UUID) -> dict:
    jd_id = uuid.UUID(str(jd_id))
    user_id = uuid.UUID(str(user_id))
    jd = session.get(JobDescription, jd_id)
    if jd is None or jd.user_id != user_id:
        raise ValueError(f"Job description {jd_id} not found")

    mentions = session.execute(
        text("""
            SELECT skill_name, weight, is_required
            FROM extracted_skill_mention
            WHERE jd_id = :jd_id
            ORDER BY is_required DESC, skill_name;
        """),
        {"jd_id": jd_id},
    ).fetchall()

    matched, missing = [], []
    for skill_name, _weight, _is_required in mentions:
        if _has_cheap_match(session, user_id, skill_name) or _has_semantic_match(session, user_id, skill_name):
            matched.append(skill_name)
        else:
            missing.append(skill_name)

    jd_skill_names = [row[0] for row in mentions]
    surplus = _find_surplus_skills(session, user_id, jd_skill_names)
    required = [(name, weight) for name, weight, is_required in mentions if is_required]
    optional = [(name, weight) for name, weight, is_required in mentions if not is_required]

    # If Gemini classified everything as nice-to-have, promote them all to
    # required so the score isn't artificially capped at 10%.
    if not required and optional:
        required = optional
        optional = []

    required_total = sum(weight for _, weight in required) or 1.0
    required_matched = sum(weight for name, weight in required if name in matched)
    optional_total = sum(weight for _, weight in optional) or 1.0
    optional_bonus = min(0.10, sum(weight for name, weight in optional if name in matched) / optional_total * 0.10)
    score = min(1.0, required_matched / required_total + optional_bonus)

    settings = get_settings()
    verdict = (
        "strong" if score >= settings.match_strong_threshold
        else "stretch" if score >= settings.match_stretch_threshold
        else "skip"
    )
    result = MatchResult(
        jd_id=jd_id,
        user_id=user_id,
        score=score,
        verdict=verdict,
        matched_skills=matched,
        missing_skills=missing,
        surplus_skills=surplus,
    )
    session.add(result)
    session.commit()
    return {
        "id": result.id,
        "score": score,
        "verdict": verdict,
        "matched": matched,
        "missing": missing,
        "surplus": surplus,
    }


def _has_cheap_match(session: Session, user_id: uuid.UUID, skill_name: str) -> bool:
    return session.execute(
        text("""
            SELECT 1 FROM skill
            WHERE user_id = :user_id
              AND similarity(lower(name), lower(:skill_name)) >= :threshold
            LIMIT 1;
        """),
        {"user_id": user_id, "skill_name": skill_name, "threshold": STRING_MATCH_THRESHOLD},
    ).first() is not None


def _has_semantic_match(session: Session, user_id: uuid.UUID, skill_name: str) -> bool:
    query_vector = embed_text(skill_name)
    hit = session.execute(
        text("""
            SELECT 1 - (embedding <=> :query_vector) AS similarity
            FROM kb_chunk
            WHERE user_id = :user_id
              AND content_type IN ('experience', 'project', 'skill')
            ORDER BY embedding <=> :query_vector
            LIMIT 1;
        """),
        {"query_vector": query_vector, "user_id": user_id},
    ).first()
    return hit is not None and hit[0] is not None and hit[0] >= SEMANTIC_MATCH_THRESHOLD


def _find_surplus_skills(session: Session, user_id: uuid.UUID, jd_skill_names: list[str]) -> list[str]:
    rows = session.execute(
        text("SELECT name FROM skill WHERE user_id = :user_id ORDER BY name;"),
        {"user_id": user_id},
    ).fetchall()
    if not jd_skill_names:
        return [row[0] for row in rows]
    return [
        name for (name,) in rows
        if not any(_similarity(session, name, jd_name) >= STRING_MATCH_THRESHOLD for jd_name in jd_skill_names)
    ]


def _similarity(session: Session, left: str, right: str) -> float:
    value = session.execute(
        text("SELECT similarity(lower(:left), lower(:right));"),
        {"left": left, "right": right},
    ).scalar()
    return float(value or 0.0)


def get_latest_match(session: Session, *, user_id: uuid.UUID, jd_id: uuid.UUID) -> MatchResult | None:
    return session.execute(
        select(MatchResult)
        .where(MatchResult.user_id == user_id, MatchResult.jd_id == jd_id)
        .order_by(MatchResult.created_at.desc())
        .limit(1)
    ).scalars().first()