"""The answer bank: save approved answers, and browse / edit / delete them.

Saving closes the loop Phase 1 left open: approved answers become kb_chunk
rows (content_type='qa_answer'), so a fact the candidate only ever wrote in an
application answer is retrievable by every other feature too.

Policies, each decided explicitly (several after a reproduced failure):

  * ONE LIVE CHUNK PER QUESTION. A question keeps every approved answer as
    history; only the newest-CREATED one is live (in retrieval and kb_chunk).
    Editing an answer corrects it in place and never changes which one is
    live - ranking by updated_at meant fixing a typo in an old version
    silently rolled the bank back to it.

  * COMPANY-SPECIFIC ANSWERS STAY OUT OF kb_chunk. kb_chunk is unscoped
    context for every feature, so "why Razorpay" stored there surfaced in the
    generation prompt for a Google application.

  * NO DUPLICATE QUESTIONS. A near-identical question (>= 0.95, by its own
    text or any alias, and in the same scope - filtered in SQL, not after a
    LIMIT) gets a new answer version instead of a new question.

  * FILING UNDER AN EXISTING QUESTION IS CHECKED. existing_question_id must
    belong to the caller, have the same category, and match the target scope
    (same company / same role). A stale UI that switched applications would
    otherwise file company B's answer under company A's question.

  * CONFIRMED PARAPHRASES BECOME ALIASES, so that wording auto-fills next time.

  * Timestamps use clock_timestamp(): now() is fixed per transaction, so two
    answers written in one transaction would tie for "newest".

  * Every read and write is scoped to the caller's user_id.
"""
from __future__ import annotations

import re
import uuid

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.llm.gemini_client import embed_text
from app.services.kb_sync import delete_kb_chunk, sync_kb_chunk
from app.services.qa_intake import KIND_MOTIVATION, KIND_PERSONAL_FACT, guess_kind
from app.services.qa_retrieval import eligible_company_question_ids, in_scope

CATEGORIES = ("reusable", "role_specific", "company_specific")
STYLES = ("concise", "detailed_star", "conversational")
DUPLICATE_QUESTION_SIMILARITY = 0.95

_ROLE_WORDS = re.compile(r"\b(this|the) (role|position|team|job)\b", re.I)
_COMPANY_WORDS = re.compile(
    r"\b(join us|our (company|team|mission|product)|why (us|here)|about us|work here|"
    r"why do you want to (work|join)|work (at|for) (us|our))\b", re.I)


def suggest_category(question: str, *, kind: str | None, jd_id: uuid.UUID | None) -> str:
    """Pre-selected default. The user always confirms it before saving.

    Behavioral questions default to REUSABLE: "describe a challenge" is asked
    by nearly every employer, and the Phase 4 milestone is exactly that such
    questions auto-resolve at Tier 1 by the fifth application.
    """
    effective = kind or guess_kind(question)
    if jd_id and (effective == KIND_MOTIVATION or _COMPANY_WORDS.search(question)):
        return "company_specific"
    if effective == KIND_PERSONAL_FACT:
        return "reusable"
    if jd_id and _ROLE_WORDS.search(question):
        return "role_specific"
    return "reusable"


def _chunk_text(question: str, answer: str) -> str:
    return f"Q: {question.strip()}\nA: {answer.strip()}"


def _fingerprint(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (value or "").lower())


def _jd_scope(session: Session, user_id, jd_id):
    if not jd_id:
        return None, None
    row = session.execute(
        text("SELECT company, role_id FROM job_description WHERE id = :id AND user_id = :u"),
        {"id": jd_id, "u": user_id},
    ).first()
    return (row[0], row[1]) if row else (None, None)


