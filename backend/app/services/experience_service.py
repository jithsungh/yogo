"""Experience - thin wrappers over kb_service, kept for existing callers.

New code should call kb_service directly (kind="experience") or go through the API.
"""
import uuid

from sqlalchemy.orm import Session

from app.services import kb_service as kb
from app.services.kb_service import experience_text as to_embedding_text  # noqa: F401 - re-export


def list_experiences(session: Session, *, user_id: uuid.UUID):
    return kb.list_items(session, "experience", user_id=user_id)


def create_experience(session: Session, *, user_id: uuid.UUID, **fields):
    item, _ = kb.create_item(session, "experience", user_id=user_id, data=fields)
    return item


def update_experience(session: Session, *, user_id: uuid.UUID, experience_id: uuid.UUID, **changes):
    item, _ = kb.update_item(session, "experience", user_id=user_id, item_id=experience_id, patch=changes)
    return item


def delete_experience(session: Session, *, user_id: uuid.UUID, experience_id: uuid.UUID) -> None:
    kb.delete_item(session, "experience", user_id=user_id, item_id=experience_id)
