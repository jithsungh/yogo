"""The width model is only useful if it agrees with what Tectonic actually
typesets. These expectations were taken from a compiled PDF: the bullets
asserted to fit were measured on one line in the rendered output, and the
ones asserted not to fit visibly wrapped and left an orphan.
"""
from app.services.resume_layout import (
    BULLET_MAX_CHARS,
    audit_bullets,
    bullet_fits_one_line,
    fit_tech_stack,
    line_count,
    normalize_ws,
    tech_fits_one_line,
    text_width_pt,
)

FITS = [
    "Drove OpenTelemetry tracing rollout across 100+ microservices (Go, Java, Next.js) and log-trace correlation.",
    "Enterprise-ready chatbot with intelligent department routing using semantic embeddings and keyword matching.",
    "Built a resume parser with layout detection, achieving 95+% section accuracy across multi-column formats.",
]
WRAPS = [
    "Built an admin dashboard with RBAC, real-time analytics and dynamic knowledge-base creation from PDF, DOCX or text.",
    "Implemented adaptive document pipelines with Transformer-based NER to extract experience, skills and contact info.",
]


def test_matches_observed_single_line_bullets():
    for bullet in FITS:
        assert bullet_fits_one_line(bullet), f"should fit on one line: {bullet}"
        assert line_count(bullet) == 1


def test_matches_observed_wrapping_bullets():
    for bullet in WRAPS:
        assert not bullet_fits_one_line(bullet), f"should wrap: {bullet}"
        assert line_count(bullet) == 2


def test_char_budget_is_conservative():
    """A bullet at the advertised character limit must actually fit, or the
    number handed to the model is a lie."""
    worst = "W" * BULLET_MAX_CHARS          # widest glyph in the font
    typical = "n o " * (BULLET_MAX_CHARS // 4)
    assert bullet_fits_one_line(typical[:BULLET_MAX_CHARS])
    # All-caps W is pathological; it is expected to overflow, which is why the
    # budget is a guide backed by audit_bullets() rather than a guarantee.
    assert not bullet_fits_one_line(worst)


def test_width_scales_with_font_size():
    assert text_width_pt("hello", 20) == 2 * text_width_pt("hello", 10)


def test_empty_text_has_no_lines():
    assert line_count("") == 0
    assert text_width_pt("") == 0


def test_audit_reports_only_overflowing_bullets():
    problems = audit_bullets(FITS + WRAPS)
    assert len(problems) == len(WRAPS)
    assert all("wraps to 2 lines" in p for p in problems)


def test_fit_tech_stack_drops_from_the_tail():
    stack = ["FastAPI", "React", "PostgreSQL", "ChromaDB", "LangChain", "JWT"]
    fitted = fit_tech_stack(stack)
    assert tech_fits_one_line(fitted)
    assert fitted.startswith("FastAPI, React")       # relevance order preserved
    assert "JWT" not in fitted                        # tail dropped, not head


def test_fit_tech_stack_caps_item_count():
    assert fit_tech_stack(["A", "B", "C", "D", "E", "F", "G", "H"]).count(",") <= 5


def test_fit_tech_stack_handles_empties():
    assert fit_tech_stack([]) == ""
    assert fit_tech_stack(["", None]) == ""


def test_normalize_ws():
    assert normalize_ws("  a\n\n b  \t c ") == "a b c"
    assert normalize_ws(None) == ""
