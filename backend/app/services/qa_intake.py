"""Turns messy pasted text (or a JD's raw text) into clean, discrete questions.

Application forms paste badly: numbered lists, instructions mixed into the
question ("In under 200 words, tell us..."), several questions run together.
Everything funnels through normalize_questions(), which returns one
IntakeQuestion per real question.

Two things the original design dropped, both kept here:

  * The LIMIT. "Answer in under 200 words" is an instruction to strip from the
    question text, but it is also a hard constraint on the answer. It is kept
    on the IntakeQuestion and passed to generation.

  * The KIND. Measured on the real KB, a question the candidate can answer
    ("describe a challenging project", best KB similarity 0.59) and one they
    cannot ("embedded firmware in Rust", 0.55) score almost identically, so
    retrieval similarity cannot decide whether to generate. What the question
    ASKS for can: salary, notice period, visa status and "why this company"
    are facts no amount of KB retrieval can supply. See KIND_* below.
"""
from __future__ import annotations

import re
import uuid
from dataclasses import asdict, dataclass

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.llm.gemini_client import generate_json

# What a question asks for. Drives both generation and category defaults.
KIND_BACKGROUND = "background"        # tell me about yourself, your education, career summary
KIND_BEHAVIORAL = "behavioral"        # describe a time you..., how do you handle...
KIND_TECHNICAL = "technical"          # experience with X, how would you design Y
KIND_MOTIVATION = "motivation"        # why this company / role / team
KIND_PERSONAL_FACT = "personal_fact"  # salary, notice period, visa, relocation, availability
KIND_OTHER = "other"

KINDS = (KIND_BACKGROUND, KIND_BEHAVIORAL, KIND_TECHNICAL,
         KIND_MOTIVATION, KIND_PERSONAL_FACT, KIND_OTHER)

# Kinds whose answer is a fact about the candidate that is not in the KB.
# Generation is skipped for these unless the user supplies the fact.
UNGROUNDABLE_KINDS = frozenset({KIND_PERSONAL_FACT, KIND_MOTIVATION})


@dataclass
class IntakeQuestion:
    text: str
    kind: str = KIND_OTHER
    word_limit: int | None = None
    char_limit: int | None = None
    # Stable within a session so Streamlit widgets keep their state across reruns.
    key: str = ""

    def __post_init__(self) -> None:
        if not self.key:
            self.key = uuid.uuid4().hex[:10]

    def to_dict(self) -> dict:
        return asdict(self)


NORMALIZE_PROMPT = """\
Extract the individual application or interview questions from the text below.

The text may be one clean question, a numbered list, or a messy blob mixing
questions with instructions. For each real question:

  - "text": the question itself, with numbering, bullets and instructions
    removed. Keep the question's own wording otherwise. Do not merge two
    questions into one, and do not split one question into two.
  - "word_limit": if an instruction caps the answer in WORDS ("under 200
    words", "max 150 words", "in 100 words or less"), that number, else null.
  - "char_limit": if an instruction caps the answer in CHARACTERS, that
    number, else null.
  - "kind": exactly one of
        background     - about the candidate overall: tell me about yourself,
                         your education, summarise your career
        behavioral     - a specific past situation: describe a time you...,
                         how do you handle conflict / pressure / failure
        technical      - skills, tools, systems, design: experience with X,
                         how would you build Y
        motivation     - why THIS company, role or team; what excites you
                         about us; why are you leaving
        personal_fact  - a logistics fact only the candidate knows: salary or
                         CTC, notice period, start date, visa or work
                         authorisation, relocation, willingness to travel,
                         links, years of experience as a number
        other          - anything else

If the text contains no questions at all (e.g. it is a job description with no
application questions in it), return an empty list. Do not invent questions
from a job description's requirements.

Return ONLY raw JSON, no markdown fences:
{{"questions": [{{"text": "...", "word_limit": null, "char_limit": null, "kind": "background"}}]}}

TEXT:
<<<{raw_text}>>>
"""

# ── Cheap, deterministic fallbacks ────────────────────────────────────────────
# Used for single-question input (no Gemini call needed) and to fill in
# anything the model leaves out.

# Phrasings seen on real forms. The single-question path never calls Gemini,
# so these regexes are the only thing that finds a limit there.
_LIMIT_LEAD = (r"(?:under|max(?:imum)?\.?|up\s+to|within|no\s+more\s+than|not\s+more\s+than|"
               r"at\s+most|less\s+than|not\s+exceed(?:ing)?|limit(?:ed)?\s+to|in)")
