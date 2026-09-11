"""Run: python -m scripts.milestone_check "what did I do with kubernetes"
Proves the KB is real: a free-text question returns your own correct,
relevant history - not keyword matching, actual semantic retrieval.
"""
import sys

from sqlalchemy import text

from app.db import get_session
from app.llm.gemini_client import embed_text
from app.config import get_settings


def main():
    query = " ".join(sys.argv[1:]) or "what did I do with Kubernetes"
    settings = get_settings()
    query_vec = embed_text(query)

    with get_session() as session:
        rows = session.execute(
            text("""
                SELECT source_table, content_type, text_for_embedding,
                       1 - (embedding <=> :q) AS similarity
                FROM kb_chunk
                WHERE user_id = :user_id
                ORDER BY embedding <=> :q
                LIMIT 5;
            """),
            {"q": query_vec, "user_id": settings.default_user_id},
        ).fetchall()

    print(f"Query: {query!r}\n")
    if not rows:
        print("  No KB chunks found. Have you entered any data yet?")
        return
    for source_table, content_type, snippet, similarity in rows:
        print(f"  {similarity:.4f}  [{content_type}/{source_table}]  {snippet[:100]}")


if __name__ == "__main__":
    main()
