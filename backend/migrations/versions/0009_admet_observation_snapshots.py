"""add ADMET observation snapshot manifests"""

import sqlalchemy as sa
from alembic import op

revision = "0009_admet_observation_snapshots"
down_revision = "0008_trajectory_assets"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "admet_observation_snapshots",
        sa.Column("snapshot_id", sa.Uuid(), primary_key=True),
        sa.Column(
            "analysis_id",
            sa.Uuid(),
            sa.ForeignKey("analyses.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("snapshot_version", sa.String(200), nullable=False),
        sa.Column("manifest_json", sa.Text(), nullable=False),
        sa.Column("manifest_sha256", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "analysis_id", "snapshot_version", name="uq_admet_snapshot_analysis_version"
        ),
    )
    op.create_index(
        "ix_admet_observation_snapshots_analysis_id",
        "admet_observation_snapshots",
        ["analysis_id"],
    )


def downgrade() -> None:
    op.drop_table("admet_observation_snapshots")
