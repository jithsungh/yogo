from app.services.latex_escape import latex_escape, latex_escape_url

def test_all_special_chars():
    raw = "AT&T earned $5M on 100% of sales; see file_{v2} & more ~ ^ \\"
    result = latex_escape(raw)
    assert "&" not in result.replace(r"\&", "")
    assert "$" not in result.replace(r"\$", "")
    assert "%" not in result.replace(r"\%", "")
    assert "_" not in result.replace(r"\_", "")
    assert r"\textbackslash{}" in result
    assert r"\textasciitilde{}" in result

def test_none_returns_empty():
    assert latex_escape(None) == ""

def test_url_underscore_not_double_escaped():
    url = "https://github.com/user/my_project"
    assert "\\_" not in latex_escape_url(url)


def test_escape_list_escapes_every_item():
    from app.services.latex_escape import escape_list
    assert escape_list(["A&B", "100%"]) == ["A\\&B", "100\\%"]


def test_escape_does_not_double_escape_its_own_output():
    """A backslash must become \\textbackslash{} without the braces it
    introduces then being escaped in turn."""
    assert latex_escape("a\\b") == "a\\textbackslash{}b"


def test_url_keeps_underscores_but_escapes_percent():
    assert latex_escape_url("https://x.com/a_b?c=100%") == "https://x.com/a_b?c=100\\%"


# ── Unicode normalization ────────────────────────────────────────────────────
# XeTeX plus the T1 Helvetica clone silently DROPS any character the font has
# no glyph for. These all arrive in real KB text.

def test_en_dash_becomes_latex_double_hyphen():
    """Dropped silently otherwise: "Staff – SRE" rendered as "Staff  SRE"."""
    assert latex_escape("Staff – SRE") == "Staff -- SRE"


def test_em_dash_and_minus():
    assert latex_escape("a — b") == "a --- b"
    assert latex_escape("− 5") == "-- 5"


def test_smart_quotes_become_tex_quotes():
    assert latex_escape("“quoted” and ‘it’s’") == "``quoted'' and `it's'"


def test_invisible_characters_are_stripped():
    assert latex_escape("a­b​c﻿d") == "abcd"


def test_symbols_map_to_commands():
    assert latex_escape("…") == r"\ldots{}"
    assert latex_escape("5° ± 2") == r"5\textdegree{} \textpm{} 2"
    assert latex_escape("p ≤ q") == r"p $\leq$ q"


def test_unicode_replacement_commands_are_not_re_escaped():
    """\\ldots{} must survive with its backslash and braces intact."""
    assert latex_escape("wait…") == r"\ldots{}".join(["wait", ""])


def test_escaping_still_applies_around_unicode():
    assert latex_escape("R&D – 100%") == r"R\&D -- 100\%"


def test_output_is_pure_ascii_for_common_kb_text():
    raw = "Member of Technical Staff – SRE • 99.77% “fast”"
    assert latex_escape(raw).isascii()
