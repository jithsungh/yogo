"""Profile (basic info) service — create, read, update.

Profile is a singleton per user (PK = user_id), so there's no list/delete.
"""
import uuid

from sqlalchemy.orm import Session

from app.models.models import ProfileBasic
from app.services.kb_sync import sync_kb_chunk, delete_kb_chunk


def to_embedding_text(row: ProfileBasic) -> str:
    parts = [row.full_name or ""]
    if row.summary:
        parts.append(row.summary)
    if row.location:
        parts.append(f"Based in {row.location}.")
    return ". ".join(p for p in parts if p)


def get_profile(session: Session, *, user_id: uuid.UUID) -> ProfileBasic | None:
    return session.get(ProfileBasic, user_id)


def upsert_profile(
    session: Session,
    *,
    user_id: uuid.UUID,
    full_name: str,
    email: str | None = None,
    phone: str | None = None,
    location: str | None = None,
    links: dict | None = None,
    summary: str | None = None,
) -> ProfileBasic:
    row = session.get(ProfileBasic, user_id)
    if row is None:
        row = ProfileBasic(
            user_id=user_id,
            full_name=full_name,
            email=email,
            phone=phone,
            location=location,
            links=links or {},
            summary=summary,
        )
        session.add(row)
    else:
        row.full_name = full_name
        row.email = email
        row.phone = phone
        row.location = location
        row.links = links or {}
        row.summary = summary
    session.flush()
    sync_kb_chunk(
        session,
        user_id=user_id,
        source_table="profile_basic",
        source_id=user_id,  # PK is user_id itself
        content_type="basic_info",
        text_for_embedding=to_embedding_text(row),
    )
    session.commit()
    return row
