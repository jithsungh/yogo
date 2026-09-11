# CareerOS — Personal Job Application Copilot
### Product Requirements Document & Build Guide (v1)

---

## 0. TL;DR

You're building a personal RAG-powered system that:
1. Knows everything about you (resume, projects, GitHub, links, experience) — **The Knowledge Base (KB)**
2. Takes a job description (paste or URL) and tells you if you're a fit — **Match Engine**
3. Generates a tailored, LaTeX-based resume for that specific JD — **Resume Tailor**
4. Answers recruiter/application questions using your KB + AI, and remembers what you approved — **Answer Bank**
5. Tracks every application and its status — **Application Tracker**
6. Shows you which skills show up most across JDs you've fed it, so you know what to learn next — **Skill Heatmap**

Everything is built on one core idea: **you have a single source of truth about yourself, and every feature is either reading from it, writing to it, or generating a draft that becomes part of it once you approve it.**

---

## 1. Problem Statement

Every job application currently costs you:
- Re-hunting for the same links (portfolio, GitHub, LinkedIn, certificates)
- Manually deciding whether a JD is even worth applying to
- Re-writing your resume by hand to emphasize the right 60% of your skills
- Re-answering the same 5–6 "tell me about yourself / your project / why this company" questions, from scratch, every time
- No memory across applications — every JD is treated as day one
- No visibility into *what the market actually wants* from someone with your profile

This is friction that causes qualified applications to never get submitted. The product exists to make "I found a JD I like" to "application submitted with a tailored resume and pre-written answers" take **minutes, not hours**.

---

## 2. Goals (v1)

- G1: Store your full profile once, structured, and query it via natural language (RAG)
- G2: Given a JD (text or URL), produce a match score + a clear "apply / stretch / skip" signal
- G3: Generate a tailored LaTeX resume per JD, downloadable as PDF, editable further in Overleaf
- G4: Given JD questions, produce grounded, reusable answers with multiple style options
- G5: Track applications and their status over time
- G6: Aggregate skill/tooling demand across all JDs you've stored, visualized as a heatmap

## 3. Non-Goals (v1)

