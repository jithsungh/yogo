"""Phase 4: answer-bank intake, retrieval tiers, generation gating, and saving.

Two layers:
  * pure-function tests for every rule (no DB, no Gemini);
  * DB tests that run the real SQL against Postgres inside a transaction that
    is rolled back, with embed_text replaced by deterministic vectors so the
    similarity between any two questions is set by the test, not by Gemini.
"""
import math
import uuid

import numpy as np
import pytest

from app.services import qa_bank_service as B
from app.services import qa_generation as G
from app.services import qa_intake as I
from app.services import qa_retrieval as R


# ══════════════════════════════════════════════════════════════════════════════
# Intake
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("question,kind", [
    ("What is your current CTC?", I.KIND_PERSONAL_FACT),
    ("What is your notice period?", I.KIND_PERSONAL_FACT),
    ("Do you require visa sponsorship?", I.KIND_PERSONAL_FACT),
    ("Are you willing to relocate to Bangalore?", I.KIND_PERSONAL_FACT),
    ("Why do you want to join Razorpay?", I.KIND_MOTIVATION),
    ("What excites you about this role?", I.KIND_MOTIVATION),
    ("Describe a time you disagreed with a teammate.", I.KIND_BEHAVIORAL),
    ("Tell us about yourself.", I.KIND_BACKGROUND),
    ("Describe your experience with Kubernetes.", I.KIND_TECHNICAL),
    ("Anything else we should know?", I.KIND_OTHER),
])
def test_guess_kind(question, kind):
    assert I.guess_kind(question) == kind


@pytest.mark.parametrize("raw,words,chars", [
    ("Tell us about yourself (max 150 words)", 150, None),
    ("In under 200 words, describe a project.", 200, None),
    ("Describe a project in 100 words or less.", 100, None),
    ("Summary, no more than 500 characters.", None, 500),
    ("Why us?", None, None),
])
def test_extract_limits(raw, words, chars):
    assert I.extract_limits(raw) == (words, chars)


def test_single_question_skips_gemini(monkeypatch):
    """The commonest input needs no model call."""
    monkeypatch.setattr(I, "generate_json", lambda *_: pytest.fail("Gemini called for one question"))
    [q] = I.normalize_questions("3. Tell us about yourself (max 150 words)?")
    assert q.text == "Tell us about yourself?"
    assert q.word_limit == 150
    assert q.kind == I.KIND_BACKGROUND


def test_multi_question_uses_gemini_and_validates(monkeypatch):
    monkeypatch.setattr(I, "generate_json", lambda *_: {"questions": [
        {"text": "Tell us about yourself.", "word_limit": 150, "kind": "background"},
        {"text": "tell us about yourself", "kind": "background"},          # duplicate
        {"text": "What is your expected CTC?", "kind": "other"},           # overridden
        {"text": "Why us?", "kind": "not-a-kind"},                          # fallback
        {"text": "Hi", "kind": "other"},                                    # too short
        "Describe a project in under 200 words",                           # bare string
    ]})
    qs = I.normalize_questions("1. a\n2. b\n3. c")
    assert [q.text for q in qs] == ["Tell us about yourself.", "What is your expected CTC?",
                                    "Why us?", "Describe a project in under 200 words"]
    assert qs[0].word_limit == 150
    assert qs[1].kind == I.KIND_PERSONAL_FACT       # deterministic override beats the model
    assert qs[2].kind == I.KIND_MOTIVATION          # invalid kind -> heuristic
    assert qs[3].word_limit == 200                  # limit recovered from the text


def test_empty_input():
    assert I.normalize_questions("   ") == []


def test_intake_question_keys_are_unique():
    assert I.IntakeQuestion("a").key != I.IntakeQuestion("a").key


# ══════════════════════════════════════════════════════════════════════════════
# Retrieval tiers (pure)
# ══════════════════════════════════════════════════════════════════════════════

ROLE_A, ROLE_B = uuid.uuid4(), uuid.uuid4()


