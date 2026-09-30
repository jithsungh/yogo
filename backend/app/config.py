"""
Central application configuration.

Reads from environment variables / a .env file. Import get_settings()
everywhere instead of os.environ - it's cached, validated, and typed.

Requires: pip install pydantic-settings
"""
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# Resolve .env relative to THIS FILE, not the current working directory.
# config.py lives at backend/app/config.py, so parents[2] is the repo root -
# the same place docker-compose.yml and .env already live. This means the
# smoke test scripts (and anything else) work identically whether you run
# them from the repo root, from backend/, or via `python -m scripts.xyz`.
PROJECT_ROOT = Path(__file__).resolve().parents[2]
ENV_PATH = PROJECT_ROOT / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ENV_PATH, env_file_encoding="utf-8", extra="ignore")

    # ---------------- App ----------------
    app_env: str = "development"
    app_debug: bool = True
    app_secret_key: str

    # ---------------- Database ----------------
    postgres_user: str
    postgres_password: str
    postgres_db: str
    postgres_host: str = "localhost"
    postgres_port: int = 5432

    @property
    def database_url(self) -> str:
        """SQLAlchemy-style URL (psycopg3 driver)."""
        return (
            f"postgresql+psycopg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def raw_database_url(self) -> str:
        """Plain libpq-style URL, for tools like psycopg.connect() directly."""
        return (
            f"postgresql://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    # ---------------- Gemini ----------------
    gemini_api_key: str
    # Model names move fast - check https://ai.google.dev/gemini-api/docs/models
    # before assuming these defaults are current.
    gemini_generation_model: str = "gemini-3.5-flash"
    gemini_embedding_model: str = "gemini-embedding-001"
    embedding_dim: int = 768

    # ---------------- Auth (multi-user ready) ----------------
    jwt_secret_key: str
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 10080  # 7 days

    # ---------------- Matching thresholds ----------------
    match_strong_threshold: float = 0.70
    match_stretch_threshold: float = 0.45

    # ---------------- Phase 4: Q&A answer bank ----------------
    # Cosine similarity between a new question and a banked one (or one of its
    # learned aliases). Measured on gemini-embedding-001 at 768 dims, default
    # task type: paraphrases score 0.66-0.90, DIFFERENT questions on
    # neighbouring topics up to 0.64. Auto-fill stays well above the latter;
    # paraphrases below it are suggested, and confirming one stores it as an
    # alias so that wording auto-fills from then on (qa_question_alias).
    # Do not switch embed_text to task_type=SEMANTIC_SIMILARITY for this: it
    # lifts paraphrases to 0.85-0.95 but different questions to 0.74-0.86,
    # which is worse separation ("notice period" vs "salary" = 0.84).
    qa_auto_resolve_threshold: float = 0.82   # reuse silently, no Gemini call
    qa_suggest_threshold: float = 0.65        # offer, but the user confirms
    # Personal facts (salary, notice period, visa) auto-fill only on a
    # near-exact match or a confirmed alias: "current CTC" vs "expected CTC"
    # embed at 0.90, and filling one with the other ships a wrong number.
    qa_fact_exact_threshold: float = 0.97
    # Floor on the best KB match before generation is even attempted. This is
    # deliberately low: measured on the real KB, answerable and unanswerable
    # questions BOTH score 0.46-0.65, so similarity cannot tell them apart.
    # Question kind (qa_intake) and the model's own self-report do that job;
    # this only catches the case where the KB is empty or unrelated.
    qa_min_context_similarity: float = 0.40

    # ---------------- File storage ----------------
    resume_storage_dir: str = "./data/resumes"

    @property
    def resume_storage_path(self) -> Path:
        """resume_storage_dir resolved against the repo root, not the process's
        working directory - Streamlit started from frontend/ and a script run
        from backend/ used to write PDFs to two different places."""
        p = Path(self.resume_storage_dir)
        return p if p.is_absolute() else (PROJECT_ROOT / p).resolve()
    tectonic_path: str = "tectonic"  # path to tectonic binary for LaTeX→PDF compilation

    # ---------------- Phase 1: single-user bootstrap ----------------
    default_user_id: str = ""  # UUID string, set after running create_default_user.py

    # ---------------- GitHub import ----------------
    github_token: str = ""  # optional — unauthenticated GitHub API = 60 req/hr, with token = 5000/hr


@lru_cache
def get_settings() -> Settings:
    return Settings()