"""Reuse and page-fitting.

The cache key has to be exactly as sensitive as it should be: unchanged
inputs must reuse (or the user pays for Gemini on every click), and ANY
changed input must not (or a KB edit or a template fix silently never reaches
the resume). Each test below pins one of those two failure modes.
"""
import copy

import pytest

from app.services import resume_version_service as V


@pytest.fixture
def source():
    return {
        "profile": {"full_name": "A", "summary": "s", "links": {"github": "g"}},
        "experiences": [{"id": "e1", "raw_bullets": ["b"], "date_range": "2025"}],
        "projects": [{"id": "p1", "title": "P", "raw_bullets": ["b"], "tech_stack": ["Go"]}],
        "skills": [{"name": "Go", "category": "language"}],
        "education": [{"degree": "B.Tech", "institution": "X"}],
        "certifications": [{"title": "C"}],
        "match_result": {"matched_skills": ["Go"], "missing_skills": [], "surplus_skills": []},
        "jd": {"company": "Acme", "role_title": "SDE", "requirements": {"seniority": "junior"}},
    }


def test_identical_inputs_produce_the_same_hash(source):
    assert V.compute_content_hash(source) == V.compute_content_hash(copy.deepcopy(source))


def test_key_order_does_not_change_the_hash(source):
    reordered = {k: source[k] for k in reversed(list(source))}
    assert V.compute_content_hash(reordered) == V.compute_content_hash(source)


@pytest.mark.parametrize("mutate", [
    pytest.param(lambda s: s["profile"].update(full_name="B"), id="profile"),
    pytest.param(lambda s: s["experiences"][0]["raw_bullets"].append("new"), id="experience-bullet"),
    pytest.param(lambda s: s["projects"].append({"id": "p2"}), id="new-project"),
    pytest.param(lambda s: s["skills"].append({"name": "Rust", "category": "language"}), id="new-skill"),
    pytest.param(lambda s: s["education"][0].update(degree="M.Tech"), id="education"),
    pytest.param(lambda s: s["certifications"].clear(), id="certifications"),
    pytest.param(lambda s: s["match_result"]["matched_skills"].append("Docker"), id="match-result"),
    pytest.param(lambda s: s["jd"].update(role_title="SDE-2"), id="jd"),
])
def test_any_changed_input_invalidates_the_cache(source, mutate):
    before = V.compute_content_hash(source)
    mutate(source)
    assert V.compute_content_hash(source) != before


def test_editing_the_template_invalidates_the_cache(source, monkeypatch):
    """Otherwise a layout fix would never reach an already-generated JD."""
    before = V.compute_content_hash(source)
    monkeypatch.setattr(V, "template_fingerprint", lambda: "different-template")
    assert V.compute_content_hash(source) != before


def test_bumping_the_prompt_version_invalidates_the_cache(source, monkeypatch):
    before = V.compute_content_hash(source)
    monkeypatch.setattr(V, "TAILOR_VERSION", "99.0")
    assert V.compute_content_hash(source) != before


# ── Page fitting ─────────────────────────────────────────────────────────────

