"""Persist the analysis-scoped potency criterion."""

import sqlalchemy as sa
from alembic import op

revision = "0012_add_potency_criterion"
down_revision = "0011_browser_analysis_history"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("analyses", sa.Column("potency_endpoint", sa.String(16), nullable=True))
    op.add_column("analyses", sa.Column("potency_maximum_value", sa.Float(), nullable=True))
    op.add_column("analyses", sa.Column("potency_unit", sa.String(8), nullable=True))


def downgrade() -> None:
    op.drop_column("analyses", "potency_unit")
    op.drop_column("analyses", "potency_maximum_value")
    op.drop_column("analyses", "potency_endpoint")
