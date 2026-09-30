"""Open a generated resume in Overleaf.

Overleaf has no write API. The only supported hook is "Open in Overleaf",
which creates a NEW Overleaf project from LaTeX you hand it, in one of two
ways:

  * POST a form to https://www.overleaf.com/docs with the source in a `snip`
    field. Needs no public URL, so it works from localhost - which is why it
    is the default here.
  * GET https://www.overleaf.com/docs?snip_uri=<public url to a .tex or .zip>.
    Needs the file served somewhere Overleaf can reach; use this once the
    backend is deployed.

Note on a pattern that turns up in a lot of write-ups: passing base64 in a GET
`snip` parameter. `snip` is a POST field and is not base64-decoded, and a URL
carrying a whole resume is near the practical length limit anyway. Use the
form.
"""
from __future__ import annotations

import html

OVERLEAF_DOCS_ENDPOINT = "https://www.overleaf.com/docs"


def overleaf_snip_uri_url(public_tex_url: str, engine: str = "pdflatex") -> str:
    """"Open in Overleaf" link for a .tex already served at a public URL.

    Requires the storage dir to be reachable from the internet, e.g.
        app.mount("/resumes", StaticFiles(directory=settings.resume_storage_dir))
    """
    from urllib.parse import quote

    return (f"{OVERLEAF_DOCS_ENDPOINT}?snip_uri={quote(public_tex_url, safe='')}"
            f"&engine={engine}")


def overleaf_form_html(
    latex_source: str,
    *,
    label: str = "Open in Overleaf",
    engine: str = "pdflatex",
    file_name: str = "resume.tex",
) -> str:
    """A self-contained HTML form that POSTs the source to Overleaf.

    Rendered with st.components.v1.html(...). Everything is HTML-escaped: the
    LaTeX goes inside a textarea, and an unescaped '</textarea>' anywhere in
    the source would break out of the field.
    """
    escaped = html.escape(latex_source)
    return f"""
<form action="{OVERLEAF_DOCS_ENDPOINT}" method="post" target="_blank"
      style="margin:0">
  <input type="hidden" name="engine" value="{html.escape(engine)}">
  <input type="hidden" name="snip_name" value="{html.escape(file_name)}">
  <textarea name="snip" style="display:none">{escaped}</textarea>
  <button type="submit" style="
      width:100%; padding:0.5rem 0.75rem; cursor:pointer;
      border:1px solid rgba(49,51,63,0.2); border-radius:0.5rem;
      background:#fff; color:#31333f; font-size:0.875rem;
      font-family:'Source Sans Pro',sans-serif;">
    {html.escape(label)}
  </button>
</form>
"""
