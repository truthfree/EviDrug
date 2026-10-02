"""create ADMET manifest and prediction persistence

Revision ID: 0002_admet_persistence
Revises: 0001_analysis_jobs
Create Date: 2026-09-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_admet_persistence"
down_revision: str | None = "0001_analysis_jobs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """stable manifest와 실행별 ADMET prediction 테이블을 생성한다."""

    op.create_table(
        "admet_model_manifests",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("manifest_sha256", sa.String(length=64), nullable=False),
        sa.Column("schema_version", sa.String(length=40), nullable=False),
        sa.Column("tool_id", sa.String(length=120), nullable=False),
        sa.Column("tool_version", sa.String(length=200), nullable=False),
        sa.Column("endpoint_metadata_sha256", sa.String(length=64), nullable=False),
        sa.Column("reference_population", sa.String(length=300), nullable=False),
        sa.Column("reference_sha256", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("manifest_sha256"),
    )
    op.create_table(
        "admet_model_artifacts",
        sa.Column("manifest_id", sa.Uuid(), nullable=False),
        sa.Column("relative_path", sa.String(length=500), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.ForeignKeyConstraint(
            ["manifest_id"],
            ["admet_model_manifests.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("manifest_id", "relative_path"),
        sa.UniqueConstraint("manifest_id", "position", name="uq_admet_artifacts_position"),
    )
    op.create_table(
        "admet_endpoints",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("manifest_id", sa.Uuid(), nullable=False),
        sa.Column("endpoint_id", sa.String(length=160), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("category", sa.String(length=120), nullable=False),
        sa.Column("name", sa.String(length=300), nullable=False),
        sa.Column("task_type", sa.String(length=32), nullable=False),
        sa.Column("dataset_size", sa.Integer(), nullable=True),
        sa.Column("units", sa.String(length=120), nullable=True),
        sa.Column("minimum", sa.Float(), nullable=True),
        sa.Column("maximum", sa.Float(), nullable=True),
        sa.Column("minimum_unbounded", sa.Boolean(), nullable=False),
        sa.Column("maximum_unbounded", sa.Boolean(), nullable=False),
        sa.Column("species", sa.String(length=200), nullable=True),
        sa.Column("tdc_rank", sa.Integer(), nullable=True),
        sa.Column("auprc", sa.Float(), nullable=True),
        sa.Column("auroc", sa.Float(), nullable=True),
        sa.Column("r_squared", sa.Float(), nullable=True),
        sa.Column("mae", sa.Float(), nullable=True),
        sa.Column("source_url", sa.String(length=1000), nullable=True),
        sa.CheckConstraint(
            "minimum IS NULL OR maximum IS NULL OR minimum <= maximum",
            name="ck_admet_endpoints_range",
        ),
        sa.CheckConstraint(
            "auprc IS NULL OR (auprc >= 0 AND auprc <= 1)",
            name="ck_admet_endpoints_auprc",
        ),
        sa.CheckConstraint(
            "auroc IS NULL OR (auroc >= 0 AND auroc <= 1)",
            name="ck_admet_endpoints_auroc",
        ),
        sa.CheckConstraint("mae IS NULL OR mae >= 0", name="ck_admet_endpoints_mae"),
        sa.ForeignKeyConstraint(
            ["manifest_id"],
            ["admet_model_manifests.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "manifest_id", name="uq_admet_endpoints_id_manifest"),
        sa.UniqueConstraint(
            "manifest_id",
            "endpoint_id",
            name="uq_admet_endpoints_manifest_key",
        ),
        sa.UniqueConstraint("manifest_id", "position", name="uq_admet_endpoints_position"),
    )
    op.create_index(
        op.f("ix_admet_endpoints_manifest_id"),
        "admet_endpoints",
        ["manifest_id"],
        unique=False,
    )
    op.create_table(
        "admet_manifest_limitations",
        sa.Column("manifest_id", sa.Uuid(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("limitation", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["manifest_id"],
            ["admet_model_manifests.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("manifest_id", "position"),
    )
    op.create_table(
        "admet_tool_executions",
        sa.Column("tool_call_id", sa.Uuid(), nullable=False),
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column("analysis_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("manifest_id", sa.Uuid(), nullable=False),
        sa.Column("canonical_smiles", sa.String(length=4096), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["analysis_id"], ["analyses.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["manifest_id"],
            ["admet_model_manifests.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("tool_call_id"),
        sa.UniqueConstraint(
            "tool_call_id",
            "manifest_id",
            name="uq_admet_executions_call_manifest",
        ),
        sa.UniqueConstraint("request_id", name="uq_admet_executions_request"),
    )
    op.create_index(
        op.f("ix_admet_tool_executions_run_id"),
        "admet_tool_executions",
        ["run_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_admet_tool_executions_analysis_id"),
        "admet_tool_executions",
        ["analysis_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_admet_tool_executions_manifest_id"),
        "admet_tool_executions",
        ["manifest_id"],
        unique=False,
    )
    op.create_table(
        "admet_predictions",
        sa.Column("tool_call_id", sa.Uuid(), nullable=False),
        sa.Column("endpoint_record_id", sa.Uuid(), nullable=False),
        sa.Column("manifest_id", sa.Uuid(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("value", sa.Float(), nullable=False),
        sa.Column("drugbank_approved_percentile", sa.Float(), nullable=False),
        sa.CheckConstraint(
            "drugbank_approved_percentile >= 0 AND drugbank_approved_percentile <= 100",
            name="ck_admet_predictions_percentile",
        ),
        sa.ForeignKeyConstraint(
            ["endpoint_record_id", "manifest_id"],
            ["admet_endpoints.id", "admet_endpoints.manifest_id"],
            name="fk_admet_predictions_endpoint_manifest",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tool_call_id", "manifest_id"],
            ["admet_tool_executions.tool_call_id", "admet_tool_executions.manifest_id"],
            name="fk_admet_predictions_execution_manifest",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("tool_call_id", "endpoint_record_id"),
        sa.UniqueConstraint("tool_call_id", "position", name="uq_admet_predictions_position"),
    )


def downgrade() -> None:
    """ADMET persistence 테이블을 의존성 역순으로 제거한다."""

    op.drop_table("admet_predictions")
    op.drop_index(
        op.f("ix_admet_tool_executions_manifest_id"),
        table_name="admet_tool_executions",
    )
    op.drop_index(
        op.f("ix_admet_tool_executions_analysis_id"),
        table_name="admet_tool_executions",
    )
    op.drop_index(
        op.f("ix_admet_tool_executions_run_id"),
        table_name="admet_tool_executions",
    )
    op.drop_table("admet_tool_executions")
    op.drop_table("admet_manifest_limitations")
    op.drop_index(op.f("ix_admet_endpoints_manifest_id"), table_name="admet_endpoints")
    op.drop_table("admet_endpoints")
    op.drop_table("admet_model_artifacts")
    op.drop_table("admet_model_manifests")
