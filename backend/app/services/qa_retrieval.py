"""Confidence-tiered retrieval against the approved answer bank.

  Tier 1  reusable answer, high similarity         -> auto-fill, no Gemini call
  Tier 2  company_specific for the SAME company,   -> auto-fill, no Gemini call
          or role_specific for the SAME role
  Tier 3  anything eligible above a lower bar      -> suggest; the user confirms
  None    nothing cleared the bar                  -> caller generates

Rules, each fixing a failure that was reproduced against real embeddings:

  * ONE embedding and ONE candidate query per question; every tier is decided
    in classify_candidates (pure, unit-tested).

  * Company-specific answers NEVER cross companies - not auto-filled, not
    suggested. Eligibility is applied in SQL *before* the LIMIT: filtering
    afterwards meant that after ~20 applications, 20 other companies'
    "why do you want to join us?" answers filled every slot and the right
    company's answer was never seen. The company is stored on the question
    (qa_question.company), so deleting a JD no longer orphans its answers.

  * Personal facts need a near-exact match. "What is your current CTC?" and
    "What is your expected CTC?" embed at 0.90 - above the auto-fill bar -
    and auto-filling one with the other ships a wrong number unseen. A fact
    question auto-fills only via a confirmed alias or at >= qa_fact_exact_threshold.

  * Motivation answers never auto-fill from the reusable bank - judged on the
    banked question's kind as well as the incoming one, because "What makes
    you want to work here?" can be saved as reusable under a different kind.

  * Matching covers learned ALIASES as well as the banked text. Paraphrases
    score 0.66-0.90 and different questions up to 0.64, so no threshold
    separates them; confirming a suggestion stores the wording as an alias,
    which then matches at ~1.0.
"""
from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import get_settings
from app.llm.gemini_client import embed_text
from app.services.qa_intake import KIND_MOTIVATION, KIND_PERSONAL_FACT, guess_kind

log = logging.getLogger("careeros.qa")

CANDIDATE_POOL = 20

_LEGAL_SUFFIX = re.compile(
    r"(?:[\s,]+(?:inc|incorporated|llc|l\.l\.c|ltd|limited|pvt|private|corp|corporation|co|"
    r"plc|gmbh|ag|sa|bv|llp|pte)\.?)+\s*$",
    re.IGNORECASE,
)
# Descriptors that only count as noise when they sit directly before a legal
# suffix ("Razorpay Software Pvt Ltd"). On their own they are part of the name:
# "Zeta Global" and "Zeta" are different employers, as are "Tata
# Technologies" and "Tata Group".
_DESCRIPTOR_BEFORE_LEGAL = re.compile(
    r"(?:\s+(?:software|technologies|technology|tech|solutions|services|systems|labs|"
    r"india|global|consulting|digital))+\s*$",
    re.IGNORECASE,
)


def normalize_company(name: str | None) -> str:
    """Identity for "is this the same employer?". Conservative on purpose: a
    false merge leaks one company's answer into another's application."""
    if not name:
        return ""
    n = name.strip().lower()
    stripped = _LEGAL_SUFFIX.sub("", n)
    if stripped != n:
        stripped = _DESCRIPTOR_BEFORE_LEGAL.sub("", stripped) or stripped
    key = re.sub(r"[^a-z0-9]", "", stripped)
    return key or re.sub(r"[^a-z0-9]", "", n)


@dataclass
class BankCandidate:
    question_id: uuid.UUID
    question_text: str
    category: str
    similarity: float
    answer_id: uuid.UUID
    answer_text: str
    style: str
    role_id: uuid.UUID | None = None
    jd_id: uuid.UUID | None = None
    company: str | None = None
    times_reused: int = 0
    matched_alias: str | None = None     # set when the hit came via a learned alias
    kind: str | None = None              # the banked question's kind

    @property
    def effective_kind(self) -> str:
        return self.kind or guess_kind(self.question_text)


@dataclass
class BankMatch:
    tier: int                      # 1, 2 or 3
    candidate: BankCandidate
    reason: str

    @property
    def confidence(self) -> float:
        return self.candidate.similarity

    @property
    def auto_fill(self) -> bool:
        return self.tier in (1, 2)


