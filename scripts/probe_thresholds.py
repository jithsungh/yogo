"""Probe actual semantic similarity scores to find the right threshold cutoff."""
import sys
from pathlib import Path

backend = str(Path(__file__).resolve().parent.parent / "backend")
if backend not in sys.path:
    sys.path.insert(0, backend)

from app.db import get_session
from app.config import get_settings
from app.llm.gemini_client import embed_text
from sqlalchemy import text

USER_ID = get_settings().default_user_id

# Skills to probe: (skill_name, should_match: True/False based on user's actual skills)
PROBES = [
    # TRUE MATCHES - user has these or very close equivalents
    ("React", True),            # user has React.js
    ("HTML5", True),            # user has HTML
    ("CSS3", True),             # user has CSS
    ("REST APIs", True),        # user has RESTful APIs
    ("Material-UI", True),      # user has Material UI
    ("Communication", True),    # user has Effective Communication
    ("Teamwork", True),         # user has Team Player
    ("Problem Solving", True),  # user has Problem-Solving
    ("NoSQL", True),            # user has MongoDB
    ("Natural Language Processing", True),  # user has NLP
    ("RAG", True),              # user has RAG Systems
    ("RESTful API", True),      # user has RESTful APIs

    # FALSE MATCHES - user does NOT have these
    ("Terraform", False),
    ("Kafka", False),
    ("Redis", False),
    ("Jenkins", False),
    ("RabbitMQ", False),
    ("Grafana", False),
    ("Prometheus", False),
    ("Angular", False),
    ("Vue", False),
    ("GSAP", False),
    ("Framer Motion", False),
    ("Jest", False),
    ("Gatsby", False),
    ("Next.js", False),
    ("Storybook", False),
    ("PyTorch", False),
    ("TensorFlow", False),
    ("Matplotlib", False),
    ("Seaborn", False),
    ("DVC", False),
    ("MLflow", False),
    ("GCP", False),
    ("Sketch", False),
    ("Pinecone", False),
    ("Weaviate", False),
    ("Hugging Face Transformers", False),
    ("CI/CD", False),
    ("Linux", False),
]


