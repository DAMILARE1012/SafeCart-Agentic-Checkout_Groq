"""promotion redemption can be voided (paid order refunded)

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-25 12:10:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(op.f("ck_promotion_redemptions_status_valid"), "promotion_redemptions", type_="check")
    op.create_check_constraint(
        op.f("ck_promotion_redemptions_status_valid"),
        "promotion_redemptions",
        "status IN ('reserved', 'committed', 'released', 'voided')",
    )


def downgrade() -> None:
    op.execute("UPDATE promotion_redemptions SET status = 'released' WHERE status = 'voided'")
    op.drop_constraint(op.f("ck_promotion_redemptions_status_valid"), "promotion_redemptions", type_="check")
    op.create_check_constraint(
        op.f("ck_promotion_redemptions_status_valid"),
        "promotion_redemptions",
        "status IN ('reserved', 'committed', 'released')",
    )
