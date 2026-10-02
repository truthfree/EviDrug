"""orchestration 소유권, Agent run과 append-only trace SQL 모델."""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from evidrug_api.analysis_jobs.models import AnalysisStageName
from evidrug_api.database import Base


class AnalysisExecutionRecord(Base):
    """분석마다 하나뿐인 orchestration 실행 소유권."""

    __tablename__ = "analysis_executions"
    __table_args__ = (
        CheckConstraint(
            "(status = 'running' AND finished_at IS NULL AND error_code IS NULL) OR "
            "(status = 'succeeded' AND finished_at IS NOT NULL AND error_code IS NULL) OR "
            "(status = 'failed' AND finished_at IS NOT NULL AND error_code IS NOT NULL)",
            name="ck_analysis_execution_outcome",
        ),
        Index("ix_analysis_execution_expiry", "status", "lease_expires_at"),
    )

    analysis_id: Mapped[UUID] = mapped_column(
        ForeignKey("analyses.id", ondelete="CASCADE"), primary_key=True
    )
    owner_token: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    profile_json: Mapped[str] = mapped_column(Text, nullable=False)
    profile_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    lease_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AgentRunRecord(Base):
    """한 Agent 실행의 현재 결과와 versioned JSON artifact."""

    __tablename__ = "agent_runs"
    __table_args__ = (
        UniqueConstraint("analysis_id", "agent_name", "attempt", name="uq_agent_run_stage_attempt"),
        CheckConstraint(
            "(status = 'running' AND finished_at IS NULL AND error_code IS NULL) OR "
            "(status IN ('completed', 'partial_failure') AND finished_at IS NOT NULL "
            "AND output_json IS NOT NULL) OR "
            "(status IN ('failed', 'skipped') AND finished_at IS NOT NULL "
            "AND error_code IS NOT NULL)",
            name="ck_agent_run_outcome",
        ),
    )

    run_id: Mapped[UUID] = mapped_column(Uuid(), primary_key=True, default=uuid4)
    analysis_id: Mapped[UUID] = mapped_column(
        ForeignKey("analyses.id", ondelete="CASCADE"), index=True
    )
    agent_name: Mapped[AnalysisStageName] = mapped_column(String(32), nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    parent_run_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("agent_runs.run_id", ondelete="SET NULL"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    input_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    input_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    output_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    output_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    execution_metadata_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(120), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AgentReplayRecord(Base):
    """개발용 단독 실행의 불변 원본 참조와 실행 계획."""

    __tablename__ = "agent_replays"

    analysis_id: Mapped[UUID] = mapped_column(
        ForeignKey("analyses.id", ondelete="CASCADE"), primary_key=True
    )
    base_analysis_id: Mapped[UUID] = mapped_column(ForeignKey("analyses.id"), index=True)
    source_run_id: Mapped[UUID] = mapped_column(ForeignKey("agent_runs.run_id"))
    manifest_json: Mapped[str] = mapped_column(Text, nullable=False)
    manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)


class ExecutionTraceRecord(Base):
    """상태 현재값과 별도로 보존하는 orchestration 실행 event."""

    __tablename__ = "execution_trace_events"

    id: Mapped[UUID] = mapped_column(Uuid(), primary_key=True, default=uuid4)
    analysis_id: Mapped[UUID] = mapped_column(
        ForeignKey("analyses.id", ondelete="CASCADE"), index=True
    )
    run_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("agent_runs.run_id", ondelete="CASCADE"), nullable=True, index=True
    )
    event_type: Mapped[str] = mapped_column(String(40), nullable=False)
    stage_name: Mapped[AnalysisStageName | None] = mapped_column(String(32), nullable=True)
    reason_code: Mapped[str | None] = mapped_column(String(120), nullable=True)
    policy_version: Mapped[str] = mapped_column(String(120), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