def probe():
    true_scores = []
    false_scores = []

    with get_session() as session:
        for skill_name, should_match in PROBES:
            query_vector = embed_text(skill_name)

            # Check against skill chunks only
            skill_hit = session.execute(
                text("""
                    SELECT 1 - (embedding <=> :qv) AS similarity, text_for_embedding
                    FROM kb_chunk
                    WHERE user_id = :uid AND content_type = 'skill'
                    ORDER BY embedding <=> :qv LIMIT 1
                """),
                {"qv": query_vector, "uid": USER_ID},
            ).first()

            # Check against ALL chunks (experience + project + skill)
            all_hit = session.execute(
                text("""
                    SELECT 1 - (embedding <=> :qv) AS similarity, content_type,
                           substring(text_for_embedding, 1, 80) as preview
                    FROM kb_chunk
                    WHERE user_id = :uid AND content_type IN ('experience', 'project', 'skill')
                    ORDER BY embedding <=> :qv LIMIT 1
                """),
                {"qv": query_vector, "uid": USER_ID},
            ).first()

            # Check trigram match
            trigram_hit = session.execute(
                text("""
                    SELECT name, similarity(lower(name), lower(:skill)) as sim
                    FROM skill WHERE user_id = :uid
                    ORDER BY similarity(lower(name), lower(:skill)) DESC LIMIT 1
                """),
                {"skill": skill_name, "uid": USER_ID},
            ).first()

            label = "✅ TRUE " if should_match else "❌ FALSE"
            skill_sim = f"{skill_hit[0]:.3f}" if skill_hit else "N/A"
            all_sim = f"{all_hit[0]:.3f}" if all_hit else "N/A"
            all_type = all_hit[1] if all_hit else ""
            trigram_sim = f"{trigram_hit[1]:.3f}" if trigram_hit else "N/A"
            trigram_name = trigram_hit[0] if trigram_hit else ""

            print(
                f"{label} | {skill_name:<30s} | "
                f"skill_sem={skill_sim:>6s} | "
                f"all_sem={all_sim:>6s} ({all_type:<10s}) | "
                f"trigram={trigram_sim:>6s} ({trigram_name})"
            )

            if should_match:
                true_scores.append((skill_name, float(skill_hit[0]) if skill_hit else 0, float(all_hit[0]) if all_hit else 0, float(trigram_hit[1]) if trigram_hit else 0))
            else:
                false_scores.append((skill_name, float(skill_hit[0]) if skill_hit else 0, float(all_hit[0]) if all_hit else 0, float(trigram_hit[1]) if trigram_hit else 0))

    print("\n" + "=" * 100)
    print("DISTRIBUTION ANALYSIS")
    print("=" * 100)

    true_skill_sems = [s[1] for s in true_scores]
    false_skill_sems = [s[1] for s in false_scores]
    true_all_sems = [s[2] for s in true_scores]
    false_all_sems = [s[2] for s in false_scores]
    true_trigrams = [s[3] for s in true_scores]
    false_trigrams = [s[3] for s in false_scores]

    print(f"\n--- Skill-only semantic similarity ---")
    print(f"  TRUE  matches: min={min(true_skill_sems):.3f}  max={max(true_skill_sems):.3f}  avg={sum(true_skill_sems)/len(true_skill_sems):.3f}")
    print(f"  FALSE matches: min={min(false_skill_sems):.3f}  max={max(false_skill_sems):.3f}  avg={sum(false_skill_sems)/len(false_skill_sems):.3f}")

    print(f"\n--- All-chunks semantic similarity ---")
    print(f"  TRUE  matches: min={min(true_all_sems):.3f}  max={max(true_all_sems):.3f}  avg={sum(true_all_sems)/len(true_all_sems):.3f}")
    print(f"  FALSE matches: min={min(false_all_sems):.3f}  max={max(false_all_sems):.3f}  avg={sum(false_all_sems)/len(false_all_sems):.3f}")

    print(f"\n--- Trigram similarity ---")
    print(f"  TRUE  matches: min={min(true_trigrams):.3f}  max={max(true_trigrams):.3f}  avg={sum(true_trigrams)/len(true_trigrams):.3f}")
    print(f"  FALSE matches: min={min(false_trigrams):.3f}  max={max(false_trigrams):.3f}  avg={sum(false_trigrams)/len(false_trigrams):.3f}")

    # Find threshold that best separates true from false
    print(f"\n--- Threshold analysis (skill-only semantic) ---")
    for threshold in [0.55, 0.60, 0.65, 0.70, 0.72, 0.75, 0.78, 0.80, 0.85]:
        tp = sum(1 for s in true_skill_sems if s >= threshold)
        fp = sum(1 for s in false_skill_sems if s >= threshold)
        fn = len(true_skill_sems) - tp
        precision = tp / (tp + fp) if (tp + fp) > 0 else 0
        recall = tp / (tp + fn) if (tp + fn) > 0 else 0
        print(f"  threshold={threshold:.2f}  TP={tp:>2d}/{len(true_skill_sems)}  FP={fp:>2d}/{len(false_skill_sems)}  precision={precision:.0%}  recall={recall:.0%}")

    print(f"\n--- Threshold analysis (all-chunks semantic) ---")
    for threshold in [0.55, 0.60, 0.65, 0.70, 0.72, 0.75, 0.78, 0.80, 0.85]:
        tp = sum(1 for s in true_all_sems if s >= threshold)
        fp = sum(1 for s in false_all_sems if s >= threshold)
        fn = len(true_all_sems) - tp
        precision = tp / (tp + fp) if (tp + fp) > 0 else 0
        recall = tp / (tp + fn) if (tp + fn) > 0 else 0
        print(f"  threshold={threshold:.2f}  TP={tp:>2d}/{len(true_all_sems)}  FP={fp:>2d}/{len(false_all_sems)}  precision={precision:.0%}  recall={recall:.0%}")

    print(f"\n--- Threshold analysis (trigram) ---")
    for threshold in [0.30, 0.35, 0.40, 0.45, 0.50, 0.55, 0.60]:
        tp = sum(1 for s in true_trigrams if s >= threshold)
        fp = sum(1 for s in false_trigrams if s >= threshold)
        fn = len(true_trigrams) - tp
        precision = tp / (tp + fp) if (tp + fp) > 0 else 0
        recall = tp / (tp + fn) if (tp + fn) > 0 else 0
        print(f"  threshold={threshold:.2f}  TP={tp:>2d}/{len(true_trigrams)}  FP={fp:>2d}/{len(false_trigrams)}  precision={precision:.0%}  recall={recall:.0%}")


if __name__ == "__main__":
    probe()
