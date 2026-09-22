"""Thin, shared wrapper around the Gemini API. Nothing else in the app should
import google.genai directly - always go through here, so model names,
retry behavior, and the Vector-wrapping gotcha live in exactly one place.
"""
import json

from google import genai
from google.genai import errors as genai_errors
from google.genai import types
from pgvector import Vector
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from app.config import get_settings

_settings = get_settings()
_client = genai.Client(api_key=_settings.gemini_api_key)


class GeminiRateLimitError(RuntimeError):
    """Raised when Gemini rejects a request because quota/rate limits were hit."""


def _is_rate_limit_error(exc: BaseException) -> bool:
    return (
        exc.__class__.__name__ == "RateLimitError"
        or getattr(exc, "code", None) == 429
        or getattr(exc, "status_code", None) == 429
    )


def _retryable(exc: BaseException) -> bool:
    # Retrying a quota error only burns time and can worsen a per-minute limit.
    return not _is_rate_limit_error(exc)


@retry(
    retry=retry_if_exception(_retryable),
    stop=stop_after_attempt(3),
    wait=wait_exponential(min=1, max=8),
)
def embed_text(text: str) -> Vector:
    """Always returns a pgvector.Vector, ready to bind directly into a query -
    callers never touch a raw list or worry about the dumper gotcha."""
    try:
        resp = _client.models.embed_content(
            model=_settings.gemini_embedding_model,
            contents=text,
            config=types.EmbedContentConfig(output_dimensionality=_settings.embedding_dim),
        )
    except Exception as exc:
        if _is_rate_limit_error(exc):
            raise GeminiRateLimitError(_rate_limit_message(exc)) from exc
        raise
    return Vector(resp.embeddings[0].values)


@retry(
    retry=retry_if_exception(_retryable),
    stop=stop_after_attempt(3),
    wait=wait_exponential(min=1, max=8),
)
def generate_text(prompt: str) -> str:
    try:
        interaction = _client.interactions.create(
            model=_settings.gemini_generation_model,
            input=prompt,
        )
    except Exception as exc:
        if _is_rate_limit_error(exc):
            raise GeminiRateLimitError(_rate_limit_message(exc)) from exc
        raise
    return interaction.output_text.strip()


def _rate_limit_message(exc: BaseException) -> str:
    detail = str(exc).strip()
    return "Gemini rate limit or quota reached. Wait a moment and retry." + (f" Details: {detail}" if detail else "")


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
