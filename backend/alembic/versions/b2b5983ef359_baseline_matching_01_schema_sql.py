"""baseline matching 01_schema.sql

Revision ID: b2b5983ef359
Revises: 
Create Date: 2026-09-11 15:33:53.823095

This is a baseline migration. The schema was already applied by
db/init/01_schema.sql on first Docker boot. This revision exists solely
so Alembic knows the current state. Both upgrade() and downgrade() are
intentionally empty — we stamp head rather than running this.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b2b5983ef359'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """No-op — schema already applied by 01_schema.sql."""
    pass


def downgrade() -> None:
    """No-op — baseline."""
    pass
