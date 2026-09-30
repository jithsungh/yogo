"""Unit tests for the deterministic half of resume tailoring.

Nothing here calls Gemini. The model's contribution is stubbed as a plain
dict, which is exactly how the rest of the pipeline sees it - so these cover
every step that turns KB rows plus a model response into a LaTeX-ready dict.
"""
import pytest

from app.services import resume_tailor as T


# ── Project dedup ────────────────────────────────────────────────────────────

def _project(pid, title, url=None, tech=None, bullets=()):
    return {"id": pid, "title": title, "github_url": url, "live_url": None,
            "tech_stack": list(tech or []), "raw_bullets": list(bullets)}


def test_dedupe_merges_github_import_with_handwritten_entry():
    """The same repo entered twice - once imported, once written by hand - is
    one project, and the hand-written framing is the one worth keeping."""
    rows = [
        _project("1", "chatbot", "https://github.com/jithsungh/chatbot",
                 ["Python", "TypeScript"], ["Built a FastAPI backend."]),
        _project("2", "Adaptive knowledge Chatbot – RAG", "github.com/jithsungh/chatbot",
                 ["FastAPI", "React", "ChromaDB"], ["Enterprise ready chatbot with routing."]),
    ]
    merged = T._dedupe_projects(rows)
    assert len(merged) == 1
    assert merged[0]["title"] == "Adaptive knowledge Chatbot – RAG"   # human title wins
    assert merged[0]["tech_stack"] == ["FastAPI", "React", "ChromaDB"]  # curated stack wins
    assert len(merged[0]["raw_bullets"]) == 2                          # evidence pooled
    assert len(merged[0]["merged_from"]) == 2


def test_dedupe_normalizes_url_variations():
    rows = [
        _project("1", "a", "https://www.github.com/u/Repo.git"),
        _project("2", "b", "http://github.com/u/repo/"),
        _project("3", "c", "github.com/u/REPO"),
    ]
    assert len(T._dedupe_projects(rows)) == 1


def test_dedupe_keeps_distinct_projects_apart():
    rows = [
        _project("1", "Newzzy", "github.com/u/Newzzy"),
        _project("2", "log-generator", "github.com/u/log-generator"),
    ]
    assert len(T._dedupe_projects(rows)) == 2


def test_dedupe_falls_back_to_title_when_no_url():
    rows = [_project("1", "Side Project"), _project("2", "side-project!")]
    assert len(T._dedupe_projects(rows)) == 1


def test_dedupe_drops_duplicate_bullets_across_the_group():
    rows = [
        _project("1", "repo", "github.com/u/r", bullets=["Built a thing."]),
        _project("2", "Real Title", "github.com/u/r", bullets=["Built a thing.", "Shipped it."]),
    ]
    merged = T._dedupe_projects(rows)[0]
    assert merged["raw_bullets"] == ["Built a thing.", "Shipped it."]


# ── Relevance ranking ────────────────────────────────────────────────────────

def test_ranking_prefers_projects_matching_the_jd():
    terms = T._jd_term_weights(
        {"required_skills": [{"name": "Go", "weight": 1.0}],
         "nice_to_have_skills": [{"name": "Docker", "weight": 0.5}]},
        {"matched_skills": ["Kubernetes"]},
    )
    relevant = _project("1", "log-generator", tech=["Go", "Docker"],
                        bullets=["Built a Go service.", "Ran on Kubernetes."])
    irrelevant = _project("2", "tts_avatar", tech=["CSS"],
                          bullets=["Built a web avatar.", "Added lip sync."])
    ranked = T._rank_projects([irrelevant, relevant], terms)
    assert ranked[0]["title"] == "log-generator"
    assert ranked[0]["prefilter_score"] > ranked[1]["prefilter_score"]


def test_ranking_does_not_match_substrings():
    """'Go' must not score a hit inside 'Google' or 'Django'."""
    terms = T._jd_term_weights({"required_skills": [{"name": "Go", "weight": 1.0}]}, {})
    p = _project("1", "x", tech=["Django"], bullets=["Used Google Maps API here."])
    assert T._rank_projects([p], terms)[0]["prefilter_score"] <= 0


# ── Validation: the anti-hallucination enforcement ───────────────────────────

def _skills():
    return [{"name": "Go", "category": "language"},
            {"name": "Docker", "category": "devops"}]


def test_validation_drops_unknown_entry_ids():
    result = {
        "summary": "s",
        "experiences": [{"entry_id": "ghost", "bullets": ["Did a thing."]}],
        "selected_projects": [{"entry_id": "ghost", "bullets": ["Did a thing."]}],
        "skill_priority": [],
    }
    clean = T._validate_against_source(result, [{"id": "real"}], [{"id": "realp", "tech_stack": []}], _skills())
    assert clean["experiences"] == []
    assert clean["selected_projects"] == []


def test_validation_drops_invented_tech_but_keeps_real_ones():
    candidates = [{"id": "p1", "tech_stack": ["Go", "Docker"]}]
    result = {"summary": "", "experiences": [], "skill_priority": [],
              "selected_projects": [{"entry_id": "p1", "rank": 1,
                                     "tech_stack": ["Docker", "Kafka", "Go"],
                                     "bullets": ["Built it."]}]}
    clean = T._validate_against_source(result, [], candidates, _skills())
    assert clean["selected_projects"][0]["tech_stack"] == ["Docker", "Go"]   # Kafka gone, order kept


