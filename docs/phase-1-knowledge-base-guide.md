# Phase 1 Implementation Guide — The Knowledge Base
### CareerOS build spec, written to be handed to an AI coding assistant one section at a time

---

## 0. Context this assistant needs (don't skip)

Phase 0 is done and verified. These conventions are already established and MUST be followed by anything built in Phase 1 — violating them will silently break things the way we already debugged once:

- **Config**: `app/config.py` exposes `get_settings()`. It resolves `.env` relative to the project root via `Path(__file__).resolve().parents[2]`, not the CWD. Never hardcode env var reads elsewhere — always go through `get_settings()`.
- **Running scripts**: always `python -m scripts.whatever` from `backend/`, never `python scripts/whatever.py` directly (the latter breaks the `app` package import).
- **Embeddings — the gotcha that already bit us once**: Gemini's `embed_content` returns a plain `list[float]`. `pgvector`'s `register_vector()` does NOT know how to serialize plain lists — only `numpy.ndarray` or `pgvector.Vector`. **Every single place that binds an embedding into a query must wrap it: `Vector(embedding_list)`.** Put this behind one shared helper (`gemini_client.embed_text`, see §2.1) so nobody has to remember it at each call site.
- **Models**: generation = `gemini-3.8-flash` via `client.interactions.create(...)`, embeddings = `gemini-embedding-001` via `client.models.embed_content(..., config=types.EmbedContentConfig(output_dimensionality=768))`. Both come from `settings`, never hardcoded in service code.
- **No-hallucination rule**: any Gemini prompt that drafts content about the user (resume extraction, GitHub project descriptions) must explicitly instruct "extract/summarize only what is explicitly present — never invent facts, numbers, or impact not stated in the source." This is a hard requirement in every extraction/generation prompt in this phase, not just resume tailoring later.
- **Tier 1 vs Tier 2**: `project.status` (and analogous fields you add) is `'verified'` (human-reviewed, trustworthy) or `'draft'` (AI-suggested, not yet reviewed). Nothing AI-generated in this phase writes as `'verified'` directly — the human approval step in the UI is what flips it.
- **Single user for now**: the schema is multi-user-ready (every table has `user_id`), but Phase 1 does not build login/auth. Bootstrap one real `users` row and hardcode its id as `DEFAULT_USER_ID` (see §4.0). Real auth is a later phase.

---

## 1. Reconcile schema ownership: Alembic vs. `01_schema.sql`

Your Postgres already has the full schema applied (docker ran `db/init/01_schema.sql` on first boot). Bringing in Alembic now without care will try to "recreate" things that already exist. Do this in order:

1. Define SQLAlchemy models (see §1.1) that mirror `01_schema.sql` exactly — same table names, column names, types.
2. `cd backend && alembic init alembic`
3. In `alembic/env.py`: import your models' `Base.metadata`, set `target_metadata = Base.metadata`, and pull the DB URL from `get_settings().database_url` instead of the placeholder in `alembic.ini` (never hardcode credentials in `alembic.ini`).
4. `alembic revision --autogenerate -m "baseline matching 01_schema.sql"` — inspect the generated file. It should be nearly empty/no-op if your models truly match what's already in the DB. If it tries to create tables/enums that already exist, that's a sign a model doesn't match the live schema — fix the model, not the DB.
5. **`alembic stamp head`** — NOT `alembic upgrade head`. The schema is already applied; you're telling Alembic "the DB is already at this version," not asking it to reapply anything.
6. From this point forward: schema changes happen by editing models → `alembic revision --autogenerate` → review the diff → `alembic upgrade head`. `01_schema.sql` becomes a historical bootstrap file for brand-new environments only — don't hand-edit it again once Alembic owns migrations.

### 1.1 SQLAlchemy model gotchas specific to this schema

- **Enums already exist in Postgres** (created by `01_schema.sql`). Declare them with `create_type=False` so SQLAlchemy doesn't try to recreate them:
  ```python
  from sqlalchemy.dialects.postgresql import ENUM as PGEnum

  qa_category = PGEnum(
      "reusable", "role_specific", "company_specific",
      name="qa_category", create_type=False,
  )
  ```
