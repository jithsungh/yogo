"""Skill service — CRUD + kb_chunk sync."""
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.models import Skill
from app.services.kb_sync import sync_kb_chunk, delete_kb_chunk


def to_embedding_text(row: Skill) -> str:
    text = f"{row.name} ({row.category}"
    if row.proficiency:
        text += f", proficiency {row.proficiency}/5"
    text += ")."
    return text


def list_skills(session: Session, *, user_id: uuid.UUID) -> list[Skill]:
    return list(session.execute(
        select(Skill).where(Skill.user_id == user_id).order_by(Skill.category, Skill.name)
    ).scalars().all())


def create_skill(
    session: Session,
    *,
    user_id: uuid.UUID,
    name: str,
    category: str,
    proficiency: int | None = None,
) -> Skill:
    row = Skill(
        user_id=user_id, name=name, category=category, proficiency=proficiency,
    )
    session.add(row)
    session.flush()
    sync_kb_chunk(
        session, user_id=user_id, source_table="skill", source_id=row.id,
        content_type="skill", text_for_embedding=to_embedding_text(row),
    )
    session.commit()
    return row


def update_skill(
    session: Session,
    *,
    skill_id: uuid.UUID,
    **kwargs,
) -> Skill:
    row = session.get(Skill, skill_id)
    if row is None:
        raise ValueError(f"Skill {skill_id} not found")
    for k, v in kwargs.items():
        if hasattr(row, k):
            setattr(row, k, v)
    session.flush()
    sync_kb_chunk(
        session, user_id=row.user_id, source_table="skill", source_id=row.id,
        content_type="skill", text_for_embedding=to_embedding_text(row),
    )
    session.commit()
    return row


def delete_skill(session: Session, *, skill_id: uuid.UUID) -> None:
    row = session.get(Skill, skill_id)
    if row is None:
        return
    delete_kb_chunk(session, source_table="skill", source_id=row.id)
    session.delete(row)
    session.commit()
