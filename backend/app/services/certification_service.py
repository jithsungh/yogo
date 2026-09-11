"""Certification service — CRUD + kb_chunk sync."""
import uuid
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.models import Certification
from app.services.kb_sync import sync_kb_chunk, delete_kb_chunk


def to_embedding_text(row: Certification) -> str:
    text = f"{row.title}"
    if row.issuer:
        text += f" issued by {row.issuer}"
    if row.date:
        text += f" ({row.date.isoformat()})"
    text += "."
    return text


def list_certifications(session: Session, *, user_id: uuid.UUID) -> list[Certification]:
    return list(session.execute(
        select(Certification).where(Certification.user_id == user_id)
        .order_by(Certification.date.desc().nulls_last())
    ).scalars().all())


def create_certification(
    session: Session,
    *,
    user_id: uuid.UUID,
    title: str,
    issuer: str | None = None,
    date: date | None = None,
    url: str | None = None,
) -> Certification:
    row = Certification(
        user_id=user_id, title=title, issuer=issuer, date=date, url=url,
    )
    session.add(row)
    session.flush()
    sync_kb_chunk(
        session, user_id=user_id, source_table="certification", source_id=row.id,
        content_type="certification", text_for_embedding=to_embedding_text(row),
    )
    session.commit()
    return row


def update_certification(
    session: Session,
    *,
    certification_id: uuid.UUID,
    **kwargs,
) -> Certification:
    row = session.get(Certification, certification_id)
    if row is None:
        raise ValueError(f"Certification {certification_id} not found")
    for k, v in kwargs.items():
        if hasattr(row, k):
            setattr(row, k, v)
    session.flush()
    sync_kb_chunk(
        session, user_id=row.user_id, source_table="certification", source_id=row.id,
        content_type="certification", text_for_embedding=to_embedding_text(row),
    )
    session.commit()
    return row


def delete_certification(session: Session, *, certification_id: uuid.UUID) -> None:
    row = session.get(Certification, certification_id)
    if row is None:
        return
    delete_kb_chunk(session, source_table="certification", source_id=row.id)
    session.delete(row)
    session.commit()
