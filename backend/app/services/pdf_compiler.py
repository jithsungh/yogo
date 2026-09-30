"""Compiles a .tex string to a PDF using Tectonic, and reports on the result.

Tectonic is called as a subprocess; it auto-downloads the LaTeX packages it
needs on first run (outbound HTTPS required) and is fully offline afterwards.

Beyond "did it compile", this module extracts two things from the TeX log that
the rest of the pipeline acts on:

  * page count  - so the caller can trim content until the resume fits.
  * overfull hboxes - every one is a line of text sticking past the right
    margin. On a resume that is a visible defect, so it is surfaced rather
    than left buried in a log nobody reads.
"""
from __future__ import annotations

import re
import subprocess
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from app.config import get_settings

# TeX hard-wraps log lines at ~79 characters, mid-token, so for a long output
# filename the "(N pages, M bytes)" tail lands on the NEXT line. Matching has
# to survive that: the log is de-wrapped before this is applied.
_PAGES_RE = re.compile(r"Output written on .{0,300}?\((\d+) pages?[,)]", re.DOTALL)
# Fallback for a log whose "Output written on" line is absent or malformed.
_PAGES_FALLBACK_RE = re.compile(r"\((\d+) pages?, \d+ bytes\)")
_OVERFULL_RE = re.compile(r"Overfull \\hbox \(([\d.]+)pt too wide\)")
# XeTeX drops a character the font has no glyph for and carries on, so this
# warning is the ONLY signal that text vanished from the PDF. latex_escape
# maps the characters that turn up in practice; this catches the rest.
_MISSING_GLYPH_RE = re.compile(r'Missing character: There is no (.) \("([0-9A-F]+)')


class LatexCompileError(Exception):
    """Tectonic exited non-zero. The message carries the log so the caller can
    show it - LaTeX errors are cryptic and the log is the only useful lead."""


@dataclass
class CompileResult:
    pdf_path: Path
    tex_path: Path
    pages: int
    overfull: list[float] = field(default_factory=list)
    missing_glyphs: list[str] = field(default_factory=list)

    @property
    def is_clean(self) -> bool:
        """Nothing visibly wrong with the typesetting: no line sticking past
        the right margin, and no character silently dropped.

        Sub-point overflows are invisible in print and TeX reports them for
        boxes that are essentially exact, so they are not worth flagging.
        """
        return all(pt < 1.0 for pt in self.overfull) and not self.missing_glyphs


def compile_to_pdf(latex_source: str, output_stem: str | None = None,
                   timeout: int = 120) -> CompileResult:
    """Compile `latex_source`; return paths plus what the log says about layout.

    Files land in RESUME_STORAGE_DIR and persist - the caller decides when to
    clean them up.
    """
    settings = get_settings()
    storage = settings.resume_storage_path
    storage.mkdir(parents=True, exist_ok=True)

    stem = output_stem or str(uuid.uuid4())
    tex_path = storage / f"{stem}.tex"
    pdf_path = storage / f"{stem}.pdf"
    log_path = storage / f"{stem}.log"

    # The rendered source inlines the document class (see
    # resume_renderer.class_preamble()), so nothing else needs to be on disk
    # for Tectonic to find.
    tex_path.write_text(latex_source, encoding="utf-8")

    try:
        result = subprocess.run(
            [settings.tectonic_path, "--keep-logs", "--outdir", str(storage), str(tex_path)],
            capture_output=True, text=True, timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise LatexCompileError(
            f"Tectonic binary not found at {settings.tectonic_path!r}. "
            "Install it and set TECTONIC_PATH in .env."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise LatexCompileError(f"Tectonic timed out after {timeout}s.") from exc

    log_text = log_path.read_text(encoding="utf-8", errors="replace") if log_path.exists() else ""

    if result.returncode != 0:
        raise LatexCompileError(
            f"Tectonic compilation failed (exit {result.returncode}).\n\n"
            f"--- Tectonic output ---\n{(result.stderr or '') + (result.stdout or '')}\n"
            f"--- TeX log (errors) ---\n{_log_errors(log_text)}\n"
            f"--- .tex source ---\n{_numbered(latex_source)}"
        )

    if not pdf_path.exists():
        raise LatexCompileError(
            f"Tectonic exited 0 but produced no PDF in {storage}."
        )

    return CompileResult(
        pdf_path=pdf_path,
        tex_path=tex_path,
        pages=_page_count(log_text),
        overfull=[float(pt) for pt in _OVERFULL_RE.findall(_dewrap(log_text))],
        missing_glyphs=_missing_glyphs(log_text),
    )


def _missing_glyphs(log_text: str) -> list[str]:
    """Distinct characters the font could not render, as "– (U+2013)"."""
    seen, out = set(), []
    for char, code in _MISSING_GLYPH_RE.findall(_dewrap(log_text)):
        if code not in seen:
            seen.add(code)
            out.append(f"{char} (U+{code.zfill(4)})")
    return out


def render_preview_images(pdf_path: Path, scale: float = 2.0) -> list[bytes]:
    """Rasterize each page to PNG bytes, for previewing in the UI.

    Browsers are the reason this exists. Embedding a PDF in an <iframe> via a
    `data:` URI is blocked outright by Chromium (Edge shows "This page has
    been blocked by Microsoft Edge"), and Streamlit's own st.pdf needs the
    streamlit-pdf component, which does not load against this Streamlit
    version. Rasterizing server-side sidesteps the whole category: an <img>
    is an <img> everywhere.

    Returns [] if pypdfium2 is not installed, so the preview degrades to the
    download button rather than taking the page down.
    """
    try:
        import pypdfium2
    except ImportError:
        return []

    import io

    document = pypdfium2.PdfDocument(str(pdf_path))
    try:
        pages = []
        for page in document:
            image = page.render(scale=scale).to_pil()
            buffer = io.BytesIO()
            image.save(buffer, format="PNG")
            pages.append(buffer.getvalue())
        return pages
    finally:
        document.close()


def _page_count(log_text: str) -> int:
    """Read the page count off the log line Tectonic always writes.

    Returns 0 only when the log genuinely does not report one. That matters:
    the caller treats the count as "does this fit", so a parse failure that
    quietly returned 0 would disable page fitting altogether and ship a
    three-page resume as if it were one page.

    The PDF itself is not parsed: xdvipdfmx writes object streams, so page
    objects are compressed and cannot be counted without a PDF library.
    """
    dewrapped = _dewrap(log_text)
    match = _PAGES_RE.search(dewrapped) or _PAGES_FALLBACK_RE.search(dewrapped)
    return int(match.group(1)) if match else 0


def _dewrap(log_text: str) -> str:
    """Undo TeX's fixed-width line wrapping.

    TeX breaks log lines at ~79 characters without regard for word or token
    boundaries, so a filename long enough to fill the line splits the record
    that follows it. Joining every line back together restores the original
    text; the regexes above are anchored tightly enough that the lost line
    breaks do not create false matches.
    """
    return "".join(log_text.splitlines())


def _log_errors(log_text: str, limit: int = 40) -> str:
    lines = [l for l in log_text.splitlines() if l.startswith("!") or "Error" in l]
    return "\n".join(lines[:limit]) or "(no explicit error lines in log)"


def _numbered(source: str, limit: int = 60) -> str:
    lines = source.splitlines()[:limit]
    return "\n".join(f"{i:4d} | {l}" for i, l in enumerate(lines, 1))
