"""Resolve raw job titles to the local canonical role taxonomy."""
import re
import uuid

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.llm.gemini_client import generate_json

TRIGRAM_CONFIDENCE_THRESHOLD = 0.35
MIN_CONTAINED_ALIAS = 4

# Seniority / level words carry no role information and stop exact matches:
# "Software Development Engineer I (SDE-1)" should resolve like
# "Software Development Engineer".
_LEVEL_TOKENS = re.compile(
    r"\b(junior|jr|senior|sr|associate|lead|staff|principal|intern|internship|trainee|"
    r"graduate|new grad|entry[- ]level|i{1,3}|iv|[1-4]|sde[- ]?[1-4])\b\.?",
    re.IGNORECASE,
)


def normalize_title(raw_title: str | None) -> str:
    """'Associate Software Engineer — Backend (Remote)' -> 'software engineer'."""
    t = (raw_title or "").lower()
    t = re.sub(r"\([^)]*\)|\[[^\]]*\]", " ", t)          # parentheticals
    t = re.split(r"\s[-–—|/]\s|,", t)[0]                   # qualifiers after a dash / comma
    t = _LEVEL_TOKENS.sub(" ", t)
    return re.sub(r"\s+", " ", t).strip(" -–—")


def resolve_role(session: Session, raw_title: str | None) -> uuid.UUID | None:
    """Resolve a title: exact name/alias, then forward containment, then
    fuzzy name similarity, then a constrained LLM classification.

    The alias step used to match in BOTH directions (`title ILIKE %alias%` OR
    `alias ILIKE %title%`) and take the longest alias - so "software
    engineer" matched inside "fda regulated software engineer" and every
    plain Software Engineer JD resolved to Medical Device Software Engineer.
    Now only an alias found INSIDE the title counts, on word boundaries, and
    at least MIN_CONTAINED_ALIAS characters long (a 2-letter alias like "ia"
    used to match inside "associate").
    """
    raw = (raw_title or "").strip().lower()
    normalized = normalize_title(raw_title)
    if not raw:
        return None

    for candidate in dict.fromkeys(t for t in (raw, normalized) if t):
        exact = session.execute(
            text("""
                SELECT id FROM role
                WHERE lower(canonical_name) = :t OR :t = ANY(aliases)
                ORDER BY (lower(canonical_name) = :t) DESC, canonical_name
                LIMIT 1
            """),
            {"t": candidate},
        ).first()
        if exact:
            return exact[0]

    contained = session.execute(
        text("""
            SELECT role.id
            FROM role
            CROSS JOIN LATERAL unnest(array_append(role.aliases, lower(role.canonical_name))) AS alias
            WHERE length(alias) >= :min_len
              AND :title ~ ('\\m' || regexp_replace(alias, '([.^$*+?(){}\\[\\]\\\\|])', '\\\\\\1', 'g') || '\\M')
            ORDER BY length(alias) DESC, (alias = lower(role.canonical_name)) DESC, role.canonical_name
            LIMIT 1
        """),
        {"title": normalized or raw, "min_len": MIN_CONTAINED_ALIAS},
    ).first()
    if contained:
        return contained[0]

    fuzzy_hit = session.execute(
        text("""
            SELECT id, similarity(lower(canonical_name), :title) AS similarity
            FROM role
            ORDER BY similarity DESC
            LIMIT 1;
        """),
        {"title": normalized or raw},
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

def re_resolve_all(session: Session, *, user_id: uuid.UUID, dry_run: bool = True,
                   allow_llm: bool = False) -> list[dict]:
    """Re-run resolution over every stored JD. Returns the changes; writes
    them unless dry_run. The LLM step is off by default so a repair pass costs
    no quota - titles that only the LLM could place are left as they are."""
    rows = session.execute(
        text("""SELECT jd.id, jd.role_title, jd.role_id, r.canonical_name
                FROM job_description jd LEFT JOIN role r ON r.id = jd.role_id
                WHERE jd.user_id = :u"""),
        {"u": user_id},
    ).fetchall()
    names = dict(session.execute(text("SELECT id, canonical_name FROM role")).fetchall())
    changes = []
    for jd_id, title, old_id, old_name in rows:
        if allow_llm:
            new_id = resolve_role(session, title)
        else:
            new_id = _resolve_without_llm(session, title)
        if new_id and new_id != old_id:
            changes.append({"jd_id": jd_id, "title": title, "old": old_name, "new": names.get(new_id),
                            "new_id": new_id})
            if not dry_run:
                session.execute(text("UPDATE job_description SET role_id = :r WHERE id = :id AND user_id = :u"),
                                {"r": new_id, "id": jd_id, "u": user_id})
    if not dry_run:
        session.commit()
    return changes


def _resolve_without_llm(session: Session, raw_title: str | None) -> uuid.UUID | None:
    import app.services.role_resolution as me
    original = me.generate_json
    me.generate_json = lambda *_a, **_k: {"role_id": None}
    try:
        return resolve_role(session, raw_title)
    finally:
        me.generate_json = original
