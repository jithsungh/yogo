"""Application Q&A: answer form questions from the bank, or draft grounded answers.

Every Gemini call happens inside a button callback. Streamlit reruns the whole
script on every interaction - typing in a box, switching tabs - so anything
that called the model from the main flow would call it again on every
keystroke. Results live in st.session_state["qa_items"] and are only ever
recomputed when a button asks for it.
"""
import sys
from pathlib import Path

_backend = str(Path(__file__).resolve().parent.parent.parent / "backend")
if _backend not in sys.path:
    sys.path.insert(0, _backend)

import streamlit as st
from sqlalchemy import text

from app.config import get_settings
from app.db import get_session
from app.llm.gemini_client import GeminiRateLimitError
from app.services.qa_bank_service import (
    CATEGORIES,
    add_alias,
    answer_history,
    bank_stats,
    delete_answer,
    delete_question,
    list_bank,
    recategorize_to_reusable,
    save_approved_answer,
    suggest_category,
    update_answer,
)
from app.services.qa_generation import generate_answer_variants, word_count
from app.services.qa_intake import IntakeQuestion, extract_questions_from_jd, normalize_questions
from app.services.qa_retrieval import find_bank_match, mark_reused

st.set_page_config(page_title="Application Q&A - CareerOS", page_icon="💬", layout="wide")
st.title("💬 Application Q&A")

user_id = get_settings().default_user_id
if not user_id:
    st.error("DEFAULT_USER_ID is not configured. Bootstrap the default user first.")
    st.stop()

STYLE_LABELS = {"concise": "Concise", "detailed_star": "Detailed (STAR)",
                "conversational": "Conversational"}
CATEGORY_LABELS = {"reusable": "Reusable — any application",
                   "role_specific": "Role-specific — this role type",
                   "company_specific": "Company-specific — this company only"}
KIND_LABELS = {"background": "background", "behavioral": "behavioral", "technical": "technical",
               "motivation": "motivation", "personal_fact": "personal fact", "other": "other"}

state = st.session_state
state.setdefault("qa_items", [])


# ══════════════════════════════════════════════════════════════════════════════
# Actions (button callbacks — the only place Gemini is called)
# ══════════════════════════════════════════════════════════════════════════════

def _item(key: str) -> dict | None:
    return next((i for i in state["qa_items"] if i["key"] == key), None)


def _set_draft(item: dict, text_value: str, style: str) -> None:
    item["draft_style"] = style
    # Writing the widget's key before it renders is how a callback changes
    # what a text_area shows.
    state[f"{item['key']}_draft"] = text_value


def _new_item(q: IntakeQuestion, jd_id) -> dict:
    return {**q.to_dict(), "status": "new", "match": None, "variants": [],
            "clarifying_question": None, "generated": False, "context_count": 0,
            "user_facts": "", "vector": None, "draft_style": "concise",
            "category": suggest_category(q.text, kind=q.kind, jd_id=jd_id),
            "saved": None, "reuse_counted": False, "error": None}


def add_questions(raw: str, jd_id) -> None:
    try:
        questions = normalize_questions(raw)
    except GeminiRateLimitError as exc:
        state["qa_flash"] = ("error", f"Gemini quota reached — {exc}")
        return
    if not questions:
        state["qa_flash"] = ("warning", "No questions found in that text.")
        return
    state["qa_items"].extend(_new_item(q, jd_id) for q in questions)
    state["qa_paste"] = ""
    state["qa_flash"] = ("success", f"Added {len(questions)} question(s). Press **Answer all**.")


def add_from_jd(jd_id) -> None:
    try:
        with get_session() as session:
            questions = extract_questions_from_jd(session, user_id=user_id, jd_id=jd_id)
    except GeminiRateLimitError as exc:
        state["qa_flash"] = ("error", f"Gemini quota reached — {exc}")
        return
    if not questions:
        state["qa_flash"] = ("info", "This job description doesn't contain any application "
                                     "questions. That's normal — paste them instead.")
        return
    state["qa_items"].extend(_new_item(q, jd_id) for q in questions)
    state["qa_flash"] = ("success", f"Found {len(questions)} question(s) in the JD.")


