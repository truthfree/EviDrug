"""create tool admission policy and budget records"""

import sqlalchemy as sa
from alembic import op

revision = "0005_tool_admission"
down_revision = "0004_tool_execution"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """서버 정책 snapshot과 원자적 호출 예산·승인 이력을 추가한다."""
    op.create_table(
        "tool_admission_runs",
        sa.Column("run_id", sa.Uuid(), primary_key=True),
        sa.Column(
            "analysis_id",
            sa.Uuid(),
            sa.ForeignKey("analyses.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("policy_json", sa.Text(), nullable=False),
        sa.Column("policy_sha256", sa.String(64), nullable=False),
        sa.Column("max_calls", sa.Integer(), nullable=False),
        sa.Column("used_calls", sa.Integer(), nullable=False),
        sa.Column("deadline", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "used_calls >= 0 AND used_calls <= max_calls AND max_calls >= 0",
            name="ck_tool_run_budget",
        ),
    )
    op.create_index("ix_tool_admission_runs_analysis_id", "tool_admission_runs", ["analysis_id"])
    op.create_table(
        "tool_admissions",
        sa.Column("request_id", sa.Uuid(), primary_key=True),
        sa.Column("tool_call_id", sa.Uuid(), unique=True, nullable=False),
        sa.Column(
            "run_id",
            sa.Uuid(),
            sa.ForeignKey("tool_admission_runs.run_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("request_sha256", sa.String(64), nullable=False),
        sa.Column("tool_id", sa.String(120), nullable=False),
        sa.Column("tool_version", sa.String(200), nullable=False),
        sa.Column("decision", sa.String(16), nullable=False),
        sa.Column("reason_code", sa.String(120), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "(decision = 'approved' AND reason_code IS NULL) OR "
            "(decision = 'rejected' AND reason_code IS NOT NULL)",
            name="ck_tool_admission_decision",
        ),
    )
    op.create_index("ix_tool_admissions_run_id", "tool_admissions", ["run_id"])


def downgrade() -> None:
    """승인·예산 이력만 제거하며 기존 도구 결과는 보존한다."""
    op.drop_index("ix_tool_admissions_run_id", table_name="tool_admissions")
    op.drop_table("tool_admissions")
    op.drop_index("ix_tool_admission_runs_analysis_id", table_name="tool_admission_runs")
    op.drop_table("tool_admission_runs")
