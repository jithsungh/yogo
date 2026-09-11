"""Work experience service — CRUD + kb_chunk sync."""
import uuid
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.models import WorkExperience
from app.services.kb_sync import sync_kb_chunk, delete_kb_chunk


def to_embedding_text(row: WorkExperience) -> str:
    date_range = ""
    if row.start_date or row.end_date:
        s = row.start_date.isoformat() if row.start_date else "?"
        e = row.end_date.isoformat() if row.end_date else "present"
        date_range = f" ({s}-{e})"
    text = f"{row.role_title} at {row.company}{date_range}. {row.description}"
    if row.tech_stack:
        text += f" Tech stack: {', '.join(row.tech_stack)}."
    return text


def list_experiences(session: Session, *, user_id: uuid.UUID) -> list[WorkExperience]:
    return list(session.execute(
        select(WorkExperience).where(WorkExperience.user_id == user_id)
        .order_by(WorkExperience.start_date.desc())
    ).scalars().all())


def create_experience(
    session: Session,
    *,
    user_id: uuid.UUID,
    company: str,
    role_title: str,
    description: str,
    tech_stack: list[str] | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
    role_id: uuid.UUID | None = None,
) -> WorkExperience:
    row = WorkExperience(
        user_id=user_id, company=company, role_title=role_title,
        description=description, tech_stack=tech_stack or [],
        start_date=start_date, end_date=end_date, role_id=role_id,
    )
    session.add(row)
    session.flush()
    sync_kb_chunk(
        session, user_id=user_id, source_table="work_experience", source_id=row.id,
        content_type="experience", text_for_embedding=to_embedding_text(row),
    )
    session.commit()
    return row


def update_experience(
    session: Session,
    *,
    experience_id: uuid.UUID,
    **kwargs,
) -> WorkExperience:
    row = session.get(WorkExperience, experience_id)
    if row is None:
        raise ValueError(f"WorkExperience {experience_id} not found")
    for k, v in kwargs.items():
        if hasattr(row, k):
            setattr(row, k, v)
    session.flush()
    sync_kb_chunk(
        session, user_id=row.user_id, source_table="work_experience", source_id=row.id,
        content_type="experience", text_for_embedding=to_embedding_text(row),
    )
    session.commit()
    return row


def delete_experience(session: Session, *, experience_id: uuid.UUID) -> None:
    row = session.get(WorkExperience, experience_id)
    if row is None:
        return
    delete_kb_chunk(session, source_table="work_experience", source_id=row.id)
    session.delete(row)
    session.commit()