- Not a job board / job search engine (you bring the JD)
- Not auto-applying on your behalf (no bot-submits-application automation in v1 — that's a v2+ risk area, see §19)
- Not multi-user / SaaS in v1 — single user, your data only
- Not building your own LaTeX renderer from scratch — reuse an existing template + toolchain
- Not fully automated Overleaf sync — Overleaf has no general write API (confirmed below), so integration is "open a pre-filled Overleaf project," not "silently push edits"

---

## 4. Design Principles (read this before building anything)

1. **One source of truth.** Every fact about you lives in the KB exactly once. Resume bullets, Q&A answers, and match analysis are all *derived views* of the KB, never separately maintained copies.
2. **Two-tier data model — this is your "separation" requirement.**
   - **Tier 1: Verified Facts** — things you typed or explicitly approved (bio, work history, project descriptions, approved Q&A answers). These are ground truth and are what gets embedded and retrieved with high trust.
   - **Tier 2: Generated Drafts** — AI-generated resume bullets, AI-drafted Q&A answers not yet approved. These are shown to you for approval before they ever get reused. Nothing generated silently becomes "fact."
   - This distinction is a column on every record (`status: verified | draft`), not a separate database.
3. **Human-in-the-loop by default.** The AI never auto-saves an answer, auto-submits a resume, or auto-marks an application. You click approve/save. This matches exactly what you described: generate → you pick/edit → then persist.
4. **Everything is retrievable, not just storable.** If it's in the KB, there must be a way to semantically query it. That's why it's in a vector DB, not just a form.
5. **JD-specific vs reusable is a first-class concept**, not an afterthought. Every Q&A pair is tagged `reusable` (biodata, "tell me about yourself," standard behavioral) or `job-specific` (this company / this role only). Reusable answers get suggested first on retrieval; job-specific ones only surface for similar JDs.

---

## 5. High-Level Architecture

```
                        ┌─────────────────────────┐
                        │        Frontend          │
                        │  (Next.js / Streamlit-v0)│
                        └────────────┬─────────────┘
                                     │ REST/JSON
                        ┌────────────▼─────────────┐
                        │        Backend API        │
                        │        (FastAPI)          │
                        ├────────────────────────────┤
                        │  Ingestion Service         │→ parses resume/GitHub/JD
                        │  Matching Service          │→ score + gap analysis
                        │  Resume Generation Service │→ LaTeX render (Tectonic)
                        │  QA Service                │→ generate/retrieve answers
                        │  Tracker Service           │→ CRUD + reminders
                        └────┬─────────────┬─────────┘
                             │             │
              ┌──────────────▼───┐   ┌─────▼─────────────┐
              │ Postgres + pgvector│  │   Gemini API       │
              │ (structured data + │  │ (embeddings + gen) │
              │  embeddings, one DB)│  └────────────────────┘
              └────────────────────┘
                             │
                    ┌────────▼────────┐
                    │  File storage    │  → generated PDFs, .tex files,
                    │ (local disk/S3)  │     uploaded certificates etc.
                    └──────────────────┘
```

**Why one DB (Postgres + `pgvector`) instead of a separate vector DB (Chroma/Pinecone/Weaviate)?**
You have relational needs too — applications have statuses, reminders have due dates, Q&A pairs have foreign keys to job descriptions. Running two databases (one relational, one vector) means constant syncing logic and two things that can go out of sync. `pgvector` gives you ACID-compliant relational storage *and* cosine-similarity vector search in the same tables, same transactions, one backup. For a single-user personal tool this is simpler to run, host, and reason about. Move to a dedicated vector DB later only if you outgrow it (you won't for a while — pgvector comfortably handles hundreds of thousands of vectors).

---

## 6. Data Model

```sql
-- Tier 1: verified profile facts
profile_basic        (id, full_name, email, phone, location, links jsonb) -- github, linkedin, portfolio, leetcode, etc.
education             (id, degree, institution, start_date, end_date, score, highlights)
work_experience       (id, company, role, start_date, end_date, description, tech_stack[], embedding)
project               (id, title, description, tech_stack[], github_url, live_url, highlights, embedding, source enum('manual','github_import'))
certification         (id, title, issuer, date, url)
skill                 (id, name, category enum('language','framework','cloud','devops','ml','tool'), proficiency)

-- JD + matching
job_description        (id, raw_text, source_url, company, role_title, parsed_requirements jsonb, embedding, created_at)
extracted_skill_mention (id, jd_id, skill_name, weight, is_required boolean)  -- feeds the heatmap
match_result            (id, jd_id, score, verdict enum('strong','stretch','skip'), matched_skills[], missing_skills[], surplus_skills[], created_at)

-- Resume generation
resume_version          (id, jd_id, latex_source text, pdf_path, generated_at, notes)

-- QA
qa_question             (id, jd_id nullable, question_text, category enum('reusable','job_specific'), embedding)
qa_answer               (id, question_id, answer_text, style enum('concise','detailed','story/STAR'), status enum('draft','approved'), created_at)

-- Application tracking
application             (id, jd_id, resume_version_id, company, role_title, applied_date, status enum('saved','applied','oa','interview_1','interview_2','offer','rejected','ghosted'), next_action_date, notes)
reminder                (id, application_id, due_date, message, done boolean)
```

Everything with `embedding` is a `vector(768)` column (or whatever dimension Gemini's embedding model returns) with an `ivfflat` or `hnsw` index on it for fast similarity search.

---

## 7. Feature Breakdown

### F1. Personal Knowledge Base (Foundation — build this first)
**User story:** "I enter my data once — resume, GitHub, links, experience — and never re-type it."

- Manual structured entry (forms) for basic info, education, experience, projects, skills, certs
- **Resume import**: upload existing PDF/DOCX → parse with Gemini (structured extraction prompt → JSON) → pre-fill forms for you to review/correct → save as verified
- **GitHub import**: given your GitHub username, pull public repos via GitHub API (name, description, README, languages, topics) → suggest as draft "Project" entries → you approve/edit which ones matter and add impact framing (GitHub READMEs rarely explain *impact*, so this step always needs a human pass)
- Every saved record gets embedded (Gemini embedding model) and stored in `pgvector`
- Acceptance: I can ask "what did I do with Kubernetes" and get back the correct project/experience via semantic search, not keyword match

### F2. JD Ingestion
**User story:** "I paste a JD or a link, and it's understood and stored."

- Paste raw text → done
- Paste URL → fetch page → strip boilerplate (nav/footer/ads) → extract the JD text. Note: many job boards (LinkedIn, Naukri) heavily JS-render or block scraping — plan for "paste text" as the reliable path and treat "give me a URL" as best-effort with a manual-paste fallback if extraction fails.
- Parse into structured requirements: required skills, nice-to-have skills, years of experience, role level, responsibilities — via a Gemini structured-extraction prompt
- Store JD + extracted skills + embedding

### F3. Match Detection / Apply-or-Not Signal
**User story:** "Tell me if I should even bother applying."

- Compare your skills/projects/experience embeddings against the JD's requirement embeddings (semantic similarity, not just string match — so "container orchestration" matches "Kubernetes")
- Compute three buckets:
  - **Matched** — you have it
  - **Missing** — required, you don't have it
  - **Surplus** — you have it, JD doesn't need it (this is what lets the resume tailor *remove* noise)
- Score formula for v1 (simple, explainable, tune later): weighted % of required skills matched, penalized more for missing "required" than missing "nice-to-have," small bonus for exact seniority/domain match
- Verdict thresholds you define (e.g., ≥70% required matched = "Strong," 45–70% = "Stretch — apply anyway," <45% = "Skip"). Make thresholds config, not hardcoded — you'll tune this after seeing 20–30 real JDs.
- Output: score, verdict, matched/missing/surplus lists — this list is also what feeds resume tailoring

### F4. Resume Tailoring (LaTeX)
**User story:** "One click → a resume rewritten for this JD, ready to download or open in Overleaf."

- Maintain **one master LaTeX resume template** (pick a clean, ATS-friendly one — e.g. a stripped-down Awesome-CV or a simple custom `.cls`; avoid heavy graphical templates, they parse badly in ATS)
- Template has content sections driven by a data structure (Jinja2 renders LaTeX — careful with LaTeX's special characters `# $ % & _ { } ~ ^ \` when injecting text; escape them)
- Generation logic per JD:
  1. Pull your verified experience/projects/skills from KB
  2. Given the match result (F3), prompt Gemini: "rewrite these bullet points to emphasize [matched_skills], de-emphasize/omit [surplus_skills] where not adding value, do not fabricate anything not present in source bullets"
  3. Reorder skills section to put JD-relevant skills first
  4. Keep a strict **no-hallucination constraint** in the prompt — the model tailors emphasis and phrasing, it must never invent a technology/metric you didn't actually use. This matters both ethically and practically (interviewers will ask about anything on the resume).
  5. Render final `.tex` → compile to PDF locally using **Tectonic** (single static binary, no full TeXLive install needed, easy to run in your backend or a Docker container)
- One-click download of the PDF, plus the `.tex` source
- **Overleaf integration — set expectations correctly:** Overleaf does not offer a general read/write API for creating or editing projects in your account programmatically. The one officially supported integration is **"Open in Overleaf"**: a link of the form `https://www.overleaf.com/docs?snip_uri=<url-to-your-.tex-or-.zip>` opens a *new* Overleaf project pre-loaded with your file, in your browser, for further manual editing. So the realistic flow is: generate PDF+`.tex` locally → serve the `.tex` at a temporary URL → "Open in Overleaf" button uses that link if you want to fine-tune formatting by hand. Don't build toward silent Overleaf sync; it isn't supported.
- Store every generated version (`resume_version`) so you can look back at what you sent to which company

### F5. Skill Heatmap
**User story:** "Across everything I've applied to, what's actually in demand?"

- Every JD you ingest contributes its `extracted_skill_mention` rows
- Aggregate: frequency count + "required vs nice-to-have" weighting, across all JDs, maybe filterable by role type (since you span dev/DevOps/SRE/AI-ML — these will cluster very differently)
- Cross-reference against your own skill list → highlight **gap skills** (high frequency across JDs, absent from your profile) vs **redundant skills** (things you have that never show up in your target JDs)
- Visualize as a heatmap/treemap: skill name × frequency, colored by whether you have it
- This is your personal "what should I learn next" signal, grounded in JDs you're actually targeting — not generic "top 10 skills" listicles

### F6. Q&A Assistant / Answer Bank
**User story:** "Give me the JD's questions, get back ready-to-paste answers, and never write the same answer twice."

- Input: paste one or more application questions (or auto-detect questions embedded in a JD/application form text)
- For each question:
  1. Embed the question, search `qa_question`/`qa_answer` for a similar **approved** answer already in the bank (reusable category first)
  2. If a good match exists (similarity above threshold) → surface it directly for reuse/light edit
  3. If no good match → generate **2–3 style variants** (e.g., concise/professional, detailed/STAR-format, conversational) using Gemini, grounded via RAG against your KB (experience, projects) — never inventing facts
  4. If the KB genuinely lacks the info needed (e.g., "why do you want to join [Company]" needs info about the company you haven't told the assistant, or a very specific personal motivation) → **don't fabricate** — flag it back to you: "I don't have enough info to answer this well — want to tell me more, or answer this one yourself?"
  5. You pick a variant (or edit it) → save as `approved` → it's now searchable for next time
- Tag each saved answer `reusable` or `job_specific` at save time (default suggestion: reusable if it doesn't mention a company/role by name)
- End state per application: a clean list of Q + final approved A, one-click copy per answer, and a "regenerate this one" button for any you want reworded

### F7. Application Tracker
**User story:** "I know what I've applied to and what's next."

- One record per application: company, role, JD link, resume version used, status, dates
- Status pipeline you control manually (saved → applied → OA → interview rounds → offer/rejected/ghosted)
- Reminders: e.g. "follow up if no response in 7 days," or manually set "OA due Friday" — simple due-date + notification list (v1: in-app list / email digest; v2: calendar sync)
- This view doubles as your dashboard: it's the thing you open every morning during a job search

---

## 8. Tech Stack Recommendation

| Layer | Recommendation | Why |
|---|---|---|
| LLM | Gemini (2.x Flash for extraction/generation, cheaper+fast; escalate to Pro for resume rewriting quality if needed) | You asked for Gemini-backed; Flash is cheap enough to iterate fast on a personal project |
| Embeddings | Gemini `text-embedding-004` (or latest embedding model) | Keep embeddings and generation on one provider initially — fewer API keys/quirks to manage |
| Database | PostgreSQL + `pgvector` extension | Single DB for structured + vector data (see §6 rationale) |
| Backend | Python + FastAPI | Fast to build, great AI/ML ecosystem, easy PDF/LaTeX subprocess handling, matches your background |
| LaTeX rendering | Tectonic | Self-contained binary, no multi-GB TeXLive install, scriptable |
| JD scraping | `httpx` + `readability-lxml`/`trafilatura` for boilerplate stripping; Playwright only if you hit JS-heavy pages | Start simple, add Playwright only when you actually hit a wall |
| GitHub import | GitHub REST API (`/users/{u}/repos`) | Official, simple, no scraping needed |
| Frontend (Phase 0–1) | Streamlit | You can go from zero to a usable internal tool in days — don't build a polished UI before the core logic works |
| Frontend (Phase 3+) | Next.js + Tailwind | Once the product proves itself to you, invest in a real UI you'll enjoy using daily |
| File storage | Local disk to start (`/data/resumes/...`), move to S3-compatible storage only if needed | You're the only user; don't over-engineer storage on day one |
| Background jobs (reminders, GitHub re-sync) | APScheduler (in-process) → Celery+Redis later if it grows | Keep infra minimal until the core product justifies more moving parts |

---

## 9. Phase-Wise Roadmap

### Phase 0 — Setup & Foundations (a few days)
- Repo scaffold (`backend/`, `frontend/`, `latex_templates/`)
- Postgres + `pgvector` running locally (Docker Compose)
- Gemini API key working, a smoke-test embed + generate call
- Basic FastAPI skeleton with health check

### Phase 1 — Knowledge Base (this is the foundation everything else depends on — don't skip ahead)
- Data models + migrations (Alembic)
- Manual entry forms (Streamlit is fine) for profile/experience/projects/skills
- Resume PDF/DOCX import → Gemini structured extraction → review/edit → save
- GitHub repo import → draft projects → review/edit → save
- Embedding pipeline: every save embeds + upserts into `pgvector`
- **Milestone check:** you can semantically query your own KB and get correct, relevant results back

### Phase 2 — JD Ingestion + Match Engine
- Paste-text JD ingestion + structured requirement extraction
- URL ingestion (best-effort, with manual-paste fallback)
- Skill extraction from JD → `extracted_skill_mention`
- Match scoring engine + matched/missing/surplus breakdown
- **Milestone check:** feed 10 real JDs you're interested in, sanity-check the verdicts against your own gut feeling, tune thresholds

### Phase 3 — Resume Tailoring
- Build/adapt one clean LaTeX master template
- Jinja2 templating layer + LaTeX-escaping utility (don't skip escaping — this breaks constantly if ignored)
- Tectonic compile pipeline → PDF
- Tailoring prompt (emphasize matched, trim surplus, zero hallucination) using F3's output
- Download button (PDF + `.tex`) + "Open in Overleaf" link
- **Milestone check:** generate a resume for a real JD, read it critically — does it sound like you, is everything on it true, would it pass a 6-second recruiter scan

### Phase 4 — Q&A Assistant
- Question input (manual paste, or extract questions embedded in JD text)
- Retrieval against `qa_answer` bank
- Multi-style generation grounded in KB, with the "insufficient info → ask me" fallback
- Approve/save flow with `reusable`/`job_specific` tagging
- One-click copy UI, per-answer regenerate
- **Milestone check:** by your 5th real application using this feature, most "tell me about yourself"-type questions should auto-resolve from the bank with no new generation needed

### Phase 5 — Skill Heatmap
- Aggregation query across all stored JDs' `extracted_skill_mention`
- Heatmap/treemap visualization, gap vs redundant skill highlighting
- Filter by role-cluster (dev / devops / SRE / ML) since your target roles vary a lot
- **Milestone check:** the top gap skills it surfaces should actually match what you're seeing repeated across the JDs you're personally targeting

### Phase 6 — Application Tracker + Polish
- Application CRUD, status pipeline, reminders
- Dashboard view (this becomes your daily driver screen)
- Migrate frontend from Streamlit → Next.js if you want a "real product" feel
- Auth (even just a password gate) if you ever deploy this off your own machine

**Suggested pacing:** Phases 0–1 are the highest-leverage and least glamorous — resist the urge to jump to resume generation before your KB is solid, since every later feature reads from it. Everything from Phase 2 onward becomes progressively more fun to build because you'll see it working on your real job search immediately.

---

## 10. MVP Cut (if you want something usable in ~2 weeks)

Minimum end-to-end loop, cutting corners everywhere except the KB:
1. Phase 1 fully done, but skip GitHub import (manual entry only)
2. Phase 2: paste-text JD only (skip URL scraping), simple keyword+embedding hybrid match score
3. Phase 3: one hardcoded LaTeX template, tailoring via a single Gemini prompt pass (skip iterative refinement)
4. Skip Phase 4/5/6 entirely for MVP
5. Streamlit UI throughout

This gives you a working "paste JD → get score → get tailored resume" loop fast, which is the core value prop, before investing in the Q&A bank and tracker.

---

## 11. Risks & Things to Watch

- **Hallucination in resume/answers**: the single biggest risk to your credibility in interviews. Always constrain generation prompts to "rewrite/emphasize only, never introduce facts not present in the source," and spot-check outputs before sending.
- **LaTeX escaping bugs**: unescaped `%`, `&`, `_` etc. from your own project descriptions will break compilation — build this utility early and test it with real messy text.
- **JD scraping fragility**: job boards change markup and block scrapers; don't over-invest here — paste-text is your reliable path.
- **Score-threshold overconfidence**: a 70% match score is a heuristic, not truth — treat verdicts as a nudge, not gospel, especially early on while you're tuning weights.
- **Q&A bank going stale**: if your experience changes (new project, new job), old approved answers referencing old context need a re-check pass — consider a simple "last verified" timestamp per answer that surfaces a "revisit?" nudge.
- **Overleaf**: don't build any feature assuming you can write into an existing Overleaf project or account — that's not supported. "Open in Overleaf" (a new project from a URL) is the only stable primitive.

---

## 12. Future / v2+ Feature Ideas (once v1 is working for you)

- **Cover letter generation** using the same tailoring engine + KB
- **Interview prep mode**: given a JD, generate likely technical + behavioral questions and let you draft/practice answers ahead of time
- **Browser extension**: capture a JD directly from LinkedIn/Naukri/Wellfound while browsing, one click to send to CareerOS
- **ATS keyword scanner**: simulate how an ATS parser would read your tailored resume (plain-text extraction check) before you submit
- **Multiple resume templates** per target role type (dev vs SRE vs ML resumes can look structurally different)
- **Analytics**: response-rate by resume version / by skill-match-score bucket, so you learn what's actually working
- **Auto-reminder emails/notifications** synced to your calendar
- **Referral tracker**: who you asked, at which company, follow-up cadence
- **Salary/negotiation notes** per application
- **Multi-user mode**: if this ever becomes a real product for others, add auth, per-user data isolation (row-level security in Postgres works well here), and a pricing/usage model for LLM costs
- **Fine-tune/personalize style**: after enough approved Q&A answers, use them as few-shot examples so new generations sound more like *you* by default
- **Offline/local-LLM fallback**: for cost control or privacy, allow swapping Gemini for a local model behind the same interface (design your prompt/service layer provider-agnostic from day one so this swap is cheap later)

---

## 13. Suggested Repo Structure

```
career-copilot/
├── backend/
│   ├── app/
│   │   ├── models/          # SQLAlchemy models incl. pgvector columns
│   │   ├── services/
│   │   │   ├── ingestion.py
│   │   │   ├── matching.py
│   │   │   ├── resume_gen.py
│   │   │   ├── qa_service.py
│   │   │   └── tracker.py
│   │   ├── llm/
│   │   │   ├── gemini_client.py   # thin wrapper, provider-agnostic interface
│   │   │   └── prompts/           # versioned prompt templates
│   │   ├── api/routes/
│   │   └── main.py
│   ├── alembic/
│   └── requirements.txt
├── latex_templates/
│   └── master_resume.tex.j2
├── frontend/            # Streamlit app.py initially, Next.js app later
├── docker-compose.yml   # postgres+pgvector, backend
└── README.md
```

---

## 14. Immediate Next Steps

1. Stand up Postgres + `pgvector` via Docker Compose and confirm a vector column + similarity query works
2. Get a Gemini API smoke test running (one embed call, one generate call)
3. Define your KB schema in SQLAlchemy + run first migration
4. Build the simplest possible Streamlit form: enter one work experience, save, embed, then query it back semantically
5. Once that loop works end-to-end, everything else in this document is additive

Once you're ready, we can start writing actual code for Phase 0/1 — schema, Gemini client wrapper, and the first ingestion form.
