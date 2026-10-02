"""분석 작업 API와 저장 모델이 공유하는 상태 및 응답 계약."""

from datetime import datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from evidrug_api.analysis_input.models import AnalysisInputResponse, TargetMode


class AnalysisStatus(StrEnum):
    """분석 전체의 영속 상태."""

    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    PARTIAL_FAILURE = "partial_failure"
    FAILED = "failed"


class AnalysisStageName(StrEnum):
    """POC에서 추적하는 계산 및 판단 단계."""

    TARGET_HYPOTHESIS = "target_hypothesis"
    ADMET = "admet"
    DTA = "dta"
    DECISION = "decision"


class AnalysisStageStatus(StrEnum):
    """개별 분석 단계의 영속 상태."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class AnalysisEventType(StrEnum):
    """현재값과 별도로 보존하는 분석 event 유형."""

    CREATED = "analysis_created"
    DISPATCH_FAILED = "queue_dispatch_failed"
    STARTED = "analysis_started"
    COMPLETED = "analysis_completed"
    PARTIAL_FAILURE = "analysis_partial_failure"
    FAILED = "analysis_failed"
    LEASE_EXPIRED = "orchestration_lease_expired"


class AnalysisStageResponse(BaseModel):
    """상태 조회에 포함되는 한 분석 단계."""

    model_config = ConfigDict(from_attributes=True)

    name: AnalysisStageName
    status: AnalysisStageStatus
    position: int
    updated_at: datetime


class AnalysisEventResponse(BaseModel):
    """사용자에게 공개 가능한 append-only 상태 event."""

    model_config = ConfigDict(from_attributes=True)

    event_type: AnalysisEventType
    status: AnalysisStatus
    reason_code: str | None
    created_at: datetime


class TargetTractabilitySummary(BaseModel):
    """공개 응답에 노출할 한 modality 평가 항목."""

    label: str = Field(min_length=1, max_length=120)
    value: bool


class TargetCausalEvidenceSummary(BaseModel):
    """공개 응답에서 추적할 수 있는 개별 인과 근거."""

    evidence_id: str = Field(min_length=1, max_length=500)
    datasource_id: str = Field(min_length=1, max_length=120)
    datatype_id: str = Field(min_length=1, max_length=120)
    axis: Literal[
        "statistical_genetics",
        "clinical_genetics",
        "somatic",
        "functional",
        "clinical_validation",
    ]
    score: float = Field(ge=0, le=1, allow_inf_nan=False)
    disease_id: str = Field(min_length=3, max_length=80)
    disease_name: str = Field(min_length=1, max_length=200)
    disease_scope: Literal["direct", "subtype"]
    direction_on_target: Literal["loss_of_function", "gain_of_function", "unknown"]
    direction_on_trait: Literal["risk", "protective", "unknown"]
    target_role: str | None = None
    confidence: str | None = None
    significant_driver_methods: tuple[str, ...] = ()


class TargetCausalSupportSummary(BaseModel):
    """association 및 tractability와 분리해 노출하는 인과 근거 요약."""

    status: Literal["supported", "conflicting", "unknown"] = "unknown"
    reason_codes: tuple[str, ...] = ("causal_evidence_missing",)
    biological_evidence_count: int = Field(default=0, ge=0)
    clinical_validation_count: int = Field(default=0, ge=0)
    therapeutic_direction: Literal["inhibit", "activate", "stabilize", "unknown"] = "unknown"
    evidence: tuple[TargetCausalEvidenceSummary, ...] = ()


class PharosTargetEvidenceSummary(BaseModel):
    """Pharos에서 조회한 표적 성숙도·지식량 요약."""

    uniprot_accession: str = Field(pattern=r"^[A-Z0-9]+$")
    development_level: Literal["Tclin", "Tchem", "Tbio", "Tdark"]
    target_family: str | None = None
    novelty: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    ligand_count: int = Field(ge=0)
    publication_count: int = Field(ge=0)
    source_version: str
    retrieved_at: datetime


class TargetRecommendationSummary(BaseModel):
    """protein sequence를 제외한 Target shortlist 후보 요약."""

    rank: int = Field(ge=1, le=5)
    ensembl_id: str = Field(pattern=r"^ENSG\d+$")
    approved_symbol: str = Field(min_length=1, max_length=120)
    uniprot_accession: str = Field(pattern=r"^[A-Z0-9]+$")
    association_score: float = Field(ge=0, le=1, allow_inf_nan=False)
    eligibility: Literal["eligible", "exploratory", "ineligible"]
    eligibility_reason_codes: tuple[str, ...]
    tractability_assessments: tuple[TargetTractabilitySummary, ...]
    pharos_evidence: PharosTargetEvidenceSummary | None = None
    causal_support: TargetCausalSupportSummary = Field(default_factory=TargetCausalSupportSummary)
    causal_evidence_ids: tuple[str, ...] = Field(default=(), max_length=20)
    modulation_action: Literal["inhibit", "activate", "stabilize", "unknown"]
    prioritization_rationale: str = Field(min_length=1, max_length=2000)
    causal_rationale: str = "개별 인과 근거가 제공되지 않아 인과성을 판단하지 않았습니다."


class ExcludedTargetSummary(BaseModel):
    """DTA shortlist에서 제외된 후보와 안정적인 사유 코드."""

    ensembl_id: str = Field(pattern=r"^ENSG\d+$")
    approved_symbol: str = Field(min_length=1, max_length=120)
    association_score: float = Field(ge=0, le=1, allow_inf_nan=False)
    reason_code: str = Field(min_length=1, max_length=120)


class TargetPrioritizationSummary(BaseModel):
    """polling API에서 확인할 수 있는 Target 우선순위 결과."""

    modality: Literal["small_molecule"]
    primary: TargetRecommendationSummary
    alternatives: tuple[TargetRecommendationSummary, ...]
    excluded_candidates: tuple[ExcludedTargetSummary, ...]
    source_version: str = Field(min_length=1, max_length=120)


class AnalysisResponse(BaseModel):
    """생성 또는 조회된 분석 작업의 현재 상태."""

    analysis_id: UUID
    status: AnalysisStatus
    input: AnalysisInputResponse
    stages: list[AnalysisStageResponse]
    events: list[AnalysisEventResponse]
    target_prioritization: TargetPrioritizationSummary | None = None
    error_code: str | None
    created_at: datetime
    updated_at: datetime


class RecentAnalysisSummary(BaseModel):
    """목록에서만 공개하는 한 분석의 입력·상태 요약."""

    analysis_id: UUID
    status: AnalysisStatus
    disease_name: str
    target_mode: TargetMode
    target_name: str | None
    canonical_smiles: str
    created_at: datetime


class RecentAnalysesResponse(BaseModel):
    """브라우저 소유권으로 제한된 최신 분석 목록."""

    items: list[RecentAnalysisSummary]
