# Phase 4 Implementation Guide — Q&A Assistant / Answer Bank
### CareerOS build spec, written to be handed to an AI coding assistant one section at a time

---

## 0. Context this assistant needs (don't skip)

Phases 1–3 are done. This phase reuses, and closes a loop on, work already built:

- `app/llm/gemini_client.py` — `embed_text`, `generate_text`, `generate_json`. Everything here goes through these.
- `app/services/kb_sync.py` — `sync_kb_chunk` / `delete_kb_chunk`. **This phase finally uses the `qa_answer` content_type slot** that was defined in the schema back in Phase 1 but never populated (Phase 1's embedding-text template table listed it as "built later, in Phase 4" — this is that moment). Every approved Q&A pair becomes a real `kb_chunk` row, so it's retrievable by *other* features too (e.g. a fact you only ever mentioned in a Q&A answer can still surface during resume tailoring).
- `kb_chunk` retrieval pattern from Phase 1: prefilter by `content_type`, then vector search.
- Role resolution + the `role` table from Phase 2.
- `DEFAULT_USER_ID`, the no-hallucination rule, and the "insufficient info → ask, don't invent" principle from earlier phases — this phase is where that principle gets its most literal implementation yet.

**One naming reconciliation, so there's no confusion between the original plan and the actual schema:** the early PRD described Q&A categories as `reusable` / `job_specific`. When we built the actual schema in Phase 0, this was refined to three categories: **`reusable`** (works for any application), **`role_specific`** (tends to repeat for a role type — e.g. an SRE incident-response question), and **`company_specific`** (tied to one JD/company only). The schema's `qa_category` enum already has exactly these three values. Use them — don't reintroduce `job_specific`.

Two distinct things get embedded in this phase, for two different purposes — don't conflate them:
- **`qa_question.embedding`** — used to find "have I answered a similar *question* before?" (question-to-question matching, tiered retrieval, §2).
- **A `kb_chunk` row per approved answer** (`content_type='qa_answer'`) — used so an approved Q&A pair can surface as *context* for unrelated retrieval later (e.g. resume tailoring pulling in a detail you only ever wrote down in a Q&A answer). Both matter; skipping the second one leaves a real KB fact unreachable to every other feature.

---

## 1. Question intake

Two entry points, both funneled through one normalizer, because raw pasted text is almost never one clean question per line — application forms paste as numbered lists, paragraphs with instructions mixed in ("Please answer in under 200 words: Tell me about..."), or several questions run together.

`app/services/qa_intake.py`

```python
"""Turns messy pasted text (or a JD's raw text) into a clean list of
discrete question strings. Handles numbered lists, inline instructions,
and paragraph-style multi-question blobs uniformly."""
from app.llm.gemini_client import generate_json

NORMALIZE_PROMPT = """
Extract the individual application/interview questions from the text below.
The text may be a clean single question, a numbered list, or a messy blob
mixing questions with instructions (e.g. word limits). Strip instructions
and formatting - return ONLY the actual questions being asked, one per item.
If the text contains no actual questions, return an empty list.

Return ONLY raw JSON: {{"questions": ["question 1", "question 2"]}}

TEXT:
<<<{raw_text}>>>
"""

def normalize_questions(raw_text: str) -> list[str]:
    if not raw_text.strip():
        return []
    result = generate_json(NORMALIZE_PROMPT.format(raw_text=raw_text))
    return [q.strip() for q in result.get("questions", []) if q.strip()]


def extract_questions_from_jd(session, jd_id) -> list[str]:
    """Some postings embed application questions directly in the JD text
    itself. Best-effort - most JDs won't have any, which is fine."""
    from sqlalchemy import text
    row = session.execute(
        text("SELECT raw_text FROM job_description WHERE id = :id"), {"id": jd_id}
    ).first()
    if not row:
        return []
    return normalize_questions(row[0])
```

**UI entry points** (both live on `frontend/pages/12_Application_QA.py`, built in §6): a text area for pasting questions directly, and an "Extract questions from this JD" button next to the JD picker that calls `extract_questions_from_jd`. Both produce the same `list[str]` that feeds into §2.

---

## 2. Retrieval against the `qa_answer` bank — confidence-tiered

Don't treat this as one flat similarity search. Check the cheapest, most-likely-to-be-right source first, and only widen the search when narrower tiers come up empty. This is what makes the milestone check's "auto-resolve without generation" actually happen.

Add to `.env.example` / `Settings`:
```
QA_AUTO_RESOLVE_THRESHOLD=0.82   # confident enough to fill in silently, no confirmation needed
QA_SUGGEST_THRESHOLD=0.70        # similar enough to suggest, but user must confirm before reuse
QA_MIN_CONTEXT_SIMILARITY=0.35   # below this, don't even bother calling Gemini to generate - go straight to "insufficient info"
```

`app/services/qa_retrieval.py`

```python
"""Confidence-tiered retrieval against previously approved Q&A pairs.
Tier 1 (reusable, high confidence) -> auto-fill, no user confirmation, no
    Gemini generation call at all. This is the tier the milestone check
    is measuring.
Tier 2 (company_specific for THIS jd, or role_specific for THIS role) ->
    still high-confidence because it's scoped tightly.
Tier 3 (anything else for this user) -> surfaced as a suggestion the user
    must confirm before reuse - not auto-filled, because cross-context
    reuse is more likely to need editing.
Tier 4: nothing cleared the bar -> caller falls through to generation (§3).
"""
import uuid
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.llm.gemini_client import embed_text
from app.config import get_settings

settings = get_settings()


def find_bank_match(
    session: Session,
    *,
    user_id: uuid.UUID,
    question_text: str,
    jd_id: uuid.UUID | None = None,
    role_id: uuid.UUID | None = None,
) -> dict | None:
    """Returns {"tier": 1|2|3, "confidence": float, "answer_text": str,
    "style": str, "question_id": uuid} or None if nothing cleared the
    lowest bar."""
    query_vec = embed_text(question_text)

    # Tier 1: reusable bank, high threshold
    hit = _best_match(session, user_id, query_vec, category="reusable")
    if hit and hit["similarity"] >= settings.qa_auto_resolve_threshold:
        return {**hit, "tier": 1, "confidence": hit["similarity"]}

    # Tier 2: scoped to this specific JD or role
    if jd_id:
        hit = _best_match(session, user_id, query_vec, category="company_specific", jd_id=jd_id)
        if hit and hit["similarity"] >= settings.qa_auto_resolve_threshold:
            return {**hit, "tier": 2, "confidence": hit["similarity"]}
    if role_id:
        hit = _best_match(session, user_id, query_vec, category="role_specific", role_id=role_id)
        if hit and hit["similarity"] >= settings.qa_auto_resolve_threshold:
            return {**hit, "tier": 2, "confidence": hit["similarity"]}

    # Tier 3: anything for this user, lower bar, needs user confirmation
    hit = _best_match(session, user_id, query_vec, category=None)
    if hit and hit["similarity"] >= settings.qa_suggest_threshold:
        return {**hit, "tier": 3, "confidence": hit["similarity"]}

    return None


def _best_match(session, user_id, query_vec, *, category=None, jd_id=None, role_id=None) -> dict | None:
    conditions = ["q.user_id = :user_id", "a.status = 'approved'"]
    params = {"user_id": user_id, "q": query_vec}
    if category:
        conditions.append("q.category = :category")
        params["category"] = category
    if jd_id:
        conditions.append("q.jd_id = :jd_id")
        params["jd_id"] = jd_id
    if role_id:
        conditions.append("q.role_id = :role_id")
        params["role_id"] = role_id

    row = session.execute(
        text(f"""
            SELECT a.id, a.answer_text, a.style, q.id AS question_id,
                   1 - (q.embedding <=> :q) AS similarity
            FROM qa_question q
            JOIN qa_answer a ON a.question_id = q.id
            WHERE {' AND '.join(conditions)}
            ORDER BY q.embedding <=> :q
            LIMIT 1;
        """),
        params,
    ).first()
    if not row:
        return None
    return {"answer_id": row[0], "answer_text": row[1], "style": row[2],
            "question_id": row[3], "similarity": row[4]}
```

**Acceptance check:** save one approved `reusable` answer manually (a quick INSERT is fine for this test), then call `find_bank_match` with a *reworded* version of the same question (not identical text) and confirm it returns a Tier 1 hit — this proves it's doing semantic matching, not string matching.

---

## 3. Multi-style generation — grounded, with the insufficient-info fallback

`app/services/qa_generation.py`

Two layers of protection against generic, hallucinated, or hollow answers:
1. **A cheap pre-check before ever calling Gemini**: if the best KB context match is below `QA_MIN_CONTEXT_SIMILARITY`, don't bother generating — go straight to "insufficient info." No API call wasted on a question the KB clearly can't answer well.
2. **An explicit instruction inside the prompt** as a second safety net, for cases where some context exists but isn't actually specific enough (the cheap check can't catch every case — a question can retrieve a "sort of related" chunk that still isn't enough to answer honestly).

```python
"""Generates grounded, multi-style answer drafts for a question that didn't
resolve from the bank. Two-layer protection against generic/hallucinated
answers: a retrieval-confidence pre-check, and an explicit instruction to
the model to self-report insufficient context rather than write filler.
"""
import uuid
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.llm.gemini_client import embed_text, generate_json
from app.config import get_settings

settings = get_settings()

GENERATION_PROMPT = """
You are drafting answers to a job application question, grounded strictly
in the candidate's actual background provided below as CONTEXT.

RULES:
1. Base every specific claim (project names, technologies, outcomes, dates,
   company names) ONLY on the CONTEXT. Never invent details.
2. Never use generic filler that could apply to anyone ("I'm a hard worker
   who loves challenges"). Every sentence should be traceable to something
   specific in the CONTEXT.
3. If the CONTEXT does not contain enough specific, concrete detail to
   answer this question honestly and specifically, do NOT write a vague
   answer. Instead return insufficient_info status with a short, specific
   clarifying question asking the candidate for exactly what's missing.

{jd_context_block}

CONTEXT (candidate's actual background):
{kb_context}

QUESTION: {question_text}

Return ONLY raw JSON in exactly this shape:
{{
  "status": "ok" or "insufficient_info",
  "clarifying_question": null or "specific question asking for the missing detail",
  "variants": [
    {{"style": "concise", "text": "2-3 sentence direct answer"}},
    {{"style": "detailed_star", "text": "fuller answer using Situation/Task/Action/Result structure where applicable"}},
    {{"style": "conversational", "text": "natural, spoken-register answer, as if said out loud in an interview"}}
  ]
}}
(if status is "insufficient_info", "variants" should be an empty list)
"""


def generate_answer_variants(
    session: Session,
    *,
    user_id: uuid.UUID,
    question_text: str,
    jd_id: uuid.UUID | None = None,
) -> dict:
    query_vec = embed_text(question_text)

    context_rows = session.execute(
        text("""
            SELECT text_for_embedding, 1 - (embedding <=> :q) AS similarity
            FROM kb_chunk
            WHERE user_id = :user_id
              AND content_type IN ('experience', 'project', 'skill', 'basic_info', 'qa_answer')
            ORDER BY embedding <=> :q
            LIMIT 8;
        """),
        {"q": query_vec, "user_id": user_id},
    ).fetchall()

    best_similarity = context_rows[0][1] if context_rows else 0.0
    if best_similarity < settings.qa_min_context_similarity:
        return {
            "status": "insufficient_info",
            "clarifying_question": "I don't have enough in your profile to answer this well yet — "
                                    "can you tell me more, or answer this one yourself?",
            "variants": [],
        }

    kb_context = "\n".join(f"- {row[0]}" for row in context_rows)

    jd_context_block = ""
    if jd_id:
        jd_row = session.execute(
            text("SELECT company, role_title FROM job_description WHERE id = :id"), {"id": jd_id}
        ).first()
        if jd_row:
            jd_context_block = f"This answer is for an application to {jd_row[0]} for the {jd_row[1]} role."

    result = generate_json(GENERATION_PROMPT.format(
        jd_context_block=jd_context_block,
        kb_context=kb_context,
        question_text=question_text,
    ))
    return result
```

**Why the JD-context block matters and also why it's dangerous:** a question like "why do you want to join [Company]" needs the company name to even make sense, but your KB has no genuine opinion about that company stored anywhere. Passing the company name alone will tempt the model to write generic corporate flattery that sounds plausible but isn't actually grounded in anything you believe. Watch this specific question type closely in the milestone check — it's the one most likely to need the insufficient-info path even when retrieval similarity looks fine, because the *question itself* needs information (your actual motivation) that nothing in this system can know unless you've written it down somewhere. If this keeps happening, the right fix is letting the user pre-write a few company-specific motivation notes as `company_specific` reusable-style facts, not tuning the prompt to guess harder.

**Acceptance check:** ask a question your KB genuinely can't answer (e.g. something about a technology you've never mentioned anywhere) and confirm it returns `insufficient_info` rather than a smooth-sounding but empty answer.

