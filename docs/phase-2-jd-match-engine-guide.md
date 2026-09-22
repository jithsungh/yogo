# Phase 2 Implementation Guide — JD Ingestion + Match Engine
### CareerOS build spec, written to be handed to an AI coding assistant one section at a time

---

## 0. Context this assistant needs (don't skip)

Phase 1 is done: the Knowledge Base is real. These pieces already exist and Phase 2 builds directly on top of them — don't recreate any of this:

- `app/llm/gemini_client.py` exposes `embed_text(text) -> Vector`, `generate_text(prompt) -> str`, `generate_json(prompt) -> dict`. Everything in Phase 2 that talks to Gemini goes through these, never `google.genai` directly.
- `app/services/kb_sync.py` exposes `sync_kb_chunk(...)` / `delete_kb_chunk(...)`. Phase 2 **reads** `kb_chunk` (for semantic skill matching) but does not write to it — JDs are not part of your personal KB, they're the thing you're matching *against* it.
- `DEFAULT_USER_ID` is set in `.env` and read via `get_settings().default_user_id`. Every query in this phase scopes to it.
- The no-hallucination rule still applies: JD extraction must only pull out what's actually written in the JD, never infer or add requirements that aren't stated.
- `MATCH_STRONG_THRESHOLD` / `MATCH_STRETCH_THRESHOLD` already exist in `.env` — the match engine reads these, never hardcodes them, because you will be tuning them during this phase's milestone check.
- `requirements-scraping.txt` (httpx + trafilatura) was deliberately kept separate from core `requirements.txt` because of a dependency conflict. **Install it now**: `pip install -r requirements-scraping.txt` — this phase's URL ingestion needs it.

One thing Phase 1 built but never populated: the **`role` table**. It exists in the schema with `aliases text[]` and the `pg_trgm` index, but nothing has ever inserted a row into it. Phase 2 can't resolve JD role titles against an empty table — §1 fixes that first.

---

## 1. Seed the role taxonomy (prerequisite — do this before anything else)

`backend/scripts/seed_roles.py` — run once (`python -m scripts.seed_roles`), safe to re-run (upsert on `canonical_name`).