_WORD_LIMIT_RE = re.compile(
    _LIMIT_LEAD + r"\s*(?:of\s*)?(\d{2,4})\s*words?"
    r"|(\d{2,4})\s*words?\s*(?:or\s+(?:less|fewer)|max(?:imum)?|limit)"
    r"|word\s+limit\s*[:\-]?\s*(\d{2,4})"
    r"|\d{2,4}\s*[-–]\s*(\d{2,4})\s*words?"          # "150-200 words" -> 200
    r"|\(\s*(\d{2,4})\s*words?\s*\)",                # "(200 words)"
    re.IGNORECASE,
)
_CHAR_LIMIT_RE = re.compile(
    _LIMIT_LEAD + r"\s*(\d{2,5})\s*char(?:acter)?s?"
    r"|(\d{2,5})\s*char(?:acter)?s?\s*(?:or\s+(?:less|fewer)|max(?:imum)?|limit)"
    r"|char(?:acter)?\s+limit\s*[:\-]?\s*(\d{2,5})"
    r"|\(\s*(\d{2,5})\s*char(?:acter)?s?\s*\)",
    re.IGNORECASE,
)

_KIND_PATTERNS: list[tuple[str, re.Pattern]] = [
    (KIND_PERSONAL_FACT, re.compile(
        r"\b(salary|ctc|compensation|expected pay|current pay|notice period|"
        r"join(?:ing)? date|start date|when can you (?:start|join)|visa|sponsorship|"
        r"work authori[sz]ation|authori[sz]ed to work|relocat\w*|willing to travel|"
        r"years of (?:professional )?experience do you have|linkedin|github profile|"
        r"portfolio (?:url|link)|date of birth|gender|nationality|pronouns)\b", re.I)),
    (KIND_MOTIVATION, re.compile(
        r"\b(why (?:do you want|would you like|are you interested|us|here|this (?:company|role|position|team))|"
        r"why (?:join|work (?:at|for|with|here))|"
        r"what (?:excites|attracts|interests|draws|motivates) you|"
        r"what makes you (?:want|excited|interested)|want to work here|"
        r"why are you (?:leaving|looking)|what do you know about (?:us|our))\b"
        # "Why Razorpay?" - a bare "why" plus a capitalised name and nothing else.
        r"|^\s*why\s+[A-Z][\w&.\- ]{1,40}\?\s*$", re.I)),
    (KIND_BEHAVIORAL, re.compile(
        r"\b(describe a (?:time|situation)|tell (?:me|us) about a (?:time|situation|challenge|conflict|failure|mistake)|"
        r"give an example|how do you (?:handle|deal|manage|prioriti[sz]e)|"
        r"a time when|biggest (?:challenge|failure|mistake|achievement)|proudest)\b", re.I)),
    (KIND_BACKGROUND, re.compile(
        r"\b(tell (?:me|us) about yourself|introduce yourself|walk (?:me|us) through your (?:resume|background|career)|"
        r"describe yourself|your background|summari[sz]e your)\b", re.I)),
    (KIND_TECHNICAL, re.compile(
        r"\b(experience (?:with|in|using)|how would you (?:design|build|implement|scale|debug)|"
        r"explain how|what is your approach to|familiar with|proficien)\b", re.I)),
]


def guess_kind(question: str) -> str:
    for kind, pattern in _KIND_PATTERNS:
        if pattern.search(question):
            return kind
    return KIND_OTHER


def extract_limits(raw: str) -> tuple[int | None, int | None]:
    words = chars = None
    if m := _WORD_LIMIT_RE.search(raw):
        words = int(next(g for g in m.groups() if g))
    if m := _CHAR_LIMIT_RE.search(raw):
        chars = int(next(g for g in m.groups() if g))
    return words, chars


