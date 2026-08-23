"""principals: issuer-qualified pin + stable id; idp_migrations table

Phase 7.8.1 (ADR-008 Decision 1). The TOFU pin becomes (issuer, sub):
a bare sub is only meaningful relative to who asserted it. The global
UNIQUE on idp_sub is REPLACED by a composite — SQLite cannot drop an
inline unnamed unique, so the table is recreated via copy_from, and
the copy_from definition must describe what actually exists BYTE FOR
BYTE: email's uniqueness lives in a UNIQUE INDEX (ix_principals_email,
per 86c82f996509's unique=True+index=True), NOT a constraint — the
first draft declared column flags, batch ignored them, and email
uniqueness silently vanished; the second declared a UniqueConstraint
and drifted from the model's metadata. Both were caught by inserting
duplicates post-migration, which is the check to repeat if this file
is ever touched.

idp_stable_id is a vendor-stable recovery attribute (Entra oid@tid),
never a join key. idp_migrations is the singleton window for the one
sanctioned re-pin path.

Known bound, stated: legacy rows carry idp_iss NULL until their next
sign-in backfills it, and SQLite treats NULLs as distinct in unique
constraints — so the composite does not guard bare-sub duplicates
against legacy rows; the service layer's IntegrityError handling
(idp-sub-collision) is the net for that edge.

Revision ID: b6e4d1a8c3f2
Revises: a3f7c9e21b04
Create Date: 2026-08-22
"""
import sqlalchemy as sa

from alembic import op

revision = "b6e4d1a8c3f2"
down_revision = "a3f7c9e21b04"
branch_labels = None
depends_on = None


def _cols():
    return [
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("email", sa.String(254), nullable=False),
        sa.Column("display_name", sa.String(128)),
        sa.Column("idp_sub", sa.String(255)),
        sa.Column("first_seen_at", sa.DateTime, nullable=False),
        sa.Column("last_seen_at", sa.DateTime),
        sa.Column("disabled_at", sa.DateTime),
    ]


def upgrade() -> None:
    # the PRE-migration shape: email unique INDEX + idp_sub inline
    # unique. The sub unique is NAMED here purely so the batch can drop
    # it — copy_from constraints CARRY THROUGH the rebuild unless
    # explicitly dropped (probe-caught: the first version left the old
    # global unique alive as an autoindex, which would have refused
    # every cross-issuer re-pin sharing a sub string).
    pre = sa.Table(
        "principals", sa.MetaData(), *_cols(),
        sa.Index("ix_principals_email", "email", unique=True),
        sa.UniqueConstraint("idp_sub", name="uq_principals_idp_sub"),
    )
    with op.batch_alter_table("principals", copy_from=pre,
                              recreate="always") as b:
        b.drop_constraint("uq_principals_idp_sub", type_="unique")
        b.add_column(sa.Column("idp_iss", sa.String(255)))
        b.add_column(sa.Column("idp_stable_id", sa.String(128)))
        b.create_unique_constraint("uq_principals_idp_iss_sub",
                                   ["idp_iss", "idp_sub"])

    op.create_table(
        "idp_migrations",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("new_issuer", sa.String(255), nullable=False),
        sa.Column("opened_by", sa.String(128), nullable=False),
        sa.Column("opened_at", sa.DateTime, nullable=False),
        sa.Column("expires_at", sa.DateTime, nullable=False),
    )


def downgrade() -> None:
    # Review-caught ordering: the principals rebuild is the operation
    # that can FAIL on head-legal data (two rows sharing one idp_sub
    # under different issuers violate the recreated global unique) — it
    # runs FIRST so a failure leaves idp_migrations intact and the
    # schema consistent at head. The tmp-table cleanup makes a retry
    # possible after the operator resolves the offending rows.
    op.execute("DROP TABLE IF EXISTS _alembic_tmp_principals")
    post = sa.Table(
        "principals", sa.MetaData(), *_cols(),
        sa.Column("idp_iss", sa.String(255)),
        sa.Column("idp_stable_id", sa.String(128)),
        sa.Index("ix_principals_email", "email", unique=True),
        sa.UniqueConstraint("idp_iss", "idp_sub",
                            name="uq_principals_idp_iss_sub"),
    )
    with op.batch_alter_table("principals", copy_from=post,
                              recreate="always") as b:
        b.drop_constraint("uq_principals_idp_iss_sub", type_="unique")
        b.drop_column("idp_stable_id")
        b.drop_column("idp_iss")
        b.create_unique_constraint("uq_principals_idp_sub", ["idp_sub"])
    op.drop_table("idp_migrations")
