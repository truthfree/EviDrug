"""Target prioritization의 외부 근거, shortlist와 검증된 DTA 입력 계약."""

import hashlib
from enum import StrEnum
from typing import Annotated, Self

from pydantic import AwareDatetime, Field, model_validator

from evidrug_api.analysis_input.models import TargetMode
from evidrug_api.execution_contracts.common import ContractModel

TargetFunctionDescription = Annotated[str, Field(min_length=1, max_length=800)]


class TherapeuticModality(StrEnum):
    """현재 분석 입력이 전제하는 치료 modality."""

    SMALL_MOLECULE = "small_molecule"


class TargetEligibility(StrEnum):
    """조회된 modality 근거에 따른 후보 자격."""

    ELIGIBLE = "eligible"
    EXPLORATORY = "exploratory"
    INELIGIBLE = "ineligible"


class ModulationAction(StrEnum):
    """질환 기전상 필요하다고 해석한 표적 조절 방향."""

    INHIBIT = "inhibit"
    ACTIVATE = "activate"
    STABILIZE = "stabilize"
    UNKNOWN = "unknown"


class CausalSupportStatus(StrEnum):
    """개별 evidence가 지지하는 생물학적 인과성 상태."""

    SUPPORTED = "supported"
    CONFLICTING = "conflicting"
    UNKNOWN = "unknown"


class CausalEvidenceAxis(StrEnum):
    """서로 혼합해서 해석하지 않는 Open Targets evidence 축."""

    STATISTICAL_GENETICS = "statistical_genetics"
    CLINICAL_GENETICS = "clinical_genetics"
    SOMATIC = "somatic"
    FUNCTIONAL = "functional"
    CLINICAL_VALIDATION = "clinical_validation"


class DiseaseEvidenceScope(StrEnum):
    """요청 질환 자체 근거인지 하위 질환에서 전파된 근거인지 구분한다."""

    DIRECT = "direct"
    SUBTYPE = "subtype"


class TargetEffectDirection(StrEnum):
    """표적 기능 변화 방향의 정규화된 표현."""

    LOSS_OF_FUNCTION = "loss_of_function"
    GAIN_OF_FUNCTION = "gain_of_function"
    UNKNOWN = "unknown"


class TraitEffectDirection(StrEnum):
    """질환 위험에 대한 evidence 방향의 정규화된 표현."""

    RISK = "risk"
    PROTECTIVE = "protective"
    UNKNOWN = "unknown"


class TargetDataTypeScore(ContractModel):
    """Open Targets가 집계한 한 evidence data type의 순위 점수."""

    data_type: str = Field(min_length=1, max_length=120)
    score: float = Field(ge=0, le=1, allow_inf_nan=False)


class TargetTractabilityAssessment(ContractModel):
    """Open Targets가 제공한 한 small-molecule tractability 판정."""

    label: str = Field(min_length=1, max_length=120)
    value: bool


class TargetDevelopmentLevel(StrEnum):
    """Pharos/IDG의 표적 개발 성숙도 분류."""

    TCLIN = "Tclin"
    TCHEM = "Tchem"
    TBIO = "Tbio"
    TDARK = "Tdark"


class PharosTargetEvidence(ContractModel):
    """인과성·tractability와 분리해 보존하는 Pharos 표적 지식 관측."""

    uniprot_accession: str = Field(pattern=r"^[A-Z0-9]+$")
    development_level: TargetDevelopmentLevel
    target_family: str | None = Field(default=None, max_length=200)
    novelty: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    ligand_count: int = Field(ge=0)
    publication_count: int = Field(ge=0)
    source_version: str = Field(min_length=1, max_length=120)
    retrieved_at: AwareDatetime


class TargetCausalEvidence(ContractModel):
    """Open Targets의 한 개별 evidence를 손실 없이 추적하는 최소 계약."""

    evidence_id: str = Field(min_length=1, max_length=500)
    datasource_id: str = Field(min_length=1, max_length=120)
    datatype_id: str = Field(min_length=1, max_length=120)
    axis: CausalEvidenceAxis
    score: float = Field(ge=0, le=1, allow_inf_nan=False)
    disease_id: str = Field(min_length=3, max_length=80)
    disease_name: str = Field(min_length=1, max_length=200)
    disease_scope: DiseaseEvidenceScope
    direction_on_target: TargetEffectDirection = TargetEffectDirection.UNKNOWN
    direction_on_trait: TraitEffectDirection = TraitEffectDirection.UNKNOWN
    target_role: str | None = Field(default=None, max_length=120)
    confidence: str | None = Field(default=None, max_length=500)
    significant_driver_methods: tuple[str, ...] = Field(default=(), max_length=20)


