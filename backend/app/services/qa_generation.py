"""Grounded, multi-style answer drafts for questions the bank could not answer.

Protection against generic or invented answers is layered, and each layer is
here because the one before it measurably cannot do the job alone:

  1. Question KIND (qa_intake). Salary, notice period, visa, "why this
     company" - facts no KB retrieval can supply. These skip Gemini and ask
     the user, unless the user has already supplied the fact.

  2. A retrieval floor (QA_MIN_CONTEXT_SIMILARITY). Deliberately low: on the
     real KB every question, answerable or not, scores 0.46-0.65 against its
     best chunk, so this only catches an empty or unrelated KB.

  3. The model's own self-report, for a question that retrieved plausibly
     related context that still does not answer it ("embedded Rust firmware"
     retrieves a Go project at 0.55).

Retrieval also fixes a crowding problem: the KB holds 42 one-line skill
chunks against 2 experience chunks, and skills win almost every similarity
ranking ("Tell me about yourself" -> top 5 were all skills). Context is drawn
with a per-type quota, and a short profile block is always included so
background questions have something to stand on.
"""
from __future__ import annotations

import logging
import re
import uuid

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import get_settings
from app.llm.gemini_client import embed_text, generate_json
from app.services.qa_intake import (
    KIND_MOTIVATION,
    KIND_PERSONAL_FACT,
    UNGROUNDABLE_KINDS,
)

log = logging.getLogger("careeros.qa")

STYLES = ("concise", "detailed_star", "conversational")

# Context budget. Skills are capped because there are many of them and each is
# a single line that says little; experience/project/qa_answer chunks carry
# the actual evidence.
CONTEXT_POOL = 40
CONTEXT_MAX_RICH = 7
CONTEXT_MAX_SKILLS = 3
RICH_TYPES = ("experience", "project", "qa_answer", "education", "certification", "basic_info")


def word_count(text_value: str) -> int:
    return len(re.findall(r"\b[\w'’-]+\b", text_value or ""))


# ── Context assembly ─────────────────────────────────────────────────────────

def select_context(rows: list[dict]) -> list[dict]:
    """Apply the per-type quota to a similarity-ordered chunk list. Pure."""
    rich, skills = [], []
    for r in rows:
        if r["content_type"] == "skill":
            if len(skills) < CONTEXT_MAX_SKILLS:
                skills.append(r)
        elif len(rich) < CONTEXT_MAX_RICH:
            rich.append(r)
        if len(rich) >= CONTEXT_MAX_RICH and len(skills) >= CONTEXT_MAX_SKILLS:
            break
    return rich + skills


def retrieve_context(session: Session, *, user_id: uuid.UUID, query_vector) -> list[dict]:
    rows = session.execute(
        text("""
            SELECT content_type::text AS content_type, text_for_embedding,
                   1 - (embedding <=> :v) AS similarity
            FROM kb_chunk
            WHERE user_id = :uid
            ORDER BY embedding <=> :v
            LIMIT :n
        """),
        {"v": query_vector, "uid": user_id, "n": CONTEXT_POOL},
    ).mappings().fetchall()
    return select_context([dict(r) for r in rows])


