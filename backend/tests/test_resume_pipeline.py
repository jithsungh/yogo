"""End-to-end: KB rows + a stubbed model response -> LaTeX -> compiled PDF.

Gemini is stubbed, Tectonic is real. Everything between the model's JSON and
the finished PDF is exercised here, which is where the formatting bugs live -
an unescaped ampersand, a tech stack too wide for its column, a bullet that
wraps and orphans two words.

The compile assertions are the point: a resume that compiles but has overfull
hboxes has text hanging past the right margin, which is exactly the defect
this format was rebuilt to prevent.
"""
import shutil
from dataclasses import dataclass

import pytest

from app.services import resume_tailor as T
from app.services.pdf_compiler import compile_to_pdf
from app.services.resume_renderer import render_latex

TECTONIC = shutil.which("tectonic")
needs_tectonic = pytest.mark.skipif(not TECTONIC, reason="tectonic not installed")


@pytest.fixture
def source():
    """Shaped exactly like collect_source_data() returns, including the
    awkward real-world cases: an ampersand in a company name, a project with
    an oversized tech stack, and an entry with no dates recorded."""
    return {
        "profile": {
            "full_name": "Jithsungh Sai Vallabhaneni",
            "email": "jithsungh@gmail.com",
            "phone": "+91 7032909135",
            "location": "Hyderabad, India",
            "links": {"github": "github.com/jithsungh",
                      "linkedin": "https://linkedin.com/in/jithsung",
                      "portfolio": "https://jithsungh.vercel.app/"},
            "summary": "Fallback summary on file.",
        },
        "experiences": [
            {"id": "e1", "company": "TechMojo Solutions & Co",
             "role_title": "Site Reliability Engineer",
             "date_range": "Aug 2025 – Present",
             "raw_bullets": ["Drove OpenTelemetry rollout.", "Built a load simulator."],
             "tech_stack": ["Go", "Java"]},
            {"id": "e2", "company": "SRM AP University", "role_title": "Research Intern",
             "date_range": "", "raw_bullets": ["Built an MRI classifier."], "tech_stack": []},
        ],
        "projects": [
            {"id": "p1", "title": "Adaptive Knowledge Chatbot – RAG",
             "github_url": "https://github.com/jithsungh/chatbot", "live_url": None,
             "tech_stack": ["FastAPI", "React", "PostgreSQL", "ChromaDB", "LangChain", "JWT"],
             "raw_bullets": ["Built a chatbot."], "prefilter_score": 9.0},
            {"id": "p2", "title": "Smart Resume Parser – ML+NLP",
             "github_url": "https://github.com/jithsungh/resume_parser", "live_url": None,
             "tech_stack": ["FastAPI", "PyMuPDF", "EasyOCR", "spaCy", "Transformers", "React"],
             "raw_bullets": ["Built a parser."], "prefilter_score": 6.0},
        ],
        "skills": [
            {"name": "Python", "category": "language"},
            {"name": "Go", "category": "language"},
            {"name": "Golang", "category": "language"},
            {"name": "PostgreSQL", "category": "database"},
            {"name": "Docker", "category": "devops"},
            {"name": "AWS", "category": "cloud"},
            {"name": "Problem-Solving", "category": "soft_skill"},
        ],
        "education": [
            {"degree": "B.Tech in Computer Science", "institution": "SRM AP University",
             "date_range": "2022 – 2026", "score": "CGPA: 9.15", "highlights": None},
        ],
        "certifications": [
            {"title": "MongoDB Certified Associate Developer", "issuer": "MongoDB",
             "date": "2025", "url": "https://bit.ly/4nRHJ76"},
            {"title": "AWS Fundamentals", "issuer": "Coursera", "date": "2024", "url": None},
        ],
        "match_result": {"matched_skills": ["Go", "Docker", "PostgreSQL"],
                         "missing_skills": ["Kafka"], "surplus_skills": ["Figma"]},
        "jd": {"company": "Razorpay", "role_title": "SDE-1", "requirements": {}},
    }


