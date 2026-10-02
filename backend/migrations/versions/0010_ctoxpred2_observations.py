"""add CToxPred2 model and channel observations"""

import sqlalchemy as sa
from alembic import op

revision = "0010_ctoxpred2_observations"
down_revision = "0009_admet_observation_snapshots"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ctoxpred2_models",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("identity_json", sa.Text(), nullable=False),
    )
    op.create_table(
        "ctoxpred2_executions",
        sa.Column("tool_call_id", sa.Uuid(), primary_key=True),
        sa.Column("request_id", sa.Uuid(), nullable=False, unique=True),
        sa.Column(
            "analysis_id",
            sa.Uuid(),
            sa.ForeignKey("analyses.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column(
            "model_key",
            sa.String(64),
            sa.ForeignKey("ctoxpred2_models.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("canonical_smiles", sa.String(5000), nullable=False),
        sa.Column("limitations_json", sa.Text(), nullable=False),
    )
    op.create_index("ix_ctoxpred2_executions_analysis_id", "ctoxpred2_executions", ["analysis_id"])
    op.create_index("ix_ctoxpred2_executions_run_id", "ctoxpred2_executions", ["run_id"])
    op.create_table(
        "ctoxpred2_predictions",
        sa.Column(
            "tool_call_id",
            sa.Uuid(),
            sa.ForeignKey("ctoxpred2_executions.tool_call_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("channel", sa.String(20), primary_key=True),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("label", sa.String(16), nullable=False),
        sa.Column("class_probability", sa.Float(), nullable=False),
        sa.CheckConstraint("label IN ('positive', 'negative')", name="ck_ctox_label"),
        sa.CheckConstraint(
            "class_probability >= 0 AND class_probability <= 1",
            name="ck_ctox_probability",
        ),
    )
    op.create_table(
        "ctoxpred2_observation_snapshots",
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
            "analysis_id", "snapshot_version", name="uq_ctox_snapshot_analysis_version"
        ),
    )
    op.create_index(
        "ix_ctoxpred2_observation_snapshots_analysis_id",
        "ctoxpred2_observation_snapshots",
        ["analysis_id"],
    )


def downgrade() -> None:
    op.drop_table("ctoxpred2_observation_snapshots")
    op.drop_table("ctoxpred2_predictions")
    op.drop_table("ctoxpred2_executions")
    op.drop_table("ctoxpred2_models")