def find_duplicate_question(
    session: Session, *, user_id, question_vector, category: str,
    company: str | None, role_id,
) -> uuid.UUID | None:
    """An existing question in the same scope that is essentially this one,
    matched on its own text or any learned alias. Scope is filtered in SQL so
    other companies' identical questions cannot crowd it out."""
    eligible = eligible_company_question_ids(session, user_id=user_id, company=company) \
        if category == "company_specific" else []
    row = session.execute(
        text("""
            WITH scope AS (
                SELECT q.id FROM qa_question q
                WHERE q.user_id = :u AND q.category = CAST(:cat AS qa_category)
                  AND (:cat <> 'company_specific' OR q.id = ANY(CAST(:eligible AS uuid[])))
                  AND (:cat <> 'role_specific' OR q.role_id = CAST(:role AS uuid))
            ),
            hits AS (
                SELECT q.id, 1 - (q.embedding <=> :v) AS sim
                FROM qa_question q JOIN scope s ON s.id = q.id WHERE q.embedding IS NOT NULL
                UNION ALL
                SELECT al.question_id, 1 - (al.embedding <=> :v)
                FROM qa_question_alias al JOIN scope s ON s.id = al.question_id
            )
            SELECT id, max(sim) AS sim FROM hits GROUP BY id
            HAVING max(sim) >= :threshold
            ORDER BY sim DESC LIMIT 1
        """),
        {"u": user_id, "cat": category, "eligible": [str(i) for i in eligible],
         "role": str(role_id) if role_id else None, "v": question_vector,
         "threshold": DUPLICATE_QUESTION_SIMILARITY},
    ).first()
    return row[0] if row else None


def add_alias(session: Session, *, user_id, question_id, alias_text: str,
              alias_vector=None, commit: bool = True) -> bool:
    """Record `alias_text` as a confirmed paraphrase of a banked question.
    Returns False when it is just the question's own wording, or already known."""
    alias_text = re.sub(r"\s+", " ", alias_text or "").strip()
    row = session.execute(
        text("SELECT question_text FROM qa_question WHERE id = :q AND user_id = :u"),
        {"q": question_id, "u": user_id},
    ).first()
    if not row or not alias_text or _fingerprint(row[0]) == _fingerprint(alias_text):
        return False
    known = session.execute(
        text("SELECT alias_text FROM qa_question_alias WHERE question_id = :q"), {"q": question_id},
    ).fetchall()
    if any(_fingerprint(k[0]) == _fingerprint(alias_text) for k in known):
        return False
    vector = alias_vector if alias_vector is not None else embed_text(alias_text)
    session.execute(
        text("INSERT INTO qa_question_alias (question_id, alias_text, embedding) "
             "VALUES (:q, :t, :v) ON CONFLICT (question_id, alias_text) DO NOTHING"),
        {"q": question_id, "t": alias_text, "v": vector},
    )
    if commit:
        session.commit()
    return True


def _resync_live_chunk(session: Session, *, user_id, question_id) -> None:
    """Make kb_chunk hold exactly the live answer for this question - or
    nothing, for a company-specific question."""
    rows = session.execute(
        text("""
            SELECT a.id, a.answer_text, q.question_text, q.role_id, q.category::text
            FROM qa_answer a JOIN qa_question q ON q.id = a.question_id
            WHERE a.question_id = :q AND q.user_id = :u AND a.status = 'approved'
            ORDER BY a.created_at DESC
        """),
        {"q": question_id, "u": user_id},
    ).fetchall()
    if not rows:
        return
    category = rows[0][4]
    live = [] if category == "company_specific" else rows[:1]
    for stale in rows[len(live):]:
        delete_kb_chunk(session, source_table="qa_answer", source_id=stale[0])
    for answer_id, answer_text, question_text, role_id, _ in live:
        sync_kb_chunk(
            session, user_id=user_id, source_table="qa_answer", source_id=answer_id,
            content_type="qa_answer", text_for_embedding=_chunk_text(question_text, answer_text),
            role_tags=[role_id] if role_id else None,
        )


def _question_scope(session: Session, user_id, question_id):
    row = session.execute(
        text("""SELECT q.category::text, COALESCE(q.company, jd.company), q.role_id
                FROM qa_question q LEFT JOIN job_description jd ON jd.id = q.jd_id
                WHERE q.id = :q AND q.user_id = :u"""),
        {"q": question_id, "u": user_id},
    ).first()
    if not row:
        raise ValueError("That banked question no longer exists.")
    return row