@pytest.fixture
def tailored():
    """What a well-behaved model returns, post-validation."""
    return {
        "summary": "Backend engineer with production Go and Docker experience "
                   "building observability tooling across 100+ microservices.",
        "experiences": [
            {"entry_id": "e1", "bullets": [
                "Drove OpenTelemetry tracing rollout across 100+ microservices in Go and Java.",
                "Built a load simulator sustaining 500 req/s per pod, monitored via Prometheus.",
            ]},
            {"entry_id": "e2", "bullets": [
                "Built a ResNet50 MRI classifier reaching 99.77% accuracy across four classes.",
            ]},
        ],
        "selected_projects": [
            {"entry_id": "p1", "rank": 1, "reason": "RAG backend on PostgreSQL",
             "tech_stack": ["FastAPI", "PostgreSQL", "ChromaDB", "LangChain", "React", "JWT"],
             "bullets": ["Built a FastAPI chatbot routing queries by department embeddings.",
                         "Cut redundant queries by deduplicating questions into canonical summaries."]},
            {"entry_id": "p2", "rank": 2, "reason": "Document pipeline at scale",
             "tech_stack": ["FastAPI", "spaCy", "Transformers", "PyMuPDF", "EasyOCR", "React"],
             "bullets": ["Built a parser with layout detection hitting 95%+ section accuracy."]},
        ],
        "skill_priority": ["Go", "Docker", "PostgreSQL"],
    }


# ── Assembly ─────────────────────────────────────────────────────────────────

def test_assemble_escapes_every_string_reaching_the_template(source, tailored):
    data, _ = T._assemble(source, tailored, max_projects=4)
    assert data["experiences"][0]["company"] == "TechMojo Solutions \\& Co"
    assert "\\_" in data["projects"][1]["link_text"]     # resume_parser underscore
    assert "\\%" in data["experiences"][1]["bullets"][0]


def test_assemble_omits_the_meta_line_when_no_dates_are_recorded(source, tailored):
    data, _ = T._assemble(source, tailored, max_projects=4)
    # En dash normalized to LaTeX `--` on the way in; see latex_escape.
    assert data["experiences"][0]["meta"] == "Aug 2025 -- Present"
    assert data["experiences"][1]["meta"] == ""


def test_assemble_trims_tech_stack_to_fit_its_column(source, tailored):
    from app.services.resume_layout import tech_fits_one_line
    data, _ = T._assemble(source, tailored, max_projects=4)
    for proj in data["projects"]:
        assert tech_fits_one_line(proj["tech"]), proj["tech"]


def test_assemble_respects_max_projects(source, tailored):
    data, _ = T._assemble(source, tailored, max_projects=1)
    assert len(data["projects"]) == 1
    assert data["projects"][0]["title"].startswith("Adaptive")   # rank 1 kept


def test_assemble_builds_header_links_in_a_stable_order(source, tailored):
    data, _ = T._assemble(source, tailored, max_projects=4)
    assert [i["label"] for i in data["profile"]["link_items"]] == ["LinkedIn", "GitHub", "Portfolio"]
    github = data["profile"]["link_items"][1]
    assert github["text"] == "github.com/jithsungh"          # display form, no scheme
    assert github["url"] == "https://github.com/jithsungh"   # bare URL got a scheme


def test_assemble_falls_back_to_the_profile_summary(source, tailored):
    tailored["summary"] = ""
    data, _ = T._assemble(source, tailored, max_projects=4)
    assert data["summary"] == "Fallback summary on file."


def test_report_flags_entries_with_no_dates(source, tailored):
    _, report = T._assemble(source, tailored, max_projects=4)
    assert report["missing_dates"] == ["Research Intern"]


def test_report_lists_chosen_projects_with_reasons(source, tailored):
    _, report = T._assemble(source, tailored, max_projects=4)
    assert [p["title"] for p in report["selected_projects"]] == [
        "Adaptive Knowledge Chatbot – RAG", "Smart Resume Parser – ML+NLP"]
    assert report["selected_projects"][0]["reason"] == "RAG backend on PostgreSQL"


def test_report_flags_bullets_that_will_wrap(source, tailored):
    tailored["experiences"][0]["bullets"] = ["W" * 200]
    _, report = T._assemble(source, tailored, max_projects=4)
    assert report["long_bullets"]


# ── Rendering ────────────────────────────────────────────────────────────────

