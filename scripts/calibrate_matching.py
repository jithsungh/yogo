"""
Calibration script: ingest 5 representative entry-level SWE JDs, compute
match scores, perform manual matching, compare results, and print analysis.
"""
import sys, uuid, json
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

# ── 5 realistic entry-level JDs ──────────────────────────────────────────

JDS = [
    {
        "label": "JD1: Full-Stack Web Developer (Startup)",
        "text": """
Full-Stack Software Engineer (Entry Level) — TechNova Inc., Bangalore

About the Role:
We're looking for an entry-level full-stack engineer to join our product team. You'll build and maintain features across our web platform using modern JavaScript frameworks.

Required Skills:
- JavaScript and TypeScript
- React.js or equivalent front-end framework
- Node.js and Express.js for backend development
- RESTful API design and implementation
- SQL and relational databases (PostgreSQL or MySQL)
- Git version control
- HTML and CSS

Nice to Have:
- Experience with MongoDB or other NoSQL databases
- Docker containerization
- AWS or cloud platform basics
- CI/CD pipeline familiarity
- Tailwind CSS or Bootstrap
- WebSocket experience for real-time features
- Figma for design handoff

Qualifications:
- B.Tech/BE in Computer Science or related field
- Strong problem-solving skills
- Good communication and teamwork abilities
""",
    },
    {
        "label": "JD2: Backend Engineer (Enterprise/Cloud Focus)",
        "text": """
Associate Software Engineer — Backend — CloudMatrix Corp., Hyderabad

About the Role:
Join our platform engineering team to build scalable backend services. You'll work on microservices architecture, cloud infrastructure, and data pipelines.

Required Skills:
- Python or Java (strong in at least one)
- SQL and database design (PostgreSQL preferred)
- RESTful API development
- Git and GitHub/GitLab for version control
- Linux command line proficiency
- Understanding of data structures and algorithms

Nice to Have:
- Go or Golang experience
- Docker and container orchestration (Kubernetes)
- AWS services (EC2, S3, Lambda, RDS)
- CI/CD tools (Jenkins, GitHub Actions)
- Message queues (RabbitMQ, Kafka)
- Redis or caching systems
- Terraform or infrastructure as code
- Monitoring and observability tools (Prometheus, Grafana)

Qualifications:
- Bachelor's degree in CS or related field
- 0-1 years experience (internships count)
- Strong analytical and problem-solving abilities
""",
    },
    {
        "label": "JD3: Frontend Developer (Design-Heavy)",
        "text": """
Junior Frontend Developer — PixelCraft Studios, Remote

About the Role:
We need a creative frontend developer who can translate beautiful designs into pixel-perfect, responsive web applications.

Required Skills:
- JavaScript (ES6+)
- React.js with hooks and functional components
- HTML5 and CSS3
- Responsive design and mobile-first development
- CSS frameworks: Tailwind CSS or Bootstrap
- Git version control
- Figma or Sketch for design-to-code workflow

Nice to Have:
- TypeScript
- Next.js or Gatsby
- Material UI or Chakra UI
- Testing with Jest and React Testing Library
- Storybook for component documentation
- Animation libraries (Framer Motion, GSAP)
- Accessibility (WCAG) best practices
- Performance optimization and Core Web Vitals

Qualifications:
- Portfolio demonstrating UI/UX sensibility
- Strong eye for design and attention to detail
- Excellent communication skills
""",
    },
    {
        "label": "JD4: ML/AI Engineer (Entry Level)",
        "text": """
Junior AI/ML Engineer — DataMind AI, Pune

About the Role:
We're seeking a junior ML engineer to help build and deploy machine learning models for our NLP and computer vision products.

Required Skills:
- Python programming
- Machine Learning fundamentals (supervised/unsupervised learning)
- Deep Learning frameworks (PyTorch or TensorFlow)
- Natural Language Processing (NLP)
- SQL for data querying
- Git version control
- Mathematics: linear algebra, probability, statistics

Nice to Have:
- Experience with CNNs for computer vision
- LLM/RAG systems and LangChain
- Vector databases (Pinecone, ChromaDB, Weaviate)
- Docker for model deployment
- AWS SageMaker or cloud ML platforms
- MLOps practices (MLflow, DVC)
- Hugging Face Transformers library
- Data visualization (Matplotlib, Seaborn)

Qualifications:
- B.Tech/MS in CS, Data Science, or related field
- Research experience or published work is a plus
- Strong problem-solving and analytical skills
""",
    },
    {
        "label": "JD5: Software Engineer (Generalist/Product)",
        "text": """
Software Engineer I — Acme SaaS, Gurgaon

About the Role:
As an SDE-1, you'll work across our product stack to ship features, fix bugs, and improve system reliability. This is a generalist role touching both frontend and backend.

Required Skills:
- Proficiency in Java or Python
- JavaScript and one modern frontend framework (React, Angular, or Vue)
- SQL databases and basic query optimization
- RESTful API design
- Git and collaborative development workflows
- Understanding of software development lifecycle (SDLC)
- Unit testing and test-driven development basics

Nice to Have:
- TypeScript
- Node.js
- Docker and basic DevOps concepts
- AWS or GCP cloud services
- MongoDB or NoSQL experience
- Agile/Scrum methodology experience
- Performance profiling and debugging
- System design fundamentals

Qualifications:
- Bachelor's degree in Computer Science or equivalent
- 0-2 years of professional experience
- Strong communication and collaboration skills
- Eagerness to learn and grow
""",
    },
]