def cand(sim, category="reusable", company=None, role=None, text="q"):
    return R.BankCandidate(question_id=uuid.uuid4(), question_text=text, category=category,
                           similarity=sim, answer_id=uuid.uuid4(), answer_text="a",
                           style="concise", role_id=role, company=company)


def classify(cands, company="Razorpay", role=ROLE_A, kind=None):
    return R.classify_candidates(cands, company=company, role_id=role, kind=kind,
                                 auto_threshold=0.82, suggest_threshold=0.65)


def test_tier1_reusable_auto_fills():
    m, _ = classify([cand(0.90)])
    assert m.tier == 1 and m.auto_fill


def test_tier2_same_company():
    m, _ = classify([cand(0.90, "company_specific", company="Razorpay Software Pvt Ltd")])
    assert m.tier == 2 and m.auto_fill


def test_company_specific_never_crosses_companies():
    """Milestone requirement: 'why Razorpay' must never surface for Google -
    not auto-filled, and not even suggested."""
    m, alts = classify([cand(0.99, "company_specific", company="Razorpay")], company="Google")
    assert m is None and alts == []


def test_company_specific_without_target_company_is_ineligible():
    m, _ = classify([cand(0.99, "company_specific", company="Razorpay")], company=None)
    assert m is None


def test_motivation_never_auto_fills_from_reusable():
    m, _ = classify([cand(0.95)], kind=I.KIND_MOTIVATION)
    assert m.tier == 3 and not m.auto_fill


def test_role_specific_same_role_is_tier2_other_role_is_suggestion():
    m, _ = classify([cand(0.9, "role_specific", role=ROLE_A)], role=ROLE_A)
    assert m.tier == 2
    m, _ = classify([cand(0.9, "role_specific", role=ROLE_B)], role=ROLE_A)
    assert m.tier == 3 and "different role" in m.reason


def test_between_thresholds_is_a_suggestion():
    m, _ = classify([cand(0.70)])
    assert m.tier == 3


def test_below_suggest_threshold_is_a_miss():
    m, _ = classify([cand(0.60)])
    assert m is None


def test_best_auto_candidate_wins_over_order():
    low, high = cand(0.84), cand(0.93, "company_specific", company="Razorpay")
    m, alts = classify([low, high])
    assert m.candidate is high
    assert low in alts


@pytest.mark.parametrize("a,b", [
    ("Qualcomm India Private Limited", "Qualcomm"),
    ("Razorpay Software Pvt. Ltd.", "razorpay"),
    ("Acme Corp.", "ACME"),
    ("Stripe, Inc.", "Stripe"),
])
def test_normalize_company(a, b):
    assert R.normalize_company(a) == R.normalize_company(b)


@pytest.mark.parametrize("a,b", [
    ("Zeta", "Zeta Global"),                 # different employers (review F6)
    ("Tata Technologies", "Tata Group"),
    ("Tech Mahindra", "Mahindra Group"),
    ("Google", "Razorpay"),
])
def test_normalize_company_keeps_distinct_names_distinct(a, b):
    assert R.normalize_company(a) != R.normalize_company(b)


def test_normalize_company_empty():
    assert R.normalize_company(None) == ""


def test_same_scope():
    res = R.RetrievalResult(match=None, query_vector=None, company="Razorpay", role_id=ROLE_A)
    assert res.same_scope(cand(0.9))
    assert res.same_scope(cand(0.9, "company_specific", company="Razorpay Software Pvt Ltd"))
    assert not res.same_scope(cand(0.9, "company_specific", company="Google"))
    assert res.same_scope(cand(0.9, "role_specific", role=ROLE_A))
    assert not res.same_scope(cand(0.9, "role_specific", role=ROLE_B))


# ══════════════════════════════════════════════════════════════════════════════
# Generation (pure parts + gating)
# ══════════════════════════════════════════════════════════════════════════════

