"""Run: python -m scripts.threshold_tuning_report"""
from sqlalchemy import text

from app.config import get_settings
from app.db import get_session


def main() -> None:
    settings = get_settings()
    with get_session() as session:
        rows = session.execute(
            text("""
                SELECT jd.company, jd.role_title, mr.score, mr.verdict
                FROM match_result AS mr
                JOIN job_description AS jd ON jd.id = mr.jd_id
                WHERE mr.user_id = :user_id
                  AND mr.id = (
                      SELECT newest.id
                      FROM match_result AS newest
                      WHERE newest.jd_id = mr.jd_id AND newest.user_id = mr.user_id
                      ORDER BY newest.created_at DESC
                      LIMIT 1
                  )
                ORDER BY mr.score DESC, jd.created_at DESC;
            """),
            {"user_id": settings.default_user_id},
        ).fetchall()

    print(f"{'Score':>6}  {'Verdict':<8}  Company / Role")
    for company, role_title, score, verdict in rows:
        print(f"{score:>6.2f}  {verdict:<8}  {company or '(unknown company)'} / {role_title or '(unknown role)'}")


if __name__ == "__main__":
    main()