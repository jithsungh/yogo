"""
Smoke test #1: confirms the Gemini API key + models actually work.

Run from backend/:  python scripts/smoke_test_gemini.py
Requires:            pip install -U google-genai pydantic-settings

Note: Google's Gemini API surface has been moving fast (new "Interactions API"
as the recommended interface, frequent model retirements/replacements). If this
script errors on a model name, check https://ai.google.dev/gemini-api/docs/models
and update GEMINI_GENERATION_MODEL / GEMINI_EMBEDDING_MODEL in .env accordingly.
"""
import sys

from google import genai
from google.genai import types

from app.config import get_settings


def main() -> None:
    settings = get_settings()
    client = genai.Client(api_key=settings.gemini_api_key)

    print(f"Generation model: {settings.gemini_generation_model}")
    print(f"Embedding model:  {settings.gemini_embedding_model}")
    print(f"Embedding dim:    {settings.embedding_dim}\n")

    # 1) Generation check
    try:
        interaction = client.interactions.create(
            model=settings.gemini_generation_model,
            input="Reply with exactly these two words: GENERATION_OK",
        )
        print("[generation] response:", interaction.output_text.strip())
    except Exception as exc:  # noqa: BLE001 - smoke test, want to see any failure
        print("[generation] FAILED:", exc)
        sys.exit(1)

    # 2) Embedding check
    try:
        embed_response = client.models.embed_content(
            model=settings.gemini_embedding_model,
            contents="This is a smoke test sentence for embeddings.",
            config=types.EmbedContentConfig(output_dimensionality=settings.embedding_dim),
        )
        vector = embed_response.embeddings[0].values
        print(f"[embedding] received a {len(vector)}-dimensional vector")
        if len(vector) != settings.embedding_dim:
            raise ValueError(
                f"Expected {settings.embedding_dim} dims, got {len(vector)}. "
                "Update EMBEDDING_DIM in .env and every VECTOR(N) column in "
                "db/init/01_schema.sql to match, then reset the DB."
            )
    except Exception as exc:  # noqa: BLE001
        print("[embedding] FAILED:", exc)
        sys.exit(1)

    print("\n✅ Gemini API connection looks good.")


if __name__ == "__main__":
    main()