- **UUID primary keys**: `sqlalchemy.dialects.postgresql.UUID(as_uuid=True)`, server default `text("gen_random_uuid()")`.
- **Arrays** (`tech_stack`, `aliases`, `role_tags`, `matched_skills`/`missing_skills`/`surplus_skills`): `sqlalchemy.dialects.postgresql.ARRAY(sqlalchemy.Text)`.
- **JSONB** (`links`, `parsed_requirements`): `sqlalchemy.dialects.postgresql.JSONB`.
- **Vector columns**: `from pgvector.sqlalchemy import Vector` → `Column(Vector(768))`. Requires `pgvector` pip package (already in `requirements.txt`).

---

## 2. Shared services layer — build this before any UI touches it

Everything else in Phase 1 depends on these two files. Get them right once.

### 2.1 `app/llm/gemini_client.py`

```python
"""Thin, shared wrapper around the Gemini API. Nothing else in the app should
import google.genai directly - always go through here, so model names,
retry behavior, and the Vector-wrapping gotcha live in exactly one place.
"""
import json

from google import genai
from google.genai import types
from pgvector import Vector
from tenacity import retry, stop_after_attempt, wait_exponential

from app.config import get_settings

_settings = get_settings()
_client = genai.Client(api_key=_settings.gemini_api_key)


@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8))
def embed_text(text: str) -> Vector:
    """Always returns a pgvector.Vector, ready to bind directly into a query -
    callers never touch a raw list or worry about the dumper gotcha."""
    resp = _client.models.embed_content(
        model=_settings.gemini_embedding_model,
        contents=text,
        config=types.EmbedContentConfig(output_dimensionality=_settings.embedding_dim),
    )
    return Vector(resp.embeddings[0].values)


@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8))
def generate_text(prompt: str) -> str:
    interaction = _client.interactions.create(
        model=_settings.gemini_generation_model,
        input=prompt,
    )
    return interaction.output_text.strip()


def generate_json(prompt: str) -> dict:
    """For structured-extraction prompts. Always instruct the model in `prompt`
    to return ONLY raw JSON, no markdown fences, no preamble - this still
    defensively strips fences in case it ignores that instruction."""
    raw = generate_text(prompt)
    cleaned = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Model did not return valid JSON. Raw output:\n{raw}") from exc
```

### 2.2 `app/services/kb_sync.py`

This is the single choke point every entity save goes through. Nothing else should write to `kb_chunk` directly.

```python
"""Keeps the unified kb_chunk retrieval index in sync with Tier-1 source
tables. Every entity service calls sync_kb_chunk() after it commits a
create/update, and delete_kb_chunk() after a delete (kb_chunk.source_id is
NOT a real foreign key - it can't be, it's polymorphic - so deletes do not
cascade automatically and must be handled explicitly here).
"""
import uuid

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.llm.gemini_client import embed_text


def sync_kb_chunk(
    session: Session,
    *,
    user_id: uuid.UUID,
    source_table: str,
    source_id: uuid.UUID,
    content_type: str,
    text_for_embedding: str,
    role_tags: list[uuid.UUID] | None = None,
) -> None:
    embedding = embed_text(text_for_embedding)
    session.execute(
        text("""
            INSERT INTO kb_chunk (user_id, source_table, source_id, content_type,
                                   role_tags, text_for_embedding, embedding, updated_at)
            VALUES (:user_id, :source_table, :source_id, :content_type,
                    :role_tags, :text_for_embedding, :embedding, now())
            ON CONFLICT (source_table, source_id) DO UPDATE SET
                content_type       = EXCLUDED.content_type,
                role_tags          = EXCLUDED.role_tags,
                text_for_embedding = EXCLUDED.text_for_embedding,
                embedding          = EXCLUDED.embedding,
                updated_at         = now();
        """),
        {
            "user_id": user_id,
            "source_table": source_table,
            "source_id": source_id,
            "content_type": content_type,
            "role_tags": role_tags or [],
            "text_for_embedding": text_for_embedding,
            "embedding": embedding,
        },
    )


def delete_kb_chunk(session: Session, *, source_table: str, source_id: uuid.UUID) -> None:
    session.execute(
        text("DELETE FROM kb_chunk WHERE source_table = :t AND source_id = :i"),
        {"t": source_table, "i": source_id},
    )
```

### 2.3 Embedding-text templates — use these exact shapes

