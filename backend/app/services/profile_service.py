"""Profile (basic info): a singleton per user, so get + upsert only."""
import uuid

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.models import ProfileBasic
from app.schemas.kb import ProfileIn, ProfileOut
from app.services.kb_sync import sync_kb_chunk


def to_embedding_text(row) -> str:
    """Name, location and summary. The old text was only "<name>. Based in
    <city>." - a background question retrieved nothing useful from it."""
    parts = [row.full_name or ""]
    if row.location:
        parts.append(f"Based in {row.location}.")
    if row.summary:
        parts.append(row.summary)
    if row.links:
        parts.append("Profiles: " + ", ".join(sorted(row.links)) + ".")
    return " ".join(p for p in parts if p)


def get_profile(session: Session, *, user_id: uuid.UUID) -> ProfileOut | None:
    row = session.get(ProfileBasic, uuid.UUID(str(user_id)))
    return ProfileOut.model_validate(row) if row else None


def upsert_profile(session: Session, *, user_id: uuid.UUID, **fields) -> ProfileOut:
    data = ProfileIn.model_validate(fields)
    uid = uuid.UUID(str(user_id))
    row = session.get(ProfileBasic, uid)
    if row is None:
        row = ProfileBasic(user_id=uid, **data.model_dump())
        session.add(row)
    else:
        for k, v in data.model_dump().items():
            setattr(row, k, v)
        row.updated_at = func.now()
    session.flush()
    session.refresh(row)
    sync_kb_chunk(session, user_id=uid, source_table="profile_basic", source_id=uid,
                  content_type="basic_info", text_for_embedding=to_embedding_text(row))
    session.commit()
    return ProfileOut.model_validate(row)