def save_approved_answer(
    session: Session,
    *,
    user_id,
    question_text: str,
    answer_text: str,
    style: str,
    category: str,
    jd_id=None,
    existing_question_id=None,
    question_vector=None,
    kind: str | None = None,
) -> dict:
    """Persist an approved answer (and its chunk, if in scope). One commit.

    Returns {"answer_id", "question_id", "reused_question": bool}.
    """
    if category not in CATEGORIES:
        raise ValueError(f"category must be one of {CATEGORIES}")
    if style not in STYLES:
        raise ValueError(f"style must be one of {STYLES}")
    question_text = (question_text or "").strip()
    answer_text = (answer_text or "").strip()
    if not question_text or not answer_text:
        raise ValueError("Question and answer are both required.")

    company, role_id = _jd_scope(session, user_id, jd_id)
    if category == "company_specific" and not company:
        raise ValueError("A company-specific answer needs a job description with a company. "
                         "Pick one, or save it as reusable.")
    if category == "role_specific" and not role_id:
        raise ValueError("A role-specific answer needs a job description whose role was "
                         "resolved. Pick one, or save it as reusable.")

    question_id = existing_question_id
    vector = question_vector
    if question_id is not None:
        q_category, q_company, q_role = _question_scope(session, user_id, question_id)
        if q_category != category:
            raise ValueError(f"That banked question is {q_category.replace('_', '-')}; "
                             f"save this as {q_category.replace('_', '-')} or as a new question.")
        if not in_scope(q_category, q_company, q_role, company=company, role_id=role_id):
            raise ValueError("That banked answer belongs to a different "
                             + ("company" if q_category == "company_specific" else "role")
                             + " than this application.")
    else:
        vector = vector if vector is not None else embed_text(question_text)
        question_id = find_duplicate_question(
            session, user_id=user_id, question_vector=vector, category=category,
            company=company, role_id=role_id,
        )
    reused_question = question_id is not None

    if question_id is None:
        question_id = uuid.uuid4()
        session.execute(
            text("""
                INSERT INTO qa_question (id, user_id, jd_id, role_id, company, kind,
                                         question_text, category, embedding)
                VALUES (:id, :u, :jd, :role, :company, :kind, :q, CAST(:cat AS qa_category), :v)
            """),
            {"id": question_id, "u": user_id,
             "jd": jd_id if category == "company_specific" else None,
             "role": role_id if category == "role_specific" else None,
             "company": company if category == "company_specific" else None,
             "kind": kind or guess_kind(question_text),
             "q": question_text, "cat": category, "v": vector},
        )
    else:
        add_alias(session, user_id=user_id, question_id=question_id,
                  alias_text=question_text, alias_vector=vector, commit=False)

    answer_id = uuid.uuid4()
    session.execute(
        text("""
            INSERT INTO qa_answer (id, question_id, answer_text, style, status,
                                   created_at, updated_at)
            VALUES (:id, :q, :a, CAST(:s AS qa_style), 'approved',
                    clock_timestamp(), clock_timestamp())
        """),
        {"id": answer_id, "q": question_id, "a": answer_text, "s": style},
    )
    _resync_live_chunk(session, user_id=user_id, question_id=question_id)
    session.commit()
    return {"answer_id": answer_id, "question_id": question_id, "reused_question": reused_question}


# ── Bank management ──────────────────────────────────────────────────────────

