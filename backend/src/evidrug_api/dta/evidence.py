"""DTA 예측과 실험 assay의 비교 가능성 및 조건부 PubChem 조회 계약."""

import math
from collections.abc import Sequence
from enum import StrEnum
from typing import Literal, Protocol, Self

from pydantic import Field, model_validator

from evidrug_api.analysis_input.models import PotencyCriterion, PotencyEndpoint
from evidrug_api.dta.contracts import DtaObservation, DtaScoreType
from evidrug_api.execution_contracts.common import ContractModel


class AssaySource(StrEnum):
    BINDINGDB = "bindingdb"
    CHEMBL = "chembl"
    PUBCHEM = "pubchem"


class AssayEvidenceKind(StrEnum):
    QUANTITATIVE = "quantitative"
    QUALITATIVE = "qualitative"


class AssayEvidence(ContractModel):
    """원 출처와 assay 의미를 잃지 않는 compound-target 실험 관측."""

    source: AssaySource
    source_record_id: str = Field(min_length=1, max_length=300)
    kind: AssayEvidenceKind
    endpoint: PotencyEndpoint | None = None
    value: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    unit: str | None = Field(default=None, pattern=r"^(pM|nM|uM|mM|M)$")
    qualitative_outcome: str | None = Field(default=None, min_length=1, max_length=120)
    assay_description: str | None = Field(default=None, max_length=1000)
    doi: str | None = Field(default=None, max_length=200)
    pmid: str | None = Field(default=None, max_length=40)

    @model_validator(mode="after")
    def validate_kind(self) -> Self:
        quantitative = (
            self.endpoint is not None and self.value is not None and self.unit is not None
        )
        if self.kind is AssayEvidenceKind.QUANTITATIVE:
            if not quantitative or self.qualitative_outcome is not None:
                raise ValueError("quantitative assay requires endpoint, value and unit only")
        elif quantitative or self.qualitative_outcome is None:
            raise ValueError("qualitative assay requires an outcome and no quantitative triplet")
        return self


class DtaAssayArguments(ContractModel):
    """실험근거 provider에 보내는 검증된 compound-target 식별자."""

    canonical_smiles: str = Field(min_length=1, max_length=4096, pattern=r"\S")
    uniprot_accession: str = Field(pattern=r"^[A-Z0-9]+$")


class DtaAssayResult(ContractModel):
    """결과 없음과 provider 실패를 구분하는 assay 조회 결과."""

    source: AssaySource
    status: Literal["succeeded", "no_records"]
    evidence: tuple[AssayEvidence, ...] = ()
    # In-process fake/provider contracts can remain at zero; HTTP providers set the actual count.
    external_requests: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_status(self) -> Self:
        if self.status == "succeeded" and not self.evidence:
            raise ValueError("successful assay result requires evidence")
        if self.status == "no_records" and self.evidence:
            raise ValueError("no-records assay result cannot contain evidence")
        if any(item.source is not self.source for item in self.evidence):
            raise ValueError("assay evidence source must match result source")
        return self


class EvidenceRelationshipStatus(StrEnum):
    CONCORDANT = "CONCORDANT"
    DECISION_RELEVANT_DISAGREEMENT = "DECISION_RELEVANT_DISAGREEMENT"
    PREDICTION_EXPERIMENT_CONFLICT = "PREDICTION_EXPERIMENT_CONFLICT"
    EXPERIMENT_SOURCE_CONFLICT = "EXPERIMENT_SOURCE_CONFLICT"
    INSUFFICIENT_EXPERIMENTAL_EVIDENCE = "INSUFFICIENT_EXPERIMENTAL_EVIDENCE"
    NOT_COMPARABLE = "NOT_COMPARABLE"


class PubchemRecallTrigger(StrEnum):
    MODEL_DECISION_REGION_DISAGREEMENT = "model_decision_region_disagreement"
    PREDICTION_EXPERIMENT_CONFLICT = "prediction_experiment_conflict"
    EXPERIMENT_SOURCE_CONFLICT = "experiment_source_conflict"
    IMPORTANT_EVIDENCE_GAP = "important_evidence_gap"


