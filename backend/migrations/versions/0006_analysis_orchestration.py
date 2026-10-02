"""add analysis orchestration runs and execution trace"""

import sqlalchemy as sa
from alembic import op

revision = "0006_analysis_orchestration"
down_revision = "0005_tool_admission"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """분석 실행 lease, Agent run과 append-only trace를 추가한다."""
    op.create_table(
        "analysis_executions",
        sa.Column(
            "analysis_id",
            sa.Uuid(),
            sa.ForeignKey("analyses.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("owner_token", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("profile_json", sa.Text(), nullable=False),
        sa.Column("profile_sha256", sa.String(64), nullable=False),
        sa.Column("error_code", sa.String(80), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "(status = 'running' AND finished_at IS NULL AND error_code IS NULL) OR "
            "(status = 'succeeded' AND finished_at IS NOT NULL AND error_code IS NULL) OR "
            "(status = 'failed' AND finished_at IS NOT NULL AND error_code IS NOT NULL)",
            name="ck_analysis_execution_outcome",
        ),
    )
    op.create_index(
        "ix_analysis_execution_expiry",
        "analysis_executions",
        ["status", "lease_expires_at"],
    )
    op.create_table(
        "agent_runs",
        sa.Column("run_id", sa.Uuid(), primary_key=True),
        sa.Column(
            "analysis_id",
            sa.Uuid(),
            sa.ForeignKey("analyses.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("agent_name", sa.String(32), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column(
            "parent_run_id",
            sa.Uuid(),
            sa.ForeignKey("agent_runs.run_id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("input_sha256", sa.String(64), nullable=False),
        sa.Column("output_json", sa.Text(), nullable=True),
        sa.Column("output_sha256", sa.String(64), nullable=True),
        sa.Column("execution_metadata_json", sa.Text(), nullable=True),
        sa.Column("error_code", sa.String(120), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint(
            "analysis_id", "agent_name", "attempt", name="uq_agent_run_stage_attempt"
        ),
        sa.CheckConstraint(
            "(status = 'running' AND finished_at IS NULL AND error_code IS NULL) OR "
            "(status IN ('completed', 'partial_failure') AND finished_at IS NOT NULL "
            "AND output_json IS NOT NULL) OR "
            "(status IN ('failed', 'skipped') AND finished_at IS NOT NULL "
            "AND error_code IS NOT NULL)",
            name="ck_agent_run_outcome",
        ),
    )
    op.create_index("ix_agent_runs_analysis_id", "agent_runs", ["analysis_id"])
    op.create_table(
        "execution_trace_events",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "analysis_id",
            sa.Uuid(),
            sa.ForeignKey("analyses.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "run_id",
            sa.Uuid(),
            sa.ForeignKey("agent_runs.run_id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("event_type", sa.String(40), nullable=False),
        sa.Column("stage_name", sa.String(32), nullable=True),
        sa.Column("reason_code", sa.String(120), nullable=True),
        sa.Column("policy_version", sa.String(120), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_execution_trace_events_analysis_id", "execution_trace_events", ["analysis_id"]
    )
    op.create_index("ix_execution_trace_events_run_id", "execution_trace_events", ["run_id"])


def downgrade() -> None:
    """실행 trace와 orchestration ledger를 의존성 역순으로 제거한다."""
    op.drop_index("ix_execution_trace_events_run_id", table_name="execution_trace_events")
    op.drop_index("ix_execution_trace_events_analysis_id", table_name="execution_trace_events")
    op.drop_table("execution_trace_events")
    op.drop_index("ix_agent_runs_analysis_id", table_name="agent_runs")
    op.drop_table("agent_runs")
    op.drop_index("ix_analysis_execution_expiry", table_name="analysis_executions")
    op.drop_table("analysis_executions")