def list_bank(session: Session, *, user_id, search: str | None = None,
              category: str | None = None) -> list[dict]:
    """One row per question, with its live answer."""
    clauses = ["q.user_id = :u"]
    params: dict = {"u": user_id}
    if category in CATEGORIES:
        clauses.append("q.category = CAST(:cat AS qa_category)")
        params["cat"] = category
    if search and search.strip():
        clauses.append("(q.question_text ILIKE :s OR a.answer_text ILIKE :s OR EXISTS ("
                       "SELECT 1 FROM qa_question_alias al WHERE al.question_id = q.id "
                       "AND al.alias_text ILIKE :s))")
        params["s"] = f"%{search.strip()}%"
    rows = session.execute(
        text(f"""
            SELECT q.id AS question_id, q.question_text, q.category::text AS category, q.kind,
                   q.created_at, COALESCE(q.company, jd.company) AS company,
                   r.canonical_name AS role_name,
                   a.id AS answer_id, a.answer_text, a.style::text AS style,
                   a.created_at AS answered_at, a.updated_at, a.times_reused, a.last_used_at,
                   (SELECT count(*) FROM qa_answer x
                     WHERE x.question_id = q.id AND x.status = 'approved') AS answer_count,
                   (SELECT coalesce(array_agg(al.alias_text ORDER BY al.created_at), '{{}}')
                     FROM qa_question_alias al WHERE al.question_id = q.id) AS aliases
            FROM qa_question q
            LEFT JOIN job_description jd ON jd.id = q.jd_id
            LEFT JOIN role r ON r.id = q.role_id
            JOIN LATERAL (
                SELECT id, answer_text, style, created_at, updated_at, times_reused, last_used_at
                FROM qa_answer WHERE question_id = q.id AND status = 'approved'
                ORDER BY created_at DESC LIMIT 1
            ) a ON true
            WHERE {' AND '.join(clauses)}
            ORDER BY a.updated_at DESC
        """),
        params,
    ).mappings().fetchall()
    return [dict(r) for r in rows]


def answer_history(session: Session, *, user_id, question_id) -> list[dict]:
    rows = session.execute(
        text("""
            SELECT a.id, a.answer_text, a.style::text AS style, a.created_at, a.updated_at,
                   a.times_reused
            FROM qa_answer a JOIN qa_question q ON q.id = a.question_id
            WHERE a.question_id = :q AND q.user_id = :u AND a.status = 'approved'
            ORDER BY a.created_at DESC
        """),
        {"q": question_id, "u": user_id},
    ).mappings().fetchall()
    return [dict(r) for r in rows]


def _owned_answer(session: Session, user_id, answer_id):
    row = session.execute(
        text("SELECT a.question_id FROM qa_answer a JOIN qa_question q ON q.id = a.question_id "
             "WHERE a.id = :a AND q.user_id = :u"),
        {"a": answer_id, "u": user_id},
    ).first()
    if not row:
        raise ValueError("Answer not found.")
    return row[0]


def update_answer(session: Session, *, user_id, answer_id, answer_text: str,
                  style: str | None = None) -> None:
    """Correct an answer in place and re-sync its chunk. Does not change which
    version is live."""
    if not (answer_text or "").strip():
        raise ValueError("Answer cannot be empty.")
    if style is not None and style not in STYLES:
        raise ValueError(f"style must be one of {STYLES}")
    question_id = _owned_answer(session, user_id, answer_id)
    session.execute(
        text("UPDATE qa_answer SET answer_text = :t, style = COALESCE(CAST(:s AS qa_style), style), "
             "updated_at = clock_timestamp() WHERE id = :a"),
        {"t": answer_text.strip(), "s": style, "a": answer_id},
    )
    _resync_live_chunk(session, user_id=user_id, question_id=question_id)
    session.commit()


def update_question_text(session: Session, *, user_id, question_id, question_text: str) -> None:
    """Reword a banked question: re-embeds it, keeps the old wording as an
    alias (it still matches), and re-syncs the live chunk."""
    question_text = re.sub(r"\s+", " ", question_text or "").strip()
    if not question_text:
        raise ValueError("Question cannot be empty.")
    row = session.execute(
        text("SELECT question_text, embedding FROM qa_question WHERE id = :q AND user_id = :u"),
        {"q": question_id, "u": user_id},
    ).first()
    if not row:
        raise ValueError("Question not found.")
    if _fingerprint(row[0]) == _fingerprint(question_text):
        return
    old_text, old_vec = row
    session.execute(
        text("UPDATE qa_question SET question_text = :t, embedding = :v, kind = :k "
             "WHERE id = :q AND user_id = :u"),
        {"t": question_text, "v": embed_text(question_text), "k": guess_kind(question_text),
         "q": question_id, "u": user_id},
    )
    session.execute(
        text("INSERT INTO qa_question_alias (question_id, alias_text, embedding) "
             "VALUES (:q, :t, :v) ON CONFLICT DO NOTHING"),
        {"q": question_id, "t": old_text, "v": old_vec},
    )
    _resync_live_chunk(session, user_id=user_id, question_id=question_id)
    session.commit()


