"""LaTeX string escaping and Unicode normalization.

Every string from the KB or from Gemini output must pass through
latex_escape() before being injected into any template placeholder.
No exceptions. Not even fields that "probably won't have special chars" -
that assumption will be wrong within your first 10 resumes.

Two jobs, in this order:

1. Escape the 10 characters LaTeX treats as syntax. An unescaped `&` is a
   compile error whose message points at the wrong line.

2. Replace typographic Unicode with its LaTeX spelling. This one is quieter
   and worse: Tectonic runs XeTeX, and the resume uses the T1-encoded
   Helvetica clone, which has no glyph for U+2013. An en dash - which arrives
   in almost every scraped job title and every "Aug 2025 - Present" pasted
   from a real resume - is DROPPED, leaving "Member of Technical Staff  Site
   Reliability Engineer". The compile succeeds; the resume is just quietly
   wrong. See pdf_compiler.CompileResult.missing_glyphs for the backstop that
   catches anything not in the table below.
"""

# Use a sentinel that won't appear in normal text to avoid
# double-escaping braces inside \textbackslash{}.
_BACKSLASH_PLACEHOLDER = "\x00BACKSLASH\x00"

_ESCAPE_TABLE = [
    ("&",   r"\&"),
    ("%",   r"\%"),
    ("$",   r"\$"),
    ("#",   r"\#"),
    ("_",   r"\_"),
    ("{",   r"\{"),
    ("}",   r"\}"),
    ("~",   r"\textasciitilde{}"),
    ("^",   r"\textasciicircum{}"),
]

# Applied AFTER escaping: several replacements introduce backslashes and
# braces that must reach the .tex intact rather than being escaped in turn.
_UNICODE_TABLE = [
    ("–", "--"),                  # – en dash
    ("—", "---"),                 # — em dash
    ("−", "--"),                  # − minus sign
    ("‘", "`"),                   # ' left single quote
    ("’", "'"),                   # ' right single quote / apostrophe
    ("“", "``"),                  # " left double quote
    ("”", "''"),                  # " right double quote
    ("…", r"\ldots{}"),           # … ellipsis
    ("•", r"\textbullet{}"),      # • bullet
    ("·", r"\textperiodcentered{}"),
    (" ", r"\nobreakspace{}"),    # non-breaking space
    ("­", ""),                    # soft hyphen - invisible, drop it
    ("​", ""),                    # zero-width space
    ("﻿", ""),                    # BOM
    ("×", r"\texttimes{}"),       # ×
    ("±", r"\textpm{}"),          # ±
    ("°", r"\textdegree{}"),      # °
    ("→", r"$\rightarrow$"),      # →
    ("≤", r"$\leq$"),             # ≤
    ("≥", r"$\geq$"),             # ≥
    ("≈", r"$\approx$"),          # ≈
    ("™", r"\texttrademark{}"),   # ™
    ("®", r"\textregistered{}"),  # ®
    ("©", r"\textcopyright{}"),   # ©
    ("₹", r"\textrupeesign{}"),   # ₹
    ("′", r"$'$"),                # ′ prime
]


def normalize_unicode(text: str) -> str:
    """Rewrite typographic Unicode as LaTeX. Safe on already-escaped text."""
    for char, replacement in _UNICODE_TABLE:
        text = text.replace(char, replacement)
    return text


def latex_escape(text: str | None) -> str:
    """Escape LaTeX special characters and normalize Unicode typography.
    Safe to call on None - returns empty string."""
    if not text:
        return ""
    # Replace backslash first with a placeholder to prevent it from
    # being affected by subsequent replacements (which introduce backslashes).
    text = text.replace("\\", _BACKSLASH_PLACEHOLDER)
    for char, escaped in _ESCAPE_TABLE:
        text = text.replace(char, escaped)
    # After escaping, so the commands introduced here survive intact.
    text = normalize_unicode(text)
    # Now swap placeholder for the final LaTeX command
    return text.replace(_BACKSLASH_PLACEHOLDER, r"\textbackslash{}")


def latex_escape_url(url: str | None) -> str:
    """URLs need different treatment: underscores inside \\href{...} args
    must NOT be escaped (\\href handles them natively), but % must still be
    encoded. Use this for any string going into a \\href{} first argument."""
    if not url:
        return ""
    return url.replace("%", "\\%")


def escape_list(items: list[str]) -> list[str]:
    return [latex_escape(item) for item in items]
