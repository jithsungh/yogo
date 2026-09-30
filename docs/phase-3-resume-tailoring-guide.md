# Phase 3 — Resume Tailoring

How the resume generator works, why each part is shaped the way it is, and
what to change when you want different output.

This replaces the original build spec. The spec described a working pipeline
but produced bad resumes; §11 records what went wrong and what changed, so the
same decisions don't get re-litigated.

---

## 1. What it does

Given one job description, the generator produces a one-page, ATS-readable PDF
tailored to it:

```
KB rows ─┐
         ├─► fingerprint ─► cache hit? ─► return stored PDF
match ───┤                      │ no
JD ──────┘                      ▼
                    dedupe projects
                          ▼
                    rank by JD keyword overlap  (deterministic, no LLM)
                          ▼
                    Gemini: select + rewrite + order skills
                          ▼
                    validate against source     (drops anything invented)
                          ▼
                    repair overlong bullets     (one pass, Gemini)
                          ▼
                    render LaTeX ─► compile ─► too long? drop lowest-ranked
                          ▲                              │  project, recompile
                          └──────────────────────────────┘
                          ▼
                    store with fingerprint
```

Everything before the first Gemini call is deterministic and free. Everything
after it is validated against what went in.

---

## 2. Files

| File | Role |
|---|---|
| `latex_templates/careeros.cls` | The style: geometry, fonts, section headings, row commands. Single source of truth for layout. |
| `latex_templates/master_resume.tex.j2` | Jinja2 template. Section macros plus an order loop. |
| `backend/app/services/resume_layout.py` | Font metrics. Answers "will this bullet fit on one line". |
| `backend/app/services/latex_escape.py` | Escapes the 10 LaTeX special characters. |
| `backend/app/services/resume_tailor.py` | Dedupe, rank, prompt, validate, assemble. The core. |
| `backend/app/services/resume_renderer.py` | Jinja2 env, inlines the class into the output. |
| `backend/app/services/pdf_compiler.py` | Tectonic subprocess; reads page count and overfull boxes back out of the log. |
| `backend/app/services/resume_version_service.py` | Caching, page fitting, persistence. |
| `backend/app/services/overleaf_service.py` | "Open in Overleaf" POST form. |
| `frontend/pages/11_Resume_Generator.py` | Streamlit UI. |

---

## 3. The format

Derived from `data/resumes/base_resume.tex` — the hand-tuned resume that
already compiled cleanly. The class reproduces its **visual** output exactly:
a4paper 10pt, Helvetica (`helvet` + `\sfdefault`), 0.6in side margins,
uppercase bold section headings with a full-width rule, blue hyperlinks.

What changed is the **mechanism**, in two places, both because a generated
resume gets strings the hand-written one never did:

**Two-column rows.** The original used `\textbf{Title} \hfill \textbf{[stack]}`.
That silently overflows the right margin once the two strings exceed
`\textwidth`. The class uses two fixed-width minipages (`\tworow`), which wrap
instead of overflowing.

**Bullets.** The original used a literal `- ` prefix. A wrapped line then
starts hard against the left margin instead of hanging under the text. The
class uses a tight `itemize`, which gives a proper hanging indent.

### Section order

One list, in `resume_renderer.py`:

```python
DEFAULT_SECTION_ORDER = ["summary", "skills", "experience",
                         "projects", "education", "certifications"]
```

Reorder it and the template follows. Summary and Skills lead because for a
candidate whose strongest evidence is projects rather than tenure, those are
what a six-second scan reads and the two sections tailoring actually rewrites.

### Self-contained output

`render_latex()` inlines `careeros.cls` into every generated `.tex` (see
`class_preamble()`), converting `\LoadClass` → `\documentclass` and
`\RequirePackage` → `\usepackage`. A document saying
`\documentclass{careeros}` only compiles next to a copy of the class file —
so it would not compile in Overleaf, which takes a single file, nor for
anyone you forward the `.tex` to. The class file stays the one place the
style is authored; the inlining is a mechanical transform of it.