def _generate(item: dict, jd_id, *, hint: str | None = None) -> None:
    with get_session() as session:
        result = generate_answer_variants(
            session, user_id=user_id, question_text=item["text"], kind=item["kind"],
            jd_id=jd_id, word_limit=item["word_limit"], char_limit=item["char_limit"],
            user_facts=item["user_facts"] or None, hint=hint, query_vector=item["vector"],
        )
    item["generated"] = item["generated"] or result.get("generated", False)
    item["context_count"] = len(result.get("context") or [])
    if result["status"] == "ok":
        item["status"] = "generated"
        item["variants"] = result["variants"]
        item["clarifying_question"] = None
        first = result["variants"][0]
        _set_draft(item, first["text"], first["style"])
    elif result["status"] == "insufficient_info":
        item["status"] = "ask"
        item["variants"] = []
        item["clarifying_question"] = result["clarifying_question"]
    else:
        item["status"] = "error"
        item["error"] = result.get("error") or "Generation failed."


def process(key: str, jd_id) -> None:
    """Bank first; generate only on a miss."""
    item = _item(key)
    if not item:
        return
    item["error"] = None
    try:
        with get_session() as session:
            found = find_bank_match(session, user_id=user_id, question_text=item["text"],
                                    kind=item["kind"], jd_id=jd_id, query_vector=item["vector"])
            item["vector"] = found.query_vector
            if found.match:
                c = found.match.candidate
                item["match"] = {"tier": found.match.tier, "similarity": c.similarity,
                                 "reason": found.match.reason, "answer_id": c.answer_id,
                                 "question_id": c.question_id, "answer_text": c.answer_text,
                                 "style": c.style, "category": c.category,
                                 "bank_question": c.question_text,
                                 "via_alias": c.matched_alias,
                                 # May a new answer for this application be filed
                                 # under this banked question?
                                 "same_scope": found.same_scope(c)}
                if found.match.auto_fill:
                    item["status"] = "resolved"
                    _set_draft(item, c.answer_text, c.style)
                    if not item["reuse_counted"]:
                        mark_reused(session, answer_id=c.answer_id)
                        item["reuse_counted"] = True
                    return
                item["status"] = "suggested"
                _set_draft(item, c.answer_text, c.style)
                return
        _generate(item, jd_id)
    except GeminiRateLimitError as exc:
        item["status"], item["error"] = "error", f"Gemini quota reached — {exc}"
    except Exception as exc:  # noqa: BLE001 - shown on the card, not a crash
        item["status"], item["error"] = "error", f"{type(exc).__name__}: {exc}"


def process_all(jd_id) -> None:
    for item in list(state["qa_items"]):
        if item["status"] in ("new", "error"):
            process(item["key"], jd_id)


def generate_fresh(key: str, jd_id) -> None:
    item = _item(key)
    if not item:
        return
    item["error"] = None
    hint = state.get(f"{key}_hint") or None
    item["user_facts"] = state.get(f"{key}_facts", item["user_facts"]) or ""
    try:
        _generate(item, jd_id, hint=hint)
    except GeminiRateLimitError as exc:
        item["status"], item["error"] = "error", f"Gemini quota reached — {exc}"
    except Exception as exc:  # noqa: BLE001
        item["status"], item["error"] = "error", f"{type(exc).__name__}: {exc}"


def pick_variant(key: str) -> None:
    item = _item(key)
    style = state.get(f"{key}_variant")
    variant = next((v for v in item["variants"] if v["style"] == style), None)
    if variant:
        _set_draft(item, variant["text"], variant["style"])


def reuse_as_is(key: str) -> None:
    item = _item(key)
    if not item or not item["match"]:
        return
    with get_session() as session:
        if not item["reuse_counted"]:
            mark_reused(session, answer_id=item["match"]["answer_id"])
            item["reuse_counted"] = True
        # The user just confirmed "this is the same question". Remember the
        # wording, so next time it auto-fills at Tier 1 instead of asking.
        if item["match"]["same_scope"]:
            add_alias(session, user_id=user_id, question_id=item["match"]["question_id"],
                      alias_text=item["text"], alias_vector=item["vector"])
    item["status"] = "saved"
    item["saved"] = {"answer_id": item["match"]["answer_id"], "reused": True}