class TargetCausalSupport(ContractModel):
    """association이나 임상 선행 근거와 분리된 인과성 요약."""

    status: CausalSupportStatus = CausalSupportStatus.UNKNOWN
    reason_codes: tuple[str, ...] = Field(default=("causal_evidence_missing",), min_length=1)
    biological_evidence_count: int = Field(default=0, ge=0)
    clinical_validation_count: int = Field(default=0, ge=0)
    therapeutic_direction: ModulationAction = ModulationAction.UNKNOWN
    evidence: tuple[TargetCausalEvidence, ...] = Field(default=(), max_length=50)


class TargetCandidate(ContractModel):
    """Open Targets와 reviewed UniProt 연결이 확인된 표적 후보."""

    ensembl_id: str = Field(pattern=r"^ENSG\d+$")
    approved_symbol: str = Field(min_length=1, max_length=120)
    approved_name: str = Field(min_length=1, max_length=300)
    biotype: str = Field(min_length=1, max_length=120)
    function_descriptions: tuple[TargetFunctionDescription, ...] = Field(default=(), max_length=2)
    uniprot_accession: str = Field(pattern=r"^[A-Z0-9]+$")
    association_score: float = Field(ge=0, le=1, allow_inf_nan=False)
    data_type_scores: tuple[TargetDataTypeScore, ...] = ()
    tractability_assessments: tuple[TargetTractabilityAssessment, ...] = ()
    causal_evidence: tuple[TargetCausalEvidence, ...] = ()
    pharos_evidence: PharosTargetEvidence | None = None


class ExcludedTargetCandidate(ContractModel):
    """후속 계산에 사용할 수 없어 결정적 정책으로 제외한 후보."""

    ensembl_id: str = Field(pattern=r"^ENSG\d+$")
    approved_symbol: str = Field(min_length=1, max_length=120)
    association_score: float = Field(ge=0, le=1, allow_inf_nan=False)
    reason_code: str = Field(min_length=1, max_length=120)


class TargetCandidateBatch(ContractModel):
    """한 번의 Open Targets 조회에서 얻은 순서가 있는 후보 집합."""

    disease_id: str = Field(min_length=3, max_length=80)
    disease_name: str = Field(min_length=1, max_length=200)
    candidates: tuple[TargetCandidate, ...] = Field(min_length=1)
    excluded_candidates: tuple[ExcludedTargetCandidate, ...] = ()
    source_version: str = Field(min_length=1, max_length=120)
    retrieved_at: AwareDatetime
    external_requests: int = Field(ge=1, le=3)


class AssessedTargetCandidate(ContractModel):
    """결정적 modality 정책이 자격과 사유를 부여한 후보."""

    candidate: TargetCandidate
    modality: TherapeuticModality
    eligibility: TargetEligibility
    reason_codes: tuple[str, ...] = Field(min_length=1)
    causal_support: TargetCausalSupport = Field(default_factory=TargetCausalSupport)


class TargetRankingItem(ContractModel):
    """LLM이 allowlist 안에서 제안한 후보 순위와 기전 해석."""

    ensembl_id: str = Field(pattern=r"^ENSG\d+$")
    modulation_action: ModulationAction
    rationale: str = Field(min_length=1, max_length=2000)
    causal_evidence_ids: tuple[str, ...] = Field(default=(), max_length=20)
    causal_rationale: str = Field(
        default="개별 인과 근거가 제공되지 않아 인과성을 판단하지 않았습니다.",
        min_length=1,
        max_length=2000,
    )


class TargetRanking(ContractModel):
    """중복되지 않은 표적 shortlist."""

    ranked_candidates: tuple[TargetRankingItem, ...] = Field(min_length=1, max_length=5)

    @model_validator(mode="after")
    def require_unique_targets(self) -> Self:
        identifiers = [candidate.ensembl_id for candidate in self.ranked_candidates]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("ranked target identifiers must be unique")
        return self


