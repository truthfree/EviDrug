"""create DTA provider result persistence

Revision ID: 0003_dta_persistence
Revises: 0002_admet_persistence
"""

import sqlalchemy as sa
from alembic import op

revision = "0003_dta_persistence"
down_revision = "0002_admet_persistence"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """모델과 완료된 실행·관측 테이블을 생성한다."""
    op.create_table(
        "dta_models",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("provider", sa.String(200), nullable=False),
        sa.Column("model_id", sa.String(200), nullable=False),
        sa.Column("version", sa.String(200), nullable=False),
        sa.Column("artifact_sha256", sa.String(64), nullable=True),
    )
    op.create_table(
        "dta_executions",
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
            sa.ForeignKey("dta_models.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("canonical_smiles", sa.String(4096), nullable=False),
        sa.Column("target_sequence", sa.Text(), nullable=False),
        sa.Column("target_sequence_sha256", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("error_code", sa.String(80), nullable=True),
        sa.Column("duration_seconds", sa.Float(), nullable=False),
        sa.CheckConstraint("duration_seconds >= 0", name="ck_dta_duration"),
        sa.CheckConstraint(
            "(status = 'succeeded' AND error_code IS NULL) OR "
            "(status = 'unavailable' AND error_code IS NOT NULL "
            "AND error_code IN ('provider_timeout', 'provider_unavailable'))",
            name="ck_dta_outcome",
        ),
    )
    op.create_index("ix_dta_executions_analysis_id", "dta_executions", ["analysis_id"])
    op.create_index("ix_dta_executions_run_id", "dta_executions", ["run_id"])
    op.create_table(
        "dta_observations",
        sa.Column(
            "tool_call_id",
            sa.Uuid(),
            sa.ForeignKey("dta_executions.tool_call_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("score_type", sa.String(40), primary_key=True),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("value", sa.Float(), nullable=False),
        sa.Column("unit", sa.String(80), nullable=False),
        sa.CheckConstraint(
            "(score_type = 'predicted_pkd' AND unit = '-log10(Kd [M])') OR "
            "(score_type = 'pic50_like' AND unit = '-log10(IC50 [M])') OR "
            "(score_type = 'binding_probability' AND unit = 'probability' "
            "AND value >= 0 AND value <= 1)",
            name="ck_dta_score_semantics",
        ),
    )


def downgrade() -> None:
    """DTA 전용 테이블만 의존성 역순으로 제거한다."""
    op.drop_table("dta_observations")
    op.drop_index("ix_dta_executions_run_id", table_name="dta_executions")
    op.drop_index("ix_dta_executions_analysis_id", table_name="dta_executions")
    op.drop_table("dta_executions")
    op.drop_table("dta_models")
