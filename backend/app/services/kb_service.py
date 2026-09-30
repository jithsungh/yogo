"""One CRUD service for every knowledge-base entity, with kb_chunk kept in sync.

Before this, each of six entity services had its own create/update/delete,
none checked that the row belonged to the caller (update_project(id) would
edit anyone's project), and every update re-embedded unconditionally. The UI
had no edit forms at all, so "editing" meant delete and re-add.

Every entity here goes through the same path:

    validate (schemas.kb)  ->  ownership check  ->  write  ->  kb_chunk sync

and kb_chunk sync only calls Gemini when the embedding text actually changed
(kb_sync.sync_kb_chunk compares it). Callers - Streamlit pages today, the
FastAPI routers in app/api - get pydantic *Out models back, never ORM rows.
"""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import Callable

from pydantic import BaseModel, ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.models import Certification, Education, Project, Skill, WorkExperience
from app.schemas import kb as S
from app.services.kb_sync import delete_kb_chunk, sync_kb_chunk


class NotFound(LookupError):
    """The row does not exist, or belongs to another user (indistinguishable
    on purpose - an API must not reveal which)."""


# ── Embedding text ────────────────────────────────────────────────────────────
# What gets embedded decides what retrieval can find. These carry the actual
# evidence (key points, responsibilities), not just titles.

def _range(start, end) -> str:
    if not (start or end):
        return ""
    s = start.strftime("%b %Y") if start else "?"
    e = end.strftime("%b %Y") if end else "present"
    return f" ({s} - {e})"


def education_text(r: Education) -> str:
    t = f"{r.degree} from {r.institution}{_range(r.start_date, r.end_date)}."
    if r.score:
        t += f" Score: {r.score}."
    if r.highlights:
        t += f" {r.highlights}"
    return t


def experience_text(r: WorkExperience) -> str:
    t = f"{r.role_title} at {r.company}{_range(r.start_date, r.end_date)}. {r.description or ''}".strip()
    if r.tech_stack:
        t += f" Tech stack: {', '.join(r.tech_stack)}."
    return t


def project_text(r: Project) -> str:
    parts = [r.title + "."]
    if r.summary:
        parts.append(r.summary.rstrip(".") + ".")
    if r.role:
        parts.append(f"Role: {r.role}.")
    if r.description:
        parts.append(r.description)
    if r.key_points:
        parts.append("Key points: " + "; ".join(r.key_points) + ".")
    if r.tech_stack:
        parts.append(f"Tech stack: {', '.join(r.tech_stack)}.")
    return " ".join(parts)


def skill_text(r: Skill) -> str:
    t = f"{r.name} ({r.category.replace('_', ' ')}"
    if r.proficiency:
        t += f", proficiency {r.proficiency}/5"
    return t + ")."


def certification_text(r: Certification) -> str:
    t = r.title
    if r.issuer:
        t += f", issued by {r.issuer}"
    if r.date:
        t += f" ({r.date.year})"
    return t + "."


def repo_key(url: str | None) -> str | None:
    """Normalized repo URL - the identity two imports of one project share."""
    if not url:
        return None
    u = url.strip().lower()
    u = re.sub(r"^https?://", "", u)
    u = re.sub(r"^www\.", "", u)
    u = re.sub(r"\.git$", "", u.rstrip("/"))
    return u or None


# ── Entity registry ───────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Entity:
    name: str
    model: type
    source_table: str
    content_type: str
    create: type[BaseModel]
    update: type[BaseModel]
    out: type[BaseModel]
    to_text: Callable
    order_by: Callable
    embeddable: Callable = lambda row: True


ENTITIES: dict[str, Entity] = {
    "education": Entity(
        "education", Education, "education", "education",
        S.EducationCreate, S.EducationUpdate, S.EducationOut, education_text,
        lambda: (Education.end_date.desc().nulls_first(), Education.created_at.desc()),
    ),
    "experience": Entity(
        "experience", WorkExperience, "work_experience", "experience",
        S.ExperienceCreate, S.ExperienceUpdate, S.ExperienceOut, experience_text,
        lambda: (WorkExperience.end_date.desc().nulls_first(), WorkExperience.start_date.desc().nulls_last()),
    ),
    "project": Entity(
        "project", Project, "project", "project",
        S.ProjectCreate, S.ProjectUpdate, S.ProjectOut, project_text,
        lambda: (Project.is_favourite.desc(), Project.priority.desc(), Project.updated_at.desc()),
        # Unreviewed imports stay out of retrieval until approved.
        embeddable=lambda row: row.status == "verified",
    ),
    "skill": Entity(
        "skill", Skill, "skill", "skill",
        S.SkillCreate, S.SkillUpdate, S.SkillOut, skill_text,
        lambda: (Skill.category, Skill.name),
    ),
    "certification": Entity(
        "certification", Certification, "certification", "certification",
        S.CertificationCreate, S.CertificationUpdate, S.CertificationOut, certification_text,
        lambda: (Certification.date.desc().nulls_last(),),
    ),
}


def _entity(kind: str) -> Entity:
    try:
        return ENTITIES[kind]
    except KeyError:
        raise ValueError(f"Unknown entity {kind!r}; expected one of {sorted(ENTITIES)}") from None


def _owned(session: Session, e: Entity, user_id, item_id):
    row = session.get(e.model, uuid.UUID(str(item_id)))
    if row is None or str(row.user_id) != str(user_id):
        raise NotFound(f"{e.name} {item_id} not found")
    return row


