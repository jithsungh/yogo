"""Thin, shared wrapper around the Gemini API. Nothing else in the app should
import google.genai directly - always go through here, so model names,
retry behaviour, quota handling and the Vector-wrapping gotcha live in
exactly one place.

  * The client is created lazily on first use. Creating it at import time
    meant no module that merely imported a service could load without a
    Gemini key - including pure-logic tests.
  * embed_text keeps an in-process LRU cache keyed by the exact text. The same
    strings (skill names on every match, questions on every rerun) were being
    re-embedded constantly; a long-lived Streamlit or API process now pays
    for each string once.
  * generate_json asks for JSON mode and parses defensively (fences, prose
    around the object, a trailing comma), so one chatty response does not
    fail a whole import.
  * Quota errors (429) surface as GeminiRateLimitError and are NOT retried;
    transient errors (5xx) are retried with backoff.
"""
from __future__ import annotations

import functools
import json
import re

from pgvector import Vector
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from app.config import get_settings


class GeminiRateLimitError(RuntimeError):
    """Raised when Gemini rejects a request because quota/rate limits were hit."""


@functools.lru_cache(maxsize=1)
def get_client():
    from google import genai

    return genai.Client(api_key=get_settings().gemini_api_key)


def __getattr__(name):
    # Backward compatibility for scripts that did `from gemini_client import _client`.
    if name == "_client":
        return get_client()
    raise AttributeError(name)


def _is_rate_limit_error(exc: BaseException) -> bool:
    return (
        exc.__class__.__name__ == "RateLimitError"
        or getattr(exc, "code", None) == 429
        or getattr(exc, "status_code", None) == 429
    )


def _retryable(exc: BaseException) -> bool:
    # Retrying a quota error only burns time and can worsen a per-minute limit.
    # GeminiRateLimitError is what the wrappers raise once they recognise a
    # quota response; it carries none of the attributes _is_rate_limit_error
    # looks for, so it is excluded explicitly.
    if isinstance(exc, (GeminiRateLimitError, ValueError)):
        return False
    return not _is_rate_limit_error(exc)


def _rate_limit_message(exc: BaseException) -> str:
    detail = str(exc).strip()
    return "Gemini rate limit or quota reached. Wait a moment and retry." + (f" Details: {detail}" if detail else "")


_retry = retry(
    retry=retry_if_exception(_retryable),
    stop=stop_after_attempt(3),
    wait=wait_exponential(min=1, max=8),
    reraise=True,
)


@_retry
def _embed_uncached(text: str) -> tuple[float, ...]:
    from google.genai import types

    settings = get_settings()
    try:
        resp = get_client().models.embed_content(
            model=settings.gemini_embedding_model,
            contents=text,
            config=types.EmbedContentConfig(output_dimensionality=settings.embedding_dim),
        )
    except Exception as exc:
        if _is_rate_limit_error(exc):
            raise GeminiRateLimitError(_rate_limit_message(exc)) from exc
        raise
    return tuple(resp.embeddings[0].values)


@functools.lru_cache(maxsize=4096)
def _embed_cached(text: str) -> tuple[float, ...]:
    return _embed_uncached(text)


def embed_text(text: str) -> Vector:
    """Always returns a pgvector.Vector, ready to bind directly into a query -
    callers never touch a raw list or worry about the dumper gotcha."""
    return Vector(list(_embed_cached(text)))


@_retry
def generate_text(prompt: str, *, json_mode: bool = False,
                  attachments: list[tuple[bytes, str]] | None = None) -> str:
    """Plain generation. attachments = [(bytes, mime_type)], e.g. a PDF."""
    from google.genai import types

    settings = get_settings()
    contents = prompt
    if attachments:
        parts = [types.Part.from_bytes(data=data, mime_type=mime) for data, mime in attachments]
        contents = [types.Content(role="user", parts=[*parts, types.Part.from_text(text=prompt)])]
    config = types.GenerateContentConfig(response_mime_type="application/json") if json_mode else None
    try:
        response = get_client().models.generate_content(
            model=settings.gemini_generation_model, contents=contents, config=config,
        )
    except Exception as exc:
        if _is_rate_limit_error(exc):
            raise GeminiRateLimitError(_rate_limit_message(exc)) from exc
        raise
    if response.text is None:
        reason = getattr(getattr(response, "candidates", [None])[0], "finish_reason", "unknown") \
            if getattr(response, "candidates", None) else "unknown"
        raise ValueError(f"Gemini returned no text (finish_reason={reason}).")
    return response.text.strip()


def parse_json_loose(raw: str):
    """Parse a model's JSON reply, tolerating fences, surrounding prose and
    trailing commas. Raises ValueError with the raw text if nothing parses."""
    cleaned = raw.strip()
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned)
    for candidate in (cleaned, _outermost_json(cleaned)):
        if not candidate:
            continue
        for attempt in (candidate, re.sub(r",\s*([}\]])", r"\1", candidate)):
            try:
                return json.loads(attempt)
            except json.JSONDecodeError:
                continue
    raise ValueError(f"Model did not return valid JSON. Raw output:\n{raw[:2000]}")


def _outermost_json(value: str) -> str | None:
    starts = [i for i in (value.find("{"), value.find("[")) if i >= 0]
    if not starts:
        return None
    start = min(starts)
    end = max(value.rfind("}"), value.rfind("]"))
    return value[start:end + 1] if end > start else None


def generate_json(prompt: str, *, attachments: list[tuple[bytes, str]] | None = None):
    """For structured-extraction prompts. The prompt should still say "return
    ONLY raw JSON"; JSON mode plus loose parsing are the safety net."""
    return parse_json_loose(generate_text(prompt, json_mode=True, attachments=attachments))