def get_user_skills(session):
    """Fetch all user skills as a set of lowercase names."""
    rows = session.execute(
        text("SELECT lower(name) FROM skill WHERE user_id = :uid"),
        {"uid": USER_ID},
    ).fetchall()
    return {row[0] for row in rows}


def manual_match(user_skills_lower, extracted_skills):
    """Do a simple case-insensitive exact match against user's skill names."""
    matched = []
    missing = []
    for skill in extracted_skills:
        if skill.lower() in user_skills_lower:
            matched.append(skill)
        else:
            missing.append(skill)
    return matched, missing


def run():
    with get_session() as session:
        user_skills = get_user_skills(session)

    print(f"Your skills ({len(user_skills)}): {sorted(user_skills)}\n")
    print("=" * 90)

    results = []

    for jd_info in JDS:
        label = jd_info["label"]
        print(f"\n{'=' * 90}")
        print(f"  {label}")
        print(f"{'=' * 90}")

        # 1) Ingest
        with get_session() as session:
            jd = ingest_job_description(
                session,
                user_id=USER_ID,
                raw_text=jd_info["text"],
            )
            jd_id = jd.id
            company = jd.company
            role = jd.role_title

        print(f"  Ingested: {company} — {role}  (id={jd_id})")

        # 2) Get extracted skills
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

        required_skills = [name for name, w, req in mentions if req]
        optional_skills = [name for name, w, req in mentions if not req]
        all_skills = [name for name, w, req in mentions]

        print(f"\n  Extracted required ({len(required_skills)}): {required_skills}")
        print(f"  Extracted optional ({len(optional_skills)}): {optional_skills}")

        # 3) Manual match
        manual_matched, manual_missing = manual_match(user_skills, all_skills)
        manual_pct = len(manual_matched) / max(len(all_skills), 1)

        print(f"\n  --- MANUAL MATCH (exact, case-insensitive) ---")
        print(f"  Matched ({len(manual_matched)}): {manual_matched}")
        print(f"  Missing ({len(manual_missing)}): {manual_missing}")
        print(f"  Manual score: {manual_pct:.0%}")

        # 4) Algorithm match
        with get_session() as session:
            algo_result = compute_match(session, user_id=USER_ID, jd_id=jd_id)

        print(f"\n  --- ALGORITHM MATCH (trigram + semantic) ---")
        print(f"  Matched ({len(algo_result['matched'])}): {algo_result['matched']}")
        print(f"  Missing ({len(algo_result['missing'])}): {algo_result['missing']}")
        print(f"  Score: {algo_result['score']:.0%}  |  Verdict: {algo_result['verdict']}")

        # 5) Compare
        algo_found_not_manual = set(algo_result["matched"]) - set(manual_matched)
        manual_found_not_algo = set(manual_matched) - set(algo_result["matched"])

        print(f"\n  --- COMPARISON ---")
        print(f"  Algorithm found but manual didn't (semantic/fuzzy wins): {algo_found_not_manual or 'none'}")
        print(f"  Manual found but algorithm didn't (algo missed):         {manual_found_not_algo or 'none'}")
        print(f"  Manual score: {manual_pct:.0%}  vs  Algorithm score: {algo_result['score']:.0%}")

        results.append({
            "label": label,
            "required_count": len(required_skills),
            "optional_count": len(optional_skills),
            "manual_matched": len(manual_matched),
            "manual_missing": len(manual_missing),
            "manual_pct": manual_pct,
            "algo_score": algo_result["score"],
            "algo_verdict": algo_result["verdict"],
            "algo_matched": len(algo_result["matched"]),
            "algo_missing": len(algo_result["missing"]),
            "algo_semantic_wins": algo_found_not_manual,
            "algo_missed": manual_found_not_algo,
        })

    # Summary
    print(f"\n\n{'=' * 90}")
    print("  SUMMARY")
    print(f"{'=' * 90}")
    print(f"{'JD':<45} {'Manual':>8} {'Algo':>8} {'Verdict':>10} {'Semantic Wins'}")
    print("-" * 90)
    for r in results:
        print(
            f"  {r['label']:<43} {r['manual_pct']:>7.0%} {r['algo_score']:>7.0%} "
            f"{r['algo_verdict']:>10}   {r['algo_semantic_wins'] or '-'}"
        )


if __name__ == "__main__":
    run()
