"""Seed the application role taxonomy.

Run from ``backend/`` with ``python -m scripts.seed_roles``. The source
taxonomy lives in ``app.data.roles`` so role resolution and seeding share one
canonical vocabulary.
"""
from collections.abc import Iterable

from sqlalchemy import text

from app.data.roles import CANONICAL_ROLES
from app.db import get_session

_DEVOPS_TERMS = (
    "devops", "reliability", "platform", "infrastructure", "cloud", "kubernetes",
    "docker", "container", "terraform", "ansible", "chef", "puppet", "sysadmin",
    "administrator", "network", "firewall", "monitoring", "observability", "release",
    "build engineer", "cicd", "ci/cd", "virtualization", "serverless", "migration",
)
_SRE_TERMS = ("site reliability", "sre", "reliability engineer", "production engineer")
_AI_TERMS = (
    "machine learning", "ml", "artificial intelligence", "ai ", "deep learning",
    "computer vision", "natural language", "nlp", "speech", "robotics ai", "recommendation",
)
_DATA_TERMS = (
    "data", "analytics", "database", "sql", "nosql", "spark", "hadoop", "kafka",
    "airflow", "snowflake", "tableau", "power bi", "looker", "statistician", "quantitative",
)
_QA_TERMS = ("qa", "quality", "test", "testing", "selenium", "cypress", "playwright", "junit", "testng")


def _contains_any(value: str, terms: Iterable[str]) -> bool:
    return any(term in value for term in terms)


def category_for_role(canonical_name: str) -> str:
    """Map the broad taxonomy to the database's stable role categories."""
    normalized = canonical_name.lower()
    if _contains_any(normalized, _SRE_TERMS):
        return "sre"
    if _contains_any(normalized, _QA_TERMS):
        return "qa"
    if _contains_any(normalized, _AI_TERMS):
        return "ai_ml"
    if _contains_any(normalized, _DATA_TERMS):
        return "data"
    if _contains_any(normalized, _DEVOPS_TERMS):
        return "devops"
    if any(term in normalized for term in ("designer", "manager", "analyst", "support", "writer", "specialist")):
        return "other"
    return "development"


def _normalized_aliases(aliases: Iterable[str]) -> list[str]:
    return sorted({alias.strip().lower() for alias in aliases if alias.strip()}, key=lambda value: (-len(value), value))


def main() -> None:
    with get_session() as session:
        for canonical_name, aliases in CANONICAL_ROLES.items():
            session.execute(
                text("""
                    INSERT INTO role (canonical_name, category, aliases)
                    VALUES (:canonical_name, :category, :aliases)
                    ON CONFLICT (canonical_name) DO UPDATE SET
                        category = EXCLUDED.category,
                        aliases = EXCLUDED.aliases;
                """),
                {
                    "canonical_name": canonical_name,
                    "category": category_for_role(canonical_name),
                    "aliases": _normalized_aliases(aliases),
                },
            )
        session.commit()
    print(f"Seeded {len(CANONICAL_ROLES)} roles.")


if __name__ == "__main__":
    main()