def _sync(session: Session, e: Entity, row, *, force: bool = False) -> bool:
    if e.embeddable(row):
        return sync_kb_chunk(
            session, user_id=row.user_id, source_table=e.source_table, source_id=row.id,
            content_type=e.content_type, text_for_embedding=e.to_text(row), force=force,
        )
    delete_kb_chunk(session, source_table=e.source_table, source_id=row.id)
    return False


def _friendly_integrity(e: Entity, exc: IntegrityError) -> ValueError:
    msg = str(exc.orig)
    if "uq_project_user_repo" in msg:
        return ValueError("You already have a project for this repository.")
    if "skill_user_id_name_key" in msg or ("skill" in msg and "unique" in msg.lower()):
        return ValueError("You already have a skill with this name.")
    return ValueError(f"Could not save {e.name}: {msg.splitlines()[0]}")


# ── CRUD ──────────────────────────────────────────────────────────────────────

def list_items(session: Session, kind: str, *, user_id, **filters) -> list[BaseModel]:
    e = _entity(kind)
    q = select(e.model).where(e.model.user_id == uuid.UUID(str(user_id)))
    for field, value in filters.items():
        if value is not None:
            q = q.where(getattr(e.model, field) == value)
    rows = session.execute(q.order_by(*e.order_by())).scalars().all()
    return [e.out.model_validate(r) for r in rows]


def get_item(session: Session, kind: str, *, user_id, item_id) -> BaseModel:
    e = _entity(kind)
    return e.out.model_validate(_owned(session, e, user_id, item_id))


def create_item(session: Session, kind: str, *, user_id, data: BaseModel | dict,
                commit: bool = True) -> tuple[BaseModel, bool]:
    """Returns (saved item, whether it was embedded)."""
    e = _entity(kind)
    payload = data if isinstance(data, e.create) else e.create.model_validate(
        data.model_dump() if isinstance(data, BaseModel) else data)
    values = payload.model_dump()
    if e.name == "project":
        values["repo_key"] = repo_key(values.get("github_url")) or repo_key(values.get("live_url"))
    if e.name == "skill":
        dup = session.execute(
            select(Skill.name).where(Skill.user_id == uuid.UUID(str(user_id)),
                                     func.lower(Skill.name) == values["name"].lower())
        ).first()
        if dup:
            raise ValueError(f"You already have the skill {dup[0]!r}.")
    row = e.model(user_id=uuid.UUID(str(user_id)), **values)
    session.add(row)
    try:
        session.flush()
    except IntegrityError as exc:
        session.rollback()
        raise _friendly_integrity(e, exc) from exc
    embedded = _sync(session, e, row)
    if commit:
        session.commit()
    return e.out.model_validate(row), embedded


def update_item(session: Session, kind: str, *, user_id, item_id, patch: BaseModel | dict,
                commit: bool = True) -> tuple[BaseModel, bool]:
    """Apply only the fields the caller sent; re-embed only if the embedded
    text changed. Returns (saved item, whether it was re-embedded)."""
    e = _entity(kind)
    row = _owned(session, e, user_id, item_id)
    if isinstance(patch, BaseModel) and not isinstance(patch, e.update):
        patch = patch.model_dump(exclude_unset=True)
    upd = patch if isinstance(patch, e.update) else e.update.model_validate(patch)
    changes = upd.model_dump(exclude_unset=True)

    # Validate the record as it will be AFTER the patch - a patch that only
    # moves end_date can still make the range invalid.
    merged = {**e.out.model_validate(row).model_dump(), **changes}
    try:
        e.create.model_validate({k: v for k, v in merged.items() if k in e.create.model_fields})
    except ValidationError as exc:
        raise ValueError(exc.errors()[0]["msg"]) from exc

    if e.name == "skill" and "name" in changes and changes["name"].lower() != row.name.lower():
        dup = session.execute(
            select(Skill.name).where(Skill.user_id == row.user_id, Skill.id != row.id,
                                     func.lower(Skill.name) == changes["name"].lower())
        ).first()
        if dup:
            raise ValueError(f"You already have the skill {dup[0]!r}.")

    for field, value in changes.items():
        setattr(row, field, value)
    if e.name == "project" and ({"github_url", "live_url"} & changes.keys()):
        row.repo_key = repo_key(row.github_url) or repo_key(row.live_url)
    if hasattr(row, "updated_at") and changes:
        row.updated_at = func.now()
    try:
        session.flush()
    except IntegrityError as exc:
        session.rollback()
        raise _friendly_integrity(e, exc) from exc
    session.refresh(row)
    reembedded = _sync(session, e, row)
    if commit:
        session.commit()
    return e.out.model_validate(row), reembedded


def delete_item(session: Session, kind: str, *, user_id, item_id, commit: bool = True) -> None:
    e = _entity(kind)
    row = _owned(session, e, user_id, item_id)
    delete_kb_chunk(session, source_table=e.source_table, source_id=row.id)
    session.delete(row)
    if commit:
        session.commit()


def reembed_all(session: Session, *, user_id, kinds: list[str] | None = None,
                force: bool = False) -> dict[str, int]:
    """Bring every chunk in line with its source row. Unchanged text is
    skipped unless force=True. Returns {kind: rows re-embedded}."""
    counts = {}
    for kind in kinds or list(ENTITIES):
        e = _entity(kind)
        rows = session.execute(
            select(e.model).where(e.model.user_id == uuid.UUID(str(user_id)))
        ).scalars().all()
        counts[kind] = sum(_sync(session, e, r, force=force) for r in rows)
    session.commit()
    return counts
