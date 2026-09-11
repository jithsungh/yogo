"""Keeps the unified kb_chunk retrieval index in sync with Tier-1 source
tables. Every entity service calls sync_kb_chunk() after it commits a
create/update, and delete_kb_chunk() after a delete (kb_chunk.source_id is
NOT a real foreign key - it can't be, it's polymorphic - so deletes do not
cascade automatically and must be handled explicitly here).
"""
import uuid

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.llm.gemini_client import embed_text


def sync_kb_chunk(
    session: Session,
    *,
    user_id: uuid.UUID,
    source_table: str,
    source_id: uuid.UUID,
    content_type: str,
    text_for_embedding: str,
    role_tags: list[uuid.UUID] | None = None,
) -> None:
    embedding = embed_text(text_for_embedding)
    session.execute(
        text("""
            INSERT INTO kb_chunk (user_id, source_table, source_id, content_type,
                                   role_tags, text_for_embedding, embedding, updated_at)
            VALUES (:user_id, :source_table, :source_id, :content_type,
                    :role_tags, :text_for_embedding, :embedding, now())
            ON CONFLICT (source_table, source_id) DO UPDATE SET
                content_type       = EXCLUDED.content_type,
                role_tags          = EXCLUDED.role_tags,
                text_for_embedding = EXCLUDED.text_for_embedding,
                embedding          = EXCLUDED.embedding,
                updated_at         = now();
        """),
        {
            "user_id": user_id,
            "source_table": source_table,
            "source_id": source_id,
            "content_type": content_type,
            "role_tags": role_tags or [],
            "text_for_embedding": text_for_embedding,
            "embedding": embedding,
        },
    )


def delete_kb_chunk(session: Session, *, source_table: str, source_id: uuid.UUID) -> None:
    session.execute(
        text("DELETE FROM kb_chunk WHERE source_table = :t AND source_id = :i"),
        {"t": source_table, "i": source_id},
    )
