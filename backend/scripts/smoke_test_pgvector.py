"""
Smoke test #2: proves the full Phase 0 milestone end-to-end -
"a vector column + similarity query works" - using real Gemini embeddings
against the running Postgres + pgvector container.

Run from backend/:  python scripts/smoke_test_pgvector.py
Requires:            pip install -U google-genai psycopg[binary] pgvector pydantic-settings
Assumes:             docker compose up -d   (postgres reachable per your .env)
"""
from google import genai
from google.genai import types
import psycopg
from pgvector import Vector
from pgvector.psycopg import register_vector

from app.config import get_settings

SAMPLE_TEXTS = [
    "Built a Kubernetes-based CI/CD pipeline for a microservices platform.",
    "Trained a computer vision model for defect detection using PyTorch.",
    "Automated AWS infrastructure provisioning with Terraform and Ansible.",
]
QUERY_TEXT = "container orchestration and deployment automation"


def embed(client: genai.Client, settings, text: str) -> list[float]:
    resp = client.models.embed_content(
        model=settings.gemini_embedding_model,
        contents=text,
        config=types.EmbedContentConfig(output_dimensionality=settings.embedding_dim),
    )
    return resp.embeddings[0].values


def main() -> None:
    settings = get_settings()
    client = genai.Client(api_key=settings.gemini_api_key)

    with psycopg.connect(settings.raw_database_url) as conn:
        # Extension must exist BEFORE register_vector() runs, or it has no
        # 'vector' type to find - and parameters silently fall back to plain
        # float arrays instead, causing "operator does not exist" errors later.
        with conn.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS vector;")
        conn.commit()

        register_vector(conn)

        with conn.cursor() as cur:
            cur.execute("DROP TABLE IF EXISTS _smoke_test_chunk;")
            cur.execute(
                f"""
                CREATE TABLE _smoke_test_chunk (
                    id SERIAL PRIMARY KEY,
                    content TEXT NOT NULL,
                    embedding VECTOR({settings.embedding_dim}) NOT NULL
                );
                """
            )

            print("Embedding + inserting sample chunks...")
            for text in SAMPLE_TEXTS:
                vec = embed(client, settings, text)
                cur.execute(
                    "INSERT INTO _smoke_test_chunk (content, embedding) VALUES (%s, %s);",
                    (text, Vector(vec)),
                )

            print(f"Embedding query: {QUERY_TEXT!r}\n")
            query_vec = Vector(embed(client, settings, QUERY_TEXT))
            cur.execute(
                """
                SELECT content, 1 - (embedding <=> %s) AS similarity
                FROM _smoke_test_chunk
                ORDER BY embedding <=> %s
                LIMIT 3;
                """,
                (query_vec, query_vec),
            )
            rows = cur.fetchall()
            cur.execute("DROP TABLE _smoke_test_chunk;")
        conn.commit()

    print("Top matches by cosine similarity:")
    for content, similarity in rows:
        print(f"  {similarity:.4f}  {content}")

    top_match = rows[0][0]
    assert "Kubernetes" in top_match, (
        "Expected the Kubernetes/CI-CD sentence to rank first for a query about "
        "container orchestration - if it didn't, something's off in the embedding setup."
    )
    print("\n✅ Postgres + pgvector + Gemini embeddings work end-to-end.")


if __name__ == "__main__":
    main()