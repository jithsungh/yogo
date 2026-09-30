"""Skill - thin wrappers over kb_service, kept for existing callers.

New code should call kb_service directly (kind="skill") or go through the API.
"""
import uuid

from sqlalchemy.orm import Session

from app.services import kb_service as kb
from app.services.kb_service import skill_text as to_embedding_text  # noqa: F401 - re-export


def list_skills(session: Session, *, user_id: uuid.UUID):
    return kb.list_items(session, "skill", user_id=user_id)


def create_skill(session: Session, *, user_id: uuid.UUID, **fields):
    item, _ = kb.create_item(session, "skill", user_id=user_id, data=fields)
    return item


def update_skill(session: Session, *, user_id: uuid.UUID, skill_id: uuid.UUID, **changes):
    item, _ = kb.update_item(session, "skill", user_id=user_id, item_id=skill_id, patch=changes)
    return item


def delete_skill(session: Session, *, user_id: uuid.UUID, skill_id: uuid.UUID) -> None:
    kb.delete_item(session, "skill", user_id=user_id, item_id=skill_id)
