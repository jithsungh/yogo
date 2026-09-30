"""Re-run match scoring on existing JDs with updated thresholds."""
import sys
from pathlib import Path

backend = str(Path(__file__).resolve().parent.parent / "backend")
if backend not in sys.path:
    sys.path.insert(0, backend)

from app.db import get_session
from app.config import get_settings
from app.services.match_service import compute_match, STRING_MATCH_THRESHOLD, SEMANTIC_MATCH_THRESHOLD
from sqlalchemy import text

USER_ID = get_settings().default_user_id

print(f"Thresholds: trigram={STRING_MATCH_THRESHOLD}, semantic={SEMANTIC_MATCH_THRESHOLD}")
print()

# Get the 5 most recent JDs (the ones from calibration)
with get_session() as session:
    jds = session.execute(
        text("""
            SELECT jd.id, jd.company, jd.role_title
            FROM job_description jd
            WHERE jd.user_id = :uid
            ORDER BY jd.created_at DESC
            LIMIT 5
        """),
        {"uid": USER_ID},
    ).fetchall()

# Get user skills for manual comparison
with get_session() as session:
    user_skills = {
        row[0]
        for row in session.execute(
            text("SELECT lower(name) FROM skill WHERE user_id = :uid"),
            {"uid": USER_ID},
        ).fetchall()
    }

for jd_id, company, role in reversed(jds):
    print(f"{'=' * 80}")
    print(f"  {company} — {role}")
    print(f"{'=' * 80}")

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

    all_skills = [name for name, w, req in mentions]
    manual_matched = [s for s in all_skills if s.lower() in user_skills]
    manual_missing = [s for s in all_skills if s.lower() not in user_skills]
    manual_pct = len(manual_matched) / max(len(all_skills), 1)

    # Recompute with new thresholds
    with get_session() as session:
        result = compute_match(session, user_id=USER_ID, jd_id=jd_id)

    print(f"  Algo matched ({len(result['matched'])}): {result['matched']}")
    print(f"  Algo missing ({len(result['missing'])}): {result['missing']}")
    print(f"  Algo score: {result['score']:.0%}  |  Verdict: {result['verdict']}")
    print(f"  Manual score: {manual_pct:.0%}  (matched {len(manual_matched)}/{len(all_skills)})")

    # Categorize semantic wins
    algo_extra = set(result['matched']) - set(manual_matched)
    algo_missed = set(manual_matched) - set(result['matched'])
    print(f"  Semantic/fuzzy wins: {algo_extra or 'none'}")
    if algo_missed:
        print(f"  ⚠️  Algo missed but manual caught: {algo_missed}")
    print()

print("=" * 80)
print("SUMMARY")
print("=" * 80)
