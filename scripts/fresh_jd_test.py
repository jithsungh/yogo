"""
Fresh JD test: Razorpay-style Backend SDE-1.
Ingest → compute match → manual ground truth comparison.
"""
import sys
from pathlib import Path

backend = str(Path(__file__).resolve().parent.parent / "backend")
if backend not in sys.path:
    sys.path.insert(0, backend)

from app.db import get_session
from app.config import get_settings
from app.services.jd_service import ingest_job_description
from app.services.match_service import compute_match
from sqlalchemy import text

USER_ID = get_settings().default_user_id

FRESH_JD = """
Software Development Engineer I (SDE-1) — Razorpay, Bangalore

About the Role:
Join Razorpay's Payments Platform team as an SDE-1 to build and scale India's most
reliable payment infrastructure. You'll work on high-throughput, low-latency backend
services that process millions of transactions daily.

Responsibilities:
- Design, develop, and maintain backend microservices handling payment flows
- Write clean, testable, production-grade code with comprehensive unit and integration tests
- Participate in code reviews and uphold engineering best practices
- Debug complex distributed system issues in production
- Collaborate with product managers, designers, and cross-functional teams

Required Skills:
- Strong proficiency in Java or Golang
- Solid understanding of data structures, algorithms, and time/space complexity analysis
- Experience with SQL databases (MySQL or PostgreSQL) and query optimization
- Familiarity with RESTful API design and HTTP fundamentals
- Version control with Git
- Understanding of object-oriented programming and design patterns
- Strong problem-solving and analytical thinking

Nice to Have:
- Experience with message queues (Kafka, RabbitMQ, or SQS)
- Docker and container orchestration (Kubernetes)
- Redis or other caching solutions
- Cloud platforms (AWS or GCP)
- CI/CD pipelines and automated testing frameworks
- Microservices architecture patterns
- Experience with monitoring tools (Prometheus, Grafana, Datadog)
- Understanding of distributed systems concepts (CAP theorem, eventual consistency)

Qualifications:
- B.Tech/BE in Computer Science or related field from a recognized university
- 0-2 years of relevant experience (internships count)
- Strong written and verbal communication skills
"""

# ── Ground truth: what a human recruiter would score ──
GROUND_TRUTH = {
    "notes": """
    HONEST ASSESSMENT of this user for a Razorpay SDE-1 role:

    STRONG MATCHES (user clearly has):
    - Java ✅ (listed skill)
    - Golang/Go ✅ (listed skill)
    - SQL ✅ (listed skill, PostgreSQL, MySQL)
    - Git ✅ (listed skill)
    - RESTful APIs ✅ (listed skill)
    - Problem-solving ✅ (listed skill)
    - Docker ✅ (listed skill)
    - Kubernetes ✅ (listed skill)
    - AWS ✅ (listed skill)
    - Communication ✅ (Effective Communication)
    - B.Tech CS ✅ (education)

    REASONABLE INFERENCES (user likely has but not explicit):
    - OOP/Design patterns ⚠️ (CS degree + Java/Python implies this)
    - DSA ⚠️ (CS degree implies this, but not listed as skill)
    - HTTP fundamentals ⚠️ (builds REST APIs, so yes)

    GENUINELY MISSING:
    - Kafka/RabbitMQ/SQS ❌ (no message queue experience)
    - Redis/caching ❌ (not in skills or projects)
    - Query optimization ❌ (uses SQL but no explicit optimization)
    - CI/CD pipelines ❌ (not listed)
    - Prometheus/Grafana/Datadog ❌ (no monitoring tools)
    - Microservices patterns ❌ (not explicitly mentioned)
    - Distributed systems concepts ❌ (not in skills)
    - Unit/integration testing ❌ (no testing tools listed)
    - GCP ❌ (only AWS)

    GROUND TRUTH SCORE:
    Required (7 skills): Java✅, Golang✅, SQL✅, Git✅, RESTful APIs✅,
                         DSA⚠️, OOP⚠️, Problem-solving✅, Query Optimization❌
    → ~6-7 out of 9 required matched = 67-78%

    Nice-to-have (8 areas): Docker✅, K8s✅, AWS✅, Communication✅
                            Kafka❌, Redis❌, CI/CD❌, Prometheus❌, Microservices❌, Distributed Systems❌
    → ~4 out of 10 optional = bonus ~4%

    GROUND TRUTH: ~70-75%, VERDICT: strong (borderline)
    Rationale: Strong backend language fit (Java + Go is rare for entry-level),
    good DB and API skills, but lacks the infrastructure/observability stack
    that Razorpay heavily uses. A realistic "worth applying" candidate.
    """,
    "expected_score_range": (0.65, 0.80),
    "expected_verdict": "strong",
}


