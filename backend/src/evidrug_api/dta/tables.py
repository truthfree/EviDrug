"""모델 metadata, 입력 및 실행 관측을 분리한 DTA SQL 테이블."""

from uuid import UUID

from sqlalchemy import CheckConstraint, Float, ForeignKey, Integer, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from evidrug_api.database import Base


class DtaModelRecord(Base):
    """metadata의 canonical JSON hash로 같은 모델 정보를 재사용한다."""

    __tablename__ = "dta_models"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    provider: Mapped[str] = mapped_column(String(200))
    model_id: Mapped[str] = mapped_column(String(200))
    version: Mapped[str] = mapped_column(String(200))
    artifact_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)


class DtaExecutionRecord(Base):
    """완료된 tool call의 불변 결과. running/lease는 작업 실행 계층의 책임이다."""

    __tablename__ = "dta_executions"
    __table_args__ = (
        CheckConstraint("duration_seconds >= 0", name="ck_dta_duration"),
        CheckConstraint(
            "(status = 'succeeded' AND error_code IS NULL) OR "
            "(status = 'unavailable' AND error_code IS NOT NULL "
            "AND error_code IN ('provider_timeout', 'provider_unavailable'))",
            name="ck_dta_outcome",
        ),
    )

    tool_call_id: Mapped[UUID] = mapped_column(Uuid(), primary_key=True)
    request_id: Mapped[UUID] = mapped_column(Uuid(), unique=True)
    analysis_id: Mapped[UUID] = mapped_column(
        ForeignKey("analyses.id", ondelete="CASCADE"), index=True
    )
    run_id: Mapped[UUID] = mapped_column(Uuid(), index=True)
    model_key: Mapped[str] = mapped_column(ForeignKey("dta_models.id", ondelete="RESTRICT"))
    canonical_smiles: Mapped[str] = mapped_column(String(4096))
    target_sequence: Mapped[str] = mapped_column(Text)
    target_sequence_sha256: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16))
    error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    duration_seconds: Mapped[float] = mapped_column(Float)

    model: Mapped[DtaModelRecord] = relationship()
    observations: Mapped[list["DtaObservationRecord"]] = relationship(
        cascade="all, delete-orphan", order_by="DtaObservationRecord.position"
    )


class DtaObservationRecord(Base):
    """행별 원 점수·종류·단위를 보존한다."""

    __tablename__ = "dta_observations"
    __table_args__ = (
        CheckConstraint(
            "(score_type = 'predicted_pkd' AND unit = '-log10(Kd [M])') OR "
            "(score_type = 'pic50_like' AND unit = '-log10(IC50 [M])') OR "
            "(score_type = 'binding_probability' AND unit = 'probability' "
            "AND value >= 0 AND value <= 1)",
            name="ck_dta_score_semantics",
        ),
    )

    tool_call_id: Mapped[UUID] = mapped_column(
        ForeignKey("dta_executions.tool_call_id", ondelete="CASCADE"), primary_key=True
    )
    score_type: Mapped[str] = mapped_column(String(40), primary_key=True)
    position: Mapped[int] = mapped_column(Integer)
    value: Mapped[float] = mapped_column(Float)
    unit: Mapped[str] = mapped_column(String(80))