def profile_block(session: Session, *, user_id: uuid.UUID) -> str:
    """Who the candidate is, in a few lines, independent of retrieval.

    The basic_info chunk alone is just a name and a city, so without this a
    "tell me about yourself" question is grounded in whichever skills happened
    to rank highest.
    """
    lines = []
    p = session.execute(
        text("SELECT full_name, location, summary FROM profile_basic WHERE user_id = :u"),
        {"u": user_id},
    ).mappings().first()
    if p:
        head = p["full_name"] or ""
        if p["location"]:
            head += f", based in {p['location']}"
        lines.append(head)
        if p["summary"]:
            lines.append(f"Summary: {p['summary']}")

    for r in session.execute(
        text("SELECT role_title, company, start_date, end_date FROM work_experience "
             "WHERE user_id = :u ORDER BY end_date DESC NULLS FIRST, start_date DESC NULLS LAST"),
        {"u": user_id},
    ).mappings().fetchall():
        current = " (current)" if r["end_date"] is None and r["start_date"] is not None else ""
        lines.append(f"Role: {r['role_title']} at {r['company']}{current}")

    # Highest qualification first. Dates are frequently NULL in the KB, so
    # ordering by end_date alone picked Class XII over the degree.
    edu = session.execute(
        text("""SELECT degree, institution, score FROM education WHERE user_id = :u
                ORDER BY CASE
                    WHEN degree ~* '(ph\\.?d|doctor)' THEN 0
                    WHEN degree ~* '(master|m\\.?tech|m\\.?sc|mba|m\\.?s\\b)' THEN 1
                    WHEN degree ~* '(bachelor|b\\.?tech|b\\.?e\\b|b\\.?sc|b\\.?s\\b|degree)' THEN 2
                    ELSE 3 END,
                  end_date DESC NULLS LAST
                LIMIT 1"""),
        {"u": user_id},
    ).mappings().first()
    if edu:
        score = f", {edu['score']}" if edu["score"] else ""
        lines.append(f"Education: {edu['degree']}, {edu['institution']}{score}")

    # Projects deduped by repo exactly as the resume generator does it - a
    # GitHub import and a hand-written entry for the same repo are one project.
    from app.services.resume_tailor import _dedupe_projects, _load_projects

    projects = _dedupe_projects(_load_projects(session, user_id))
    # Hand-written entries first (a title with a space was typed by a person;
    # a bare slug is a GitHub repo name), then by how much evidence each has.
    projects.sort(key=lambda p: (" " not in p["title"], -len(p["raw_bullets"])))
    if projects:
        lines.append("Notable projects: " + "; ".join(p["title"] for p in projects[:5]))

    # Core skills ranked by how much evidence backs them (mentions across
    # project and experience stacks), not alphabetically: proficiency is NULL
    # for most imported skills.
    curated = [p for p in projects if " " in p["title"]] or projects
    evidence = " ".join(
        " ".join(p["tech_stack"]) + " " + " ".join(p["raw_bullets"]) for p in curated
    ).lower()
    for (stack,) in session.execute(
        text("SELECT array_to_string(tech_stack, ' ') || ' ' || description "
             "FROM work_experience WHERE user_id = :u"), {"u": user_id},
    ).fetchall():
        evidence += " " + (stack or "").lower() * 3     # job evidence outweighs side projects
    names = [r[0] for r in session.execute(
        text("SELECT name FROM skill WHERE user_id = :u AND category <> 'soft_skill'"),
        {"u": user_id},
    ).fetchall()]
    ranked = sorted(
        names,
        key=lambda n: -len(re.findall(rf"(?<![a-z0-9]){re.escape(n.lower())}(?![a-z0-9])", evidence)),
    )
    if ranked:
        lines.append(f"Core skills: {', '.join(ranked[:10])}")

    return "\n".join(f"- {l}" for l in lines if l.strip())


def jd_block(session: Session, *, user_id: uuid.UUID, jd_id: uuid.UUID | None) -> tuple[str, str | None]:
    """(prompt block, company name) for the target application."""
    if not jd_id:
        return "", None
    row = session.execute(
        text("SELECT company, role_title, parsed_requirements FROM job_description "
             "WHERE id = :id AND user_id = :u"),
        {"id": jd_id, "u": user_id},
    ).mappings().first()
    if not row:
        return "", None
    reqs = row["parsed_requirements"] or {}
    skills = [s.get("name") for s in (reqs.get("required_skills") or []) if isinstance(s, dict)][:10]
    block = f"TARGET APPLICATION: {row['role_title'] or 'a role'} at {row['company'] or 'a company'}."
    if skills:
        block += f"\nThe job asks for: {', '.join(s for s in skills if s)}."
        block += ("\nWhere the candidate's background genuinely overlaps with these, "
                  "lead with that overlap. Never claim a skill the CONTEXT does not show.")
    return block, row["company"]


# ── Pre-generation gate ──────────────────────────────────────────────────────