def approve(key: str, jd_id) -> None:
    item = _item(key)
    if not item:
        return
    body = (state.get(f"{key}_draft") or "").strip()
    category = state.get(f"{key}_category", item["category"])
    if not body:
        item["error"] = "The answer is empty."
        return
    # An edited suggestion is a new version of the SAME banked question, as
    # long as the user kept its category and the scope still fits.
    match = item["match"]
    existing = (match["question_id"] if match and item["status"] == "suggested"
                and match["same_scope"] and match["category"] == category else None)
    try:
        with get_session() as session:
            saved = save_approved_answer(
                session, user_id=user_id, question_text=item["text"], answer_text=body,
                style=item["draft_style"], category=category, jd_id=jd_id,
                existing_question_id=existing, question_vector=item["vector"],
            )
        item["status"], item["saved"], item["category"], item["error"] = "saved", saved, category, None
        item["final_text"] = body
    except (ValueError, GeminiRateLimitError) as exc:
        item["error"] = str(exc)


def reopen(key: str) -> None:
    item = _item(key)
    if item:
        item["status"] = "generated" if item["variants"] else ("suggested" if item["match"] else "ask")


def remove(key: str) -> None:
    state["qa_items"] = [i for i in state["qa_items"] if i["key"] != key]


def clear_all() -> None:
    state["qa_items"] = []


# ══════════════════════════════════════════════════════════════════════════════
# Page
# ══════════════════════════════════════════════════════════════════════════════

answer_tab, bank_tab = st.tabs(["Answer questions", "Answer bank"])