def test_select_context_caps_skills():
    """42 skill chunks must not crowd out 2 experience chunks."""
    rows = [{"content_type": "skill", "text_for_embedding": f"s{i}", "similarity": 0.9}
            for i in range(10)]
    rows += [{"content_type": "experience", "text_for_embedding": "e", "similarity": 0.5}]
    picked = G.select_context(rows)
    assert sum(r["content_type"] == "skill" for r in picked) == G.CONTEXT_MAX_SKILLS
    assert any(r["content_type"] == "experience" for r in picked)


def test_validate_output_insufficient_info():
    out = G.validate_output({"status": "insufficient_info", "clarifying_question": "Which project?",
                             "variants": [{"style": "concise", "text": "x"}]},
                            word_limit=None, char_limit=None)
    assert out == {"status": "insufficient_info", "clarifying_question": "Which project?",
                   "variants": []}


def test_validate_output_filters_and_flags():
    out = G.validate_output({"status": "ok", "variants": [
        {"style": "conversational", "text": "one two three four five six"},
        {"style": "concise", "text": "one two"},
        {"style": "concise", "text": "duplicate style"},
        {"style": "haiku", "text": "bad style"},
        {"style": "detailed_star", "text": "  "},
    ]}, word_limit=5, char_limit=None)
    assert [v["style"] for v in out["variants"]] == ["concise", "conversational"]
    assert out["variants"][0]["over_limit"] is False
    assert out["variants"][1]["over_limit"] is True
    assert out["variants"][1]["word_count"] == 6


def test_validate_output_rejects_garbage():
    assert G.validate_output(None, word_limit=None, char_limit=None)["status"] == "error"
    assert G.validate_output({"status": "ok", "variants": []},
                             word_limit=None, char_limit=None)["status"] == "error"


def test_prompt_carries_limit_facts_and_hint():
    prompt = G.build_prompt(question="Q?", profile="- me", context=[], jd="",
                            word_limit=150, char_limit=None,
                            user_facts="I use their payment links.", hint="shorter")
    assert "at most 150 words" in prompt
    assert "I use their payment links." in prompt
    assert "shorter" in prompt
    assert "(nothing relevant found)" in prompt


@pytest.mark.parametrize("kind", [I.KIND_PERSONAL_FACT, I.KIND_MOTIVATION])
def test_ungroundable_kinds_ask_without_calling_gemini(monkeypatch, kind):
    monkeypatch.setattr(G, "generate_json", lambda *_: pytest.fail("generation called"))
    monkeypatch.setattr(G, "embed_text", lambda *_: pytest.fail("embedding called"))
    out = G.generate_answer_variants(None, user_id=uuid.uuid4(), question_text="Why us?", kind=kind)
    assert out["status"] == "insufficient_info"
    assert out["generated"] is False
    assert out["clarifying_question"]


def test_word_count():
    assert G.word_count("I've built two-phase commits, fast.") == 5
    assert G.word_count("") == 0


# ══════════════════════════════════════════════════════════════════════════════
# Category defaults and save validation (pure)
# ══════════════════════════════════════════════════════════════════════════════

JD = uuid.uuid4()


@pytest.mark.parametrize("question,kind,jd,expected", [
    ("Why do you want to join us?", I.KIND_MOTIVATION, JD, "company_specific"),
    ("Why do you want to join us?", I.KIND_MOTIVATION, None, "reusable"),
    ("Describe a challenging project.", I.KIND_BEHAVIORAL, JD, "reusable"),   # milestone
    ("What is your notice period?", I.KIND_PERSONAL_FACT, JD, "reusable"),
    ("How would you approach this role in the first 90 days?", I.KIND_OTHER, JD, "role_specific"),
])
def test_suggest_category(question, kind, jd, expected):
    assert B.suggest_category(question, kind=kind, jd_id=jd) == expected