class _FakeCompile:
    """Reports one page per two projects, so the trim loop has to iterate."""
    def __init__(self, data):
        self.pages = max(1, (len(data["projects"]) + 1) // 2)
        self.overfull = []
        self.pdf_path = self.tex_path = None


def _patch_compiler(monkeypatch, data_holder):
    monkeypatch.setattr(V, "render_latex", lambda d: f"tex:{len(d['projects'])}")
    monkeypatch.setattr(V, "compile_to_pdf",
                        lambda src, output_stem=None: _FakeCompile(data_holder["data"]))


def _data(n):
    return {"projects": [{"title": f"P{i}"} for i in range(n)]}


def test_fitting_is_a_no_op_when_it_already_fits(monkeypatch):
    holder = {"data": _data(2)}
    _patch_compiler(monkeypatch, holder)
    _, compiled, report = V._compile_to_fit(holder["data"], {}, stem="s", max_pages=1)
    assert compiled.pages == 1
    assert report["trimmed_projects"] == []
    assert len(holder["data"]["projects"]) == 2


def test_fitting_drops_the_lowest_ranked_project_first(monkeypatch):
    holder = {"data": _data(4)}
    _patch_compiler(monkeypatch, holder)
    _, compiled, report = V._compile_to_fit(holder["data"], {}, stem="s", max_pages=1)
    assert compiled.pages == 1
    # Projects arrive best-first, so the tail is what should go.
    assert report["trimmed_projects"] == ["P3", "P2"]
    assert [p["title"] for p in holder["data"]["projects"]] == ["P0", "P1"]


def test_fitting_never_empties_the_projects_section(monkeypatch):
    holder = {"data": _data(3)}
    monkeypatch.setattr(V, "render_latex", lambda d: "tex")

    class _AlwaysTooLong:
        pages, overfull, pdf_path, tex_path = 9, [], None, None

    monkeypatch.setattr(V, "compile_to_pdf", lambda src, output_stem=None: _AlwaysTooLong())
    _, _, report = V._compile_to_fit(holder["data"], {}, stem="s", max_pages=1)
    assert len(holder["data"]["projects"]) == 1
    assert report["trimmed_projects"] == ["P2", "P1"]


def test_fitting_gives_up_rather_than_failing(monkeypatch):
    """An overlong resume is still usable; a crash at the end of the pipeline
    is not. The report is what tells the UI to say something."""
    holder = {"data": _data(20)}
    monkeypatch.setattr(V, "render_latex", lambda d: "tex")

    class _AlwaysTooLong:
        pages, overfull, pdf_path, tex_path = 9, [], None, None

    monkeypatch.setattr(V, "compile_to_pdf", lambda src, output_stem=None: _AlwaysTooLong())
    _, _, report = V._compile_to_fit(holder["data"], {}, stem="s", max_pages=1)
    assert report["fit_failed"] is True
    assert report["fit_attempts"] == V.MAX_FIT_ATTEMPTS
    # The shipped build is the most-trimmed one, not the original.
    assert len(holder["data"]["projects"]) == 20 - V.MAX_FIT_ATTEMPTS


# ── Per-version output files ─────────────────────────────────────────────────

def test_each_version_gets_its_own_output_stem(monkeypatch):
    """A stem of "<user>_<jd>" made every regeneration for the same JD
    overwrite the same PDF, so every stored row pointed at the newest one and
    the history silently showed the wrong resume for old versions.
    """
    import uuid as uuidmod

    stems = []

    class _Compiled:
        pages, overfull, missing_glyphs = 1, [], []
        pdf_path = tex_path = None

    def _spy(src, output_stem=None):
        stems.append(output_stem)
        return _Compiled()

    monkeypatch.setattr(V, "render_latex", lambda d: "tex")
    monkeypatch.setattr(V, "compile_to_pdf", _spy)

    for _ in range(3):
        V._compile_to_fit({"projects": []}, {}, stem=f"resume_{uuidmod.uuid4()}", max_pages=1)

    assert len(set(stems)) == 3, "versions must not share an output filename"
    assert all(s.startswith("resume_") for s in stems)


# ── Trusting a stored PDF path ───────────────────────────────────────────────

def test_pdf_path_is_trusted_only_when_named_for_its_version(tmp_path):
    """Rows written before per-version filenames all point at one shared
    "<user>_<jd>.pdf" that later builds overwrote. Handing that file back
    would show a different version's resume under this version's label."""
    version_id = "d660b230-20b7-484d-b563-48a993716880"

    correct = tmp_path / f"resume_{version_id}.pdf"
    correct.write_bytes(b"%PDF-1.7")
    assert V.version_pdf_path(version_id, str(correct)) == correct

    legacy = tmp_path / "00ee03dd-6bea_5137e869-4d2a.pdf"
    legacy.write_bytes(b"%PDF-1.7")
    assert V.version_pdf_path(version_id, str(legacy)) is None

    other = tmp_path / "resume_eec97426-6718-4599-9181-39d09efd0219.pdf"
    other.write_bytes(b"%PDF-1.7")
    assert V.version_pdf_path(version_id, str(other)) is None


def test_pdf_path_is_none_when_the_file_is_gone(tmp_path):
    version_id = "d660b230-20b7-484d-b563-48a993716880"
    assert V.version_pdf_path(version_id, str(tmp_path / f"resume_{version_id}.pdf")) is None
    assert V.version_pdf_path(version_id, None) is None
    assert V.version_pdf_path(version_id, "") is None
