"""operator_links (same-operator graph, Workstream C)

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-03

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "operator_links",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("seller_a", sa.BigInteger(), sa.ForeignKey("sellers.id"), nullable=False),
        sa.Column("seller_b", sa.BigInteger(), sa.ForeignKey("sellers.id"), nullable=False),
        sa.Column("basis", sa.Text(), nullable=False),
        sa.Column("confidence", sa.Float()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("manual_override", sa.Text()),
        sa.UniqueConstraint("seller_a", "seller_b", "basis"),
    )
    op.create_index("ix_operator_links_seller_a", "operator_links", ["seller_a"])
    op.create_index("ix_operator_links_seller_b", "operator_links", ["seller_b"])


def downgrade() -> None:
    op.drop_index("ix_operator_links_seller_b", table_name="operator_links")
    op.drop_index("ix_operator_links_seller_a", table_name="operator_links")
    op.drop_table("operator_links")