---

## 4. Approve/save flow with category tagging

`app/services/qa_save_service.py`

```python
"""Persists an approved answer: creates/updates the qa_question row (with
its own embedding for future retrieval), inserts the qa_answer row, and -
this is the loop-closing step from Phase 1 - syncs it into kb_chunk so
other features can retrieve this fact too.
"""
import uuid
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.llm.gemini_client import embed_text
from app.services.kb_sync import sync_kb_chunk


def suggest_category(question_text: str, jd_id: uuid.UUID | None) -> str:
    """Cheap heuristic default - always shown to the user as a preselected
    but overridable choice, never auto-saved without their confirmation."""
    lowered = question_text.lower()
    if jd_id and any(w in lowered for w in ["this role", "this position", "join us", "our company", "why do you want"]):
        return "company_specific"
    if any(w in lowered for w in ["describe a time", "tell me about a challenge", "how do you handle", "walk me through"]):
        return "role_specific"
    return "reusable"


def save_approved_answer(
    session: Session,
    *,
    user_id: uuid.UUID,
    question_text: str,
    answer_text: str,
    style: str,
    category: str,               # user-confirmed, from suggest_category() or overridden
    jd_id: uuid.UUID | None = None,
    role_id: uuid.UUID | None = None,
    existing_question_id: uuid.UUID | None = None,
) -> uuid.UUID:
    question_id = existing_question_id or uuid.uuid4()
    if not existing_question_id:
        q_embedding = embed_text(question_text)
        session.execute(
            text("""
                INSERT INTO qa_question (id, user_id, jd_id, role_id, question_text, category, embedding)
                VALUES (:id, :user_id, :jd_id, :role_id, :question_text, :category, :embedding);
            """),
            {"id": question_id, "user_id": user_id,
             "jd_id": jd_id if category == "company_specific" else None,
             "role_id": role_id if category == "role_specific" else None,
             "question_text": question_text, "category": category, "embedding": q_embedding},
        )

    answer_id = uuid.uuid4()
    session.execute(
        text("""
            INSERT INTO qa_answer (id, question_id, answer_text, style, status)
            VALUES (:id, :question_id, :answer_text, :style, 'approved');
        """),
        {"id": answer_id, "question_id": question_id, "answer_text": answer_text, "style": style},
    )

    # Closes the Phase 1 placeholder: approved QA pairs become retrievable
    # KB context for every other feature, not just this one.
    sync_kb_chunk(
        session, user_id=user_id, source_table="qa_answer", source_id=answer_id,
        content_type="qa_answer",
        text_for_embedding=f"Q: {question_text}\nA: {answer_text}",
    )

    session.commit()
    return answer_id
```

