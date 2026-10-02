"""add structured trajectory assets"""

import sqlalchemy as sa
from alembic import op

revision = "0008_trajectory_assets"
down_revision = "0007_agent_replay"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "trajectory_episodes",
        sa.Column("episode_id", sa.Uuid(), primary_key=True),
        sa.Column(
            "analysis_id",
            sa.Uuid(),
            sa.ForeignKey("analyses.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("profile_json", sa.Text(), nullable=False),
        sa.Column("profile_sha256", sa.String(64), nullable=False),
        sa.Column("error_code", sa.String(120), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "(status = 'collecting' AND finished_at IS NULL AND error_code IS NULL) OR "
            "(status = 'completed' AND finished_at IS NOT NULL AND error_code IS NULL) OR "
            "(status = 'failed' AND finished_at IS NOT NULL AND error_code IS NOT NULL)",
            name="ck_trajectory_episode_outcome",
        ),
    )
    op.create_index("ix_trajectory_episodes_analysis_id", "trajectory_episodes", ["analysis_id"])
    op.create_table(
        "trajectory_steps",
        sa.Column("step_id", sa.Uuid(), primary_key=True),
        sa.Column(
            "episode_id",
            sa.Uuid(),
            sa.ForeignKey("trajectory_episodes.episode_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("sequence_number", sa.Integer(), nullable=False),
        sa.Column("agent_name", sa.String(32), nullable=False),
        sa.Column("step_json", sa.Text(), nullable=False),
        sa.Column("step_sha256", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("episode_id", "sequence_number", name="uq_trajectory_step_sequence"),
    )
    op.create_index("ix_trajectory_steps_episode_id", "trajectory_steps", ["episode_id"])
    op.create_table(
        "trajectory_evaluations",
        sa.Column("evaluation_id", sa.Uuid(), primary_key=True),
        sa.Column(
            "episode_id",
            sa.Uuid(),
            sa.ForeignKey("trajectory_episodes.episode_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("evaluator_id", sa.String(120), nullable=False),
        sa.Column("evaluator_version", sa.String(200), nullable=False),
        sa.Column("verdict", sa.String(16), nullable=False),
        sa.Column("evaluation_json", sa.Text(), nullable=False),
        sa.Column("evaluation_sha256", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "episode_id", "evaluator_id", "evaluator_version", name="uq_trajectory_evaluator"
        ),
    )
    op.create_index(
        "ix_trajectory_evaluations_episode_id", "trajectory_evaluations", ["episode_id"]
    )
    op.create_table(
        "trajectory_preferences",
        sa.Column("preference_id", sa.Uuid(), primary_key=True),
        sa.Column(
            "preferred_episode_id",
            sa.Uuid(),
            sa.ForeignKey("trajectory_episodes.episode_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "rejected_episode_id",
            sa.Uuid(),
            sa.ForeignKey("trajectory_episodes.episode_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("evaluator_id", sa.String(120), nullable=False),
        sa.Column("evaluator_version", sa.String(200), nullable=False),
        sa.Column("preference_json", sa.Text(), nullable=False),
        sa.Column("preference_sha256", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "preferred_episode_id",
            "rejected_episode_id",
            "evaluator_id",
            "evaluator_version",
            name="uq_trajectory_preference_evaluator",
        ),
    )
    op.create_index(
        "ix_trajectory_preferences_preferred_episode_id",
        "trajectory_preferences",
        ["preferred_episode_id"],
    )
    op.create_index(
        "ix_trajectory_preferences_rejected_episode_id",
        "trajectory_preferences",
        ["rejected_episode_id"],
    )


def downgrade() -> None:
    op.drop_table("trajectory_preferences")
    op.drop_table("trajectory_evaluations")
    op.drop_table("trajectory_steps")
    op.drop_table("trajectory_episodes")
