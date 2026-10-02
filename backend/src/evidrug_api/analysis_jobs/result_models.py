"""내부 실행 envelope와 분리한 전문 결과 공개 DTO."""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel

from evidrug_api.admet.toxicity import ToxicityAxes
from evidrug_api.analysis_jobs.models import AnalysisStageName, AnalysisStatus
from evidrug_api.decision.policy import DecisionMetadata
from evidrug_api.execution_contracts.agent import AgentOutputStatus
from evidrug_api.execution_contracts.common import TokenUsage
from evidrug_api.orchestration.reasoning import Interpretation


class AdmetDomainSummary(BaseModel):
    endpoint_ids: tuple[str, ...]
    interpretation: Interpretation | None
    error_code: str | None
    token_usage: TokenUsage | None
    external_requests: int


class PublicRun[ResultT: BaseModel](BaseModel):
    """최신 run 상태와 공개 projection의 가용성을 별도로 표시한다."""

    run_id: UUID | None = None
    status: AgentOutputStatus | Literal["running"] | None = None
    projection_status: Literal["available", "unavailable", "invalid"] = "unavailable"
    result: ResultT | None = None
    error_code: str | None = None
    warning_codes: tuple[str, ...] = ()
    token_usage: TokenUsage | None = None


class AdmetEndpointSummary(BaseModel):
    endpoint_id: str
    name: str
    category: str
    task_type: str
    value: float
    units: str | None
    drugbank_approved_percentile: float
    species: str | None
    source_url: str | None


class AdmetSummary(BaseModel):
    toxicity_axes: ToxicityAxes | None = None
    source_run_id: UUID
    source_tool_call_id: UUID
    tool_version: str
    reference_population: str
    selected_endpoint_count: int
    catalog_endpoint_count: int
    selection_is_subset: bool
    missing_endpoints: tuple[str, ...]
    endpoints: tuple[AdmetEndpointSummary, ...]
    interpretation: Interpretation | None
    adme: AdmetDomainSummary | None = None
    toxicity: AdmetDomainSummary | None = None
    interpretation_note: str
    limitations: tuple[str, ...]


class CardiacChannelSummary(BaseModel):
    channel: str
    label: str
    class_probability: float


class CardiacRecallSummary(BaseModel):
    source_tool_call_id: UUID
    tool_call_id: UUID
    tool_version: str
    execution_mode: Literal["live", "replay"]
    predictions: tuple[CardiacChannelSummary, ...]
    limitations: tuple[str, ...]


class DtaScoreSummary(BaseModel):
    score_type: str
    value: float
    unit: str


class DtaModelSummary(BaseModel):
    provider: str
    model_id: str
    version: str


class DtaModelRunSummary(BaseModel):
    """한 후보의 모델별 관측과 실패를 합치지 않고 공개한다."""

    tool_id: str
    tool_call_id: UUID | None
    status: Literal["succeeded", "failed", "skipped"]
    model: DtaModelSummary | None
    observations: tuple[DtaScoreSummary, ...]
    error_code: str | None
    duration_ms: int


class DtaAssayEvidenceSummary(BaseModel):
    source: Literal["bindingdb", "chembl", "pubchem"]
    source_record_id: str
    kind: Literal["quantitative", "qualitative"]
    endpoint: Literal["Kd", "Ki", "IC50"] | None
    value: float | None
    unit: Literal["pM", "nM", "uM", "mM", "M"] | None
    qualitative_outcome: str | None
    assay_description: str | None
    doi: str | None
    pmid: str | None


class DtaEvidenceAssessmentSummary(BaseModel):
    region_status: str = "insufficient_model_results"
    experimental_binding_support: bool = False
    pubchem_execution_issue: str | None = None
    criterion: dict[str, object] | None = None
    status: Literal[
        "CONCORDANT",
        "DECISION_RELEVANT_DISAGREEMENT",
        "PREDICTION_EXPERIMENT_CONFLICT",
        "EXPERIMENT_SOURCE_CONFLICT",
        "INSUFFICIENT_EXPERIMENTAL_EVIDENCE",
        "NOT_COMPARABLE",
    ]
    recall_trigger: str | None
    pubchem_requested: bool
    pubchem_executed: bool
    pubchem_resolved: bool
    limitations: tuple[str, ...]


class DtaCandidateSummary(BaseModel):
    ensembl_id: str
    approved_symbol: str
    uniprot_accession: str
    status: Literal["succeeded", "failed", "skipped"]
    tool_call_id: UUID | None
    model: DtaModelSummary | None
    observations: tuple[DtaScoreSummary, ...]
    error_code: str | None
    model_runs: tuple[DtaModelRunSummary, ...] = ()
    experimental_evidence: tuple[DtaAssayEvidenceSummary, ...] = ()
    evidence_assessment: DtaEvidenceAssessmentSummary | None = None
    evidence_errors: tuple[str, ...] = ()


class DtaSummary(BaseModel):
    assay_policy_version: str = "legacy"
    source_target_run_id: UUID
    candidates: tuple[DtaCandidateSummary, ...]
    interpretation: Interpretation | None
    interpretation_note: str


class DecisionNextActionSummary(BaseModel):
    status: Literal["proposed"]
    action: str
    rationale: str
    decision_impact: str


class DecisionSummary(BaseModel):
    headline: str | None = None
    assessment: dict[str, object] | None = None
    key_strengths: tuple[str, ...] | None = None
    key_concerns: tuple[str, ...] | None = None
    server_metadata: DecisionMetadata | None = None
    context_version: str | None = None
    decision_phase: Literal["final", "request_recall"] = "final"
    recall_request: dict[str, object] | None = None
    verdict: Literal["go", "conditional_go", "no_go"]
    rationale: str
    used_evidence_ids: tuple[str, ...]
    conflicts: tuple[str, ...]
    gaps: tuple[str, ...]
    next_actions: tuple[DecisionNextActionSummary, ...] = ()
    source_run_ids: tuple[UUID, ...]
    missing_stages: tuple[str, ...]
    scope: Literal["research_prioritization_only"]


class AgentCallRequestSummary(BaseModel):
    """한 Decision run이 발행한 요청. 현재 정책은 run당 요청 한 건이다."""

    requesting_run_id: UUID
    gap_kind: str
    objective: str
    reason_code: str
    required_endpoints: tuple[str, ...]


class AgentCallSummary(BaseModel):
    """Agent별 호출 순서와 요청·응답 관계를 보존하는 공개 기록."""

    agent_name: AnalysisStageName
    call_number: int
    run_id: UUID
    status: AgentOutputStatus | Literal["running"] | None
    purpose: Literal["initial", "evidence_followup", "reassessment", "unverified"]
    triggering_run_id: UUID | None = None
    responds_to_run_id: UUID | None = None
    request: AgentCallRequestSummary | None = None
    response_run_id: UUID | None = None
    result_kind: Literal[
        "target_reference", "admet_baseline", "cardiac_ion_channel", "dta", "decision"
    ]
    projection_status: Literal["available", "unavailable", "invalid", "not_projected"]
    result: AdmetSummary | CardiacRecallSummary | DtaSummary | DecisionSummary | None = None
    error_code: str | None = None
    warning_codes: tuple[str, ...] = ()
    token_usage: TokenUsage | None = None


class SpecialistResultsResponse(BaseModel):
    analysis_id: UUID
    status: AnalysisStatus
    admet: PublicRun[AdmetSummary]
    dta: PublicRun[DtaSummary]
    decision: PublicRun[DecisionSummary]
    calls: tuple[AgentCallSummary, ...] = ()
