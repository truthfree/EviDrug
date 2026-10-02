"""CToxPred2 모델 identity와 채널별 관측 SQL 모델."""

from uuid import UUID

from sqlalchemy import CheckConstraint, Float, ForeignKey, Integer, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from evidrug_api.database import Base


class CtoxModelRecord(Base):
    __tablename__ = "ctoxpred2_models"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    identity_json: Mapped[str] = mapped_column(Text)


class CtoxExecutionRecord(Base):
    __tablename__ = "ctoxpred2_executions"

    tool_call_id: Mapped[UUID] = mapped_column(Uuid(), primary_key=True)
    request_id: Mapped[UUID] = mapped_column(Uuid(), unique=True)
    analysis_id: Mapped[UUID] = mapped_column(
        ForeignKey("analyses.id", ondelete="CASCADE"), index=True
    )
    run_id: Mapped[UUID] = mapped_column(Uuid(), index=True)
    model_key: Mapped[str] = mapped_column(ForeignKey("ctoxpred2_models.id", ondelete="RESTRICT"))
    canonical_smiles: Mapped[str] = mapped_column(String(5000))
    limitations_json: Mapped[str] = mapped_column(Text)

    model: Mapped[CtoxModelRecord] = relationship()
    predictions: Mapped[list["CtoxPredictionRecord"]] = relationship(
        cascade="all, delete-orphan", order_by="CtoxPredictionRecord.position"
    )


class CtoxPredictionRecord(Base):
    __tablename__ = "ctoxpred2_predictions"
    __table_args__ = (
        CheckConstraint("label IN ('positive', 'negative')", name="ck_ctox_label"),
        CheckConstraint(
            "class_probability >= 0 AND class_probability <= 1",
            name="ck_ctox_probability",
        ),
    )

    tool_call_id: Mapped[UUID] = mapped_column(
        ForeignKey("ctoxpred2_executions.tool_call_id", ondelete="CASCADE"), primary_key=True
    )
    channel: Mapped[str] = mapped_column(String(20), primary_key=True)
    position: Mapped[int] = mapped_column(Integer)
    label: Mapped[str] = mapped_column(String(16))
    class_probability: Mapped[float] = mapped_column(Float)
