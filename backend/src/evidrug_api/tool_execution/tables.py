"""도구 호출마다 한 행을 보존하는 실행 ledger."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from evidrug_api.database import Base


class ToolExecutionRecord(Base):
    """terminal 상태는 다시 running으로 바꾸지 않는다. 재시도는 새 호출 ID다."""

    __tablename__ = "tool_executions"
    __table_args__ = (
        CheckConstraint(
            "(status = 'running' AND finished_at IS NULL AND error_code IS NULL) OR "
            "(status = 'succeeded' AND finished_at IS NOT NULL AND error_code IS NULL) OR "
            "(status = 'failed' AND finished_at IS NOT NULL AND error_code IS NOT NULL)",
            name="ck_tool_execution_outcome",
        ),
        Index("ix_tool_execution_expiry", "status", "lease_expires_at"),
    )

    tool_call_id: Mapped[UUID] = mapped_column(Uuid(), primary_key=True)
    request_id: Mapped[UUID] = mapped_column(Uuid(), unique=True)
    analysis_id: Mapped[UUID] = mapped_column(
        ForeignKey("analyses.id", ondelete="CASCADE"), index=True
    )
    run_id: Mapped[UUID] = mapped_column(Uuid(), index=True)
    tool_id: Mapped[str] = mapped_column(String(120))
    input_sha256: Mapped[str] = mapped_column(String(64))
    owner_token: Mapped[UUID] = mapped_column(Uuid())
    status: Mapped[str] = mapped_column(String(16))
    error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    lease_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