class DtaEvidenceAssessment(ContractModel):
    """Decision에 전달하는 결정적 관계 판정과 PubChem 조회 상태."""

    status: EvidenceRelationshipStatus
    criterion: PotencyCriterion | None = None
    comparable_record_ids: tuple[str, ...] = ()
    recall_trigger: PubchemRecallTrigger | None = None
    pubchem_requested: bool = False
    pubchem_executed: bool = False
    pubchem_resolved: bool = False
    limitations: tuple[str, ...] = ()
    region_status: Literal[
        "same_region_meets_reference",
        "same_region_below_reference",
        "split_across_reference",
        "insufficient_model_results",
    ] = "insufficient_model_results"
    experimental_binding_support: bool = False
    pubchem_execution_issue: (
        Literal[
            "provider_disabled",
            "tool_budget_exhausted",
            "execution_failed",
        ]
        | None
    ) = None


_MOLAR_FACTORS = {"pM": 1e-12, "nM": 1e-9, "uM": 1e-6, "mM": 1e-3, "M": 1.0}


class ModelRunLike(Protocol):
    @property
    def status(self) -> str: ...

    @property
    def observations(self) -> tuple[DtaObservation, ...]: ...


def _criterion_molar(criterion: PotencyCriterion) -> float:
    return criterion.maximum_value * _MOLAR_FACTORS[criterion.unit]


def _experimental_side(evidence: AssayEvidence, criterion: PotencyCriterion) -> bool | None:
    if (
        evidence.kind is not AssayEvidenceKind.QUANTITATIVE
        or evidence.endpoint is not criterion.endpoint
        or evidence.value is None
        or evidence.unit is None
    ):
        return None
    return evidence.value * _MOLAR_FACTORS[evidence.unit] <= _criterion_molar(criterion)


def _prediction_sides(
    model_runs: Sequence[ModelRunLike], criterion: PotencyCriterion
) -> tuple[bool, ...]:
    if criterion.endpoint is not PotencyEndpoint.KD:
        return ()
    threshold_pkd = -math.log10(_criterion_molar(criterion))
    return tuple(
        observation.value >= threshold_pkd
        for run in model_runs
        if run.status == "succeeded"
        for observation in run.observations
        if observation.score_type is DtaScoreType.PREDICTED_PKD
    )


