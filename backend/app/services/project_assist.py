"""Draft a project's summary, key points and short resume bullets from what is
already written about it. Suggestions only: nothing is saved until the user
reviews and applies them.

Guards, because a resume bullet with an invented metric is worse than none:
  * Every number in a suggestion must appear in the project's own text.
    Anything else is dropped - "reduced latency by 40%" cannot appear unless
    40% was already written down.
  * Resume bullets are measured with the same font metrics the resume uses
    (resume_layout), and any that would wrap are flagged.
"""
from __future__ import annotations

import re

from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.llm.gemini_client import generate_json
from app.services import kb_service as kb
from app.services.resume_layout import BULLET_MAX_CHARS, BULLET_MIN_CHARS, bullet_fits_one_line


class BulletCheck(BaseModel):
    text: str
    chars: int
    fits_one_line: bool


class ProjectSuggestion(BaseModel):
    summary: str | None = None
    key_points: list[str] = []
    resume_bullets: list[BulletCheck] = []
    dropped: list[str] = []           # suggestions removed by the number guard


PROMPT = """\
You are helping a candidate write up ONE of their own projects for their
knowledge base and resume. Use ONLY the material below. Never add a
technology, metric, user count, outcome or feature that is not written here.

PROJECT: {title}
SUMMARY ON FILE: {summary}
CANDIDATE'S ROLE: {role}
TECH STACK: {tech}
DESCRIPTION:
{description}
KEY POINTS ON FILE:
{key_points}

Produce:
  "summary": one sentence, under 25 words: what it is and what it does.
  "key_points": 3-6 detailed points - concrete things built, how, and any
       result that is WRITTEN above. Each 1-2 sentences.
  "resume_bullets": 3-4 resume lines. Each starts with a past-tense verb
       (Built, Designed, Implemented, Reduced...), names the technology, and
       is {min_chars}-{max_chars} characters INCLUDING spaces. Count them.
       No filler ("robust", "cutting-edge", "leveraged", "seamless").

Return ONLY raw JSON, no fences:
{{"summary": "...", "key_points": ["..."], "resume_bullets": ["..."]}}
"""

# A number with an optional scale suffix: 500,000 / 500K / 1M+ / 2.5k / 99.77
_NUMBER = re.compile(r"(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s*([kKmMbB])?(?![a-zA-Z])")
_SCALE = {"k": 1e3, "m": 1e6, "b": 1e9}


def _numbers(value: str) -> set[float]:
    """Numeric VALUES, so notation does not matter: the source's "500,000"
    and a suggestion's "500K" are the same fact. (Comparing strings dropped a
    perfectly grounded bullet for exactly this.)"""
    out = set()
    for digits, suffix in _NUMBER.findall(value or ""):
        n = float(digits.replace(",", ""))
        out.add(round(n * _SCALE.get(suffix.lower(), 1) if suffix else n, 6))
    return out


def grounded(candidate: str, source_numbers: set[str]) -> bool:
    """True if every number in `candidate` is present in the source text."""
    return _numbers(candidate) <= source_numbers


def suggest_project_content(session: Session, *, user_id, project_id) -> ProjectSuggestion:
    p = kb.get_item(session, "project", user_id=user_id, item_id=project_id)
    source_text = " ".join([p.title, p.summary or "", p.role or "", p.description or "",
                            " ".join(p.key_points), " ".join(p.tech_stack)])
    if len(source_text.strip()) < 40:
        raise ValueError("There is too little written about this project to draft from. "
                         "Add a description or a few key points first.")

    raw = generate_json(PROMPT.format(
        title=p.title, summary=p.summary or "(none)", role=p.role or "(not stated)",
        tech=", ".join(p.tech_stack) or "(not recorded)",
        description=p.description or "(none)",
        key_points="\n".join(f"- {k}" for k in p.key_points) or "(none)",
        min_chars=BULLET_MIN_CHARS, max_chars=BULLET_MAX_CHARS,
    ))
    if not isinstance(raw, dict):
        raise ValueError("The model did not return a usable suggestion.")

    nums = _numbers(source_text)
    dropped: list[str] = []

    def keep(values) -> list[str]:
        out = []
        for v in values if isinstance(values, list) else []:
            v = re.sub(r"\s+", " ", str(v or "")).strip().lstrip("-•* ")
            if not v:
                continue
            if grounded(v, nums):
                out.append(v)
            else:
                dropped.append(v)
        return out

    summary = str(raw.get("summary") or "").strip() or None
    if summary and not grounded(summary, nums):
        dropped.append(summary)
        summary = None
    bullets = keep(raw.get("resume_bullets"))
    return ProjectSuggestion(
        summary=summary,
        key_points=keep(raw.get("key_points")),
        resume_bullets=[BulletCheck(text=b, chars=len(b), fits_one_line=bullet_fits_one_line(b))
                        for b in bullets],
        dropped=dropped,
    )


def check_bullets(bullets: list[str]) -> list[BulletCheck]:
    """Line-fit check for bullets the user typed, for live feedback in the UI."""
    return [BulletCheck(text=b, chars=len(b), fits_one_line=bullet_fits_one_line(b))
            for b in bullets if b and b.strip()]