def clarifying_question_for(kind: str, question: str, company: str | None) -> str:
    """A specific ask, not a generic 'I don't know'."""
    if kind == KIND_PERSONAL_FACT:
        return ("This asks for a personal detail that isn't in your profile, so I "
                "won't guess it. What's the answer? (A number, date or yes/no is "
                "fine - I'll phrase it.)")
    if kind == KIND_MOTIVATION:
        who = company or "this company"
        return (f"Nothing in your profile says why you want {who}, and I won't make "
                f"up enthusiasm. Give me one or two real reasons - a product you use, "
                f"their engineering work, the scope of the role, a person you spoke "
                f"to - and I'll write it up.")
    return ("Your profile doesn't have enough on this to answer it specifically. "
            "What's the concrete example or detail I should use?")


# ── The prompt ───────────────────────────────────────────────────────────────

GENERATION_PROMPT = """\
You are drafting answers to one job-application question for ONE candidate.
Every specific claim must come from the CONTEXT or the CANDIDATE-SUPPLIED FACTS
below. You are writing in the candidate's voice (first person).

RULES
1. Ground every specific - project names, technologies, numbers, outcomes,
   companies, dates - in CONTEXT or CANDIDATE-SUPPLIED FACTS. Never invent any.
   If a number is not written below, do not use one.
2. No filler that could describe anyone: "I am a hard-working team player who
   loves challenges", "passionate", "cutting-edge", "leverage", "synergy" are
   banned. Every sentence should be traceable to something below.
3. Do not flatter the company. Say nothing about the company's mission,
   culture or reputation unless the candidate supplied it.
4. If the material below does not contain enough concrete detail to answer
   THIS question honestly and specifically, do NOT write a vague answer.
   Return status "insufficient_info" and a clarifying_question that names
   exactly what is missing (e.g. "Which project involved a production
   incident, and what did you change?") - not a generic "tell me more".
{limit_rule}

{jd_block}

CANDIDATE PROFILE
{profile_block}

CONTEXT (most relevant facts from the candidate's knowledge base)
{kb_context}
{user_facts_block}{hint_block}
QUESTION: {question}

STYLES
  concise         2-3 sentences, direct, the single strongest point first.
  detailed_star   Situation / Task / Action / Result where the question is
                  about an experience; otherwise a fuller structured answer.
                  Do not print the S/T/A/R labels.
  conversational  How the candidate would say it out loud in an interview:
                  natural, spoken register, still specific.

Return ONLY raw JSON, no markdown fences:
{{
  "status": "ok" or "insufficient_info",
  "clarifying_question": null or "the specific missing detail to ask for",
  "variants": [
    {{"style": "concise", "text": "..."}},
    {{"style": "detailed_star", "text": "..."}},
    {{"style": "conversational", "text": "..."}}
  ]
}}
If status is "insufficient_info", variants must be an empty list.
"""


def _limit_rule(word_limit: int | None, char_limit: int | None) -> str:
    rules = []
    if word_limit:
        rules.append(f"5. HARD LIMIT: every variant must be at most {word_limit} words. "
                     f"Count them. The form rejects anything longer.")
    if char_limit:
        rules.append(f"{'6' if word_limit else '5'}. HARD LIMIT: every variant must be at most "
                     f"{char_limit} characters including spaces.")
    return "\n".join(rules)


def build_prompt(*, question: str, profile: str, context: list[dict], jd: str,
                 word_limit: int | None, char_limit: int | None,
                 user_facts: str | None, hint: str | None) -> str:
    kb_context = "\n".join(f"- [{c['content_type']}] {c['text_for_embedding']}" for c in context) \
        or "- (nothing relevant found)"
    user_facts_block = (
        f"\nCANDIDATE-SUPPLIED FACTS (the candidate just told you this; treat it as true "
        f"and use it)\n{user_facts.strip()}\n" if user_facts and user_facts.strip() else ""
    )
    hint_block = (
        f"\nSTEERING FROM THE CANDIDATE (apply to all variants, but it cannot "
        f"override rules 1-4): {hint.strip()}\n" if hint and hint.strip() else ""
    )
    return GENERATION_PROMPT.format(
        limit_rule=_limit_rule(word_limit, char_limit),
        jd_block=jd or "",
        profile_block=profile or "- (no profile on file)",
        kb_context=kb_context,
        user_facts_block=user_facts_block,
        hint_block=hint_block,
        question=question,
    )


