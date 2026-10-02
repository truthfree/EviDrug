"""create common tool execution ledger

Revision ID: 0004_tool_execution
Revises: 0003_dta_persistence
"""

import sqlalchemy as sa
from alembic import op

revision = "0004_tool_execution"
down_revision = "0003_dta_persistence"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """실행 소유권과 완료 이력을 보관하는 공통 테이블을 만든다."""
    op.create_table(
        "tool_executions",
        sa.Column("tool_call_id", sa.Uuid(), primary_key=True),
        sa.Column("request_id", sa.Uuid(), nullable=False, unique=True),
        sa.Column(
            "analysis_id",
            sa.Uuid(),
            sa.ForeignKey("analyses.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("tool_id", sa.String(120), nullable=False),
        sa.Column("input_sha256", sa.String(64), nullable=False),
        sa.Column("owner_token", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("error_code", sa.String(80), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "(status = 'running' AND finished_at IS NULL AND error_code IS NULL) OR "
            "(status = 'succeeded' AND finished_at IS NOT NULL AND error_code IS NULL) OR "
            "(status = 'failed' AND finished_at IS NOT NULL AND error_code IS NOT NULL)",
            name="ck_tool_execution_outcome",
        ),
    )
    op.create_index("ix_tool_execution_expiry", "tool_executions", ["status", "lease_expires_at"])
    op.create_index("ix_tool_executions_analysis_id", "tool_executions", ["analysis_id"])
    op.create_index("ix_tool_executions_run_id", "tool_executions", ["run_id"])


def downgrade() -> None:
    """ledger만 삭제한다. 기존 ADMET/DTA 결과 테이블은 유지한다."""
    op.drop_index("ix_tool_executions_run_id", table_name="tool_executions")
    op.drop_index("ix_tool_executions_analysis_id", table_name="tool_executions")
    op.drop_index("ix_tool_execution_expiry", table_name="tool_executions")
    op.drop_table("tool_executions")