class VerifiedProtein(ContractModel):
    """UniProtKB/Swiss-Prot에서 확인한 human protein sequence."""

    accession: str = Field(pattern=r"^[A-Z0-9]+$")
    entry_name: str = Field(min_length=1, max_length=120)
    gene_symbol: str = Field(min_length=1, max_length=120)
    protein_name: str = Field(min_length=1, max_length=500)
    organism_taxon_id: int
    sequence: str = Field(
        min_length=1,
        max_length=50000,
        pattern=r"^[ACDEFGHIKLMNPQRSTVWY]+$",
    )
    retrieved_at: AwareDatetime

    @model_validator(mode="after")
    def require_human_protein(self) -> Self:
        if self.organism_taxon_id != 9606:
            raise ValueError("target protein must be human")
        return self


class TargetRecommendation(ContractModel):
    """순위·tractability·기전 해석과 검증된 DTA 입력을 묶은 후보."""

    rank: int = Field(ge=1, le=5)
    ensembl_id: str = Field(pattern=r"^ENSG\d+$")
    approved_symbol: str = Field(min_length=1, max_length=120)
    approved_name: str = Field(min_length=1, max_length=300)
    uniprot_accession: str = Field(pattern=r"^[A-Z0-9]+$")
    protein_name: str = Field(min_length=1, max_length=500)
    target_sequence: str = Field(
        min_length=1,
        max_length=50000,
        pattern=r"^[ACDEFGHIKLMNPQRSTVWY]+$",
    )
    target_sequence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    association_score: float = Field(ge=0, le=1, allow_inf_nan=False)
    data_type_scores: tuple[TargetDataTypeScore, ...] = ()
    modality: TherapeuticModality
    eligibility: TargetEligibility
    eligibility_reason_codes: tuple[str, ...] = Field(min_length=1)
    tractability_assessments: tuple[TargetTractabilityAssessment, ...] = ()
    pharos_evidence: PharosTargetEvidence | None = None
    causal_support: TargetCausalSupport = Field(default_factory=TargetCausalSupport)
    causal_evidence_ids: tuple[str, ...] = Field(default=(), max_length=20)
    modulation_action: ModulationAction
    prioritization_rationale: str = Field(min_length=1, max_length=2000)
    causal_rationale: str = Field(
        default="개별 인과 근거가 제공되지 않아 인과성을 판단하지 않았습니다.",
        min_length=1,
        max_length=2000,
    )

    @model_validator(mode="after")
    def validate_sequence_hash(self) -> Self:
        expected = hashlib.sha256(self.target_sequence.encode("ascii")).hexdigest()
        if self.target_sequence_sha256 != expected:
            raise ValueError("target sequence hash does not match sequence")
        if self.eligibility is TargetEligibility.INELIGIBLE:
            raise ValueError("ineligible target cannot be recommended for DTA")
        allowed_evidence_ids = {evidence.evidence_id for evidence in self.causal_support.evidence}
        if not set(self.causal_evidence_ids) <= allowed_evidence_ids:
            raise ValueError("selected causal evidence must belong to the target")
        return self


class TargetHypothesisResult(ContractModel):
    """후속 다중 DTA가 사용할 수 있는 검증된 Target shortlist."""

    target_mode: TargetMode
    disease_id: str = Field(min_length=3, max_length=80)
    disease_name: str = Field(min_length=1, max_length=200)
    modality: TherapeuticModality
    primary: TargetRecommendation
    alternatives: tuple[TargetRecommendation, ...] = Field(default=(), max_length=4)
    excluded_candidates: tuple[ExcludedTargetCandidate, ...] = ()
    source_version: str = Field(min_length=1, max_length=120)
    source_retrieved_at: AwareDatetime

    @model_validator(mode="after")
    def validate_ranking(self) -> Self:
        recommendations = (self.primary, *self.alternatives)
        ranks = [candidate.rank for candidate in recommendations]
        if ranks != list(range(1, len(recommendations) + 1)):
            raise ValueError("target recommendation ranks must be contiguous")
        identifiers = [candidate.ensembl_id for candidate in recommendations]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("target recommendations must be unique")
        excluded = {candidate.ensembl_id for candidate in self.excluded_candidates}
        if excluded.intersection(identifiers):
            raise ValueError("recommended target cannot also be excluded")
        return self