def run():
    print("=" * 80)
    print("  FRESH JD TEST: Razorpay SDE-1")
    print("=" * 80)

    # Get user skills
    with get_session() as session:
        user_skills = {
            row[0]
            for row in session.execute(
                text("SELECT lower(name) FROM skill WHERE user_id = :uid"),
                {"uid": USER_ID},
            ).fetchall()
        }

    # Ingest
    with get_session() as session:
        jd = ingest_job_description(
            session,
            user_id=USER_ID,
            raw_text=FRESH_JD,
        )
        jd_id = jd.id
        company = jd.company
        role = jd.role_title
    print(f"  Ingested: {company} — {role} (id={jd_id})")

    # Get extracted skills
    with get_session() as session:
        mentions = session.execute(
            text("""
                SELECT skill_name, weight, is_required
                FROM extracted_skill_mention
                WHERE jd_id = :jd_id
                ORDER BY is_required DESC, skill_name
            """),
            {"jd_id": jd_id},
        ).fetchall()

    required = [(name, w) for name, w, req in mentions if req]
    optional = [(name, w) for name, w, req in mentions if not req]
    all_skills = [name for name, w, req in mentions]

    print(f"\n  Extracted required ({len(required)}):")
    for name, w in required:
        in_skills = "✅" if name.lower() in user_skills else "❌"
        print(f"    {in_skills} {name} (weight={w})")

    print(f"\n  Extracted optional ({len(optional)}):")
    for name, w in optional:
        in_skills = "✅" if name.lower() in user_skills else "❌"
        print(f"    {in_skills} {name} (weight={w})")

    # Manual match
    manual_matched = [s for s in all_skills if s.lower() in user_skills]
    manual_missing = [s for s in all_skills if s.lower() not in user_skills]
    manual_pct = len(manual_matched) / max(len(all_skills), 1)

    print(f"\n  --- MANUAL (exact case-insensitive) ---")
    print(f"  Matched: {manual_matched}")
    print(f"  Missing: {manual_missing}")
    print(f"  Score: {manual_pct:.0%}")

    # Algorithm match
    with get_session() as session:
        result = compute_match(session, user_id=USER_ID, jd_id=jd_id)

    print(f"\n  --- ALGORITHM ---")
    print(f"  Matched ({len(result['matched'])}): {result['matched']}")
    print(f"  Missing ({len(result['missing'])}): {result['missing']}")
    print(f"  Score: {result['score']:.0%}  |  Verdict: {result['verdict']}")

    # Semantic wins analysis
    algo_extra = set(result['matched']) - set(manual_matched)
    print(f"\n  Semantic/fuzzy wins: {algo_extra or 'none'}")

    # Ground truth comparison
    lo, hi = GROUND_TRUTH["expected_score_range"]
    algo_score = result["score"]
    in_range = lo <= algo_score <= hi

    print(f"\n  --- GROUND TRUTH COMPARISON ---")
    print(f"  Algorithm score:     {algo_score:.0%}")
    print(f"  Ground truth range:  {lo:.0%} - {hi:.0%}")
    print(f"  Expected verdict:    {GROUND_TRUTH['expected_verdict']}")
    print(f"  Actual verdict:      {result['verdict']}")
    print(f"  Score in range:      {'✅ YES' if in_range else '❌ NO (off by ' + f'{abs(algo_score - (lo+hi)/2):.0%}' + ')'}")
    print(f"  Verdict correct:     {'✅ YES' if result['verdict'] == GROUND_TRUTH['expected_verdict'] else '❌ NO'}")


if __name__ == "__main__":
    run()