def test_validation_falls_back_to_recorded_stack_when_nothing_survives():
    candidates = [{"id": "p1", "tech_stack": ["Go"]}]
    result = {"summary": "", "experiences": [], "skill_priority": [],
              "selected_projects": [{"entry_id": "p1", "rank": 1,
                                     "tech_stack": ["Rust"], "bullets": ["x"]}]}
    clean = T._validate_against_source(result, [], candidates, _skills())
    assert clean["selected_projects"][0]["tech_stack"] == ["Go"]


def test_validation_drops_invented_skill_names():
    result = {"summary": "", "experiences": [], "selected_projects": [],
              "skill_priority": ["Docker", "Fortran", "go"]}
    clean = T._validate_against_source(result, [], [], _skills())
    assert clean["skill_priority"] == ["Docker", "Go"]   # matched case-insensitively


def test_validation_caps_bullet_counts_and_strips_markers():
    candidates = [{"id": "p1", "tech_stack": []}]
    result = {"summary": "", "experiences": [], "skill_priority": [],
              "selected_projects": [{"entry_id": "p1", "rank": 1, "tech_stack": [],
                                     "bullets": ["- one", "• two", "* three", "four", "five"]}]}
    clean = T._validate_against_source(result, [], candidates, _skills())
    bullets = clean["selected_projects"][0]["bullets"]
    assert len(bullets) == T.MAX_PROJECT_BULLETS
    assert bullets == ["one", "two", "three"]


def test_validation_sorts_projects_by_rank():
    candidates = [{"id": "a", "tech_stack": []}, {"id": "b", "tech_stack": []}]
    result = {"summary": "", "experiences": [], "skill_priority": [],
              "selected_projects": [{"entry_id": "a", "rank": 2, "tech_stack": [], "bullets": ["x"]},
                                    {"entry_id": "b", "rank": 1, "tech_stack": [], "bullets": ["y"]}]}
    clean = T._validate_against_source(result, [], candidates, _skills())
    assert [p["entry_id"] for p in clean["selected_projects"]] == ["b", "a"]


# ── Skills section ───────────────────────────────────────────────────────────

def test_skills_merge_cloud_and_devops_into_one_row():
    skills = [{"name": "AWS", "category": "cloud"}, {"name": "Docker", "category": "devops"}]
    rows = T._build_skills_sections(skills, {}, [])
    assert [r["label"] for r in rows] == ["Cloud \\& DevOps"]
    assert "AWS" in rows[0]["items_text"] and "Docker" in rows[0]["items_text"]


def test_skills_collapse_aliases():
    """'Go' and 'Golang' on the same line reads as carelessness."""
    skills = [{"name": "Go", "category": "language"}, {"name": "Golang", "category": "language"}]
    rows = T._build_skills_sections(skills, {}, [])
    assert rows[0]["items_text"] == "Go"


def test_skills_put_jd_priority_first_then_matched():
    skills = [{"name": "Python", "category": "language"},
              {"name": "Go", "category": "language"},
              {"name": "Java", "category": "language"}]
    rows = T._build_skills_sections(
        skills, {"matched_skills": ["Java"]}, skill_priority=["Go"])
    assert rows[0]["items_text"] == "Go, Java, Python"


def test_skills_category_labels_are_human_readable():
    skills = [{"name": "X", "category": "ml"}, {"name": "Y", "category": "soft_skill"}]
    labels = [r["label"] for r in T._build_skills_sections(skills, {}, [])]
    assert labels == ["AI/ML", "Other Skills"]


def test_skills_are_latex_escaped():
    rows = T._build_skills_sections([{"name": "C# & F#", "category": "language"}], {}, [])
    assert rows[0]["items_text"] == "C\\# \\& F\\#"


# ── Date formatting ──────────────────────────────────────────────────────────

from datetime import date


@pytest.mark.parametrize("start,end,expected", [
    (date(2025, 8, 1), None, "Aug 2025 – Present"),
    (date(2024, 1, 1), date(2024, 6, 1), "Jan 2024 – Jun 2024"),
    (None, date(2024, 6, 1), "Jun 2024"),
    (None, None, ""),          # the bug that printed a bare " – Present"
])
def test_date_range(start, end, expected):
    assert T._date_range(start, end) == expected


def test_date_range_year_only():
    assert T._date_range(date(2022, 8, 1), date(2026, 5, 1), year_only=True) == "2022 – 2026"


# ── Certification rows ───────────────────────────────────────────────────────

def test_cert_row_shortens_a_long_url():
    row = T._certification_row({"title": "T", "issuer": "I", "date": "2025",
                                "url": "https://ti-user-certificates.s3.amazonaws.com/" + "x" * 120})
    assert row["link_text"] == "[link]"


def test_cert_row_prints_a_short_url_verbatim():
    row = T._certification_row({"title": "T", "issuer": "", "date": "", "url": "https://bit.ly/4nRHJ76"})
    assert row["link_text"] == "[bit.ly/4nRHJ76]"


def test_cert_row_without_url_has_no_link():
    row = T._certification_row({"title": "Club Lead", "issuer": "SRM AP", "date": "2024", "url": None})
    assert row["link_text"] == ""
    assert "\\textbf{Club Lead}" in row["text"]


def test_cert_row_escapes_title():
    row = T._certification_row({"title": "A & B 100%", "issuer": "", "date": "", "url": ""})
    assert "\\&" in row["text"] and "\\%" in row["text"]
