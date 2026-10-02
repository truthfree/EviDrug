"""run 예산과 요청별 승인 결과를 재시작 이후에도 보존한다."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from evidrug_api.database import Base


class ToolRunRecord(Base):
    __tablename__ = "tool_admission_runs"
    __table_args__ = (
        CheckConstraint(
            "used_calls >= 0 AND used_calls <= max_calls AND max_calls >= 0",
            name="ck_tool_run_budget",
        ),
    )

    run_id: Mapped[UUID] = mapped_column(Uuid(), primary_key=True)
    analysis_id: Mapped[UUID] = mapped_column(
        ForeignKey("analyses.id", ondelete="CASCADE"), index=True
    )
    policy_json: Mapped[str] = mapped_column(Text())
    policy_sha256: Mapped[str] = mapped_column(String(64))
    max_calls: Mapped[int] = mapped_column(Integer())
    used_calls: Mapped[int] = mapped_column(Integer(), default=0)
    deadline: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ToolAdmissionRecord(Base):
    __tablename__ = "tool_admissions"
    __table_args__ = (
        CheckConstraint(
            "(decision = 'approved' AND reason_code IS NULL) OR "
            "(decision = 'rejected' AND reason_code IS NOT NULL)",
            name="ck_tool_admission_decision",
        ),
    )

    request_id: Mapped[UUID] = mapped_column(Uuid(), primary_key=True)
    tool_call_id: Mapped[UUID] = mapped_column(Uuid(), unique=True)
    run_id: Mapped[UUID] = mapped_column(
        ForeignKey("tool_admission_runs.run_id", ondelete="CASCADE"), index=True
    )
    request_sha256: Mapped[str] = mapped_column(String(64))
    tool_id: Mapped[str] = mapped_column(String(120))
    tool_version: Mapped[str] = mapped_column(String(200))
    decision: Mapped[str] = mapped_column(String(16))
    reason_code: Mapped[str | None] = mapped_column(String(120), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
