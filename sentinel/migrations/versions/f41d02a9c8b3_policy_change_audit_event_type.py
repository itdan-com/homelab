"""policy_change audit event type (7.2.4)

One new enum value so policy-store activations (and rejected
attempts) are first-class audit events rather than borrowed
semantics. Fourth hand-widened CHECK in a row — Alembic still does
not diff CHECK constraints, so batch mode rebuilds the table and the
constraint actually changes (same trap, same fix as 80951989de42 /
e0b009f26f01 / cdf028306ccc).

Revision ID: f41d02a9c8b3
Revises: 86c82f996509
Create Date: 2026-08-02
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'f41d02a9c8b3'
down_revision: Union[str, Sequence[str], None] = '86c82f996509'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_OLD = ('request', 'grant', 'denial', 'use', 'revocation',
        'kill_engaged', 'kill_released', 'claim',
        'auth_success', 'auth_failure', 'credential_added')
_NEW = _OLD + ('policy_change',)


def _enum(values):
    return sa.Enum(*values, name='auditeventtype', native_enum=False,
                   create_constraint=True, length=16)


def upgrade() -> None:
    with op.batch_alter_table('audit_events', schema=None) as batch_op:
        batch_op.alter_column('event_type', existing_type=_enum(_OLD),
                              type_=_enum(_NEW), existing_nullable=False)


def downgrade() -> None:
    # Existing policy_change rows would violate the narrowed CHECK —
    # fine for a dev-time downgrade, needs a data migration in prod
    # (same caveat as every prior widening).
    with op.batch_alter_table('audit_events', schema=None) as batch_op:
        batch_op.alter_column('event_type', existing_type=_enum(_NEW),
                              type_=_enum(_OLD), existing_nullable=False)