---

## 4. Line length is not cosmetic

This is the part that separates a typeset resume from a generated one, and it
cannot be solved by telling the model "keep bullets short".

`resume_layout.py` carries the Adobe AFM widths for Helvetica. `helvet` is a
Helvetica clone with the same metrics, so a width computed in Python matches
what Tectonic typesets:

```python
BULLET_WIDTH_PT = 499.9      # \textwidth minus the itemize indent
BULLET_MAX_CHARS = 108       # derived from that, rounded down for headroom
```

This model was checked against a compiled PDF: the bullets it says fit were
measured on one line, and the ones it says wrap visibly wrapped. Those exact
strings are pinned in `tests/test_resume_layout.py`, so a change to the
geometry that breaks the relationship fails the suite.

The budget is used three times:

1. **In the prompt** — the model is given `108` as a hard limit and 70–105 as
   the target, with an explanation of *why* (an orphan line looks careless).
2. **In a repair pass** — anything still over goes back to Gemini once, to be
   shortened. Truncating in code is not an option; it would cut a resume claim
   mid-word.
3. **In the report** — anything still over after that is surfaced in the UI
   rather than silently shipped.

`fit_tech_stack()` does the same job for the bracketed tech stack in the
narrow right-hand column, dropping items from the tail (the tail is the least
JD-relevant, because the model orders them).

---

## 4b. Unicode, and characters that vanish

Tectonic runs **XeTeX**, and the resume uses the T1-encoded Helvetica clone.
When that font has no glyph for a character, XeTeX drops it, logs a warning,
and exits 0. The compile succeeds and the resume is quietly wrong:

```
Member of Technical Staff – SRE   ->   Member of Technical Staff  SRE
```

An en dash is in almost every scraped job title and every
"Aug 2025 – Present" pasted out of a real resume, so this is not an edge case.

`latex_escape()` therefore does two jobs: escapes the ten LaTeX specials
*and* rewrites typographic Unicode into LaTeX (`–`→`--`, `—`→`---`, smart
quotes, `…`, `•`, non-breaking space, `≤`, `°`, `₹`, and the invisible
characters that come with copy-paste). See `_UNICODE_TABLE`.

For anything not in that table, `pdf_compiler` parses `Missing character`
warnings out of the TeX log into `CompileResult.missing_glyphs`, which the UI
shows as an error. Nothing disappears silently. Add the character to
`_UNICODE_TABLE` when one turns up.

---

## 5. Project dedup

The KB holds a GitHub-imported row *and* a hand-written row for the same
project. On the real dataset that was 20 rows for 15 projects, and the old
pipeline printed "Resume Parser" twice.

Projects are grouped by normalized repo URL (scheme, `www.`, trailing slash
and `.git` stripped, lowercased), falling back to a normalized title. Within a
group:

- **Title**: the one containing a space wins. A human typed
  "Smart Resume Parser – ML+NLP"; GitHub supplied `resume_parser`.
- **Tech stack**: the curated one wins. GitHub gives detected *languages*
  ("Jupyter Notebook"); the human entry gives *frameworks* ("FastAPI, spaCy").
- **Bullets**: unioned. The import often carries detail the hand-written row
  lacks.

---

## 6. Selection

Twenty projects is not a resume. Selection happens in two stages:

**Pre-filter (deterministic).** Each project is scored by weighted keyword
overlap with the JD: matched skills 4.0, required skills 3.0, nice-to-haves
1.5, responsibility tokens 0.4, minus a penalty for having under two source
bullets. Matching is word-boundary anchored, so `Go` does not score a hit
inside `Google`. Top 8 go to the model.

**Model.** Picks and ranks the best 4 for this JD, with a short reason for
each (shown in the UI). `MAX_PROJECTS` and `PROJECT_CANDIDATES` are at the top
of `resume_tailor.py`.

---

## 7. The prompt

In `resume_tailor.py` as `TAILOR_PROMPT`. Structure:

1. **Target job** — company, role, seniority, required/nice-to-have skills,
   responsibilities.
2. **Match analysis** — matched (surface these), missing (never imply these),
   surplus (keep off the page). Stated as ground truth from Phase 2.
3. **Hard rules** — source-only, allowed operations, banned filler words,
   "never round or drop a real number".
4. **Line length** — the character budget from §4, with the reason.
5. **Style** — three of the candidate's own real bullets as few-shot examples.
   Not invented ones; the register to match is the one already on file.
6. **Source material** — skills inventory, all experience entries, the 8
   project candidates, each with an id.
7. **Output schema** — summary, per-entry bullets, selected projects with
   rank/reason/reordered stack, and `skill_priority`.

### The prompt is not the safety mechanism

`_validate_against_source()` is. Whatever the prompt says, the model's output
is intersected with its input:

- an `entry_id` not in the source → the entry is dropped
- a tech item not in that project's recorded stack → dropped (Kafka cannot
  appear because the model thought it fit the JD)
- a skill name not in the KB → dropped from `skill_priority`
- bullets are capped, stripped of list markers, whitespace-normalized

Anything invented is discarded rather than trusted. The prompt reduces how
often that has to fire; it does not decide whether a claim reaches the page.

---

## 8. Reuse

Regenerating costs two Gemini calls and a Tectonic run, and almost every click
happens with nothing changed.

`compute_content_hash()` fingerprints **every** input: profile, experiences,
projects, skills, education, certifications, match result, JD, plus
`template_fingerprint()` (sha256 of the template *and* the class file) and
`TAILOR_VERSION`. Identical hash plus a PDF still on disk → the stored PDF is
returned and no LLM is called.

Including the template files matters: without them, editing the layout would
silently not reach any JD already generated. Bump `TAILOR_VERSION` when you
change the prompt for the same reason.

`force=True` (the "Force regenerate" checkbox) bypasses the cache. The only
reason to use it is wanting a different draft from the model.

---

## 9. Page fitting

How many projects fit depends on how long the bullets came out, which is not
knowable in advance. So the resume is compiled, the page count read back out
of the TeX log, and if it is over the limit the lowest-ranked project is
dropped and it is compiled again — up to `MAX_FIT_ATTEMPTS`.

Trimming never re-runs the model: the project list is already rank-ordered, so
slicing the tail removes exactly the least JD-relevant content. Each retry
costs one Tectonic run (~2s).

If it still does not fit, the most-trimmed build ships and `fit_failed` is set,
which the UI surfaces. An overlong resume is usable; a crash at the end of the
pipeline is not.

The compiler also parses `Overfull \hbox` warnings out of the log. Each one is
a line of text hanging past the right margin. They are reported, and
`tests/test_resume_pipeline.py` asserts a clean compile has none.

---

## 9b. Version history

Every build is stored with its own LaTeX, its build report, and a PDF named
`resume_<version_id>.pdf`. The **Previous resumes** tab lists them and reopens
any one with the same preview, downloads and Overleaf button as a fresh build.

Filenames are per-version on purpose. They used to be `<user_id>_<jd_id>`, so
every regeneration for the same JD overwrote the same PDF and every older row
pointed at the newest file — the history showed the wrong resume and said
nothing. `version_pdf_path()` now only trusts a stored path when the file
exists *and* is named for that version; anything else reports as missing.

A missing PDF is not a dead end. `restore_version_pdf()` recompiles from the
stored `latex_source`, which reproduces the original exactly and costs no
Gemini call — the tailoring decisions are already baked into the saved source.
That is also the repair path for rows written before the rename.

---

## 10. Operating notes

**Tectonic** must be on `PATH` or set via `TECTONIC_PATH`. First run downloads
the package bundle (needs outbound HTTPS to
`data.tectonic-typesetting.github.io`); afterwards it is offline.

```bash
tectonic --version
```