Embedding quality depends entirely on what text you feed in. Don't embed raw DB rows; format them intentionally per `content_type`:

| content_type | source_table | Template |
|---|---|---|
| `basic_info` | `profile_basic` | `"{full_name}. {summary}. Based in {location}."` |
| `education` | `education` | `"{degree} from {institution} ({start_date}-{end_date}). {highlights}"` |
| `experience` | `work_experience` | `"{role_title} at {company} ({start_date}-{end_date}). {description} Tech stack: {', '.join(tech_stack)}."` |
| `project` | `project` | `"{title}. {description} Tech stack: {', '.join(tech_stack)}. {highlights}"` |
| `certification` | `certification` | `"{title} issued by {issuer} ({date})."` |
| `skill` | `skill` | `"{name} ({category}, proficiency {proficiency}/5)."` |
| `qa_answer` | `qa_answer` | `"Q: {question_text}\nA: {answer_text}"` (built later, in Phase 4 — noted here for consistency) |

Put each template as a small `to_embedding_text(row) -> str` function living next to its entity service, not duplicated inline.

### 2.4 Entity service pattern (apply identically to all six entities)

One file each: `app/services/profile_service.py`, `education_service.py`, `experience_service.py`, `project_service.py`, `certification_service.py`, `skill_service.py`. Every create/update function follows this shape (shown for experience, replicate for the rest):

```python
def create_experience(session, *, user_id, company, role_title, description, tech_stack, start_date, end_date, role_id=None):
    row = WorkExperience(user_id=user_id, company=company, role_title=role_title,
                          description=description, tech_stack=tech_stack,
                          start_date=start_date, end_date=end_date, role_id=role_id)
    session.add(row)
    session.flush()  # need row.id before syncing the chunk
    sync_kb_chunk(
        session, user_id=user_id, source_table="work_experience", source_id=row.id,
        content_type="experience", text_for_embedding=to_embedding_text(row),
    )
    session.commit()
    return row
```

`update_experience(...)` is identical except it mutates an existing row instead of constructing one — it still calls `sync_kb_chunk` at the end (the `ON CONFLICT` upsert means edits correctly refresh the embedding, so nothing goes stale). `delete_experience(...)` calls `delete_kb_chunk(session, source_table="work_experience", source_id=id)` before deleting the row.

**Acceptance check for §2:** write one throwaway script that calls `create_experience(...)` with a real sentence, then queries `kb_chunk` directly and confirms a row exists with a non-null 768-dim embedding.

---

## 3. Default user bootstrap

No login UI this phase. One-time script:

`backend/scripts/create_default_user.py`
```python
"""Run once: python -m scripts.create_default_user
Prints a UUID - paste it into .env as DEFAULT_USER_ID.
"""
import uuid
from sqlalchemy import text
from app.db import get_session  # your SQLAlchemy session factory
from app.config import get_settings

def main():
    settings = get_settings()
    with get_session() as session:
        user_id = uuid.uuid4()
        session.execute(
            text("INSERT INTO users (id, email, password_hash, full_name) VALUES (:id, :email, 'not-used-yet', :name)"),
            {"id": user_id, "email": settings.app_secret_key[:8] + "@local", "name": "Me"},
        )
        session.commit()
    print(f"Created default user: {user_id}")
    print("Add this to .env as DEFAULT_USER_ID")

if __name__ == "__main__":
    main()
```
Add `DEFAULT_USER_ID` to `.env` and `.env.example`, read it via `get_settings().default_user_id` (add the field to `Settings`). Every Streamlit page and service call in this phase uses this id — don't build a user picker yet.

---

## 4. Manual entry forms (Streamlit)

