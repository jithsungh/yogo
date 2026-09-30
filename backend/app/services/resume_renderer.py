"""Renders a data dict into a LaTeX string using the master Jinja2 template.

The caller is responsible for ensuring all string values in the data dict have
already been passed through latex_escape() - this layer does NOT auto-escape,
because it cannot tell a plain-text field from an already-safe LaTeX fragment.
resume_tailor.build_tailored_resume_data() is the function that owes that
guarantee; anything else calling render_latex() owes it too.
"""
import re
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

TEMPLATE_DIR = Path(__file__).resolve().parents[3] / "latex_templates"
TEMPLATE_NAME = "master_resume.tex.j2"
CLASS_FILE = TEMPLATE_DIR / "careeros.cls"

# Order the sections appear in. Summary and Skills lead because for a
# candidate whose strongest evidence is projects rather than tenure, those two
# are what a six-second scan actually reads, and they are the two sections the
# tailoring step rewrites per JD. Reorder this list to change the layout - it
# is the only thing that needs to change.
DEFAULT_SECTION_ORDER = [
    "summary",
    "skills",
    "experience",
    "projects",
    "education",
    "certifications",
]


def _make_env() -> Environment:
    return Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        # Override all Jinja2 delimiters to avoid clashing with LaTeX syntax.
        # {{ }} -> << >>   (variable substitution)
        # {% %} -> ##      (block tags: for, if, etc.)
        # {# #} -> ###     (comments)
        block_start_string="##",
        block_end_string="##",
        variable_start_string="<<",
        variable_end_string=">>",
        comment_start_string="###",
        comment_end_string="###",
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
        # A missing key is a bug in the data builder, not a blank line in a
        # resume someone is about to send to an employer. Fail loudly.
        undefined=StrictUndefined,
    )


_env = _make_env()

_REQUIRED_KEYS = (
    "profile",
    "summary",
    "skills_sections",
    "experiences",
    "projects",
    "education",
    "certifications",
)


def class_preamble() -> str:
    """careeros.cls rewritten as an inline preamble.

    Every generated .tex is self-contained. A document that says
    \\documentclass{careeros} only compiles next to a copy of the class file,
    which means it does not compile in Overleaf (the "Open in Overleaf" hook
    posts a single file) and does not compile for anyone the user forwards the
    .tex to. Inlining costs ~120 lines in the output and removes that whole
    class of failure.

    careeros.cls stays the one place the style is authored; this is a
    mechanical transform of it, not a second copy.
    """
    source = CLASS_FILE.read_text(encoding="utf-8")
    lines = []
    for line in source.splitlines():
        if line.startswith(("\\NeedsTeXFormat", "\\ProvidesClass")):
            continue
        # \LoadClass[opts]{article} is how a class inherits; a document says
        # \documentclass[opts]{article} to do the same thing.
        line = re.sub(r"^\\LoadClass(\[[^\]]*\])?\{(\w+)\}",
                      r"\\documentclass\1{\2}", line)
        # \RequirePackage is the class-file spelling of \usepackage.
        line = line.replace("\\RequirePackage", "\\usepackage")
        lines.append(line)
    return "\n".join(lines).strip()


def render_latex(data: dict) -> str:
    """Render master_resume.tex.j2 with the provided data dict.
    Returns a self-contained .tex source as a string."""
    payload = dict(data)
    payload.setdefault("section_order", DEFAULT_SECTION_ORDER)
    payload["preamble"] = class_preamble()
    for key in _REQUIRED_KEYS:
        payload.setdefault(key, "" if key == "summary" else [])
    template = _env.get_template(TEMPLATE_NAME)
    return template.render(**payload)


def template_fingerprint() -> str:
    """sha256 over the template + class file.

    Folded into the resume cache key so that editing the layout invalidates
    every cached resume - otherwise a template fix would silently not apply to
    any JD that had already been generated.
    """
    import hashlib

    h = hashlib.sha256()
    for path in (TEMPLATE_DIR / TEMPLATE_NAME, CLASS_FILE):
        h.update(path.read_bytes())
    return h.hexdigest()