This is a **starter list**, not a final one — you'll add to it over time as real JDs use titles that don't match anything (see §2's fallback chain, which is designed around exactly that gap). Seed it with roles across the areas you actually target, since your background spans dev/DevOps/SRE/AI-ML:

```python
"""Seeds the role taxonomy. Run: python -m scripts.seed_roles
Safe to re-run - upserts on canonical_name.
"""
from sqlalchemy import text
from app.db import get_session

ROLES = [
    # (canonical_name, category, aliases)
    ("Software Development Engineer", "development",
     ["sde", "software engineer", "swe", "software developer", "backend engineer", "backend developer",
      "full stack developer", "full stack engineer", "frontend developer", "frontend engineer"]),
    ("DevOps Engineer", "devops",
     ["devops", "platform engineer", "build and release engineer", "infrastructure engineer"]),
    ("Site Reliability Engineer", "sre",
     ["sre", "reliability engineer", "production engineer"]),
    ("Machine Learning Engineer", "ai_ml",
     ["ml engineer", "machine learning engineer", "applied scientist", "ai engineer"]),
    ("Data Scientist", "data",
     ["data scientist", "data science"]),
    ("Data Engineer", "data",
     ["data engineer", "big data engineer", "etl engineer"]),
    ("Cloud Engineer", "devops",
     ["cloud engineer", "cloud infrastructure engineer", "aws engineer", "azure engineer", "gcp engineer"]),
    ("QA / SDET", "qa",
     ["qa engineer", "sdet", "test engineer", "automation test engineer", "quality engineer"]),
]

def main():
    with get_session() as session:
        for canonical_name, category, aliases in ROLES:
            session.execute(
                text("""
                    INSERT INTO role (canonical_name, category, aliases)
                    VALUES (:name, :category, :aliases)
                    ON CONFLICT (canonical_name) DO UPDATE SET
                        category = EXCLUDED.category,
                        aliases  = EXCLUDED.aliases;
                """),
                {"name": canonical_name, "category": category,
                 "aliases": [a.lower() for a in aliases]},
            )
        session.commit()
    print(f"Seeded {len(ROLES)} roles.")

if __name__ == "__main__":
    main()
```

**Acceptance check:** `SELECT canonical_name, aliases FROM role;` returns all 8 rows with lowercased aliases.

---

## 2. Role resolution service — the fallback chain

`app/services/role_resolution.py`

A raw JD title ("Senior Site Reliability Engineer II") will rarely exactly equal an alias. Resolve in three cheap-to-expensive steps, stopping at the first confident hit:

```python
"""Resolves a raw JD role_title string to a canonical role.id, using the
cheapest method that's confident enough before falling back to a more
expensive one. Never guesses silently past all three - an unresolved role
surfaces in the UI rather than being force-matched to something wrong.
"""
import uuid
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.llm.gemini_client import generate_json

TRIGRAM_CONFIDENCE_THRESHOLD = 0.35


def resolve_role(session: Session, raw_title: str) -> uuid.UUID | None:
    normalized = raw_title.strip().lower()
    if not normalized:
        return None

    # Step 1: alias containment - cheap, no API call. Catches "SRE" inside
    # "Senior SRE II" and "Site Reliability Engineer" inside a longer title.
    hit = session.execute(
        text("""
            SELECT id FROM role
            WHERE EXISTS (
                SELECT 1 FROM unnest(aliases) AS a
                WHERE :title ILIKE '%' || a || '%' OR a ILIKE '%' || :title || '%'
            )
            LIMIT 1;
        """),
        {"title": normalized},
    ).first()
    if hit:
        return hit[0]

    # Step 2: trigram fuzzy match on canonical_name - still no API call.
    hit = session.execute(
        text("""
            SELECT id, similarity(canonical_name, :title) AS sim
            FROM role
            ORDER BY sim DESC
            LIMIT 1;
        """),
        {"title": normalized},
    ).first()
    if hit and hit[1] >= TRIGRAM_CONFIDENCE_THRESHOLD:
        return hit[0]

    # Step 3: ask Gemini to classify against the actual role list - only
    # reached when both cheap methods failed, so this stays rare in practice.
    roles = session.execute(text("SELECT id, canonical_name, category FROM role;")).fetchall()
    role_list_text = "\n".join(f"- {r[1]} (category: {r[2]}, id: {r[0]})" for r in roles)
    prompt = f"""
Given this raw job title: "{raw_title}"

And this list of canonical roles:
{role_list_text}

If the raw title clearly matches one of these roles, return its id.
If it does NOT clearly match any of them, return null - do not force a match.
Return ONLY raw JSON: {{"role_id": "<uuid or null>"}}
"""
    result = generate_json(prompt)
    role_id = result.get("role_id")
    return uuid.UUID(role_id) if role_id else None
```

**When all three fail** (returns `None`): the JD ingestion flow (§3) still saves the JD, just with `role_id = NULL`. The JD review UI must show "role not recognized — pick one or add a new role" rather than silently leaving it unresolved forever. If the user picks/creates a role there, append the raw title as a new alias on that role (`UPDATE role SET aliases = array_append(aliases, :raw_title)`) — this is what makes the taxonomy improve over time instead of hitting the same gap on every similar JD.

**Acceptance check:** call `resolve_role` with `"Senior SRE"`, `"Backend Software Engineer II"`, and a deliberately weird title like `"Growth Ninja"` — the first two should resolve via steps 1–2 (no Gemini call), the third should fall through to step 3 and return `None`.

---

## 3. JD ingestion pipeline

### 3.1 Extraction + storage

`app/services/jd_service.py`

```python
"""Ingests a job description: extracts structured requirements, resolves
the role, embeds it, and stores everything needed for matching later."""
import uuid
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.llm.gemini_client import embed_text, generate_json
from app.services.role_resolution import resolve_role

EXTRACTION_PROMPT = """
You are extracting structured requirements from a job description. Extract
ONLY what is explicitly stated. Do not infer seniority, years of experience,
or requirements that aren't directly written. Normalize skill/tool names to
their common form (e.g. "k8s" -> "Kubernetes", "js" -> "JavaScript") but
never invent a skill that isn't mentioned.

Weight guidance: required_skills default weight 1.0, but use 1.5 for skills
described with strong emphasis ("must have", "expert level", "extensive
experience with"). nice_to_have_skills default weight 0.5.

Return ONLY raw JSON matching exactly this shape:
{
  "company": "",
  "role_title": "",
  "seniority": "",
  "years_experience_min": null,
  "required_skills": [{"name": "", "weight": 1.0}],
  "nice_to_have_skills": [{"name": "", "weight": 0.5}],
  "responsibilities": [""]
}

JOB DESCRIPTION:
<<<{jd_text}>>>
"""


def ingest_job_description(session: Session, *, user_id: uuid.UUID, raw_text: str, source_url: str | None = None) -> uuid.UUID:
    parsed = generate_json(EXTRACTION_PROMPT.format(jd_text=raw_text))
    role_id = resolve_role(session, parsed.get("role_title", ""))
    embedding = embed_text(raw_text)

    jd_id = uuid.uuid4()
    session.execute(
        text("""
            INSERT INTO job_description
                (id, user_id, raw_text, source_url, company, role_title, role_id, parsed_requirements, embedding)
            VALUES (:id, :user_id, :raw_text, :source_url, :company, :role_title, :role_id, :parsed, :embedding);
        """),
        {
            "id": jd_id, "user_id": user_id, "raw_text": raw_text, "source_url": source_url,
            "company": parsed.get("company"), "role_title": parsed.get("role_title"),
            "role_id": role_id, "parsed": parsed, "embedding": embedding,
        },
    )

    for skill in parsed.get("required_skills", []):
        _insert_skill_mention(session, jd_id, skill, is_required=True)
    for skill in parsed.get("nice_to_have_skills", []):
        _insert_skill_mention(session, jd_id, skill, is_required=False)

    session.commit()
    return jd_id


def _insert_skill_mention(session: Session, jd_id: uuid.UUID, skill: dict, is_required: bool) -> None:
    session.execute(
        text("""
            INSERT INTO extracted_skill_mention (jd_id, skill_name, weight, is_required)
            VALUES (:jd_id, :skill_name, :weight, :is_required);
        """),
        {"jd_id": jd_id, "skill_name": skill["name"], "weight": skill.get("weight", 1.0), "is_required": is_required},
    )
```

### 3.2 URL ingestion (best-effort, with fallback — don't over-invest here)

Add to `jd_service.py`:

```python
import httpx
import trafilatura

MIN_VIABLE_LENGTH = 200  # chars - below this, treat extraction as failed

def fetch_jd_from_url(url: str) -> str | None:
    try:
        resp = httpx.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10, follow_redirects=True)
        resp.raise_for_status()
    except httpx.HTTPError:
        return None
    extracted = trafilatura.extract(resp.text)
    if not extracted or len(extracted) < MIN_VIABLE_LENGTH:
        return None
    return extracted
```

**UI flow** (`frontend/pages/9_Add_Job_Description.py`): a radio choice between "Paste text" and "Paste a URL." If URL is chosen and `fetch_jd_from_url` returns `None`, show a clear message ("couldn't extract this page — paste the text instead") and drop straight into the paste-text box rather than making the user start over. Expect this to fail on LinkedIn/Naukri and similar JS-heavy or scraper-blocking sites — that's expected, not a bug to chase.

**Acceptance check:** paste 3 real JDs as text and confirm each produces a `job_description` row with a resolved (or flagged-unresolved) role and correct `extracted_skill_mention` rows. Try 2–3 real JD URLs and confirm the ones that work extract cleanly, and the ones that don't fall back to the paste box instead of crashing.

---

## 4. Match scoring engine

`app/services/match_service.py`

The key design decision here: **don't call Gemini once per skill per JD.** Try a cheap match first (string/trigram against the user's actual `skill` rows); only fall back to a semantic embedding check — searching `kb_chunk` — for skills that didn't get a cheap match. A typical JD has 10–20 skills; doing this in two tiers keeps most JDs to zero or one Gemini calls for matching, not fifteen.

