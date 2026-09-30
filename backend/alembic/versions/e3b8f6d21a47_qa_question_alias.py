"""qa_question_alias: learned paraphrases of banked questions

Measured on gemini-embedding-001 (768d), paraphrases of one question score
0.66-0.90 while DIFFERENT questions on neighbouring topics score up to 0.64
("notice period" vs "salary expectations" = 0.61). No single threshold can
auto-fill paraphrases without eventually auto-filling a wrong answer.

So the bank learns instead. When the user confirms a Tier 3 suggestion ("yes,
this is the same question"), the new wording is stored here with its own
embedding. The next time that wording appears it matches the alias at ~1.0 and
auto-fills at Tier 1 - but only ever for a phrasing a human has approved once.

Revision ID: e3b8f6d21a47
Revises: d7e2a91c4f05
"""
from typing import Sequence, Union

from alembic import op

revision: str = "e3b8f6d21a47"
down_revision: Union[str, Sequence[str], None] = "d7e2a91c4f05"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE qa_question_alias (
            id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            question_id UUID NOT NULL REFERENCES qa_question(id) ON DELETE CASCADE,
            alias_text  TEXT NOT NULL,
            embedding   VECTOR(768) NOT NULL,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (question_id, alias_text)
        )
    """)
    op.execute("CREATE INDEX idx_qa_alias_question ON qa_question_alias(question_id)")
    op.execute("CREATE INDEX idx_qa_alias_embedding_hnsw ON qa_question_alias "
               "USING hnsw (embedding vector_cosine_ops)")


def downgrade() -> None:
    op.execute("DROP TABLE qa_question_alias")