@pytest.mark.parametrize("kwargs,msg", [
    ({"category": "job_specific"}, "category"),
    ({"style": "haiku"}, "style"),
    ({"answer_text": "  "}, "required"),
])
def test_save_validates_before_touching_the_db(kwargs, msg):
    base = dict(user_id=uuid.uuid4(), question_text="Q?", answer_text="A.",
                style="concise", category="reusable")
    with pytest.raises(ValueError, match=msg):
        B.save_approved_answer(None, **{**base, **kwargs})


# ══════════════════════════════════════════════════════════════════════════════
# DB tests: real SQL, rolled back, deterministic embeddings
# ══════════════════════════════════════════════════════════════════════════════

def _db_available():
    try:
        from sqlalchemy import text

        from app.db import _engine
        with _engine.connect() as c:
            c.execute(text("SELECT 1 FROM qa_question_alias LIMIT 0"))
        return True
    except Exception:
        return False


needs_db = pytest.mark.skipif(not _db_available(), reason="database not reachable / not migrated")


class FakeEmbedder:
    """Unit vectors whose pairwise cosine the test chooses.

    Each 'topic' gets its own orthogonal axis; a paraphrase is the topic axis
    tilted toward a private axis by the angle that yields the requested
    similarity. Unknown texts get a fresh orthogonal axis (similarity 0).
    """

    def __init__(self):
        self._next = 0
        self._vectors: dict[str, np.ndarray] = {}

    def _axis(self):
        v = np.zeros(768)
        v[self._next] = 1.0
        self._next += 1
        return v

    def topic(self, text_value: str) -> None:
        self._vectors[text_value] = self._axis()

    def paraphrase(self, text_value: str, of: str, similarity: float) -> None:
        base = self._vectors[of]
        angle = math.acos(similarity)
        self._vectors[text_value] = math.cos(angle) * base + math.sin(angle) * self._axis()

    def __call__(self, text_value: str):
        from pgvector import Vector
        if text_value not in self._vectors:
            self._vectors[text_value] = self._axis()
        return Vector(self._vectors[text_value].tolist())


@pytest.fixture
def db(monkeypatch):
    """A session whose commits land in SAVEPOINTs of one outer transaction
    that is rolled back at the end - the real user's data is never touched."""
    from sqlalchemy.orm import Session

    from app.db import _engine
    conn = _engine.connect()
    outer = conn.begin()
    session = Session(bind=conn, join_transaction_mode="create_savepoint")

    embed = FakeEmbedder()
    for module in (R, B, G):
        monkeypatch.setattr(module, "embed_text", embed, raising=False)
    import app.services.kb_sync as kb
    monkeypatch.setattr(kb, "embed_text", embed)

    yield session, embed
    session.close()
    outer.rollback()
    conn.close()


@pytest.fixture
def user_and_jds(db):
    from sqlalchemy import text
    session, _ = db
    uid = uuid.uuid4()
    session.execute(text("INSERT INTO users (id, email, password_hash) VALUES (:id, :e, 'x')"),
                    {"id": uid, "e": f"{uid}@test.invalid"})
    role = session.execute(text("SELECT id FROM role LIMIT 1")).scalar()
    jds = {}
    for company in ("Razorpay", "Google"):
        jid = uuid.uuid4()
        session.execute(text("INSERT INTO job_description (id, user_id, raw_text, company, role_title, role_id) "
                             "VALUES (:id, :u, 'jd text', :c, 'SDE', :r)"),
                        {"id": jid, "u": uid, "c": company, "r": role})
        jds[company] = jid
    session.flush()
    return uid, jds


def _save(session, uid, q, a, category="reusable", jd=None, existing=None):
    return B.save_approved_answer(session, user_id=uid, question_text=q, answer_text=a,
                                  style="concise", category=category, jd_id=jd,
                                  existing_question_id=existing)