def assess_dta_evidence(
    model_runs: Sequence[ModelRunLike],
    evidence: tuple[AssayEvidence, ...],
    criterion: PotencyCriterion | None,
    *,
    pubchem_executed: bool = False,
    pubchem_execution_issue: Literal[
        "provider_disabled",
        "tool_budget_exhausted",
        "execution_failed",
    ]
    | None = None,
) -> DtaEvidenceAssessment:
    """사전 기준이 있을 때만 판단 영역을 비교하고 recall trigger를 만든다."""

    if criterion is None:
        return DtaEvidenceAssessment(
            status=(
                EvidenceRelationshipStatus.INSUFFICIENT_EXPERIMENTAL_EVIDENCE
                if not evidence
                else EvidenceRelationshipStatus.NOT_COMPARABLE
            ),
            limitations=("사전 지정 potency criterion이 없어 판단 영역을 비교하지 않습니다.",),
        )

    prediction_sides = _prediction_sides(model_runs, criterion)
    comparable = tuple(item for item in evidence if _experimental_side(item, criterion) is not None)
    experimental_sides = tuple(_experimental_side(item, criterion) for item in comparable)
    sources = {
        item.source: {
            side
            for candidate in comparable
            if candidate.source is item.source
            if (side := _experimental_side(candidate, criterion)) is not None
        }
        for item in comparable
    }

    trigger: PubchemRecallTrigger | None = None
    status = EvidenceRelationshipStatus.CONCORDANT
    if len(set(prediction_sides)) > 1:
        status = EvidenceRelationshipStatus.DECISION_RELEVANT_DISAGREEMENT
        trigger = PubchemRecallTrigger.MODEL_DECISION_REGION_DISAGREEMENT
    elif any(len(sides) > 1 for sides in sources.values()) or (
        AssaySource.BINDINGDB in sources
        and AssaySource.CHEMBL in sources
        and sources[AssaySource.BINDINGDB] != sources[AssaySource.CHEMBL]
    ):
        status = EvidenceRelationshipStatus.EXPERIMENT_SOURCE_CONFLICT
    elif (
        prediction_sides and experimental_sides and set(prediction_sides) != set(experimental_sides)
    ):
        status = EvidenceRelationshipStatus.PREDICTION_EXPERIMENT_CONFLICT
    elif not comparable:
        status = EvidenceRelationshipStatus.INSUFFICIENT_EXPERIMENTAL_EVIDENCE

    requested = trigger is not None
    pubchem_comparable = tuple(item for item in comparable if item.source is AssaySource.PUBCHEM)
    resolved = (
        pubchem_executed
        and bool(pubchem_comparable)
        and status
        not in {
            EvidenceRelationshipStatus.EXPERIMENT_SOURCE_CONFLICT,
            EvidenceRelationshipStatus.PREDICTION_EXPERIMENT_CONFLICT,
        }
    )
    return DtaEvidenceAssessment(
        status=status,
        criterion=criterion,
        comparable_record_ids=tuple(item.source_record_id for item in comparable),
        recall_trigger=trigger,
        pubchem_requested=requested,
        pubchem_executed=pubchem_executed,
        pubchem_resolved=resolved,
        limitations=(("비교 가능한 정량 실험근거가 없습니다.",) if not comparable else ()),
        region_status=model_boundary_relationship(model_runs, criterion),
        experimental_binding_support=experimental_binding_support(evidence, criterion),
        pubchem_execution_issue=pubchem_execution_issue,
    )


def model_boundary_relationship(
    model_runs: Sequence[ModelRunLike],
    criterion: PotencyCriterion | None,
) -> Literal[
    "same_region_meets_reference",
    "same_region_below_reference",
    "split_across_reference",
    "insufficient_model_results",
]:
    """모델 원 예측만으로 비교 영역을 고정한다. 실측 조회와 독립적이다."""
    if criterion is None:
        return "insufficient_model_results"
    sides = _prediction_sides(model_runs, criterion)
    if len(sides) < 2:
        return "insufficient_model_results"
    if len(set(sides)) > 1:
        return "split_across_reference"
    return "same_region_meets_reference" if sides[0] else "same_region_below_reference"


def assay_deduplication_key(item: AssayEvidence) -> tuple[object, ...]:
    """원 논문과 정규화 측정값으로 동일 실측을 식별한다."""
    reference = (
        ("doi", item.doi.casefold())
        if item.doi
        else ("pmid", item.pmid)
        if item.pmid
        else ("record", item.source.value, item.source_record_id)
    )
    return (
        *reference,
        item.kind.value,
        item.endpoint.value if item.endpoint else None,
        format(item.value * _MOLAR_FACTORS[item.unit], ".12g")
        if item.value is not None and item.unit
        else None,
        item.qualitative_outcome.casefold() if item.qualitative_outcome else None,
    )


def experimental_binding_support(
    evidence: tuple[AssayEvidence, ...],
    criterion: PotencyCriterion | None,
) -> bool:
    """provider가 accession을 확인한 정량 Kd만 중복 제거 후 모두 충족해야 지지한다."""
    if criterion is None or criterion.endpoint is not PotencyEndpoint.KD:
        return False
    records = {
        assay_deduplication_key(item): item
        for item in evidence
        if item.kind is AssayEvidenceKind.QUANTITATIVE and item.endpoint is PotencyEndpoint.KD
    }
    return bool(records) and all(
        _experimental_side(item, criterion) is True for item in records.values()
    )
