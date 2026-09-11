"""Thin, shared wrapper around the Gemini API. Nothing else in the app should
import google.genai directly - always go through here, so model names,
retry behavior, and the Vector-wrapping gotcha live in exactly one place.
"""
import json

from google import genai
from google.genai import types
from pgvector import Vector
from tenacity import retry, stop_after_attempt, wait_exponential

from app.config import get_settings

_settings = get_settings()
_client = genai.Client(api_key=_settings.gemini_api_key)


@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8))
def embed_text(text: str) -> Vector:
    """Always returns a pgvector.Vector, ready to bind directly into a query -
    callers never touch a raw list or worry about the dumper gotcha."""
    resp = _client.models.embed_content(
        model=_settings.gemini_embedding_model,
        contents=text,
        config=types.EmbedContentConfig(output_dimensionality=_settings.embedding_dim),
    )
    return Vector(resp.embeddings[0].values)


@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8))
def generate_text(prompt: str) -> str:
    interaction = _client.interactions.create(
        model=_settings.gemini_generation_model,
        input=prompt,
    )
    return interaction.output_text.strip()


def generate_json(prompt: str) -> dict:
    """For structured-extraction prompts. Always instruct the model in `prompt`
    to return ONLY raw JSON, no markdown fences, no preamble - this still
    defensively strips fences in case it ignores that instruction."""
    raw = generate_text(prompt)
    cleaned = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Model did not return valid JSON. Raw output:\n{raw}") from exc