@needs_db
def test_db_reworded_question_resolves_tier1(db, user_and_jds):
    """Guide acceptance check: a reworded question hits the banked answer."""
    session, embed = db
    uid, jds = user_and_jds
    embed.topic("Tell us about yourself.")
    embed.paraphrase("Please introduce yourself.", of="Tell us about yourself.", similarity=0.90)
    _save(session, uid, "Tell us about yourself.", "I'm an SRE at TechMojo.")

    res = R.find_bank_match(session, user_id=uid, question_text="Please introduce yourself.")
    assert res.match.tier == 1
    assert res.match.candidate.answer_text == "I'm an SRE at TechMojo."
    assert res.match.confidence == pytest.approx(0.90, abs=1e-3)


@needs_db
def test_db_confirmed_paraphrase_becomes_tier1_via_alias(db, user_and_jds):
    """The milestone mechanism: a low-similarity paraphrase is only suggested
    the first time; once confirmed, the same wording auto-fills."""
    session, embed = db
    uid, _ = user_and_jds
    embed.topic("Tell us about yourself.")
    embed.paraphrase("Walk me through your background.", of="Tell us about yourself.", similarity=0.72)
    saved = _save(session, uid, "Tell us about yourself.", "Intro answer.")

    first = R.find_bank_match(session, user_id=uid, question_text="Walk me through your background.")
    assert first.match.tier == 3

    assert B.add_alias(session, user_id=uid, question_id=saved["question_id"],
                       alias_text="Walk me through your background.",
                       alias_vector=first.query_vector)
    again = R.find_bank_match(session, user_id=uid, question_text="Walk me through your background.")
    assert again.match.tier == 1
    assert again.match.candidate.matched_alias == "Walk me through your background."


@needs_db
def test_db_company_answers_stay_with_their_company(db, user_and_jds):
    session, embed = db
    uid, jds = user_and_jds
    embed.topic("Why do you want to join us?")
    _save(session, uid, "Why do you want to join us?", "Payments reliability.",
          category="company_specific", jd=jds["Razorpay"])

    same = R.find_bank_match(session, user_id=uid, question_text="Why do you want to join us?",
                             jd_id=jds["Razorpay"], kind=I.KIND_MOTIVATION)
    other = R.find_bank_match(session, user_id=uid, question_text="Why do you want to join us?",
                              jd_id=jds["Google"], kind=I.KIND_MOTIVATION)
    assert same.match.tier == 2
    assert other.match is None and other.alternatives == []


@needs_db
def test_db_duplicate_question_adds_answer_and_alias_not_question(db, user_and_jds):
    from sqlalchemy import text
    session, embed = db
    uid, _ = user_and_jds
    embed.topic("Describe a challenging project.")
    embed.paraphrase("Describe a challenging project!", of="Describe a challenging project.", similarity=0.97)
    first = _save(session, uid, "Describe a challenging project.", "v1")
    second = _save(session, uid, "Describe a challenging project!", "v2")

    assert second["question_id"] == first["question_id"] and second["reused_question"]
    count = session.execute(text("SELECT count(*) FROM qa_question WHERE user_id = :u"), {"u": uid}).scalar()
    assert count == 1
    # Newest answer is the one retrieval returns...
    res = R.find_bank_match(session, user_id=uid, question_text="Describe a challenging project.")
    assert res.match.candidate.answer_text == "v2"
    # ...and the only one living in kb_chunk.
    chunks = session.execute(text(
        "SELECT text_for_embedding FROM kb_chunk k JOIN qa_answer a ON a.id = k.source_id "
        "WHERE a.question_id = :q"), {"q": first["question_id"]}).scalars().all()
    assert chunks == ["Q: Describe a challenging project.\nA: v2"]