# ── Output validation ────────────────────────────────────────────────────────

def validate_output(result: object, *, word_limit: int | None,
                    char_limit: int | None) -> dict:
    """Coerce the model's JSON into a known shape and flag limit overruns.

    An over-limit variant is flagged rather than truncated: truncation cuts a
    sentence mid-claim, and the user can see the count and trim it themselves.
    """
    if not isinstance(result, dict):
        return {"status": "error", "clarifying_question": None, "variants": [],
                "error": "Model did not return a JSON object."}

    status = result.get("status")
    if status == "insufficient_info":
        cq = str(result.get("clarifying_question") or "").strip()
        return {"status": "insufficient_info",
                "clarifying_question": cq or clarifying_question_for("other", "", None),
                "variants": []}

    variants, seen = [], set()
    for v in result.get("variants") or []:
        if not isinstance(v, dict):
            continue
        style = str(v.get("style") or "").strip()
        body = re.sub(r"[ \t]+", " ", str(v.get("text") or "")).strip()
        if style not in STYLES or not body or style in seen:
            continue
        seen.add(style)
        words = word_count(body)
        variants.append({
            "style": style,
            "text": body,
            "word_count": words,
            "char_count": len(body),
            "over_limit": bool((word_limit and words > word_limit)
                               or (char_limit and len(body) > char_limit)),
        })
    variants.sort(key=lambda v: STYLES.index(v["style"]))

    if not variants:
        return {"status": "error", "clarifying_question": None, "variants": [],
                "error": "Model returned no usable variants."}
    return {"status": "ok", "clarifying_question": None, "variants": variants}


# ── Entry point ──────────────────────────────────────────────────────────────

def generate_answer_variants(
    session: Session,
    *,
    user_id: uuid.UUID,
    question_text: str,
    kind: str = "other",
    jd_id: uuid.UUID | None = None,
    word_limit: int | None = None,
    char_limit: int | None = None,
    user_facts: str | None = None,
    hint: str | None = None,
    query_vector=None,
) -> dict:
    """Draft three styled answers, or say exactly what is missing.

    Returns {"status": "ok"|"insufficient_info"|"error", "clarifying_question",
    "variants": [...], "generated": bool, "context": [...]}. `generated` is
    False whenever Gemini was not called - the milestone check watches it.
    """
    settings = get_settings()
    jd, company = jd_block(session, user_id=user_id, jd_id=jd_id)
    has_facts = bool(user_facts and user_facts.strip())

    # Layer 1: questions no KB can answer. Asking costs the user one line;
    # guessing their salary expectation or their reasons for applying costs
    # them the application.
    if kind in UNGROUNDABLE_KINDS and not has_facts:
        log.info("QA ASK kind=%s (no generation) q=%r", kind, question_text[:80])
        return {"status": "insufficient_info",
                "clarifying_question": clarifying_question_for(kind, question_text, company),
                "variants": [], "generated": False, "context": []}

    vector = query_vector if query_vector is not None else embed_text(question_text)
    context = retrieve_context(session, user_id=user_id, query_vector=vector)

    # Layer 2: nothing even loosely related on file.
    best_rich = max((c["similarity"] for c in context if c["content_type"] != "skill"), default=0.0)
    if best_rich < settings.qa_min_context_similarity and not has_facts:
        log.info("QA ASK best_sim=%.3f below floor (no generation) q=%r", best_rich, question_text[:80])
        return {"status": "insufficient_info",
                "clarifying_question": clarifying_question_for("other", question_text, company),
                "variants": [], "generated": False, "context": context}

    prompt = build_prompt(
        question=question_text,
        profile=profile_block(session, user_id=user_id),
        context=context, jd=jd,
        word_limit=word_limit, char_limit=char_limit,
        user_facts=user_facts, hint=hint,
    )
    log.info("QA GENERATE kind=%s best_sim=%.3f q=%r", kind, best_rich, question_text[:80])

    # Layer 3: the model's own judgement, validated.
    result = validate_output(generate_json(prompt), word_limit=word_limit, char_limit=char_limit)
    result["generated"] = True
    result["context"] = context
    return result