@dataclass
class RetrievalResult:
    match: BankMatch | None
    query_vector: object                         # pgvector Vector, reused by generation
    alternatives: list[BankCandidate] = field(default_factory=list)
    company: str | None = None                   # of the target JD
    role_id: uuid.UUID | None = None             # of the target JD

    def same_scope(self, candidate: BankCandidate) -> bool:
        return in_scope(candidate.category, candidate.company, candidate.role_id,
                        company=self.company, role_id=self.role_id)


def in_scope(category: str, q_company: str | None, q_role, *, company: str | None, role_id) -> bool:
    """May an answer for (company, role_id) be filed under / taken from a
    banked question with this category and scope?"""
    if category == "reusable":
        return True
    if category == "company_specific":
        return bool(company) and normalize_company(q_company) == normalize_company(company)
    return role_id is not None and q_role is not None and str(q_role) == str(role_id)


def classify_candidates(
    candidates: list[BankCandidate],
    *,
    company: str | None,
    role_id: uuid.UUID | None,
    kind: str | None,
    auto_threshold: float,
    suggest_threshold: float,
    fact_threshold: float = 0.97,
) -> tuple[BankMatch | None, list[BankCandidate]]:
    """Pick the best tier from a similarity-ordered candidate list. Pure."""
    def eligible(c: BankCandidate) -> bool:
        if c.category == "company_specific":
            return in_scope(c.category, c.company, c.role_id, company=company, role_id=role_id)
        return True

    pool = [c for c in candidates if eligible(c)]

    auto: list[BankMatch] = []
    for c in pool:
        if c.similarity < auto_threshold:
            continue
        banked_kind = c.effective_kind
        is_fact = KIND_PERSONAL_FACT in (kind, banked_kind)
        if is_fact and not c.matched_alias and c.similarity < fact_threshold:
            continue        # "current CTC" must not auto-fill "expected CTC"
        if c.category == "reusable":
            if KIND_MOTIVATION in (kind, banked_kind):
                continue    # suggested below, never auto-filled
            auto.append(BankMatch(1, c, "reusable answer"))
        elif c.category == "company_specific":
            auto.append(BankMatch(2, c, f"answered before for {c.company}"))
        elif c.category == "role_specific" and in_scope(c.category, c.company, c.role_id,
                                                       company=company, role_id=role_id):
            auto.append(BankMatch(2, c, "answered before for this role"))

    if auto:
        best = max(auto, key=lambda m: (m.candidate.similarity, -m.tier))
        alternatives = [c for c in pool if c.answer_id != best.candidate.answer_id]
        return best, alternatives[:3]

    suggestions = [c for c in pool if c.similarity >= suggest_threshold]
    if suggestions:
        best_c = suggestions[0]
        why = {
            "reusable": "similar reusable answer",
            "role_specific": "answered for this role"
                             if in_scope("role_specific", None, best_c.role_id, company=None, role_id=role_id)
                             else "answered for a different role",
            "company_specific": f"answered before for {best_c.company}",
        }.get(best_c.category, "similar answer")
        return BankMatch(3, best_c, why), suggestions[1:4]

    return None, pool[:3]


def eligible_company_question_ids(session: Session, *, user_id, company: str | None) -> list[uuid.UUID]:
    """company_specific questions that belong to `company`. Small list; the
    normalization runs in Python so SQL and Python can never disagree."""
    if not company:
        return []
    target = normalize_company(company)
    rows = session.execute(
        text("""SELECT q.id, COALESCE(q.company, jd.company) FROM qa_question q
                LEFT JOIN job_description jd ON jd.id = q.jd_id
                WHERE q.user_id = :u AND q.category = 'company_specific'"""),
        {"u": user_id},
    ).fetchall()
    return [qid for qid, c in rows if normalize_company(c) == target]