@needs_db
def test_db_every_approved_answer_reaches_kb_chunk_and_deletes_clean(db, user_and_jds):
    from sqlalchemy import text
    session, embed = db
    uid, _ = user_and_jds
    saved = _save(session, uid, "Tell us about yourself.", "Intro.")
    stats = B.bank_stats(session, user_id=uid)
    assert stats == {"questions": 1, "answers": 1, "reuses": 0, "kb_chunks": 1, "aliases": 0}

    B.update_answer(session, user_id=uid, answer_id=saved["answer_id"], answer_text="Better intro.")
    chunk = session.execute(text("SELECT text_for_embedding FROM kb_chunk WHERE source_id = :a"),
                            {"a": saved["answer_id"]}).scalar()
    assert chunk.endswith("Better intro.")

    assert B.delete_answer(session, user_id=uid, answer_id=saved["answer_id"]) is True
    stats = B.bank_stats(session, user_id=uid)
    assert stats == {"questions": 0, "answers": 0, "reuses": 0, "kb_chunks": 0, "aliases": 0}


@needs_db
def test_db_reuse_is_counted(db, user_and_jds):
    session, _ = db
    uid, _ = user_and_jds
    saved = _save(session, uid, "Tell us about yourself.", "Intro.")
    R.mark_reused(session, user_id=uid, answer_id=saved["answer_id"])
    R.mark_reused(session, user_id=uid, answer_id=saved["answer_id"])
    assert B.bank_stats(session, user_id=uid)["reuses"] == 2


@needs_db
def test_db_scoped_save_requires_scope(db, user_and_jds):
    session, _ = db
    uid, _ = user_and_jds
    with pytest.raises(ValueError, match="company"):
        _save(session, uid, "Why us?", "a", category="company_specific", jd=None)


@needs_db
def test_db_other_users_bank_is_invisible(db, user_and_jds):
    from sqlalchemy import text
    session, embed = db
    uid, _ = user_and_jds
    other = uuid.uuid4()
    session.execute(text("INSERT INTO users (id, email, password_hash) VALUES (:id, :e, 'x')"),
                    {"id": other, "e": f"{other}@test.invalid"})
    _save(session, other, "Tell us about yourself.", "Someone else's intro.")
    res = R.find_bank_match(session, user_id=uid, question_text="Tell us about yourself.")
    assert res.match is None


# ══════════════════════════════════════════════════════════════════════════════
# Regressions from the Phase 4 adversarial review
# ══════════════════════════════════════════════════════════════════════════════

def test_review_f1_fact_question_needs_near_exact_match():
    """'current CTC' vs 'expected CTC' embed at 0.90; must not auto-fill."""
    m, _ = R.classify_candidates([cand(0.90, text="What is your current CTC?")], company=None,
                                 role_id=None, kind=I.KIND_PERSONAL_FACT,
                                 auto_threshold=0.82, suggest_threshold=0.65, fact_threshold=0.97)
    assert m.tier == 3 and not m.auto_fill


def test_review_f1_fact_question_auto_fills_via_confirmed_alias():
    c = cand(0.99, text="What is your notice period?")
    c.matched_alias = "How soon can you join?"
    m, _ = R.classify_candidates([c], company=None, role_id=None, kind=I.KIND_PERSONAL_FACT,
                                 auto_threshold=0.82, suggest_threshold=0.65, fact_threshold=0.97)
    assert m.tier == 1


def test_review_f1_banked_fact_is_guarded_even_if_incoming_kind_differs():
    m, _ = R.classify_candidates([cand(0.90, text="What is your expected CTC?")], company=None,
                                 role_id=None, kind=None, auto_threshold=0.82,
                                 suggest_threshold=0.65, fact_threshold=0.97)
    assert m.tier == 3


def test_review_f3_banked_motivation_never_auto_fills():
    """Saved as reusable with kind 'other'; the banked wording is motivation."""
    m, _ = classify([cand(0.95, text="What makes you want to work here?")], kind=None)
    assert m.tier == 3 and not m.auto_fill


@needs_db
def test_review_f2_save_under_other_company_question_is_rejected(db, user_and_jds):
    session, embed = db
    uid, jds = user_and_jds
    saved = _save(session, uid, "Why do you want to join us?", "Razorpay reasons.",
                  category="company_specific", jd=jds["Razorpay"])
    with pytest.raises(ValueError, match="different company"):
        _save(session, uid, "Why do you want to join us?", "Google reasons.",
              category="company_specific", jd=jds["Google"], existing=saved["question_id"])


