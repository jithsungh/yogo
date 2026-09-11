"""Project service — CRUD + kb_chunk sync.

GitHub-imported drafts do NOT get a kb_chunk row until approved (status='verified').
"""
import uuid
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.models import Project
from app.services.kb_sync import sync_kb_chunk, delete_kb_chunk


def to_embedding_text(row: Project) -> str:
    text = f"{row.title}. {row.description}"
    if row.tech_stack:
        text += f" Tech stack: {', '.join(row.tech_stack)}."
    if row.highlights:
        text += f" {row.highlights}"
    return text


def list_projects(session: Session, *, user_id: uuid.UUID, status: str | None = None) -> list[Project]:
    q = select(Project).where(Project.user_id == user_id)
    if status:
        q = q.where(Project.status == status)
    return list(session.execute(q.order_by(Project.created_at.desc())).scalars().all())


def create_project(
    session: Session,
    *,
    user_id: uuid.UUID,
    title: str,
    description: str,
    tech_stack: list[str] | None = None,
    github_url: str | None = None,
    live_url: str | None = None,
    highlights: str | None = None,
    source: str = "manual",
    status: str = "verified",
) -> Project:
    row = Project(
        user_id=user_id, title=title, description=description,
        tech_stack=tech_stack or [], github_url=github_url, live_url=live_url,
        highlights=highlights, source=source, status=status,
    )
    session.add(row)
    session.flush()
    # Only sync to kb_chunk if verified — drafts (e.g. GitHub imports) don't get embedded yet
    if status == "verified":
        sync_kb_chunk(
            session, user_id=user_id, source_table="project", source_id=row.id,
            content_type="project", text_for_embedding=to_embedding_text(row),
        )
    session.commit()
    return row


def update_project(
    session: Session,
    *,
    project_id: uuid.UUID,
    **kwargs,
) -> Project:
    row = session.get(Project, project_id)
    if row is None:
        raise ValueError(f"Project {project_id} not found")
    old_status = row.status
    for k, v in kwargs.items():
        if hasattr(row, k):
            setattr(row, k, v)
    session.flush()
    # Sync kb_chunk if now verified (handles draft→verified approval)
    if row.status == "verified":
        sync_kb_chunk(
            session, user_id=row.user_id, source_table="project", source_id=row.id,
            content_type="project", text_for_embedding=to_embedding_text(row),
        )
    elif old_status == "verified" and row.status != "verified":
        # Edge case: un-verifying removes from KB
        delete_kb_chunk(session, source_table="project", source_id=row.id)
    session.commit()
    return row


def approve_project(session: Session, *, project_id: uuid.UUID) -> Project:
    """Flip a draft project to verified and sync to kb_chunk."""
    return update_project(session, project_id=project_id, status="verified")


def delete_project(session: Session, *, project_id: uuid.UUID) -> None:
    row = session.get(Project, project_id)
    if row is None:
        return
    delete_kb_chunk(session, source_table="project", source_id=row.id)
    session.delete(row)
    session.commit()