def delete_alias(session: Session, *, user_id, question_id, alias_text: str) -> None:
    session.execute(
        text("""DELETE FROM qa_question_alias al USING qa_question q
                WHERE al.question_id = q.id AND q.id = :q AND q.user_id = :u
                  AND al.alias_text = :t"""),
        {"q": question_id, "u": user_id, "t": alias_text},
    )
    session.commit()


def delete_answer(session: Session, *, user_id, answer_id) -> bool:
    """Delete one answer version. Removes the question too if it was the last
    one; otherwise the next newest becomes live. Returns True if the question
    was removed."""
    question_id = _owned_answer(session, user_id, answer_id)
    delete_kb_chunk(session, source_table="qa_answer", source_id=answer_id)
    session.execute(text("DELETE FROM qa_answer WHERE id = :a"), {"a": answer_id})
    remaining = session.execute(
        text("SELECT count(*) FROM qa_answer WHERE question_id = :q"), {"q": question_id},
    ).scalar()
    if remaining == 0:
        session.execute(text("DELETE FROM qa_question WHERE id = :q AND user_id = :u"),
                        {"q": question_id, "u": user_id})
    else:
        _resync_live_chunk(session, user_id=user_id, question_id=question_id)
    session.commit()
    return remaining == 0


def delete_question(session: Session, *, user_id, question_id) -> None:
    """Delete a question, all its answers and aliases, and their chunks."""
    answer_ids = [r[0] for r in session.execute(
        text("SELECT a.id FROM qa_answer a JOIN qa_question q ON q.id = a.question_id "
             "WHERE a.question_id = :q AND q.user_id = :u"),
        {"q": question_id, "u": user_id},
    ).fetchall()]
    for aid in answer_ids:
        delete_kb_chunk(session, source_table="qa_answer", source_id=aid)
    session.execute(text("DELETE FROM qa_question WHERE id = :q AND user_id = :u"),
                    {"q": question_id, "u": user_id})
    session.commit()


def recategorize_to_reusable(session: Session, *, user_id, question_id) -> None:
    """Widen a ROLE-specific answer to reusable (e.g. "describe a challenge"
    first saved for one role).

    Company-specific answers cannot be widened: they are about one employer,
    and their aliases were confirmed inside that company's scope - widening
    made a Razorpay answer auto-fill on every other application.
    """
    q_category, _, _ = _question_scope(session, user_id, question_id)
    if q_category == "company_specific":
        raise ValueError("A company-specific answer can't be made reusable - it's about one "
                         "employer. Save a new reusable answer instead.")
    if q_category == "reusable":
        return
    session.execute(
        text("UPDATE qa_question SET category = 'reusable', jd_id = NULL, role_id = NULL "
             "WHERE id = :q AND user_id = :u"),
        {"q": question_id, "u": user_id},
    )
    _resync_live_chunk(session, user_id=user_id, question_id=question_id)
    session.commit()


def bank_stats(session: Session, *, user_id) -> dict:
    row = session.execute(
        text("""
            SELECT
              (SELECT count(*) FROM qa_question WHERE user_id = :u) AS questions,
              (SELECT count(*) FROM qa_answer a JOIN qa_question q ON q.id = a.question_id
                WHERE q.user_id = :u AND a.status = 'approved') AS answers,
              (SELECT coalesce(sum(a.times_reused), 0) FROM qa_answer a
                JOIN qa_question q ON q.id = a.question_id WHERE q.user_id = :u) AS reuses,
              (SELECT count(*) FROM kb_chunk WHERE user_id = :u AND content_type = 'qa_answer') AS kb_chunks,
              (SELECT count(*) FROM qa_question_alias al JOIN qa_question q ON q.id = al.question_id
                WHERE q.user_id = :u) AS aliases
        """),
        {"u": user_id},
    ).mappings().first()
    return dict(row)