@needs_db
def test_review_f5_category_mismatch_with_existing_question_is_rejected(db, user_and_jds):
    session, _ = db
    uid, jds = user_and_jds
    saved = _save(session, uid, "Tell us about yourself.", "Intro.")
    with pytest.raises(ValueError, match="reusable"):
        _save(session, uid, "Tell us about yourself.", "x", category="company_specific",
              jd=jds["Razorpay"], existing=saved["question_id"])


@needs_db
def test_review_f4_company_answer_cannot_be_made_reusable(db, user_and_jds):
    session, _ = db
    uid, jds = user_and_jds
    saved = _save(session, uid, "Why do you want to join us?", "a", category="company_specific",
                  jd=jds["Razorpay"])
    with pytest.raises(ValueError, match="can't be made reusable"):
        B.recategorize_to_reusable(session, user_id=uid, question_id=saved["question_id"])


@needs_db
def test_review_sql_f2_company_answers_stay_out_of_kb_chunk(db, user_and_jds):
    from sqlalchemy import text
    session, _ = db
    uid, jds = user_and_jds
    _save(session, uid, "Why do you want to join us?", "I love Razorpay.",
          category="company_specific", jd=jds["Razorpay"])
    n = session.execute(text("SELECT count(*) FROM kb_chunk WHERE user_id = :u AND "
                             "content_type = 'qa_answer'"), {"u": uid}).scalar()
    assert n == 0


@needs_db
def test_review_sql_f3_other_companies_cannot_crowd_out_the_right_one(db, user_and_jds):
    """20+ other companies' identical questions used to fill every LIMIT slot."""
    from sqlalchemy import text
    session, embed = db
    uid, jds = user_and_jds
    embed.topic("Why do you want to join us?")
    embed.paraphrase("Why do you want to work at our company?", of="Why do you want to join us?",
                     similarity=0.90)
    _save(session, uid, "Why do you want to work at our company?", "Razorpay answer.",
          category="company_specific", jd=jds["Razorpay"])
    role = session.execute(text("SELECT id FROM role LIMIT 1")).scalar()
    for n in range(R.CANDIDATE_POOL + 3):
        jid = uuid.uuid4()
        session.execute(text("INSERT INTO job_description (id, user_id, raw_text, company, role_id) "
                             "VALUES (:id, :u, 'x', :c, :r)"),
                        {"id": jid, "u": uid, "c": f"Other Co {n}", "r": role})
        _save(session, uid, "Why do you want to join us?", f"answer {n}",
              category="company_specific", jd=jid)
    res = R.find_bank_match(session, user_id=uid, question_text="Why do you want to join us?",
                            jd_id=jds["Razorpay"], kind=I.KIND_MOTIVATION)
    assert res.match is not None and res.match.candidate.answer_text == "Razorpay answer."


@needs_db
def test_review_sql_f4_deleting_the_jd_keeps_the_company_scope(db, user_and_jds):
    from sqlalchemy import text
    session, embed = db
    uid, jds = user_and_jds
    _save(session, uid, "Why do you want to join us?", "Razorpay answer.",
          category="company_specific", jd=jds["Razorpay"])
    session.execute(text("DELETE FROM job_description WHERE id = :id"), {"id": jds["Razorpay"]})
    new_jd = uuid.uuid4()
    session.execute(text("INSERT INTO job_description (id, user_id, raw_text, company) "
                         "VALUES (:id, :u, 'x', 'Razorpay')"), {"id": new_jd, "u": uid})
    res = R.find_bank_match(session, user_id=uid, question_text="Why do you want to join us?",
                            jd_id=new_jd, kind=I.KIND_MOTIVATION)
    assert res.match and res.match.tier == 2


