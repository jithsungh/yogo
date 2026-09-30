"""resume_version: cache key + build metadata

Adds the columns that let a resume be REUSED instead of regenerated.

content_hash fingerprints every input the resume was built from (KB rows,
match result, JD, template files, prompt version). When it is unchanged, the
stored PDF is still correct and there is no reason to pay for another Gemini
call or another Tectonic run.

Revision ID: c41a7e2f0b13
Revises: b2b5983ef359
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c41a7e2f0b13"
down_revision: Union[str, Sequence[str], None] = "b2b5983ef359"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("resume_version", sa.Column("content_hash", sa.Text(), nullable=True))
    op.add_column("resume_version", sa.Column("pages", sa.SmallInteger(), nullable=True))
    op.add_column("resume_version", sa.Column("build_report", postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    # Lookup is always "is there a current resume for this user+JD", newest first.
    op.create_index(
        "idx_resume_version_cache",
        "resume_version",
        ["user_id", "jd_id", "content_hash"],
    )


def downgrade() -> None:
    op.drop_index("idx_resume_version_cache", table_name="resume_version")
    op.drop_column("resume_version", "build_report")
    op.drop_column("resume_version", "pages")
    op.drop_column("resume_version", "content_hash")
