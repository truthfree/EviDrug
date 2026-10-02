"""ADMET stable manifest와 실행별 prediction의 SQLAlchemy 모델."""

from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from evidrug_api.admet.contracts import AdmetTaskType
from evidrug_api.analysis_jobs.tables import utc_now
from evidrug_api.database import Base

if TYPE_CHECKING:
    from evidrug_api.analysis_jobs.tables import AnalysisRecord


class AdmetModelManifestRecord(Base):
    """모델·endpoint metadata·DrugBank 기준 집단의 중복 없는 manifest."""

    __tablename__ = "admet_model_manifests"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    manifest_sha256: Mapped[str] = mapped_column(String(64), unique=True)
    schema_version: Mapped[str] = mapped_column(String(40))
    tool_id: Mapped[str] = mapped_column(String(120))
    tool_version: Mapped[str] = mapped_column(String(200))
    endpoint_metadata_sha256: Mapped[str] = mapped_column(String(64))
    reference_population: Mapped[str] = mapped_column(String(300))
    reference_sha256: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    model_artifacts: Mapped[list["AdmetModelArtifactRecord"]] = relationship(
        back_populates="manifest",
        cascade="all, delete-orphan",
        order_by="AdmetModelArtifactRecord.position",
    )
    endpoints: Mapped[list["AdmetEndpointRecord"]] = relationship(
        back_populates="manifest",
        cascade="all, delete-orphan",
        order_by="AdmetEndpointRecord.position",
    )
    limitations: Mapped[list["AdmetManifestLimitationRecord"]] = relationship(
        back_populates="manifest",
        cascade="all, delete-orphan",
        order_by="AdmetManifestLimitationRecord.position",
    )
    executions: Mapped[list["AdmetToolExecutionRecord"]] = relationship(back_populates="manifest")


class AdmetModelArtifactRecord(Base):
    """manifest를 구성하는 모델 파일 hash."""

    __tablename__ = "admet_model_artifacts"
    __table_args__ = (
        UniqueConstraint("manifest_id", "position", name="uq_admet_artifacts_position"),
    )

    manifest_id: Mapped[UUID] = mapped_column(
        ForeignKey("admet_model_manifests.id", ondelete="CASCADE"),
        primary_key=True,
    )
    relative_path: Mapped[str] = mapped_column(String(500), primary_key=True)
    position: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64))

    manifest: Mapped[AdmetModelManifestRecord] = relationship(back_populates="model_artifacts")


