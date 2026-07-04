"""brand_reviews cache + graded-issue columns on reports (Workstreams A/B/D)

Revision ID: 0003
Revises: 0002
Create Date: 2026-07-03

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "brand_reviews",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("seller_id", sa.BigInteger(), sa.ForeignKey("sellers.id"), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("source_url", sa.Text()),
        sa.Column("source_author", sa.Text()),
        sa.Column("source_author_age_days", sa.Integer()),
        sa.Column("source_subreddit", sa.Text()),
        sa.Column("posted_at", sa.DateTime(timezone=True)),
        sa.Column("raw_text", sa.Text(), nullable=False),
        sa.Column("issue_categories", postgresql.JSONB()),
        sa.Column("sentiment", sa.Text()),
        sa.Column("spam_score", sa.Float()),
        sa.Column("extracted_at", sa.DateTime(timezone=True)),
        sa.Column("fetched_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("source_url", "seller_id"),
    )
    op.create_index("ix_brand_reviews_seller", "brand_reviews", ["seller_id"])

    op.create_table(
        "brand_review_freshness",
        sa.Column("seller_id", sa.BigInteger(), sa.ForeignKey("sellers.id"), primary_key=True),
        sa.Column("source", sa.Text(), primary_key=True),
        sa.Column("last_scraped_at", sa.DateTime(timezone=True)),
    )

    op.add_column("reports", sa.Column("issue_categories", postgresql.JSONB()))
    op.add_column("reports", sa.Column("spam_score", sa.Float()))
    op.add_column("reports", sa.Column("reporter_trust", sa.Float()))


def downgrade() -> None:
    op.drop_column("reports", "reporter_trust")
    op.drop_column("reports", "spam_score")
    op.drop_column("reports", "issue_categories")
    op.drop_table("brand_review_freshness")
    op.drop_index("ix_brand_reviews_seller", table_name="brand_reviews")
    op.drop_table("brand_reviews")
