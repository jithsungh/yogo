-- =========================================================================
-- CareerOS - Knowledge Base Schema
-- Target: Postgres 16 + pgvector  (image: pgvector/pgvector:pg16)
--
-- VECTOR(768) assumes GEMINI_EMBEDDING_MODEL configured with
-- output_dimensionality = 768 (see .env). If you ever change EMBEDDING_DIM,
-- every VECTOR(768) below must change to match, and existing embeddings
-- must be regenerated - pgvector does not auto-migrate dimensions.
-- =========================================================================

-- ---------------- Extensions ----------------
CREATE EXTENSION IF NOT EXISTS vector;   -- embeddings + ANN similarity search
CREATE EXTENSION IF NOT EXISTS pg_trgm;  -- fuzzy text matching (role/skill name resolution)
-- gen_random_uuid() has been built into Postgres core since v13 - no extension needed.

-- ---------------- Enums ----------------
CREATE TYPE skill_category AS ENUM
    ('language','framework','cloud','devops','ml','database','tool','soft_skill');

CREATE TYPE role_category AS ENUM
    ('development','devops','sre','ai_ml','data','qa','other');

CREATE TYPE match_verdict AS ENUM ('strong','stretch','skip');

CREATE TYPE resume_source AS ENUM ('manual','github_import');

-- reusable   -> generic, works for any application (bio, "tell me about yourself")
-- role_specific    -> tends to repeat across a role type (e.g. an SRE-flavoured incident question)
-- company_specific -> tied to one JD/company only ("why do you want to join Acme")
CREATE TYPE qa_category AS ENUM ('reusable','role_specific','company_specific');

CREATE TYPE qa_style AS ENUM ('concise','detailed_star','conversational');
CREATE TYPE qa_status AS ENUM ('draft','approved');

CREATE TYPE application_status AS ENUM
    ('saved','applied','oa','interview_1','interview_2','interview_3','offer','rejected','ghosted','withdrawn');

-- drives prefiltering in kb_chunk - see the accompanying retrieval-strategy notes
CREATE TYPE kb_content_type AS ENUM
    ('experience','project','skill','education','certification','qa_answer','basic_info');

