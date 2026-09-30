"""project: structured fields, resume bullets, user priority; merge duplicates

New project fields
  summary          one-line pitch of the project
  role             the candidate's part in it ("Solo", "Backend lead, team of 4")
  key_points[]     detailed achievements - the evidence
  resume_bullets[] short (<= 108 char) resume-ready points, curated by the user;
                   the resume tailor prefers these over rewriting key_points
  start_date/end_date
  priority         0-5 user score ("personal best" = 5); boosts JD ranking
  is_favourite     always offered first when choosing projects for a resume
  repo_key         normalized repo URL, the identity used to dedupe imports

Data repair
  The table held the same project several times (a GitHub import and a
  hand-written entry for one repo; one repo imported twice). Rows are grouped
  by repo_key and merged into the hand-written one: its title and curated tech
  stack win, key points are pooled. Every row is written to
  data/backups/project_backup_f5a1c9e03b72.json FIRST, so the merge is
  reversible by hand. kb_chunk rows of merged-away projects are deleted; the
  surviving rows' chunks are re-embedded by
  `python -m scripts.reembed_projects` (the migration cannot call Gemini).

qa_question
  company  the employer a company_specific answer belongs to, stored on the
           question. It used to be derived from job_description via jd_id,
           which is ON DELETE SET NULL - deleting a JD silently orphaned its
           answers. Backfilled from job_description.
  kind     the question kind at save time, so retrieval can refuse to
           auto-fill a banked motivation answer regardless of how the NEW
           question was worded.

Revision ID: f5a1c9e03b72
Revises: e3b8f6d21a47
"""
import json
import re
from pathlib import Path
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "f5a1c9e03b72"
down_revision: Union[str, Sequence[str], None] = "e3b8f6d21a47"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

BACKUP = Path(__file__).resolve().parents[3] / "data" / "backups" / "project_backup_f5a1c9e03b72.json"


def repo_key(url):
    if not url:
        return None
    u = url.strip().lower()
    u = re.sub(r"^https?://", "", u)
    u = re.sub(r"^www\.", "", u)
    u = re.sub(r"\.git$", "", u.rstrip("/"))
    return u or None


def split_points(*blocks):
    out, seen = [], set()
    for block in blocks:
        for line in (block or "").split("\n"):
            s = re.sub(r"\s+", " ", line).strip().lstrip("-•*·").strip()
            if len(s) >= 12 and s.lower() not in seen:
                seen.add(s.lower())
                out.append(s)
    return out


def upgrade() -> None:
    op.execute("""
        ALTER TABLE project
          ADD COLUMN summary        TEXT,
          ADD COLUMN role           TEXT,
          ADD COLUMN key_points     TEXT[] NOT NULL DEFAULT '{}',
          ADD COLUMN resume_bullets TEXT[] NOT NULL DEFAULT '{}',
          ADD COLUMN start_date     DATE,
          ADD COLUMN end_date       DATE,
          ADD COLUMN priority       SMALLINT NOT NULL DEFAULT 0
                                    CHECK (priority BETWEEN 0 AND 5),
          ADD COLUMN is_favourite   BOOLEAN NOT NULL DEFAULT false,
          ADD COLUMN repo_key       TEXT
    """)
    op.execute("ALTER TABLE qa_question ADD COLUMN company TEXT, ADD COLUMN kind TEXT")
    op.execute("""
        UPDATE qa_question q SET company = jd.company
        FROM job_description jd WHERE jd.id = q.jd_id AND q.company IS NULL
    """)

    bind = op.get_bind()
    rows = [dict(r._mapping) for r in bind.execute(sa.text("SELECT * FROM project ORDER BY created_at"))]
    BACKUP.parent.mkdir(parents=True, exist_ok=True)
    BACKUP.write_text(json.dumps(rows, default=str, indent=1))

    groups: dict = {}
    for r in rows:
        key = repo_key(r["github_url"]) or repo_key(r["live_url"]) \
            or "title:" + re.sub(r"[^a-z0-9]", "", r["title"].lower())
        groups.setdefault((r["user_id"], key), []).append(r)

    for (user_id, key), group in groups.items():
        curated = [g for g in group if " " in g["title"]]
        primary = max(curated or group, key=lambda g: (len(g["title"]), len(g["description"] or "")))
        tech = next((g["tech_stack"] for g in curated if g["tech_stack"]), None) \
            or next((g["tech_stack"] for g in group if g["tech_stack"]), [])
        points = split_points(*[x for g in [primary] + [g for g in group if g is not primary]
                                for x in (g["highlights"],)])
        description = max((g["description"] or "" for g in group), key=len)
        bind.execute(sa.text("""
            UPDATE project SET key_points = :kp, tech_stack = :tech, description = :d,
                   github_url = :gh, live_url = :live, repo_key = :rk, updated_at = now()
            WHERE id = :id
        """), {
            "kp": points, "tech": tech, "d": description,
            "gh": next((g["github_url"] for g in group if g["github_url"]), None),
            "live": next((g["live_url"] for g in group if g["live_url"]), None),
            "rk": None if key.startswith("title:") else key, "id": primary["id"],
        })
        for g in group:
            if g is primary:
                continue
            bind.execute(sa.text("DELETE FROM kb_chunk WHERE source_table = 'project' AND source_id = :id"),
                         {"id": g["id"]})
            bind.execute(sa.text("DELETE FROM project WHERE id = :id"), {"id": g["id"]})

    op.execute("CREATE UNIQUE INDEX uq_project_user_repo ON project(user_id, repo_key) "
               "WHERE repo_key IS NOT NULL")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_project_user_repo")
    op.execute("ALTER TABLE qa_question DROP COLUMN company, DROP COLUMN kind")
    op.execute("""
        ALTER TABLE project DROP COLUMN summary, DROP COLUMN role, DROP COLUMN key_points,
          DROP COLUMN resume_bullets, DROP COLUMN start_date, DROP COLUMN end_date,
          DROP COLUMN priority, DROP COLUMN is_favourite, DROP COLUMN repo_key
    """)
    # Merged-away rows are not restored automatically; see the JSON backup.
