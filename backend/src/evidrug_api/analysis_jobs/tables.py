"""분석 작업, 단계와 append-only event의 SQLAlchemy 모델."""

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import DateTime, Enum, Float, ForeignKey, Integer, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from evidrug_api.analysis_input.models import TargetMode
from evidrug_api.analysis_jobs.models import (
    AnalysisEventType,
    AnalysisStageName,
    AnalysisStageStatus,
    AnalysisStatus,
)
from evidrug_api.database import Base


def utc_now() -> datetime:
    """데이터베이스 기본값에 사용할 timezone-aware UTC 시각을 반환한다."""

    return datetime.now(UTC)


class AnalysisRecord(Base):
    """사용자가 제출한 입력과 분석 전체의 현재 상태."""

    __tablename__ = "analyses"
    __table_args__ = (
        UniqueConstraint(
            "session_fingerprint",
            "idempotency_key",
            name="uq_analyses_session_idempotency",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    session_fingerprint: Mapped[str] = mapped_column(String(64))
    visitor_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    idempotency_key: Mapped[str] = mapped_column(String(128))
    status: Mapped[AnalysisStatus] = mapped_column(
        Enum(AnalysisStatus, native_enum=False, length=32),
        default=AnalysisStatus.QUEUED,
    )
    disease_id: Mapped[str] = mapped_column(String(80))
    disease_name: Mapped[str] = mapped_column(String(200))
    target_mode: Mapped[TargetMode] = mapped_column(Enum(TargetMode, native_enum=False, length=16))
    target_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    original_smiles: Mapped[str] = mapped_column(String(4096))
    canonical_smiles: Mapped[str] = mapped_column(String(4096))
    potency_endpoint: Mapped[str | None] = mapped_column(String(16), nullable=True)
    potency_maximum_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    potency_unit: Mapped[str | None] = mapped_column(String(8), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )

    stages: Mapped[list["AnalysisStageRecord"]] = relationship(
        back_populates="analysis",
        cascade="all, delete-orphan",
        order_by="AnalysisStageRecord.position",
    )
    events: Mapped[list["AnalysisEventRecord"]] = relationship(
        back_populates="analysis",
        cascade="all, delete-orphan",
        order_by="AnalysisEventRecord.created_at",
    )


class AnalysisStageRecord(Base):
    """한 분석에서 실행할 단계의 현재 상태."""

    __tablename__ = "analysis_stages"
    __table_args__ = (
        UniqueConstraint("analysis_id", "name", name="uq_analysis_stages_analysis_name"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    analysis_id: Mapped[UUID] = mapped_column(
        ForeignKey("analyses.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[AnalysisStageName] = mapped_column(
        Enum(AnalysisStageName, native_enum=False, length=32)
    )
    status: Mapped[AnalysisStageStatus] = mapped_column(
        Enum(AnalysisStageStatus, native_enum=False, length=16),
        default=AnalysisStageStatus.PENDING,
    )
    position: Mapped[int] = mapped_column(Integer)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )

    analysis: Mapped[AnalysisRecord] = relationship(back_populates="stages")


class AnalysisEventRecord(Base):
    """분석 상태가 만들어지거나 실패한 이유를 추가만 하는 event."""

    __tablename__ = "analysis_events"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    analysis_id: Mapped[UUID] = mapped_column(
        ForeignKey("analyses.id", ondelete="CASCADE"), index=True
    )
    event_type: Mapped[AnalysisEventType] = mapped_column(
        Enum(AnalysisEventType, native_enum=False, length=40)
    )
    status: Mapped[AnalysisStatus] = mapped_column(
        Enum(AnalysisStatus, native_enum=False, length=32)
    )
    reason_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    analysis: Mapped[AnalysisRecord] = relationship(back_populates="events")