-- ---------------- Users (multi-user from day one) ----------------
CREATE TABLE users (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email         TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    full_name     TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------- Roles + AKAs (canonical role taxonomy) ----------------
CREATE TABLE role (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    canonical_name TEXT NOT NULL UNIQUE,           -- e.g. 'Site Reliability Engineer'
    category       role_category NOT NULL,
    aliases        TEXT[] NOT NULL DEFAULT '{}',   -- lower-cased, e.g. ARRAY['sre','reliability engineer','infra engineer']
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- exact/contains lookup: WHERE 'sre' = ANY(aliases)
CREATE INDEX idx_role_aliases_gin ON role USING GIN (aliases);
-- fuzzy fallback when a raw JD role_title doesn't hit an alias exactly
CREATE INDEX idx_role_canonical_trgm ON role USING GIN (canonical_name gin_trgm_ops);

-- ---------------- Profile (Tier 1 - verified facts) ----------------
CREATE TABLE profile_basic (
    user_id    UUID PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    full_name  TEXT NOT NULL,
    email      TEXT,
    phone      TEXT,
    location   TEXT,
    links      JSONB NOT NULL DEFAULT '{}',  -- {"github": "...", "linkedin": "...", "portfolio": "...", "leetcode": "..."}
    summary    TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE education (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    degree      TEXT NOT NULL,
    institution TEXT NOT NULL,
    start_date  DATE,
    end_date    DATE,
    score       TEXT,
    highlights  TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_education_user ON education(user_id);

CREATE TABLE work_experience (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    company     TEXT NOT NULL,
    role_title  TEXT NOT NULL,                 -- raw string as you'd type it
    role_id     UUID REFERENCES role(id),       -- resolved canonical role (nullable until resolved)
    start_date  DATE,
    end_date    DATE,                           -- NULL = current
    description TEXT NOT NULL,
    tech_stack  TEXT[] NOT NULL DEFAULT '{}',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_work_experience_user ON work_experience(user_id);
CREATE INDEX idx_work_experience_role ON work_experience(role_id);
CREATE INDEX idx_work_experience_tech_gin ON work_experience USING GIN (tech_stack);

CREATE TABLE project (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title       TEXT NOT NULL,
    description TEXT NOT NULL,
    tech_stack  TEXT[] NOT NULL DEFAULT '{}',
    github_url  TEXT,
    live_url    TEXT,
    highlights  TEXT,
    source      resume_source NOT NULL DEFAULT 'manual',
    status      TEXT NOT NULL DEFAULT 'verified',  -- 'verified' | 'draft' - Tier1/Tier2 marker (e.g. unreviewed GitHub imports)
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_project_user ON project(user_id);
CREATE INDEX idx_project_tech_gin ON project USING GIN (tech_stack);
CREATE INDEX idx_project_status ON project(user_id, status);

CREATE TABLE certification (
    id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title   TEXT NOT NULL,
    issuer  TEXT,
    date    DATE,
    url     TEXT
);
CREATE INDEX idx_certification_user ON certification(user_id);

CREATE TABLE skill (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    category    skill_category NOT NULL,
    proficiency SMALLINT CHECK (proficiency BETWEEN 1 AND 5),
    UNIQUE (user_id, name)
);
CREATE INDEX idx_skill_user ON skill(user_id);
CREATE INDEX idx_skill_name_trgm ON skill USING GIN (name gin_trgm_ops);  -- fuzzy match against raw JD skill text

-- ---------------- Job Descriptions + Matching ----------------
CREATE TABLE job_description (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id             UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    raw_text            TEXT NOT NULL,
    source_url          TEXT,
    company             TEXT,
    role_title          TEXT,                       -- raw string as written in the JD
    role_id             UUID REFERENCES role(id),     -- resolved canonical role
    parsed_requirements JSONB NOT NULL DEFAULT '{}',
    embedding           VECTOR(768),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_jd_user ON job_description(user_id);
CREATE INDEX idx_jd_role ON job_description(role_id);
CREATE INDEX idx_jd_embedding_hnsw ON job_description USING hnsw (embedding vector_cosine_ops);

-- what the skill heatmap aggregates over
CREATE TABLE extracted_skill_mention (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    jd_id       UUID NOT NULL REFERENCES job_description(id) ON DELETE CASCADE,
    skill_name  TEXT NOT NULL,
    weight      REAL NOT NULL DEFAULT 1.0,
    is_required BOOLEAN NOT NULL DEFAULT true
);
CREATE INDEX idx_skill_mention_jd ON extracted_skill_mention(jd_id);
CREATE INDEX idx_skill_mention_name ON extracted_skill_mention(skill_name);

CREATE TABLE match_result (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    jd_id          UUID NOT NULL REFERENCES job_description(id) ON DELETE CASCADE,
    user_id        UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    score          REAL NOT NULL,
    verdict        match_verdict NOT NULL,
    matched_skills TEXT[] NOT NULL DEFAULT '{}',
    missing_skills TEXT[] NOT NULL DEFAULT '{}',
    surplus_skills TEXT[] NOT NULL DEFAULT '{}',
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_match_result_jd ON match_result(jd_id);
CREATE INDEX idx_match_result_user ON match_result(user_id);

-- ---------------- Resume Generation ----------------
CREATE TABLE resume_version (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id      UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    jd_id        UUID NOT NULL REFERENCES job_description(id) ON DELETE CASCADE,
    latex_source TEXT NOT NULL,
    pdf_path     TEXT,
    notes        TEXT,
    -- Fingerprint of every input this resume was built from (KB rows, match
    -- result, JD, template files, prompt version). Unchanged hash = the stored
    -- PDF is still correct, so it is reused instead of regenerated.
    content_hash TEXT,
    pages        SMALLINT,
    build_report JSONB,
    generated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_resume_version_user ON resume_version(user_id);
CREATE INDEX idx_resume_version_cache ON resume_version(user_id, jd_id, content_hash);
CREATE INDEX idx_resume_version_jd ON resume_version(jd_id);

-- ---------------- Q&A Answer Bank ----------------
CREATE TABLE qa_question (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id       UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    jd_id         UUID REFERENCES job_description(id) ON DELETE SET NULL,  -- set when category = company_specific
    role_id       UUID REFERENCES role(id),                                 -- set when category = role_specific
    question_text TEXT NOT NULL,
    category      qa_category NOT NULL DEFAULT 'reusable',
    embedding     VECTOR(768),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_qa_question_user ON qa_question(user_id);
CREATE INDEX idx_qa_question_category ON qa_question(user_id, category);  -- the prefilter for "check reusable bank first"
CREATE INDEX idx_qa_question_jd ON qa_question(jd_id);
CREATE INDEX idx_qa_question_role ON qa_question(role_id);
CREATE INDEX idx_qa_question_embedding_hnsw ON qa_question USING hnsw (embedding vector_cosine_ops);

CREATE TABLE qa_answer (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    question_id UUID NOT NULL REFERENCES qa_question(id) ON DELETE CASCADE,
    answer_text TEXT NOT NULL,
    style       qa_style NOT NULL,
    status      qa_status NOT NULL DEFAULT 'draft',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_qa_answer_question ON qa_answer(question_id);
CREATE INDEX idx_qa_answer_status ON qa_answer(status);

-- ---------------- Applications ----------------
CREATE TABLE application (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id           UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    jd_id             UUID NOT NULL REFERENCES job_description(id) ON DELETE CASCADE,
    resume_version_id UUID REFERENCES resume_version(id),
    company           TEXT NOT NULL,
    role_title        TEXT NOT NULL,
    status            application_status NOT NULL DEFAULT 'saved',
    applied_date      DATE,
    next_action_date  DATE,
    notes             TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_application_user ON application(user_id);
CREATE INDEX idx_application_status ON application(user_id, status);
-- partial index: only indexes rows that actually need a reminder check, keeps it tiny
CREATE INDEX idx_application_next_action ON application(next_action_date) WHERE next_action_date IS NOT NULL;

CREATE TABLE reminder (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    application_id UUID NOT NULL REFERENCES application(id) ON DELETE CASCADE,
    due_date       DATE NOT NULL,
    message        TEXT NOT NULL,
    done           BOOLEAN NOT NULL DEFAULT false
);
CREATE INDEX idx_reminder_application ON reminder(application_id);
CREATE INDEX idx_reminder_due ON reminder(due_date) WHERE done = false;

-- ---------------------------------------------------------------------------
-- Unified retrieval index. Every Tier-1 KB fact gets ONE row here when it's
-- created/updated, regardless of which source table it lives in. This is what
-- every feature queries for semantic context - see the "which tables do I
-- query" explanation in chat for why this exists instead of ANN-searching
-- all 6 source tables separately.
-- ---------------------------------------------------------------------------
CREATE TABLE kb_chunk (
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id            UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    source_table       TEXT NOT NULL,   -- 'work_experience' | 'project' | 'skill' | 'education' | 'certification' | 'qa_answer' | 'profile_basic'
    source_id          UUID NOT NULL,
    content_type       kb_content_type NOT NULL,
    role_tags          UUID[] NOT NULL DEFAULT '{}',  -- optional: role ids this chunk is especially relevant to
    text_for_embedding TEXT NOT NULL,    -- the compact string that was actually embedded (kept in sync when source changes)
    embedding          VECTOR(768) NOT NULL,
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (source_table, source_id)
);
CREATE INDEX idx_kb_chunk_user ON kb_chunk(user_id);
CREATE INDEX idx_kb_chunk_user_type ON kb_chunk(user_id, content_type);  -- THE prefilter index - see notes
CREATE INDEX idx_kb_chunk_role_tags_gin ON kb_chunk USING GIN (role_tags);
CREATE INDEX idx_kb_chunk_embedding_hnsw ON kb_chunk USING hnsw (embedding vector_cosine_ops);
