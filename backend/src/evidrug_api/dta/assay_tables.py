"""외부 assay 조회 실행과 정규화된 실험 근거 SQL 테이블."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from evidrug_api.database import Base


class DtaAssayQueryRecord(Base):
    """외부 provider 호출의 입력 identity와 최종 outcome."""

    __tablename__ = "dta_assay_queries"
    __table_args__ = (
        CheckConstraint(
            "(status IN ('succeeded', 'no_records') AND error_code IS NULL) OR "
            "(status = 'failed' AND error_code IS NOT NULL)",
            name="ck_dta_assay_query_outcome",
        ),
        CheckConstraint("external_requests >= 0", name="ck_dta_assay_external_requests"),
        CheckConstraint("duration_ms >= 0", name="ck_dta_assay_duration"),
        CheckConstraint(
            "provider IN ('bindingdb', 'chembl', 'pubchem')",
            name="ck_dta_assay_provider",
        ),
    )

    tool_call_id: Mapped[UUID] = mapped_column(Uuid(), primary_key=True)
    request_id: Mapped[UUID] = mapped_column(Uuid(), unique=True)
    analysis_id: Mapped[UUID] = mapped_column(
        ForeignKey("analyses.id", ondelete="CASCADE"), index=True
    )
    run_id: Mapped[UUID] = mapped_column(
        ForeignKey("agent_runs.run_id", ondelete="CASCADE"), index=True
    )
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    provider_version: Mapped[str] = mapped_column(String(200), nullable=False)
    canonical_smiles_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    uniprot_accession: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    external_requests: Mapped[int] = mapped_column(Integer, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False)

    evidence: Mapped[list["DtaAssayEvidenceRecord"]] = relationship(
        cascade="all, delete-orphan", order_by="DtaAssayEvidenceRecord.position"
    )


class DtaAssayEvidenceRecord(Base):
    """provider 응답에서 공개 계약으로 정규화된 assay 한 건."""

    __tablename__ = "dta_assay_evidence"
    __table_args__ = (
        UniqueConstraint(
            "query_tool_call_id",
            "source_record_id",
            name="uq_dta_assay_evidence_source_record",
        ),
        CheckConstraint(
            "(kind = 'quantitative' AND endpoint IS NOT NULL AND value IS NOT NULL "
            "AND unit IS NOT NULL AND qualitative_outcome IS NULL) OR "
            "(kind = 'qualitative' AND endpoint IS NULL AND value IS NULL "
            "AND unit IS NULL AND qualitative_outcome IS NOT NULL)",
            name="ck_dta_assay_evidence_kind",
        ),
        CheckConstraint("value IS NULL OR value > 0", name="ck_dta_assay_evidence_value"),
        CheckConstraint("position >= 0", name="ck_dta_assay_evidence_position"),
        CheckConstraint(
            "endpoint IS NULL OR endpoint IN ('Kd', 'Ki', 'IC50')",
            name="ck_dta_assay_evidence_endpoint",
        ),
        CheckConstraint(
            "unit IS NULL OR unit IN ('pM', 'nM', 'uM', 'mM', 'M')",
            name="ck_dta_assay_evidence_unit",
        ),
    )

    query_tool_call_id: Mapped[UUID] = mapped_column(
        ForeignKey("dta_assay_queries.tool_call_id", ondelete="CASCADE"), primary_key=True
    )
    position: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_record_id: Mapped[str] = mapped_column(String(300), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    endpoint: Mapped[str | None] = mapped_column(String(16), nullable=True)
    value: Mapped[float | None] = mapped_column(Float, nullable=True)
    unit: Mapped[str | None] = mapped_column(String(8), nullable=True)
    qualitative_outcome: Mapped[str | None] = mapped_column(String(120), nullable=True)
    assay_description: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    doi: Mapped[str | None] = mapped_column(String(200), nullable=True)
    pmid: Mapped[str | None] = mapped_column(String(40), nullable=True)
