"""qa_answer: edit + reuse tracking for the answer bank

updated_at      - answers are editable from the bank UI; the kb_chunk built
                  from an answer is re-synced on edit, and this records when.
times_reused    - incremented each time a banked answer is reused for a new
last_used_at      application. This is the Phase 4 milestone measured
                  directly: by the 5th application the common questions
                  should be climbing here instead of being regenerated.

Revision ID: d7e2a91c4f05
Revises: c41a7e2f0b13
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d7e2a91c4f05"
down_revision: Union[str, Sequence[str], None] = "c41a7e2f0b13"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("qa_answer", sa.Column("updated_at", sa.DateTime(timezone=True),
                                         nullable=False, server_default=sa.text("now()")))
    op.add_column("qa_answer", sa.Column("times_reused", sa.Integer(),
                                         nullable=False, server_default=sa.text("0")))
    op.add_column("qa_answer", sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("qa_answer", "last_used_at")
    op.drop_column("qa_answer", "times_reused")
    op.drop_column("qa_answer", "updated_at")