Layout (Streamlit's multipage convention — file names control sidebar order and labels):
```
frontend/
├── Home.py
└── pages/
    ├── 1_Profile.py
    ├── 2_Education.py
    ├── 3_Experience.py
    ├── 4_Projects.py
    ├── 5_Skills.py
    ├── 6_Certifications.py
    ├── 7_Import_Resume.py     (§5)
    └── 8_Import_GitHub.py     (§6)
```

Every page (except Profile, which is a singleton) follows the same pattern:
1. Query all rows for `DEFAULT_USER_ID`, render as `st.dataframe(...)`.
2. Below it, `st.form(...)` for adding a new entry — fields matching the entity's columns, `st.multiselect` or comma-separated text input for `tech_stack`.
3. On submit, call the matching service function (`create_experience`, etc.) — never write SQL directly from the Streamlit page.
4. `st.rerun()` after a successful save so the table reflects the new row immediately.
5. Add an "Edit" / "Delete" action per row (expander + form, or a selectbox to pick a row to edit) — wire these to `update_x` / `delete_x` from the same service.

**Acceptance check for §4:** enter one real work experience and one real project through the UI, confirm both appear in their tables, and confirm `kb_chunk` now has two new rows (query it directly or add a tiny "Chunk count" debug line at the bottom of `Home.py`).

---

## 5. Resume import pipeline

`frontend/pages/7_Import_Resume.py` + `app/services/resume_import_service.py`

**Flow:**
1. `st.file_uploader(type=["pdf", "docx"])`.
2. **PDF**: send the raw bytes directly to Gemini as inline document data (Gemini reads PDFs natively — no separate parsing library needed for this path).
3. **DOCX**: extract raw text locally first via `python-docx` (add to `requirements.txt`), then send that extracted text to Gemini as plain text — Gemini's multimodal file input doesn't cover `.docx`.
4. Send the extraction prompt below alongside the resume content. Both paths converge to the same JSON schema.
5. Parse the response through `generate_json` (already handles fence-stripping); if it raises `ValueError`, show the raw model output in an `st.expander` so nothing is silently lost, and let the user retry or fall back to manual entry.
6. Render each extracted section (`education`, `work_experience`, `projects`, `skills`, `certifications`) as pre-filled, **editable** Streamlit forms — one form per item, each with its own "Approve & Save" button, so the user can accept some and skip/edit others individually rather than all-or-nothing.
7. On approve, call the matching entity service's `create_x(...)` — this is the human-review gate, so what gets saved is Tier 1 (`verified`) even though it originated from AI extraction.

**Extraction prompt template:**
```
You are extracting structured data from a resume. Extract ONLY what is
explicitly stated in the text below. Do not infer, estimate, or invent any
detail, number, date, or skill that isn't directly present. If a field is
not present for an item, omit that field entirely rather than guessing.

Return ONLY raw JSON (no markdown fences, no commentary) matching exactly
this shape:

{
  "basic_info": {"full_name": "", "email": "", "phone": "", "location": "", "links": {"github": "", "linkedin": "", "portfolio": ""}},
  "education": [{"degree": "", "institution": "", "start_date": "", "end_date": "", "score": "", "highlights": ""}],
  "work_experience": [{"company": "", "role_title": "", "start_date": "", "end_date": "", "description": "", "tech_stack": []}],
  "projects": [{"title": "", "description": "", "tech_stack": [], "github_url": "", "live_url": "", "highlights": ""}],
  "skills": [{"name": "", "category": "language|framework|cloud|devops|ml|database|tool|soft_skill"}],
  "certifications": [{"title": "", "issuer": "", "date": "", "url": ""}]
}

RESUME CONTENT:
<<< paste extracted text / attach PDF here >>>
```

**Acceptance check for §5:** upload your actual current resume, confirm every real work experience and project on it shows up as a reviewable item, and confirm nothing appears that isn't actually on the resume (spot-check against the no-hallucination rule).

---

## 6. GitHub import pipeline

`frontend/pages/8_Import_GitHub.py` + `app/services/github_import_service.py`

Add to `.env.example`: `GITHUB_TOKEN=` (optional — unauthenticated GitHub API calls are capped at 60/hour, a token raises that to 5000/hour; not required to get started).

**Flow:**
1. Input: GitHub username.
2. `GET https://api.github.com/users/{username}/repos?sort=updated&per_page=100` via `httpx`, with `Authorization: Bearer {token}` header if `GITHUB_TOKEN` is set. Paginate if the user has more than 100 repos.
3. Filter out forks by default (`fork: false`), with a checkbox to include them if the user wants.
4. Per repo, best-effort fetch the README: `GET /repos/{owner}/{repo}/readme` with `Accept: application/vnd.github.raw` — handle 404 gracefully (many repos have none), don't fail the whole import over one missing README.
5. Build a **draft** project per repo (`source='github_import'`, `status='draft'`): `title` = repo name, `description` = repo's own description (or empty), `tech_stack` = `[language] + topics`, `github_url` = `html_url`.
6. Optional per-repo "Suggest a description" button → prompt Gemini with name + description + README + topics + language, **explicitly instructed not to invent impact/metrics not present in the README** (READMEs rarely state impact — if it's not there, tell the user to add it themselves rather than fabricating).
7. User reviews/edits each draft in the UI. "Approve" flips `status` to `'verified'` and calls `create_project`/`update_project` — this is what actually triggers the `kb_chunk` sync; unapproved drafts should NOT be embedded/searchable yet (filter `kb_chunk` writes to only fire on approval, not on the initial draft fetch).

**Acceptance check for §6:** import your real GitHub username, confirm your actual repos show up as drafts, approve two or three, and confirm only the approved ones show up in the Projects page and in `kb_chunk` — not the ones you skipped.

---

## 7. Cross-cutting checklist — don't skip any of these

- [ ] Every entity's `create_x` calls `sync_kb_chunk`
- [ ] Every entity's `update_x` calls `sync_kb_chunk` (so edits refresh the embedding, not just inserts)
- [ ] Every entity's `delete_x` calls `delete_kb_chunk` **before** deleting the row (`kb_chunk.source_id` is not a real FK — nothing cascades automatically)
- [ ] GitHub-imported drafts do NOT get a `kb_chunk` row until approved
- [ ] Resume-imported items do NOT get a `kb_chunk` row until approved (approval = the `create_x` call, which is already gated behind the UI's per-item "Approve" button)
- [ ] Every embedding passed into a query is wrapped in `Vector(...)` — this only happens automatically if everyone actually goes through `gemini_client.embed_text`, so audit for anywhere that might call `embed_content` directly

---

## 8. Milestone check — the actual Phase 1 exit criteria

`backend/scripts/milestone_check.py`
```python
"""Run: python -m scripts.milestone_check "what did I do with kubernetes"
Proves the KB is real: a free-text question returns your own correct,
relevant history - not keyword matching, actual semantic retrieval.
"""
import sys
from sqlalchemy import text
from app.db import get_session
from app.llm.gemini_client import embed_text
from app.config import get_settings

def main():
    query = " ".join(sys.argv[1:]) or "what did I do with Kubernetes"
    settings = get_settings()
    query_vec = embed_text(query)

    with get_session() as session:
        rows = session.execute(
            text("""
                SELECT source_table, content_type, text_for_embedding,
                       1 - (embedding <=> :q) AS similarity
                FROM kb_chunk
                WHERE user_id = :user_id
                ORDER BY embedding <=> :q
                LIMIT 5;
            """),
            {"q": query_vec, "user_id": settings.default_user_id},
        ).fetchall()

    print(f"Query: {query!r}\n")
    for source_table, content_type, snippet, similarity in rows:
        print(f"  {similarity:.4f}  [{content_type}/{source_table}]  {snippet[:100]}")

if __name__ == "__main__":
    main()
```

**Phase 1 is done when:**
- You've entered/imported real data across all six entity types (at least a couple of each)
- Running `milestone_check.py` with 3–4 different real questions about your own background returns the correct entries at the top, every time
- You can point to at least one resume-imported item and one GitHub-imported item that went through review before landing in the KB as `verified`

---

## 9. Suggested build order (paste to your AI coding assistant one step at a time)

Don't hand all of this to an assistant in one shot — review each step's acceptance check before moving to the next, or errors compound silently (as we saw in Phase 0, small ordering/type mistakes fail loudly *later*, not where the bug actually is).

1. SQLAlchemy models (§1.1) + Alembic baseline (§1) → verify `alembic stamp head` succeeds with no drift
2. `gemini_client.py` + `kb_sync.py` (§2.1–2.2) → verify with the throwaway script in §2.4's acceptance check
3. Six entity services + embedding-text templates (§2.3–2.4)
4. `create_default_user.py` (§3) → paste the id into `.env`
5. Streamlit pages 1–6, manual entry (§4) → verify acceptance check
6. Resume import (§5) → verify acceptance check
7. GitHub import (§6) → verify acceptance check
8. Cross-cutting audit (§7) — go through the checklist explicitly, don't assume
9. `milestone_check.py` (§8) — this is the real finish line for Phase 1