with answer_tab:
    with get_session() as session:
        jds = session.execute(
            text("SELECT id, company, role_title FROM job_description WHERE user_id = :u "
                 "ORDER BY created_at DESC"),
            {"u": user_id},
        ).fetchall()
    jd_options = {"— No specific application —": None}
    jd_options.update({f"{r[1] or 'Unknown'} — {r[2] or 'Unknown role'}": r[0] for r in jds})
    jd_label = st.selectbox("Application", list(jd_options),
                            help="Company-specific answers are scoped to this company, and "
                                 "role-specific ones to its role.")
    jd_id = jd_options[jd_label]

    with st.expander("Add questions", expanded=not state["qa_items"]):
        st.text_area("Paste one or more questions", key="qa_paste", height=150,
                     placeholder="1. Tell us about yourself (max 150 words)\n"
                                 "2. Why do you want to join us?\n"
                                 "3. Describe a challenging project.")
        c1, c2 = st.columns(2)
        c1.button("Add questions", type="primary", width="stretch",
                  on_click=lambda: add_questions(state.get("qa_paste", ""), jd_id))
        c2.button("Extract questions from this JD", width="stretch",
                  on_click=add_from_jd, args=(jd_id,), disabled=jd_id is None)

    if flash := state.pop("qa_flash", None):
        getattr(st, flash[0])(flash[1])

    items = state["qa_items"]
    if items:
        pending = sum(1 for i in items if i["status"] in ("new", "error"))
        auto = sum(1 for i in items if i["match"] and i["match"]["tier"] in (1, 2))
        gen = sum(1 for i in items if i["generated"])
        done = sum(1 for i in items if i["status"] == "saved")
        m = st.columns(4)
        m[0].metric("Questions", len(items))
        m[1].metric("Auto-filled from bank", auto)
        m[2].metric("Drafted by Gemini", gen)
        m[3].metric("Done", done)

        b1, b2 = st.columns([3, 1])
        b1.button(f"Answer all ({pending} pending)", type="primary", width="stretch",
                  on_click=process_all, args=(jd_id,), disabled=pending == 0)
        b2.button("Clear list", width="stretch", on_click=clear_all)

    for n, item in enumerate(items, 1):
        key = item["key"]
        limit = []
        if item["word_limit"]:
            limit.append(f"≤ {item['word_limit']} words")
        if item["char_limit"]:
            limit.append(f"≤ {item['char_limit']} chars")
        status_icon = {"new": "⚪", "resolved": "🟢", "suggested": "🟡", "generated": "🔵",
                       "ask": "🟠", "saved": "✅", "error": "🔴"}[item["status"]]
        title = f"{status_icon} {n}. {item['text']}"
        with st.expander(title, expanded=item["status"] not in ("saved",)):
            st.caption(" · ".join([f"kind: {KIND_LABELS.get(item['kind'], item['kind'])}"] + limit))
            if item["error"]:
                st.error(item["error"])

            match = item["match"]
            if item["status"] == "new":
                cc = st.columns([1, 1, 4])
                cc[0].button("Answer", key=f"{key}_go", on_click=process, args=(key, jd_id))
                cc[1].button("Remove", key=f"{key}_rm", on_click=remove, args=(key,))
                continue

            if item["status"] == "saved":
                final = item.get("final_text") or state.get(f"{key}_draft") or (match or {}).get("answer_text", "")
                how = "Reused from your bank" if (item["saved"] or {}).get("reused") else \
                    ("Saved to your bank" + (" (added to an existing question)"
                                              if (item["saved"] or {}).get("reused_question") else ""))
                st.success(f"{how}. Copy it with the icon in the corner.")
                st.code(final, language=None, wrap_lines=True)
                cc = st.columns([1, 1, 4])
                cc[0].button("Edit again", key=f"{key}_reopen", on_click=reopen, args=(key,))
                cc[1].button("Remove", key=f"{key}_rm2", on_click=remove, args=(key,))
                continue

            # ── Where the current text came from ──
            if item["status"] == "resolved":
                via = " via a wording you confirmed before" if match.get("via_alias") else ""
                st.success(f"Auto-filled from your answer bank — {match['similarity']:.0%} match "
                           f"({match['reason']}{via}). No Gemini call was made.")
            elif item["status"] == "suggested":
                st.info(f"Similar answer found — {match['similarity']:.0%} match ({match['reason']}). "
                        f"Banked question: “{match['bank_question']}”. Reuse it as-is, edit it, "
                        f"or generate a fresh one."
                        + (" Reusing it teaches the bank this wording, so it auto-fills next time."
                           if match["same_scope"] else ""))
            elif item["status"] == "generated":
                st.caption(f"Drafted from {item['context_count']} facts in your knowledge base.")
                options = [v["style"] for v in item["variants"]]
                labels = {}
                for v in item["variants"]:
                    extra = f" · {v['word_count']} words" + (" ⚠ over limit" if v["over_limit"] else "")
                    labels[v["style"]] = STYLE_LABELS[v["style"]] + extra
                if state.get(f"{key}_variant") not in options:
                    state[f"{key}_variant"] = item["draft_style"] if item["draft_style"] in options else options[0]
                st.radio("Style", options, key=f"{key}_variant", horizontal=True,
                         format_func=lambda s, labels=labels: labels[s],
                         on_change=pick_variant, args=(key,))
            elif item["status"] == "ask":
                st.warning(item["clarifying_question"])
                state.setdefault(f"{key}_facts", item["user_facts"])
                st.text_area("Your answer / the missing detail", key=f"{key}_facts", height=90,
                             placeholder="Write it in plain words — I'll phrase it properly.")
                st.button("Draft it with this", key=f"{key}_retry", type="primary",
                          on_click=generate_fresh, args=(key, jd_id))
                st.caption("Or write the whole answer yourself below and save it.")
                state.setdefault(f"{key}_draft", "")

            # ── Editor ──
            st.text_area("Answer", key=f"{key}_draft", height=180)
            draft = state.get(f"{key}_draft", "")
            wc = word_count(draft)
            over = item["word_limit"] and wc > item["word_limit"]
            st.caption(f"{wc} words · {len(draft)} characters"
                       + (f" — ⚠ over the {item['word_limit']}-word limit" if over else ""))

            cat_index = CATEGORIES.index(item["category"]) if item["category"] in CATEGORIES else 0
            st.selectbox("Save to bank as", CATEGORIES, index=cat_index, key=f"{key}_category",
                         format_func=lambda c: CATEGORY_LABELS[c],
                         help="Reusable answers auto-fill on any future application. "
                              "Company-specific ones only for this company.")

            a = st.columns([1.3, 1.3, 1, 1])
            a[0].button("Approve & save", key=f"{key}_save", type="primary",
                        on_click=approve, args=(key, jd_id), width="stretch")
            if item["status"] == "suggested":
                a[1].button("Reuse as-is", key=f"{key}_reuse", on_click=reuse_as_is,
                            args=(key,), width="stretch")
            a[2].button("Regenerate" if item["status"] == "generated" else "Generate fresh",
                        key=f"{key}_regen", on_click=generate_fresh, args=(key, jd_id),
                        width="stretch")
            a[3].button("Remove", key=f"{key}_rm3", on_click=remove, args=(key,), width="stretch")
            st.text_input("Steer the next draft (optional)", key=f"{key}_hint",
                          placeholder="e.g. shorter, lead with the SRE work, less formal")

    finished = [i for i in items if i["status"] == "saved"]
    if len(finished) > 1:
        st.divider()
        st.subheader("Copy all")
        st.code("\n\n".join(
            f"Q: {i['text']}\nA: {i.get('final_text') or state.get(i['key'] + '_draft') or (i['match'] or {}).get('answer_text', '')}"
            for i in finished), language=None, wrap_lines=True)