def test_render_produces_a_self_contained_document(source, tailored):
    """No \\documentclass{careeros}: the class is inlined, so the .tex compiles
    on its own in Overleaf or anywhere the user forwards it."""
    data, _ = T._assemble(source, tailored, max_projects=4)
    tex = render_latex(data)
    assert "\\documentclass[a4paper,10pt]{article}" in tex
    assert "{careeros}" not in tex
    assert "\\RequirePackage" not in tex
    assert tex.rstrip().endswith("\\end{document}")
    for section in ("Summary", "Skills", "Work Experience", "Projects",
                    "Education", "Awards"):
        assert f"\\section*{{{section}" in tex or section in tex


def test_render_omits_sections_with_no_data(source, tailored):
    data, _ = T._assemble(source, tailored, max_projects=4)
    data["certifications"] = []
    data["projects"] = []
    tex = render_latex(data)
    assert "Awards" not in tex
    assert "\\section*{Projects}" not in tex


def test_render_honours_section_order(source, tailored):
    data, _ = T._assemble(source, tailored, max_projects=4)
    data["section_order"] = ["education", "summary"]
    tex = render_latex(data)
    assert tex.index("\\section*{Education}") < tex.index("\\section*{Summary}")
    assert "\\section*{Projects}" not in tex


def test_render_rejects_a_malformed_data_dict(source, tailored):
    """StrictUndefined: a missing field is a bug, not a blank line on a
    document someone is about to send to an employer."""
    from jinja2 import UndefinedError
    data, _ = T._assemble(source, tailored, max_projects=4)
    del data["profile"]["full_name"]
    with pytest.raises(UndefinedError):
        render_latex(data)


# ── Compilation ──────────────────────────────────────────────────────────────

@dataclass
class _Settings:
    resume_storage_dir: str
    tectonic_path: str = "tectonic"


@needs_tectonic
def test_compiles_to_a_clean_single_page(source, tailored, tmp_path, monkeypatch):
    monkeypatch.setattr("app.services.pdf_compiler.get_settings",
                        lambda: _Settings(str(tmp_path), TECTONIC))
    data, _ = T._assemble(source, tailored, max_projects=4)

    result = compile_to_pdf(render_latex(data), output_stem="resume")

    assert result.pdf_path.exists() and result.pdf_path.stat().st_size > 0
    assert result.pages == 1
    assert result.is_clean, f"text hangs past the right margin: {result.overfull}"


@needs_tectonic
def test_compiles_with_hostile_characters_in_every_field(tmp_path, monkeypatch, source, tailored):
    """Every LaTeX special character, in the fields most likely to carry one."""
    monkeypatch.setattr("app.services.pdf_compiler.get_settings",
                        lambda: _Settings(str(tmp_path), TECTONIC))
    hostile = r"A&B 50% $100 #1 a_b {x} ~y ^z \d"
    source["profile"]["full_name"] = hostile
    source["experiences"][0]["company"] = hostile
    source["projects"][0]["title"] = hostile
    source["certifications"][0]["title"] = hostile
    tailored["summary"] = hostile

    data, _ = T._assemble(source, tailored, max_projects=4)
    result = compile_to_pdf(render_latex(data), output_stem="hostile")
    assert result.pages >= 1


@needs_tectonic
def test_compile_error_carries_the_log(tmp_path, monkeypatch):
    from app.services.pdf_compiler import LatexCompileError
    monkeypatch.setattr("app.services.pdf_compiler.get_settings",
                        lambda: _Settings(str(tmp_path), TECTONIC))
    with pytest.raises(LatexCompileError) as exc:
        compile_to_pdf("\\documentclass{article}\n\\begin{document}\\undefinedcmd\n\\end{document}")
    assert "Tectonic" in str(exc.value)


def test_missing_tectonic_binary_gives_an_actionable_error(tmp_path, monkeypatch):
    from app.services.pdf_compiler import LatexCompileError
    monkeypatch.setattr("app.services.pdf_compiler.get_settings",
                        lambda: _Settings(str(tmp_path), "/nonexistent/tectonic"))
    with pytest.raises(LatexCompileError, match="TECTONIC_PATH"):
        compile_to_pdf("\\documentclass{article}\\begin{document}x\\end{document}")


# ── Page counting ────────────────────────────────────────────────────────────

