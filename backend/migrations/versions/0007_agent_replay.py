"""Agent 입력 snapshot과 명시적 단독 실행 계획을 보존한다."""

import sqlalchemy as sa
from alembic import op

revision = "0007_agent_replay"
down_revision = "0006_analysis_orchestration"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("agent_runs", sa.Column("input_json", sa.Text(), nullable=True))
    op.create_table(
        "agent_replays",
        sa.Column(
            "analysis_id",
            sa.Uuid(),
            sa.ForeignKey("analyses.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("base_analysis_id", sa.Uuid(), sa.ForeignKey("analyses.id"), nullable=False),
        sa.Column("source_run_id", sa.Uuid(), sa.ForeignKey("agent_runs.run_id"), nullable=False),
        sa.Column("manifest_json", sa.Text(), nullable=False),
        sa.Column("manifest_sha256", sa.String(64), nullable=False),
    )
    op.create_index("ix_agent_replays_base_analysis_id", "agent_replays", ["base_analysis_id"])


def downgrade() -> None:
    op.drop_table("agent_replays")
    op.drop_column("agent_runs", "input_json")