**Regenerate semantics — decide this explicitly:** regenerating never mutates or deletes an existing *approved* answer. It only ever produces new **draft** `qa_answer` rows for the user to review (multiple `qa_answer` rows per `qa_question` are allowed by the schema on purpose). Approving a new variant simply inserts another approved answer — your answer history for a question is never silently overwritten.

---

## 5. One-click copy + regenerate UI

Streamlit's `st.code(...)` renders a built-in copy-to-clipboard icon automatically — no custom JS/component needed for the "one-click copy" requirement. Use it for every finalized answer:

```python
st.code(answer_text, language=None)  # copy icon appears automatically in the top-right
```

Regenerate is just a button that discards the current *draft* variants shown on screen and re-calls `generate_answer_variants` — optionally with a short free-text hint the user can type ("make it more concise", "focus more on the leadership angle") appended to the question before regenerating, so users can steer without editing the underlying prompt code.

---

## 6. The Application Q&A page

`frontend/pages/12_Application_QA.py`

Page flow:
1. JD picker (same pattern as Phase 3's resume page).
2. Question intake: a text area for pasting, or an "Extract from this JD" button (§1) — both populate a working list of questions in `st.session_state`.
3. For each question, in order:
   - Call `find_bank_match` (§2) first.
     - **Tier 1/2 hit**: show the answer pre-filled with a small badge — `"Auto-filled from your answer bank (94% match)"` — no generation call made.
     - **Tier 3 hit**: show the answer with a badge — `"Similar answer found (76% match) — reuse as-is, edit, or generate fresh?"` — three explicit buttons, none of them auto-applied.
     - **No hit**: call `generate_answer_variants` (§3) and show either the three style variants as selectable options, or the insufficient-info prompt with a text input for the user to supply the missing detail (then a "try again" button that re-runs generation with that detail appended to the question context).
   - An editable text area holding whatever the user currently has selected/is editing.
   - A category `st.selectbox`, preselected via `suggest_category` (§4), always overridable.
   - "Approve & Save" → calls `save_approved_answer`.
   - Once saved: `st.code(...)` block for one-click copy (§5), plus a "Regenerate" button.
4. Optional nice-to-have, low effort: a "Copy all" section at the bottom that concatenates every approved Q&A pair for this session into one `st.code` block, for portals where you're filling many fields at once.

---

## 7. Cross-cutting checklist

- [ ] A Tier 1 bank hit never triggers a call to `generate_answer_variants` — verify by temporarily logging/printing whenever generation actually runs, and confirm it stays silent for genuinely repeat questions
- [ ] Every approved `qa_answer` produces a `kb_chunk` row with `content_type='qa_answer'` — spot-check with `SELECT count(*) FROM kb_chunk WHERE content_type = 'qa_answer';` after a few approvals
- [ ] `qa_question.embedding` is populated at save time, not left null — Tier 1–3 retrieval depends entirely on it
- [ ] The insufficient-info path is reachable and actually fires for at least one real question in testing — if it never fires, the pre-check threshold or prompt instruction likely needs tightening, not loosening
- [ ] Regenerating never overwrites or deletes an already-approved `qa_answer` row
- [ ] Category defaults from `suggest_category` are shown as overridable, never silently auto-applied

---

## 8. Milestone check — the actual Phase 4 exit criteria

There's no need for a dedicated analytics table for this — a simple print/log line inside `find_bank_match` and `generate_answer_variants` ("AUTO-RESOLVED tier=1 sim=0.89" vs. "GENERATED FRESH") is enough observability to eyeball this as you go.

Use the feature for real, across **5 real applications** (roughly 15–20 total questions once you account for overlap — most applications share 3–5 common questions: "tell me about yourself," "describe a challenge," "why this field," etc.).

**Phase 4 is done when:**
- By the 5th application, the common introductory/behavioral questions ("tell me about yourself," biodata-style questions, your standard "describe a project" prompt) are resolving at **Tier 1 — auto-filled, no generation call** — because you approved good answers to their first occurrences and the bank is doing its job.
- Company-specific questions ("why [Company]") are still correctly triggering fresh generation or the insufficient-info path each time — if these started falsely auto-resolving from the bank, something is scoped wrong in §2 (they should almost never hit Tier 1/2 across different companies).
- You've hit the insufficient-info path at least once and it asked a specific, useful clarifying question rather than either fabricating an answer or giving an unhelpfully vague "I don't know."
- The `kb_chunk` table has real `qa_answer` rows in it, confirming the Phase 1 loop is actually closed.

---

## 9. Suggested build order

1. `qa_intake.py` (§1) → test `normalize_questions` on a real messy pasted block from an actual application form
2. `qa_retrieval.py` (§2) → acceptance check with a manually-inserted approved answer + a reworded query
3. `qa_generation.py` (§3) → test both a question the KB can answer well and one it genuinely can't, confirm the insufficient-info path fires correctly on the latter
4. `qa_save_service.py` (§4) → save one answer, confirm both the `qa_question`/`qa_answer` rows AND the `kb_chunk` row exist
5. Streamlit page (§6), wiring all three services together
6. Cross-cutting audit (§7)
7. Real usage across 5 applications + milestone check (§8) — this is the actual finish line, and it's the one phase where "done" is measured over days of real use, not a single test run