```python
import uuid
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.llm.gemini_client import embed_text
from app.config import get_settings

SEMANTIC_MATCH_THRESHOLD = 0.55
STRING_MATCH_THRESHOLD = 0.6  # pg_trgm similarity


def compute_match(session: Session, *, user_id: uuid.UUID, jd_id: uuid.UUID) -> dict:
    settings = get_settings()

    skill_mentions = session.execute(
        text("SELECT skill_name, weight, is_required FROM extracted_skill_mention WHERE jd_id = :jd_id;"),
        {"jd_id": jd_id},
    ).fetchall()

    matched, missing = [], []
    for skill_name, weight, is_required in skill_mentions:
        if _has_cheap_match(session, user_id, skill_name) or _has_semantic_match(session, user_id, skill_name):
            matched.append(skill_name)
        else:
            missing.append(skill_name)

    surplus = _find_surplus_skills(session, user_id, [s[0] for s in skill_mentions])

    required = [(n, w) for n, w, req in skill_mentions if req]
    nice_to_have = [(n, w) for n, w, req in skill_mentions if not req]
    required_weight_total = sum(w for _, w in required) or 1.0
    required_weight_matched = sum(w for n, w in required if n in matched)
    nice_to_have_bonus = min(
        0.10,
        sum(w for n, w in nice_to_have if n in matched) / (sum(w for _, w in nice_to_have) or 1.0) * 0.10,
    )
    score = min(1.0, (required_weight_matched / required_weight_total) + nice_to_have_bonus)

    if score >= settings.match_strong_threshold:
        verdict = "strong"
    elif score >= settings.match_stretch_threshold:
        verdict = "stretch"
    else:
        verdict = "skip"

    result_id = uuid.uuid4()
    session.execute(
        text("""
            INSERT INTO match_result (id, jd_id, user_id, score, verdict, matched_skills, missing_skills, surplus_skills)
            VALUES (:id, :jd_id, :user_id, :score, :verdict, :matched, :missing, :surplus);
        """),
        {"id": result_id, "jd_id": jd_id, "user_id": user_id, "score": score, "verdict": verdict,
         "matched": matched, "missing": missing, "surplus": surplus},
    )
    session.commit()
    return {"score": score, "verdict": verdict, "matched": matched, "missing": missing, "surplus": surplus}


def _has_cheap_match(session: Session, user_id: uuid.UUID, skill_name: str) -> bool:
    hit = session.execute(
        text("""
            SELECT 1 FROM skill
            WHERE user_id = :user_id AND similarity(name, :skill_name) >= :threshold
            LIMIT 1;
        """),
        {"user_id": user_id, "skill_name": skill_name, "threshold": STRING_MATCH_THRESHOLD},
    ).first()
    return hit is not None


def _has_semantic_match(session: Session, user_id: uuid.UUID, skill_name: str) -> bool:
    query_vec = embed_text(skill_name)
    hit = session.execute(
        text("""
            SELECT 1 - (embedding <=> :q) AS similarity
            FROM kb_chunk
            WHERE user_id = :user_id AND content_type IN ('experience', 'project', 'skill')
            ORDER BY embedding <=> :q
            LIMIT 1;
        """),
        {"q": query_vec, "user_id": user_id},
    ).first()
    return hit is not None and hit[0] >= SEMANTIC_MATCH_THRESHOLD


def _find_surplus_skills(session: Session, user_id: uuid.UUID, jd_skill_names: list[str]) -> list[str]:
    if not jd_skill_names:
        return [row[0] for row in session.execute(text("SELECT name FROM skill WHERE user_id = :uid;"), {"uid": user_id}).fetchall()]
    rows = session.execute(
        text("""
            SELECT name FROM skill
            WHERE user_id = :user_id
              AND NOT EXISTS (
                  SELECT 1 FROM unnest(:jd_skills::text[]) AS js
                  WHERE similarity(skill.name, js) >= :threshold
              );
        """),
        {"user_id": user_id, "jd_skills": jd_skill_names, "threshold": STRING_MATCH_THRESHOLD},
    ).fetchall()
    return [r[0] for r in rows]
```