**Gemini quota.** A 429 surfaces as `GeminiRateLimitError` with a readable
message, and is *not* retried — retrying a quota error burns time and can
worsen a per-minute limit. Transient 503s *are* retried three times with
exponential backoff.

**PDF preview.** The UI rasterizes the PDF with `pypdfium2` and shows PNGs.
Two approaches that do not work: an `<iframe src="data:application/pdf;...">`
is blocked by Chromium browsers (Edge shows "This page has been blocked by
Microsoft Edge"), and `st.pdf` requires the `streamlit-pdf` component, which
fails to register against current Streamlit. If `pypdfium2` is missing the
preview is skipped and the download button still works.

**Missing dates.** If `work_experience.start_date` / `end_date` are null the
date line is left blank rather than printing a bare `– Present`. The UI flags
which entries are affected; fix them on the Experience page.

**Tests.**

```bash
cd backend && .venv/bin/python -m pytest tests/ -q
```

95 tests, no network and no database. Gemini is stubbed; Tectonic is real, so
the compile assertions (one page, no overfull boxes, survives every LaTeX
special character) actually exercise the typesetting.

---

## 11. What was wrong before

For the record, since the old spec's pipeline ran fine and produced unusable
output.

| Symptom | Cause | Fix |
|---|---|---|
| 21 projects, 6 pages | Every KB project rendered | Pre-filter + model selection + page-fit loop (§6, §9) |
| "Resume Parser" twice | GitHub import and manual entry are separate rows | Dedupe by repo URL (§5) |
| `– Present` everywhere, including on Class X | `f"{start} – {end}"` with both null | `_date_range()` returns `""` when nothing is recorded |
| Bullets wrapping with two-word orphans | "one line ideally, two max" means nothing to a model | Measured character budget + repair pass (§4) |
| `Ml:`, `Soft Skill:` as section labels | `category.replace("_"," ").title()` | Explicit label map; `cloud`+`devops` merged |
| `Go` and `Golang` side by side | No alias collapsing | `SKILL_ALIASES` |
| Skills table overflowing the margin | `tabular*` with no wrapping column | Minipage rows that wrap (§3) |
| Certifications running together | Jinja `if` blocks leaving items on one line | `\certentry` with its own column split |
| Regenerating always re-ran Gemini | No cache key | Content fingerprint (§8) |
| Quota errors shown as `RetryError[<Future…>]` | `reraise` not set, and `GeminiRateLimitError` was itself retried | Both fixed in `gemini_client.py` |
| En dashes missing from the PDF (`Staff  SRE`) | XeTeX drops glyphs the T1 font lacks and still exits 0 | Unicode normalized in `latex_escape`, plus `missing_glyphs` reporting (§4b) |
| History showed the newest resume for every version | All versions for a JD shared one output filename | Per-version filenames + `version_pdf_path()` (§9b) |
| PDF preview blocked by the browser | `data:` PDF URIs are blocked in Chromium | Rasterize to PNG server-side (§10) |
| Overleaf link opened an uncompilable project | Base64 in a GET `snip` param is not a real Overleaf feature, and the `.tex` needed a class file | POST form + self-contained `.tex` (§3) |

---

## 12. Tuning

| Want | Change |
|---|---|
| More/fewer projects | `MAX_PROJECTS` in `resume_tailor.py` |
| Two-page resume | "Max pages" in the UI, or `DEFAULT_MAX_PAGES` |
| Different section order | `DEFAULT_SECTION_ORDER` in `resume_renderer.py` |
| Different font | The `helvet` line in `careeros.cls` — pick one Tectonic can fetch |
| Tighter/looser spacing | `\titlespacing`, `\entrygap`, `rbullets` in `careeros.cls` |
| Different bullet voice | The STYLE block in `TAILOR_PROMPT`, using your own real bullets |
| Longer bullets | `BULLET_MAX_CHARS` — but re-check it against a compiled PDF first |

After changing the prompt, bump `TAILOR_VERSION`. After changing the template
or class, nothing is needed: the fingerprint picks it up.