def test_page_count_survives_a_wrapped_log_line():
    """TeX wraps log lines at ~79 chars mid-token. With a long output stem -
    which is exactly what "<user_uuid>_<jd_uuid>" produces - the
    "(1 page, ...)" tail lands on the next line. A parse failure here returns
    0, which reads as "fits on one page" and silently disables page fitting.
    """
    from app.services.pdf_compiler import _page_count

    stem = "00ee03dd-6bea-4eb5-be79-6651679eedbb_5137e869-4d2a-440c-b9c4-0cee59475b82"
    wrapped = f"Output written on {stem}\n.xdv (3 pages, 37900 bytes).\n"
    assert _page_count(wrapped) == 3


def test_page_count_reads_an_unwrapped_log_line():
    from app.services.pdf_compiler import _page_count
    assert _page_count("Output written on r.xdv (1 page, 6784 bytes).") == 1


def test_page_count_is_zero_when_the_log_says_nothing():
    from app.services.pdf_compiler import _page_count
    assert _page_count("This log has no output record.") == 0


@needs_tectonic
def test_page_count_is_read_correctly_for_a_long_output_stem(source, tailored, tmp_path, monkeypatch):
    """The end-to-end version of the above, through a real compile."""
    monkeypatch.setattr("app.services.pdf_compiler.get_settings",
                        lambda: _Settings(str(tmp_path), TECTONIC))
    data, _ = T._assemble(source, tailored, max_projects=4)
    long_stem = "00ee03dd-6bea-4eb5-be79-6651679eedbb_5137e869-4d2a-440c-b9c4-0cee59475b82"
    result = compile_to_pdf(render_latex(data), output_stem=long_stem)
    assert result.pages == 1


# ── Dropped glyphs ───────────────────────────────────────────────────────────

def test_missing_glyphs_are_extracted_and_deduplicated():
    from app.services.pdf_compiler import _missing_glyphs
    log = ('Missing character: There is no – ("2013) in font phvb8t!\n'
           'Missing character: There is no – ("2013) in font phvb8t!\n'
           'Missing character: There is no ₹ ("20B9) in font phvr8t!\n')
    assert _missing_glyphs(log) == ["– (U+2013)", "₹ (U+20B9)"]


@needs_tectonic
def test_real_kb_text_loses_no_characters(source, tailored, tmp_path, monkeypatch):
    """The regression that shipped "Member of Technical Staff  SRE": an en
    dash reaches the font, the font has no glyph, XeTeX drops it and the
    compile still succeeds."""
    monkeypatch.setattr("app.services.pdf_compiler.get_settings",
                        lambda: _Settings(str(tmp_path), TECTONIC))
    source["experiences"][0]["role_title"] = "Member of Technical Staff – SRE"
    source["certifications"][0]["title"] = "MongoDB Developer – Global “Cert” • 2025"
    tailored["summary"] = "Ranges 2022–2026, quotes “like this”, and ≥ 5 years…"

    data, _ = T._assemble(source, tailored, max_projects=4)
    result = compile_to_pdf(render_latex(data), output_stem="glyphs")

    assert result.missing_glyphs == [], f"characters dropped: {result.missing_glyphs}"
    assert result.is_clean


# ── Preview rendering ────────────────────────────────────────────────────────

@needs_tectonic
def test_preview_renders_one_png_per_page(source, tailored, tmp_path, monkeypatch):
    """The in-app preview. Chromium blocks a PDF embedded as a data: URI, so
    the page is rasterized server-side and shown as an image instead."""
    from app.services.pdf_compiler import render_preview_images

    monkeypatch.setattr("app.services.pdf_compiler.get_settings",
                        lambda: _Settings(str(tmp_path), TECTONIC))
    data, _ = T._assemble(source, tailored, max_projects=4)
    result = compile_to_pdf(render_latex(data), output_stem="preview")

    pages = render_preview_images(result.pdf_path)
    assert len(pages) == result.pages
    assert all(png.startswith(b"\x89PNG") for png in pages)
    assert all(len(png) > 10_000 for png in pages)


def test_preview_degrades_when_pypdfium2_is_absent(monkeypatch, tmp_path):
    """Missing preview must not take the page down - the download still works."""
    import builtins

    from app.services.pdf_compiler import render_preview_images

    real_import = builtins.__import__

    def no_pypdfium2(name, *args, **kwargs):
        if name == "pypdfium2":
            raise ImportError("not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_pypdfium2)
    assert render_preview_images(tmp_path / "missing.pdf") == []
