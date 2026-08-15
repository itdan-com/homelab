"""audit_events tier + velocity index

Revision ID: a3f7c9e21b04
Revises: e0d96023a23d
Create Date: 2026-08-15 00:00:00.000000

ADR-007 Decision 1 — velocity as a Cedar context flag. `tier` lets the
count query group by (principal, tool, tier) instead of the concrete
resource, which is the whole point: a bulk action touching a hundred
different resources must accumulate into ONE count, not a hundred
counts of one. The composite index is load-bearing, not a nice-to-have
— this query now runs before every person-path decide() call.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a3f7c9e21b04'
down_revision: Union[str, Sequence[str], None] = 'e0d96023a23d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('audit_events', schema=None) as batch_op:
        batch_op.add_column(sa.Column('tier', sa.String(length=64), nullable=True))
        batch_op.create_index(
            'ix_audit_events_velocity',
            ['principal', 'tool', 'tier', 'ts'],
            unique=False,
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('audit_events', schema=None) as batch_op:
        batch_op.drop_index('ix_audit_events_velocity')
        batch_op.drop_column('tier')