@needs_db
def test_review_f6_editing_an_old_version_does_not_make_it_live(db, user_and_jds):
    session, embed = db
    uid, _ = user_and_jds
    first = _save(session, uid, "Tell us about yourself.", "v1")
    _save(session, uid, "Tell us about yourself.", "v2")
    B.update_answer(session, user_id=uid, answer_id=first["answer_id"], answer_text="v1 fixed")
    res = R.find_bank_match(session, user_id=uid, question_text="Tell us about yourself.")
    assert res.match.candidate.answer_text == "v2"


@needs_db
def test_review_delete_live_answer_promotes_the_previous_one(db, user_and_jds):
    from sqlalchemy import text
    session, _ = db
    uid, _ = user_and_jds
    _save(session, uid, "Tell us about yourself.", "v1")
    second = _save(session, uid, "Tell us about yourself.", "v2")
    B.delete_answer(session, user_id=uid, answer_id=second["answer_id"])
    chunk = session.execute(text("SELECT text_for_embedding FROM kb_chunk WHERE user_id = :u AND "
                                 "content_type = 'qa_answer'"), {"u": uid}).scalars().all()
    assert chunk == ["Q: Tell us about yourself.\nA: v1"]


@needs_db
def test_review_sql_f6_other_users_question_cannot_be_touched(db, user_and_jds):
    from sqlalchemy import text
    session, _ = db
    uid, _ = user_and_jds
    other = uuid.uuid4()
    session.execute(text("INSERT INTO users (id, email, password_hash) VALUES (:id, :e, 'x')"),
                    {"id": other, "e": f"{other}@test.invalid"})
    theirs = _save(session, other, "What is your notice period?", "SECRET 30 days",
                   category="reusable")
    with pytest.raises(ValueError):
        B.recategorize_to_reusable(session, user_id=uid, question_id=theirs["question_id"])
    R.mark_reused(session, user_id=uid, answer_id=theirs["answer_id"])
    assert B.bank_stats(session, user_id=other)["reuses"] == 0
    leaked = session.execute(text("SELECT count(*) FROM kb_chunk WHERE user_id = :u"), {"u": uid}).scalar()
    assert leaked == 0


@needs_db
def test_rewording_a_question_keeps_the_old_wording_as_alias(db, user_and_jds):
    session, embed = db
    uid, _ = user_and_jds
    saved = _save(session, uid, "Tell us about yourself.", "Intro.")
    B.update_question_text(session, user_id=uid, question_id=saved["question_id"],
                           question_text="Introduce yourself.")
    res = R.find_bank_match(session, user_id=uid, question_text="Tell us about yourself.")
    assert res.match.tier == 1 and res.match.candidate.matched_alias == "Tell us about yourself."


@pytest.mark.parametrize("raw,words,chars,clean", [
    ("Answer in not more than 150 words: Tell us about yourself?", 150, None, "Tell us about yourself?"),
    ("Tell us about yourself (Max. 150 words)", 150, None, "Tell us about yourself"),
    ("Describe a project (200 words)", 200, None, "Describe a project"),
    ("Why us? (in 300 characters)", None, 300, "Why us?"),
    ("Describe a project, 150-200 words", 200, None, "Describe a project"),
    ("Word limit: 200. Describe your biggest failure.", 200, None, "Describe your biggest failure"),
])
def test_review_limit_phrasings_extracted_and_stripped(monkeypatch, raw, words, chars, clean):
    monkeypatch.setattr(I, "generate_json", lambda *_: pytest.fail("Gemini called"))
    [q] = I.normalize_questions(raw)
    assert (q.word_limit, q.char_limit, q.text) == (words, chars, clean)


@pytest.mark.parametrize("q", ["What makes you want to work here?", "Why Razorpay?",
                               "What draws you to Razorpay?", "What makes you excited to join Razorpay?"])
def test_review_motivation_wordings(q):
    assert I.guess_kind(q) == I.KIND_MOTIVATION
