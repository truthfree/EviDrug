"""create persistent analysis jobs

Revision ID: 0001_analysis_jobs
Revises:
Create Date: 2026-09-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001_analysis_jobs"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """분석, 단계와 append-only event 테이블을 생성한다."""

    op.create_table(
        "analyses",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("session_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("disease_id", sa.String(length=80), nullable=False),
        sa.Column("disease_name", sa.String(length=200), nullable=False),
        sa.Column("target_mode", sa.String(length=16), nullable=False),
        sa.Column("target_name", sa.String(length=200), nullable=True),
        sa.Column("original_smiles", sa.String(length=4096), nullable=False),
        sa.Column("canonical_smiles", sa.String(length=4096), nullable=False),
        sa.Column("error_code", sa.String(length=80), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "session_fingerprint",
            "idempotency_key",
            name="uq_analyses_session_idempotency",
        ),
    )
    op.create_table(
        "analysis_stages",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("analysis_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["analysis_id"], ["analyses.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("analysis_id", "name", name="uq_analysis_stages_analysis_name"),
    )
    op.create_index(
        op.f("ix_analysis_stages_analysis_id"),
        "analysis_stages",
        ["analysis_id"],
        unique=False,
    )
    op.create_table(
        "analysis_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("analysis_id", sa.Uuid(), nullable=False),
        sa.Column("event_type", sa.String(length=40), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("reason_code", sa.String(length=80), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["analysis_id"], ["analyses.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_analysis_events_analysis_id"),
        "analysis_events",
        ["analysis_id"],
        unique=False,
    )


def downgrade() -> None:
    """분석 작업 테이블을 의존성 역순으로 제거한다."""

    op.drop_index(op.f("ix_analysis_events_analysis_id"), table_name="analysis_events")
    op.drop_table("analysis_events")
    op.drop_index(op.f("ix_analysis_stages_analysis_id"), table_name="analysis_stages")
    op.drop_table("analysis_stages")
    op.drop_table("analyses")
