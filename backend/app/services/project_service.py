"""Projects - thin wrappers over kb_service, kept for existing callers.

New code should call kb_service directly (create_item / update_item with
kind="project") or go through the API.
"""
import uuid

from sqlalchemy.orm import Session

from app.services import kb_service as kb
from app.services.kb_service import project_text as to_embedding_text  # noqa: F401 - re-export


def list_projects(session: Session, *, user_id: uuid.UUID, status: str | None = None):
    return kb.list_items(session, "project", user_id=user_id, status=status)


def create_project(session: Session, *, user_id: uuid.UUID, **fields):
    """Create a project, or return the existing one for the same repository.

    Imports call this repeatedly; the old version inserted a new row every
    time, which is how one repo ended up in the table three times.
    """
    from sqlalchemy import select

    from app.models.models import Project

    rk = kb.repo_key(fields.get("github_url")) or kb.repo_key(fields.get("live_url"))
    if rk:
        existing = session.execute(
            select(Project).where(Project.user_id == user_id, Project.repo_key == rk)
        ).scalars().first()
        if existing:
            return kb.get_item(session, "project", user_id=user_id, item_id=existing.id)
    item, _ = kb.create_item(session, "project", user_id=user_id, data=fields)
    return item


def update_project(session: Session, *, user_id: uuid.UUID, project_id: uuid.UUID, **changes):
    item, _ = kb.update_item(session, "project", user_id=user_id, item_id=project_id, patch=changes)
    return item


def approve_project(session: Session, *, user_id: uuid.UUID, project_id: uuid.UUID):
    return update_project(session, user_id=user_id, project_id=project_id, status="verified")


def delete_project(session: Session, *, user_id: uuid.UUID, project_id: uuid.UUID) -> None:
    kb.delete_item(session, "project", user_id=user_id, item_id=project_id)