# ══════════════════════════════════════════════════════════════════════════════
# Answer bank
# ══════════════════════════════════════════════════════════════════════════════
with bank_tab:
    with get_session() as session:
        stats = bank_stats(session, user_id=user_id)
    s = st.columns(4)
    s[0].metric("Questions", stats["questions"])
    s[1].metric("Approved answers", stats["answers"])
    s[2].metric("Times reused", stats["reuses"],
                help="Each reuse is one question answered without calling Gemini.")
    s[3].metric("In knowledge base", stats["kb_chunks"],
                help="Approved answers are retrievable as context by the resume "
                     "generator and future answers.")

    f1, f2 = st.columns([3, 1])
    search = f1.text_input("Search questions and answers", key="bank_search")
    cat_filter = f2.selectbox("Category", ["all"] + list(CATEGORIES), key="bank_cat")

    with get_session() as session:
        rows = list_bank(session, user_id=user_id, search=search or None,
                         category=None if cat_filter == "all" else cat_filter)

    if not rows:
        st.caption("Nothing here yet. Approve an answer on the other tab and it lands here.")

    for row in rows:
        scope = {"reusable": "reusable",
                 "role_specific": f"role: {row['role_name'] or '?'}",
                 "company_specific": f"company: {row['company'] or '(JD deleted)'}"}[row["category"]]
        head = f"{row['question_text']}  ·  {scope}  ·  reused {row['times_reused']}×"
        with st.expander(head):
            st.caption(f"{STYLE_LABELS.get(row['style'], row['style'])} · "
                       f"updated {row['updated_at']:%d %b %Y} · "
                       f"{row['answer_count']} version(s)")
            st.code(row["answer_text"], language=None, wrap_lines=True)

            ek = f"bank_{row['answer_id']}"
            new_text = st.text_area("Edit", value=row["answer_text"], key=f"{ek}_edit", height=150)
            e = st.columns(4)
            if e[0].button("Save edit", key=f"{ek}_save", disabled=new_text.strip() == row["answer_text"]):
                with get_session() as session:
                    update_answer(session, user_id=user_id, answer_id=row["answer_id"],
                                  answer_text=new_text)
                st.rerun()
            if row["category"] != "reusable" and e[1].button("Make reusable", key=f"{ek}_widen"):
                with get_session() as session:
                    recategorize_to_reusable(session, user_id=user_id, question_id=row["question_id"])
                st.rerun()
            if e[2].button("Delete this version", key=f"{ek}_del"):
                with get_session() as session:
                    delete_answer(session, user_id=user_id, answer_id=row["answer_id"])
                st.rerun()
            if e[3].button("Delete question", key=f"{ek}_delq"):
                with get_session() as session:
                    delete_question(session, user_id=user_id, question_id=row["question_id"])
                st.rerun()

            if row["answer_count"] > 1:
                with get_session() as session:
                    history = answer_history(session, user_id=user_id, question_id=row["question_id"])
                st.markdown("**Earlier versions**")
                for h in history[1:]:
                    st.caption(f"{h['updated_at']:%d %b %Y} · {STYLE_LABELS.get(h['style'], h['style'])}")
                    st.code(h["answer_text"], language=None, wrap_lines=True)
