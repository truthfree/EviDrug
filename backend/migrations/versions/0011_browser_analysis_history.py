"""Add browser-scoped ownership for saved analyses."""

import sqlalchemy as sa
from alembic import op

revision = "0011_browser_analysis_history"
down_revision = "0010_ctoxpred2_observations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("analyses", sa.Column("visitor_fingerprint", sa.String(64), nullable=True))
    op.create_index("ix_analyses_visitor_fingerprint", "analyses", ["visitor_fingerprint"])


def downgrade() -> None:
    op.drop_index("ix_analyses_visitor_fingerprint", table_name="analyses")
    op.drop_column("analyses", "visitor_fingerprint")
