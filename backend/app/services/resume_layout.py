"""Measures how wide a string renders in the resume's font, so the tailoring
step can be told a real character budget instead of guessing.

Why this exists: a bullet that is 5% too long does not look 5% worse, it wraps
and leaves a two-word orphan on its own line. Across a dozen bullets that is
the difference between a resume that looks typeset and one that looks
generated. Guidance like "one line ideally, two max" cannot prevent it because
the model has no idea where the line breaks.

The widths below are the Adobe AFM metrics for Helvetica, in 1/1000 em. The
resume class loads `helvet`, which is a Helvetica clone with these same
metrics, so a width computed here matches what Tectonic will typeset to within
rounding. Kerning is ignored; it moves the total by well under one character,
which the safety margin in BULLET_TARGET_CHARS already covers.
"""
from __future__ import annotations

import re

# ── Page arithmetic, mirroring careeros.cls ───────────────────────────────────
_A4_WIDTH_PT = 597.5           # 210mm
_SIDE_MARGINS_PT = 2 * 43.3    # 0.6in each side
TEXT_WIDTH_PT = _A4_WIDTH_PT - _SIDE_MARGINS_PT   # ~511pt
BODY_FONT_PT = 10.0

# itemize leftmargin (1.1em) is lost to the bullet and its indent.
BULLET_INDENT_PT = 1.1 * BODY_FONT_PT
BULLET_WIDTH_PT = TEXT_WIDTH_PT - BULLET_INDENT_PT

# Right-hand column of \projentry, set in \small (~9pt).
TECH_COLUMN_WIDTH_PT = 0.365 * TEXT_WIDTH_PT
TECH_FONT_PT = 9.0

_HELVETICA = {
    " ": 278, "!": 278, '"': 355, "#": 556, "$": 556, "%": 889, "&": 667,
    "'": 191, "(": 333, ")": 333, "*": 389, "+": 584, ",": 278, "-": 333,
    ".": 278, "/": 278, ":": 278, ";": 278, "<": 584, "=": 584, ">": 584,
    "?": 556, "@": 1015, "[": 278, "\\": 278, "]": 278, "^": 469, "_": 556,
    "`": 333, "{": 334, "|": 260, "}": 334, "~": 584,
    "A": 667, "B": 667, "C": 722, "D": 722, "E": 667, "F": 611, "G": 778,
    "H": 722, "I": 278, "J": 500, "K": 667, "L": 556, "M": 833, "N": 722,
    "O": 778, "P": 667, "Q": 778, "R": 722, "S": 667, "T": 611, "U": 722,
    "V": 667, "W": 944, "X": 667, "Y": 667, "Z": 611,
    "a": 556, "b": 556, "c": 500, "d": 556, "e": 556, "f": 278, "g": 556,
    "h": 556, "i": 222, "j": 222, "k": 500, "l": 222, "m": 833, "n": 556,
    "o": 556, "p": 556, "q": 556, "r": 333, "s": 500, "t": 278, "u": 556,
    "v": 500, "w": 722, "x": 500, "y": 500, "z": 500,
}
_DIGIT_WIDTH = 556
# Bold Helvetica is wider; a flat factor is within a few units/em of the real
# bold metrics over normal prose and keeps one table instead of two.
_BOLD_FACTOR = 1.06
_FALLBACK_WIDTH = 556   # en/em dashes, accents, anything outside the table


def text_width_pt(text: str, font_pt: float = BODY_FONT_PT, bold: bool = False) -> float:
    """Rendered width of `text` in points. Expects PLAIN text, not LaTeX:
    measure before escaping, because `\\%` occupies one glyph, not two."""
    total = sum(
        _DIGIT_WIDTH if ch.isdigit() else _HELVETICA.get(ch, _FALLBACK_WIDTH)
        for ch in text
    )
    width = total / 1000.0 * font_pt
    return width * _BOLD_FACTOR if bold else width


def line_count(text: str, width_pt: float = BULLET_WIDTH_PT,
               font_pt: float = BODY_FONT_PT, bold: bool = False) -> int:
    """Number of typeset lines, by greedy word wrapping - the same thing TeX
    does for a single paragraph with no hyphenation."""
    words = text.split()
    if not words:
        return 0
    lines, current = 1, 0.0
    space = text_width_pt(" ", font_pt, bold)
    for word in words:
        w = text_width_pt(word, font_pt, bold)
        if current and current + space + w > width_pt:
            lines += 1
            current = w
        else:
            current += (space if current else 0) + w
    return lines


def bullet_fits_one_line(text: str) -> bool:
    return text_width_pt(text) <= BULLET_WIDTH_PT


def tech_fits_one_line(tech: str) -> bool:
    """The bracketed tech stack sits in a narrow right-hand column; when it
    wraps, the project title and its stack stop sharing a baseline."""
    return text_width_pt(f"[ {tech} ]", TECH_FONT_PT, bold=True) <= TECH_COLUMN_WIDTH_PT


# A character budget is what an LLM can actually follow; a point budget is not.
# Derived from BULLET_WIDTH_PT over average English prose in this font
# (~4.93pt/char at 10pt), then rounded down for headroom.
BULLET_MAX_CHARS = 108
BULLET_MIN_CHARS = 70
BULLET_TARGET_CHARS = 100


def audit_bullets(bullets: list[str]) -> list[str]:
    """Human-readable warnings for bullets that will not sit on one line.
    Used to surface layout problems in the UI rather than silently shipping
    a resume full of two-word orphan lines."""
    problems = []
    for b in bullets:
        if not bullet_fits_one_line(b):
            n = line_count(b)
            problems.append(
                f"{len(b)} chars wraps to {n} lines "
                f"(budget {BULLET_MAX_CHARS}): {b[:60]}..."
            )
    return problems


def fit_tech_stack(items: list[str], max_items: int = 6) -> str:
    """Join a tech stack, dropping trailing entries until it fits the column.
    Keeps leading items because the tailoring step orders them by relevance to
    the JD, so the first ones are the ones worth showing."""
    kept = [i for i in items if i][:max_items]
    while kept:
        joined = ", ".join(kept)
        if tech_fits_one_line(joined):
            return joined
        kept.pop()
    return ""


_WS = re.compile(r"\s+")


def normalize_ws(text: str) -> str:
    return _WS.sub(" ", (text or "").strip())