class AdmetEndpointRecord(Base):
    """manifest별 endpoint metadata column."""

    __tablename__ = "admet_endpoints"
    __table_args__ = (
        UniqueConstraint("manifest_id", "endpoint_id", name="uq_admet_endpoints_manifest_key"),
        UniqueConstraint("manifest_id", "position", name="uq_admet_endpoints_position"),
        UniqueConstraint("id", "manifest_id", name="uq_admet_endpoints_id_manifest"),
        CheckConstraint(
            "minimum IS NULL OR maximum IS NULL OR minimum <= maximum",
            name="ck_admet_endpoints_range",
        ),
        CheckConstraint(
            "auprc IS NULL OR (auprc >= 0 AND auprc <= 1)",
            name="ck_admet_endpoints_auprc",
        ),
        CheckConstraint(
            "auroc IS NULL OR (auroc >= 0 AND auroc <= 1)",
            name="ck_admet_endpoints_auroc",
        ),
        CheckConstraint("mae IS NULL OR mae >= 0", name="ck_admet_endpoints_mae"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    manifest_id: Mapped[UUID] = mapped_column(
        ForeignKey("admet_model_manifests.id", ondelete="CASCADE"),
        index=True,
    )
    endpoint_id: Mapped[str] = mapped_column(String(160))
    position: Mapped[int] = mapped_column(Integer)
    category: Mapped[str] = mapped_column(String(120))
    name: Mapped[str] = mapped_column(String(300))
    task_type: Mapped[AdmetTaskType] = mapped_column(
        Enum(AdmetTaskType, native_enum=False, length=32)
    )
    dataset_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    units: Mapped[str | None] = mapped_column(String(120), nullable=True)
    minimum: Mapped[float | None] = mapped_column(Float, nullable=True)
    maximum: Mapped[float | None] = mapped_column(Float, nullable=True)
    minimum_unbounded: Mapped[bool] = mapped_column(Boolean, default=False)
    maximum_unbounded: Mapped[bool] = mapped_column(Boolean, default=False)
    species: Mapped[str | None] = mapped_column(String(200), nullable=True)
    tdc_rank: Mapped[int | None] = mapped_column(Integer, nullable=True)
    auprc: Mapped[float | None] = mapped_column(Float, nullable=True)
    auroc: Mapped[float | None] = mapped_column(Float, nullable=True)
    r_squared: Mapped[float | None] = mapped_column(Float, nullable=True)
    mae: Mapped[float | None] = mapped_column(Float, nullable=True)
    source_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)

    manifest: Mapped[AdmetModelManifestRecord] = relationship(back_populates="endpoints")


class AdmetManifestLimitationRecord(Base):
    """순서를 보존하는 manifest 해석 제한사항."""

    __tablename__ = "admet_manifest_limitations"

    manifest_id: Mapped[UUID] = mapped_column(
        ForeignKey("admet_model_manifests.id", ondelete="CASCADE"),
        primary_key=True,
    )
    position: Mapped[int] = mapped_column(Integer, primary_key=True)
    limitation: Mapped[str] = mapped_column(Text)

    manifest: Mapped[AdmetModelManifestRecord] = relationship(back_populates="limitations")


class AdmetToolExecutionRecord(Base):
    """분석 Agent run에서 승인되어 실행된 ADMET tool call."""

    __tablename__ = "admet_tool_executions"
    __table_args__ = (
        UniqueConstraint("request_id", name="uq_admet_executions_request"),
        UniqueConstraint(
            "tool_call_id",
            "manifest_id",
            name="uq_admet_executions_call_manifest",
        ),
    )

    tool_call_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    request_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True))
    analysis_id: Mapped[UUID] = mapped_column(
        ForeignKey("analyses.id", ondelete="CASCADE"),
        index=True,
    )
    run_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), index=True)
    manifest_id: Mapped[UUID] = mapped_column(
        ForeignKey("admet_model_manifests.id", ondelete="RESTRICT"),
        index=True,
    )
    canonical_smiles: Mapped[str] = mapped_column(String(4096))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    analysis: Mapped["AnalysisRecord"] = relationship()
    manifest: Mapped[AdmetModelManifestRecord] = relationship(back_populates="executions")
    predictions: Mapped[list["AdmetPredictionRecord"]] = relationship(
        back_populates="execution",
        cascade="all, delete-orphan",
        order_by="AdmetPredictionRecord.position",
        overlaps="manifest",
    )


class AdmetPredictionRecord(Base):
    """실행별 값만 보관하고 endpoint metadata는 foreign key로 참조하는 행."""

    __tablename__ = "admet_predictions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tool_call_id", "manifest_id"],
            ["admet_tool_executions.tool_call_id", "admet_tool_executions.manifest_id"],
            ondelete="CASCADE",
            name="fk_admet_predictions_execution_manifest",
        ),
        ForeignKeyConstraint(
            ["endpoint_record_id", "manifest_id"],
            ["admet_endpoints.id", "admet_endpoints.manifest_id"],
            ondelete="RESTRICT",
            name="fk_admet_predictions_endpoint_manifest",
        ),
        UniqueConstraint("tool_call_id", "position", name="uq_admet_predictions_position"),
        CheckConstraint(
            "drugbank_approved_percentile >= 0 AND drugbank_approved_percentile <= 100",
            name="ck_admet_predictions_percentile",
        ),
    )

    tool_call_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    endpoint_record_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    manifest_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True))
    position: Mapped[int] = mapped_column(Integer)
    value: Mapped[float] = mapped_column(Float)
    drugbank_approved_percentile: Mapped[float] = mapped_column(Float)

    execution: Mapped[AdmetToolExecutionRecord] = relationship(
        back_populates="predictions",
        foreign_keys=[tool_call_id, manifest_id],
        overlaps="manifest",
    )
    endpoint: Mapped[AdmetEndpointRecord] = relationship(
        foreign_keys=[endpoint_record_id, manifest_id],
        overlaps="execution,predictions",
    )
