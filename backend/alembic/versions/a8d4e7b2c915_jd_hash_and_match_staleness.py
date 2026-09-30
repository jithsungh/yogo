"""job_description.content_hash; match_result staleness fields

job_description.content_hash
    sha256 of the JD text after normalization (lowercased, whitespace
    collapsed, job-board boilerplate removed). Ingest checks it BEFORE the
    extraction call: 7 of 19 stored JDs were exact re-pastes, each one a
    wasted Gemini call and a second copy with its own, different extraction.
    Non-unique on purpose - existing duplicates are left for the user to
    review and delete, not silently merged.

match_result.algo_version / inputs_hash
    Nothing marked a match result as outdated when the skills, the JD's
    extracted requirements, or the scoring code changed, so verdicts drifted
    silently (a JD matching 9/9 skills showed "skip 10%"). A result is stale
    when either value differs from what compute_match would use now. Existing
    rows get NULL and therefore read as stale.

Revision ID: a8d4e7b2c915
Revises: f5a1c9e03b72
"""
import hashlib
import re
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a8d4e7b2c915"
down_revision: Union[str, Sequence[str], None] = "f5a1c9e03b72"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Kept identical to jd_service.jd_content_hash (duplicated so the migration
# never breaks when application code changes).
_BOILERPLATE = re.compile(
    r"^(company logo for.*|about the job|show more|show less|.*people clicked apply.*|"
    r"your profile and resume match.*|apply|save|easy apply|promoted|actively recruiting)$",
    re.IGNORECASE,
)


def content_hash(raw: str) -> str:
    lines = [l.strip() for l in (raw or "").splitlines()]
    kept = [l for l in lines if l and not _BOILERPLATE.match(l)]
    norm = re.sub(r"\s+", " ", " ".join(kept).lower()).strip()
    return hashlib.sha256(norm.encode()).hexdigest()


def upgrade() -> None:
    op.add_column("job_description", sa.Column("content_hash", sa.Text(), nullable=True))
    op.create_index("idx_jd_user_hash", "job_description", ["user_id", "content_hash"])
    bind = op.get_bind()
    for jd_id, raw in bind.execute(sa.text("SELECT id, raw_text FROM job_description")).fetchall():
        bind.execute(sa.text("UPDATE job_description SET content_hash = :h WHERE id = :id"),
                     {"h": content_hash(raw), "id": jd_id})
    op.add_column("match_result", sa.Column("algo_version", sa.Text(), nullable=True))
    op.add_column("match_result", sa.Column("inputs_hash", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("match_result", "inputs_hash")
    op.drop_column("match_result", "algo_version")
    op.drop_index("idx_jd_user_hash", table_name="job_description")
    op.drop_column("job_description", "content_hash")