_NUMBERING_RE = re.compile(r"^\s*(?:\(?\d{1,2}[.)\]:]|[-*•]|q\d{1,2}[.):]?)\s*", re.I)
# Built from the same vocabulary as the limit regexes, so whatever is
# extracted as a limit is also stripped from the question text.
_INSTRUCTION_RE = re.compile(
    r"[\s,;:.\-]*[\(\[]?\s*(?:please\s+)?(?:answer|respond|write|keep\s+it)?\s*"
    r"(?:(?:in\s+)?" + _LIMIT_LEAD + r"\s*(?:of\s*)?\d{2,5}\s*(?:[-–]\s*\d{2,5}\s*)?"
    r"(?:words?|char(?:acter)?s?)(?:\s+or\s+(?:less|fewer))?"
    r"|(?:word|char(?:acter)?)\s+limit\s*[:\-]?\s*\d{2,5}"
    r"|\d{2,5}\s*(?:[-–]\s*\d{2,5}\s*)?(?:words?|char(?:acter)?s?))"
    r"\s*[\)\]]?\s*[.:;,]?",
    re.IGNORECASE,
)


def _clean_single(raw: str) -> str:
    q = _NUMBERING_RE.sub("", raw.strip())
    q = _INSTRUCTION_RE.sub(" ", q)
    q = re.sub(r"\s+", " ", q)
    # Removing "(max 150 words)" leaves a space before the trailing "?".
    q = re.sub(r"\s+([?.!,;:])", r"\1", q)
    return q.strip(" .:;,-")


def _looks_like_single_question(raw: str) -> bool:
    stripped = raw.strip()
    return (
        "\n" not in stripped
        and len(stripped) <= 400
        and stripped.count("?") <= 1
    )


def normalize_questions(raw_text: str) -> list[IntakeQuestion]:
    """Split pasted text into questions, keeping limits and kind.

    A single short question skips Gemini entirely - it is by far the most
    common input and there is nothing to split.
    """
    if not raw_text or not raw_text.strip():
        return []

    if _looks_like_single_question(raw_text):
        words, chars = extract_limits(raw_text)
        cleaned = _clean_single(raw_text)
        if not cleaned:
            return []
        if not cleaned.endswith("?") and raw_text.strip().endswith("?"):
            cleaned += "?"
        return [IntakeQuestion(text=cleaned, kind=guess_kind(cleaned),
                               word_limit=words, char_limit=chars)]

    result = generate_json(NORMALIZE_PROMPT.format(raw_text=raw_text.strip()[:12000]))
    items = result.get("questions") if isinstance(result, dict) else None
    return _coerce_items(items or [])


def _coerce_items(items: list) -> list[IntakeQuestion]:
    """Validate the model's output instead of trusting it."""
    out: list[IntakeQuestion] = []
    seen: set[str] = set()
    for item in items:
        if isinstance(item, str):
            item = {"text": item}
        if not isinstance(item, dict):
            continue
        q = re.sub(r"\s+", " ", str(item.get("text") or "")).strip()
        if len(q) < 5:
            continue
        fingerprint = re.sub(r"[^a-z0-9]", "", q.lower())
        if fingerprint in seen:
            continue
        seen.add(fingerprint)

        kind = str(item.get("kind") or "").strip().lower()
        if kind not in KINDS:
            kind = guess_kind(q)
        # A deterministic hit on a personal fact outranks the model: the cost
        # of generating a made-up salary expectation is far higher than the
        # cost of asking.
        if guess_kind(q) == KIND_PERSONAL_FACT:
            kind = KIND_PERSONAL_FACT

        word_limit = _positive_int(item.get("word_limit"))
        char_limit = _positive_int(item.get("char_limit"))
        if word_limit is None and char_limit is None:
            word_limit, char_limit = extract_limits(q)

        out.append(IntakeQuestion(text=q, kind=kind, word_limit=word_limit, char_limit=char_limit))
    return out


def _positive_int(value) -> int | None:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def extract_questions_from_jd(session: Session, *, user_id: uuid.UUID,
                              jd_id: uuid.UUID) -> list[IntakeQuestion]:
    """Application questions embedded in a JD's own text. Best-effort: most
    JDs have none, and the prompt is told not to invent any from requirements."""
    row = session.execute(
        text("SELECT raw_text FROM job_description WHERE id = :id AND user_id = :uid"),
        {"id": jd_id, "uid": user_id},
    ).first()
    if not row or not row[0]:
        return []
    # Force the multi-question path: a JD is never "one short question".
    result = generate_json(NORMALIZE_PROMPT.format(raw_text=row[0].strip()[:12000]))
    items = result.get("questions") if isinstance(result, dict) else None
    return _coerce_items(items or [])