Notes on this implementation:
- `matched`/`missing`/`surplus` are stored as plain arrays on `match_result` — deliberately simple and auditable. You can always look at *why* a score came out the way it did, which matters for tuning thresholds in the milestone check.
- The score formula is intentionally simple (weighted % of required matched, small nice-to-have bonus capped at +10%) per the original PRD design — resist the urge to make this more "clever" before you've seen it run against real JDs. Tune the threshold constants, not the formula shape, first.
- `SEMANTIC_MATCH_THRESHOLD = 0.55` and `STRING_MATCH_THRESHOLD = 0.6` are starting guesses, not settled values — expect to adjust them alongside the strong/stretch thresholds during §6's milestone check.

**Acceptance check:** run `compute_match` against 2–3 JDs you've already ingested; manually inspect the `matched`/`missing` lists and confirm they look right (e.g. a JD mentioning "container orchestration" should match if you have Kubernetes experience, even though the exact string never appears in your `skill` table).

---

## 5. Match review UI

`frontend/pages/10_Match_Results.py`

- A table listing every ingested JD with its latest verdict (color-coded: strong=green, stretch=yellow, skip=gray) so you can scan your whole JD history at a glance.
- Selecting one JD shows: score, verdict, and three columns — **Matched**, **Missing**, **Surplus** — pulled straight from the stored `match_result` row.
- A "Recompute match" button that re-runs `compute_match` — useful after you've added new skills/experience to your profile and want to see if a previously-"stretch" JD is now "strong."

