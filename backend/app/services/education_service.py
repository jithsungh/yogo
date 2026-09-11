"""Education service — CRUD + kb_chunk sync."""
import uuid
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.models import Education
from app.services.kb_sync import sync_kb_chunk, delete_kb_chunk


def to_embedding_text(row: Education) -> str:
    date_range = ""
    if row.start_date or row.end_date:
        s = row.start_date.isoformat() if row.start_date else "?"
        e = row.end_date.isoformat() if row.end_date else "present"
        date_range = f" ({s}-{e})"
    text = f"{row.degree} from {row.institution}{date_range}."
    if row.highlights:
        text += f" {row.highlights}"
    return text


def list_education(session: Session, *, user_id: uuid.UUID) -> list[Education]:
    return list(session.execute(
        select(Education).where(Education.user_id == user_id).order_by(Education.start_date.desc())
    ).scalars().all())


def create_education(
    session: Session,
    *,
    user_id: uuid.UUID,
    degree: str,
    institution: str,
    start_date: date | None = None,
    end_date: date | None = None,
    score: str | None = None,
    highlights: str | None = None,
) -> Education:
    row = Education(
        user_id=user_id, degree=degree, institution=institution,
        start_date=start_date, end_date=end_date, score=score, highlights=highlights,
    )
    session.add(row)
    session.flush()
    sync_kb_chunk(
        session, user_id=user_id, source_table="education", source_id=row.id,
        content_type="education", text_for_embedding=to_embedding_text(row),
    )
    session.commit()
    return row


def update_education(
    session: Session,
    *,
    education_id: uuid.UUID,
    **kwargs,
) -> Education:
    row = session.get(Education, education_id)
    if row is None:
        raise ValueError(f"Education {education_id} not found")
    for k, v in kwargs.items():
        if hasattr(row, k):
            setattr(row, k, v)
    session.flush()
    sync_kb_chunk(
        session, user_id=row.user_id, source_table="education", source_id=row.id,
        content_type="education", text_for_embedding=to_embedding_text(row),
    )
    session.commit()
    return row


def delete_education(session: Session, *, education_id: uuid.UUID) -> None:
    row = session.get(Education, education_id)
    if row is None:
        return
    delete_kb_chunk(session, source_table="education", source_id=row.id)
    session.delete(row)
    session.commit()
