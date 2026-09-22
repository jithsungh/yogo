"""Resolve raw job titles to the local canonical role taxonomy."""
import uuid

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.llm.gemini_client import generate_json

TRIGRAM_CONFIDENCE_THRESHOLD = 0.35


def resolve_role(session: Session, raw_title: str | None) -> uuid.UUID | None:
    """Resolve a title using aliases, fuzzy matching, then constrained LLM classification."""
    normalized = (raw_title or "").strip().lower()
    if not normalized:
        return None

    alias_hit = session.execute(
        text("""
            SELECT role.id
            FROM role
            CROSS JOIN LATERAL unnest(role.aliases) AS alias
            WHERE :title ILIKE '%' || alias || '%'
               OR alias ILIKE '%' || :title || '%'
            ORDER BY length(alias) DESC, role.canonical_name
            LIMIT 1;
        """),
        {"title": normalized},
    ).first()
    if alias_hit:
        return alias_hit[0]

    fuzzy_hit = session.execute(
        text("""
            SELECT id, similarity(lower(canonical_name), :title) AS similarity
            FROM role
            ORDER BY similarity DESC
            LIMIT 1;
        """),
        {"title": normalized},
    ).first()
    if fuzzy_hit and fuzzy_hit[1] >= TRIGRAM_CONFIDENCE_THRESHOLD:
        return fuzzy_hit[0]

    roles = session.execute(
        text("SELECT id, canonical_name, category FROM role ORDER BY canonical_name;")
    ).fetchall()
    if not roles:
        return None

    role_list = "\n".join(
        f"- {role_id}: {canonical_name} (category: {category})"
        for role_id, canonical_name, category in roles
    )
    result = generate_json(f"""
Given this raw job title: {raw_title!r}

Choose a role only when the title clearly matches one role in this list.
Do not infer a match from weak or unrelated similarities. If no role clearly
matches, return null. Return ONLY raw JSON in this exact shape:
{{"role_id": "<uuid or null>"}}

CANONICAL ROLES:
{role_list}
""")
    role_id = result.get("role_id")
    if not role_id:
        return None
    try:
        candidate = uuid.UUID(str(role_id))
    except (TypeError, ValueError) as exc:
        raise ValueError("Role classifier returned an invalid role id") from exc
    return candidate if any(row[0] == candidate for row in roles) else None


def add_role_alias(session: Session, *, role_id: uuid.UUID, raw_title: str) -> None:
    """Add a reviewed raw title as a normalized alias, without duplicates."""
    normalized = raw_title.strip().lower()
    if not normalized:
        return
    session.execute(
        text("""
            UPDATE role
            SET aliases = CASE
                WHEN :alias = ANY(aliases) THEN aliases
                ELSE array_append(aliases, :alias)
            END
            WHERE id = :role_id;
        """),
        {"alias": normalized, "role_id": role_id},
    )
    session.commit()


def list_roles(session: Session) -> list[tuple[uuid.UUID, str, str]]:
    return [tuple(row) for row in session.execute(
        text("SELECT id, canonical_name, category FROM role ORDER BY canonical_name;")
    ).fetchall()]


def assign_role(
    session: Session,
    *,
    user_id: uuid.UUID,
    jd_id: uuid.UUID,
    role_id: uuid.UUID,
    raw_title: str | None = None,
) -> None:
    session.execute(
        text("""
            UPDATE job_description
            SET role_id = :role_id
            WHERE id = :jd_id AND user_id = :user_id;
        """),
        {"role_id": role_id, "jd_id": jd_id, "user_id": user_id},
    )
    if raw_title:
        session.execute(
            text("""
                UPDATE role
                SET aliases = CASE
                    WHEN lower(:raw_title) = ANY(aliases) THEN aliases
                    ELSE array_append(aliases, lower(:raw_title))
                END
                WHERE id = :role_id;
            """),
            {"raw_title": raw_title.strip(), "role_id": role_id},
        )
    session.commit()