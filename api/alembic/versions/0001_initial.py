"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-07-01

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "sellers",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("ig_handle", sa.Text(), nullable=False),
        sa.Column("ig_user_id", sa.Text()),
        sa.Column("display_name", sa.Text()),
        sa.Column("category", sa.Text()),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("last_checked_at", sa.DateTime(timezone=True)),
        sa.Column("is_verified", sa.Boolean(), server_default=sa.false()),
        sa.Column("verification_status", sa.Text(), server_default="none"),
        sa.UniqueConstraint("ig_handle"),
    )
    op.create_index("ix_sellers_ig_handle", "sellers", ["ig_handle"])

    op.create_table(
        "seller_aliases",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("seller_id", sa.BigInteger(), sa.ForeignKey("sellers.id")),
        sa.Column("ig_handle", sa.Text(), nullable=False),
        sa.Column("seen_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.create_table(
        "payment_identities",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.UniqueConstraint("kind", "value"),
    )
    op.create_index("ix_payment_identities_kind_value", "payment_identities", ["kind", "value"])

    op.create_table(
        "seller_payment_links",
        sa.Column("seller_id", sa.BigInteger(), sa.ForeignKey("sellers.id"), primary_key=True),
        sa.Column(
            "payment_identity_id",
            sa.BigInteger(),
            sa.ForeignKey("payment_identities.id"),
            primary_key=True,
        ),
        sa.Column("source", sa.Text()),
        sa.Column("seen_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.create_table(
        "checks",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("seller_id", sa.BigInteger(), sa.ForeignKey("sellers.id")),
        sa.Column("requested_by_chat_id", sa.Text()),
        sa.Column("requested_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("served_from_cache", sa.Boolean()),
        sa.Column("result", postgresql.JSONB()),
    )

    op.create_table(
        "risk_snapshots",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("seller_id", sa.BigInteger(), sa.ForeignKey("sellers.id")),
        sa.Column("computed_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("signals", postgresql.JSONB(), nullable=False),
        sa.Column("patterns_matched", sa.Integer()),
        sa.Column("patterns_total", sa.Integer()),
        sa.Column("risk_band", sa.Text()),
    )

    op.create_table(
        "reports",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("seller_id", sa.BigInteger(), sa.ForeignKey("sellers.id")),
        sa.Column("reporter_chat_id", sa.Text()),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("narrative", sa.Text()),
        sa.Column("evidence_file_id", sa.Text()),
        sa.Column("payment_identity_id", sa.BigInteger(), sa.ForeignKey("payment_identities.id")),
        sa.Column("status", sa.Text(), server_default="unreviewed"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.create_table(
        "followups",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("check_id", sa.BigInteger(), sa.ForeignKey("checks.id")),
        sa.Column("scheduled_for", sa.DateTime(timezone=True)),
        sa.Column("sent_at", sa.DateTime(timezone=True)),
        sa.Column("response", sa.Text()),
        sa.Column("responded_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_followups_scheduled_for", "followups", ["scheduled_for"])

    op.create_table(
        "raw_mentions",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("source_url", sa.Text()),
        sa.Column("fetched_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("content", sa.Text()),
        sa.Column("processed", sa.Boolean(), server_default=sa.false()),
    )
    op.create_index("ix_raw_mentions_processed", "raw_mentions", ["processed"])

    op.create_table(
        "verifications",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("seller_id", sa.BigInteger(), sa.ForeignKey("sellers.id")),
        sa.Column("legal_name", sa.Text()),
        sa.Column("gstin", sa.Text()),
        sa.Column("pan_last4", sa.Text()),
        sa.Column("kyc_doc_refs", postgresql.JSONB()),
        sa.Column("razorpay_subscription_id", sa.Text()),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("lapsed_at", sa.DateTime(timezone=True)),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("revoke_reason", sa.Text()),
    )


def downgrade() -> None:
    op.drop_table("verifications")
    op.drop_index("ix_raw_mentions_processed", table_name="raw_mentions")
    op.drop_table("raw_mentions")
    op.drop_index("ix_followups_scheduled_for", table_name="followups")
    op.drop_table("followups")
    op.drop_table("reports")
    op.drop_table("risk_snapshots")
    op.drop_table("checks")
    op.drop_table("seller_payment_links")
    op.drop_index("ix_payment_identities_kind_value", table_name="payment_identities")
    op.drop_table("payment_identities")
    op.drop_table("seller_aliases")
    op.drop_index("ix_sellers_ig_handle", table_name="sellers")
    op.drop_table("sellers")