---

## 6. Cross-cutting checklist

- [ ] Every ingested JD (paste or URL) produces a `job_description` row, `extracted_skill_mention` rows, and an attempted role resolution
- [ ] Unresolved roles (`role_id IS NULL`) are visibly flagged in the JD list/review UI, not silently ignored
- [ ] Match scoring tries the cheap trigram match before ever calling `embed_text` for semantic matching — verify this by checking Gemini call counts don't scale linearly with skill count for JDs where most skills already exist verbatim in your `skill` table
- [ ] Thresholds (`MATCH_STRONG_THRESHOLD`, `MATCH_STRETCH_THRESHOLD`, and the two constants in `match_service.py`) are all easy to find and adjust in one pass — you will be tuning them in §7
- [ ] URL ingestion never crashes the app on a bad/blocked page — always falls back to the paste-text box

---

## 7. Milestone check — the actual Phase 2 exit criteria

Feed **10 real JDs you're actually interested in** (paste-text is the reliable path — don't burn the milestone on URL-scraping edge cases). For each, record score + verdict, then sanity-check against your own gut feeling: does "strong" line up with JDs you'd genuinely apply to without hesitation? Does "skip" line up with ones you'd have passed on anyway?

A small helper makes this comparison easy instead of clicking through 10 separate pages:

`backend/scripts/threshold_tuning_report.py`
```python
"""Run: python -m scripts.threshold_tuning_report
Prints every match result for the default user, sorted by score, so you can
eyeball verdicts against your own judgement in one pass and decide whether
MATCH_STRONG_THRESHOLD / MATCH_STRETCH_THRESHOLD need adjusting.
"""
from sqlalchemy import text
from app.db import get_session
from app.config import get_settings

def main():
    settings = get_settings()
    with get_session() as session:
        rows = session.execute(
            text("""
                SELECT jd.company, jd.role_title, mr.score, mr.verdict
                FROM match_result mr
                JOIN job_description jd ON jd.id = mr.jd_id
                WHERE mr.user_id = :uid
                ORDER BY mr.score DESC;
            """),
            {"uid": settings.default_user_id},
        ).fetchall()

    print(f"{'Score':>6}  {'Verdict':<8}  Company / Role")
    for company, role_title, score, verdict in rows:
        print(f"{score:>6.2f}  {verdict:<8}  {company} / {role_title}")

if __name__ == "__main__":
    main()
```

**Phase 2 is done when:**
- 10 real JDs are ingested and scored
- The verdicts broadly match your own gut sense of fit (after adjusting `.env` thresholds at least once based on the first pass — don't expect the defaults to be right immediately)
- At least one JD you fed in triggered the "role not recognized" path, and you resolved it manually — proving that fallback actually surfaces instead of failing silently

---

## 8. Suggested build order (paste to your AI coding assistant one step at a time)

1. `seed_roles.py` (§1) → verify the 8 rows exist
2. `role_resolution.py` (§2) → verify all three fallback tiers individually with the three test titles given
3. `jd_service.py` paste-text path (§3.1) → ingest one real JD, inspect the DB row and its skill mentions directly
4. `jd_service.py` URL path (§3.2) + the Streamlit "Add Job Description" page → verify both the success and fallback paths
5. `match_service.py` (§4) → run against your first ingested JD, manually sanity-check matched/missing/surplus
6. `10_Match_Results.py` (§5)
7. Cross-cutting audit (§6)
8. `threshold_tuning_report.py` + the real 10-JD milestone run (§7) — this is the finish line for Phase 2