def fetch_candidates(session: Session, *, user_id: uuid.UUID, query_vector,
                     company: str | None = None, limit: int = CANDIDATE_POOL) -> list[BankCandidate]:
    """Top-N eligible banked questions by similarity - via their own text or
    any learned alias - each with its newest approved answer."""
    eligible = eligible_company_question_ids(session, user_id=user_id, company=company)
    rows = session.execute(
        text("""
            WITH scope AS (
                SELECT q.id FROM qa_question q
                WHERE q.user_id = :uid
                  AND (q.category <> 'company_specific' OR q.id = ANY(CAST(:eligible AS uuid[])))
            ),
            hits AS (
                (SELECT q.id AS question_id, 1 - (q.embedding <=> :v) AS similarity,
                        NULL::text AS matched_alias
                 FROM qa_question q JOIN scope s ON s.id = q.id
                 WHERE q.embedding IS NOT NULL
                 ORDER BY q.embedding <=> :v
                 LIMIT :limit)
                UNION ALL
                (SELECT al.question_id, 1 - (al.embedding <=> :v), al.alias_text
                 FROM qa_question_alias al JOIN scope s ON s.id = al.question_id
                 ORDER BY al.embedding <=> :v
                 LIMIT :limit)
            ),
            best AS (
                SELECT DISTINCT ON (question_id) question_id, similarity, matched_alias
                FROM hits
                ORDER BY question_id, similarity DESC
            )
            SELECT q.id AS question_id, q.question_text, q.category::text AS category,
                   q.role_id, q.jd_id, COALESCE(q.company, jd.company) AS company, q.kind,
                   b.similarity, b.matched_alias,
                   a.id AS answer_id, a.answer_text, a.style::text AS style, a.times_reused
            FROM best b
            JOIN qa_question q ON q.id = b.question_id
            LEFT JOIN job_description jd ON jd.id = q.jd_id
            JOIN LATERAL (
                SELECT id, answer_text, style, times_reused, created_at
                FROM qa_answer
                WHERE question_id = q.id AND status = 'approved'
                ORDER BY created_at DESC
                LIMIT 1
            ) a ON true
            ORDER BY b.similarity DESC, a.created_at DESC
            LIMIT :limit
        """),
        {"v": query_vector, "uid": user_id, "limit": limit,
         "eligible": [str(i) for i in eligible]},
    ).mappings().fetchall()
    return [BankCandidate(**{k: v for k, v in dict(r).items()}) for r in rows]


def find_bank_match(
    session: Session,
    *,
    user_id: uuid.UUID,
    question_text: str,
    kind: str | None = None,
    jd_id: uuid.UUID | None = None,
    query_vector=None,
) -> RetrievalResult:
    """Look the question up in the bank. Embeds it once (unless a vector is
    passed in) and returns the vector for reuse by generation and saving."""
    settings = get_settings()
    vector = query_vector if query_vector is not None else embed_text(question_text)

    company, role_id = None, None
    if jd_id:
        row = session.execute(
            text("SELECT company, role_id FROM job_description WHERE id = :id AND user_id = :uid"),
            {"id": jd_id, "uid": user_id},
        ).first()
        if row:
            company, role_id = row[0], row[1]

    candidates = fetch_candidates(session, user_id=user_id, query_vector=vector, company=company)
    match, alternatives = classify_candidates(
        candidates, company=company, role_id=role_id, kind=kind,
        auto_threshold=settings.qa_auto_resolve_threshold,
        suggest_threshold=settings.qa_suggest_threshold,
        fact_threshold=settings.qa_fact_exact_threshold,
    )

    # The milestone check reads these: common questions should drift from
    # "MISS" to "tier=1" across the first few applications.
    if match:
        log.info("QA bank tier=%s sim=%.3f auto=%s alias=%s q=%r", match.tier, match.confidence,
                 match.auto_fill, bool(match.candidate.matched_alias), question_text[:80])
    else:
        best = candidates[0].similarity if candidates else 0.0
        log.info("QA bank MISS best_sim=%.3f q=%r", best, question_text[:80])

    return RetrievalResult(match=match, query_vector=vector, alternatives=alternatives,
                           company=company, role_id=role_id)


def mark_reused(session: Session, *, user_id: uuid.UUID, answer_id: uuid.UUID) -> None:
    """Count a reuse - only for the caller's own answer."""
    session.execute(
        text("""UPDATE qa_answer a SET times_reused = a.times_reused + 1, last_used_at = now()
                FROM qa_question q
                WHERE a.id = :id AND q.id = a.question_id AND q.user_id = :u"""),
        {"id": answer_id, "u": user_id},
    )
    session.commit()
