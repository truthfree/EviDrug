"""도구가 해결할 수 있는 evidence gap과 결정적 후보 생성을 정의한다."""

from enum import StrEnum
from typing import TYPE_CHECKING, Literal, Self
from uuid import UUID

from pydantic import Field, model_validator

from evidrug_api.analysis_jobs.models import AnalysisStageName
from evidrug_api.ctoxpred2.contracts import CtoxChannel
from evidrug_api.dta.contracts import SCORE_UNITS, DtaScoreType
from evidrug_api.execution_contracts.common import ContractModel
from evidrug_api.trajectory.contracts import (
    TrajectoryActionCandidate,
    TrajectoryActionKind,
)

if TYPE_CHECKING:
    from evidrug_api.tool_admission.registry import ToolBinding, ToolRegistry


class EvidenceGapKind(StrEnum):
    """정책과 평가에서 의미를 공유하는 제한 vocabulary."""

    ADMET_PROFILE = "admet_profile"
    TOXICITY_RISK = "toxicity_risk"
    CARDIAC_ION_CHANNEL = "cardiac_ion_channel_evidence"
    CLINICAL_CARDIAC_SAFETY = "clinical_cardiac_safety"
    TARGET_BINDING = "target_binding"
    TARGET_MATURITY = "target_maturity"
    DISEASE_CAUSALITY = "disease_causality"


class EvidenceKind(StrEnum):
    MODEL_PREDICTION = "model_prediction"
    EXPERIMENTAL_MEASUREMENT = "experimental_measurement"
    CURATED_KNOWLEDGE = "curated_knowledge"
    STRUCTURAL_ALERT = "structural_alert"


class ResourceClass(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ExecutionLocation(StrEnum):
    LOCAL = "local"
    EXTERNAL = "external"


class CapabilityEndpoint(ContractModel):
    """endpoint별 결과 의미. endpoint/field 이름은 도구가 확장하는 식별자다."""

    endpoint_id: str = Field(min_length=1, max_length=160)
    evidence_kind: EvidenceKind
    expected_fields: tuple[str, ...] = Field(min_length=1)
    unit: str | None = Field(default=None, min_length=1, max_length=120)

    @model_validator(mode="after")
    def unique_fields(self) -> Self:
        if len(self.expected_fields) != len(set(self.expected_fields)):
            raise ValueError("endpoint expected fields must be unique")
        return self


class ToolCapability(ContractModel):
    """실행 권한이 아닌, 고정 tool binding의 조사 능력 metadata."""

    schema_version: Literal["1"] = "1"
    capability_id: str = Field(min_length=1, max_length=160)
    tool_id: str = Field(min_length=1, max_length=120)
    tool_version: str = Field(min_length=1, max_length=200)
    agents: tuple[AnalysisStageName, ...] = Field(min_length=1)
    answers: tuple[EvidenceGapKind, ...] = Field(min_length=1)
    does_not_answer: tuple[EvidenceGapKind, ...] = ()
    required_inputs: tuple[str, ...] = Field(min_length=1)
    endpoints: tuple[CapabilityEndpoint, ...] = Field(min_length=1)
    applicability: tuple[str, ...] = Field(min_length=1)
    interpretation_limits: tuple[str, ...] = Field(min_length=1)
    provider: str = Field(min_length=1, max_length=200)
    model_provenance: str = Field(min_length=1, max_length=500)
    data_provenance: str = Field(min_length=1, max_length=500)
    execution_location: ExecutionLocation
    cost_class: ResourceClass
    latency_class: ResourceClass
    makes_external_request: bool

    @model_validator(mode="after")
    def validate_sets_and_location(self) -> Self:
        unique_groups = (
            (self.agents, "agents"),
            (self.answers, "answers"),
            (self.does_not_answer, "does_not_answer"),
            (self.required_inputs, "required_inputs"),
        )
        for values, name in unique_groups:
            if len(values) != len(set(values)):
                raise ValueError(f"capability {name} must be unique")
        if set(self.answers) & set(self.does_not_answer):
            raise ValueError("answers and does_not_answer must not overlap")
        endpoint_ids = [endpoint.endpoint_id for endpoint in self.endpoints]
        if len(endpoint_ids) != len(set(endpoint_ids)):
            raise ValueError("capability endpoints must be unique")
        if self.execution_location is ExecutionLocation.LOCAL and self.makes_external_request:
            raise ValueError("local capabilities cannot make external requests")
        if any(not value.strip() for value in (*self.applicability, *self.interpretation_limits)):
            raise ValueError("capability descriptions must not contain blank values")
        return self


class EvidenceGap(ContractModel):
    """한 선택 시점에 해소하려는 versioned evidence 요구."""

    schema_version: Literal["1"] = "1"
    gap_id: UUID
    kind: EvidenceGapKind
    objective: str = Field(min_length=1, max_length=1000)
    target_context: str = Field(min_length=1, max_length=500)
    required_evidence_kind: EvidenceKind
    endpoint_id: str | None = Field(default=None, min_length=1, max_length=160)
    expected_field: str | None = Field(default=None, min_length=1, max_length=160)
    unit: str | None = Field(default=None, min_length=1, max_length=120)
    priority: int = Field(ge=1, le=5)


class CapabilityMatchReason(StrEnum):
    CAPABILITY_MATCH = "capability_match"
    MISSING_REQUIRED_INPUT = "missing_required_input"
    UNSUPPORTED_GAP_KIND = "unsupported_gap_kind"
    UNSUPPORTED_EVIDENCE_KIND = "unsupported_evidence_kind"
    UNSUPPORTED_ENDPOINT = "unsupported_endpoint"
    UNSUPPORTED_FIELD = "unsupported_field"
    UNIT_MISMATCH = "unit_mismatch"
    EXPLICITLY_OUT_OF_SCOPE = "explicitly_out_of_scope"
    AGENT_NOT_ALLOWED = "agent_not_allowed"


def validate_capabilities(
    capabilities: tuple[ToolCapability, ...], registry: "ToolRegistry"
) -> None:
    """Capability 설명이 실제 고정 binding보다 권한을 넓히지 않는지 검증한다."""

    capability_ids = [item.capability_id for item in capabilities]
    if len(capability_ids) != len(set(capability_ids)):
        raise ValueError("duplicate capability ID")
    for capability in capabilities:
        binding = registry.get(capability.tool_id, capability.tool_version)
        if binding is None:
            raise ValueError("capability references an unregistered tool version")
        if capability.agents != binding.agents:
            raise ValueError("capability agents must exactly match the tool binding")


def capabilities_for_bindings(
    bindings: tuple["ToolBinding", ...],
) -> tuple[ToolCapability, ...]:
    """현재 세 예측 binding에서 version이 일치하는 capability catalog를 만든다."""

    factories = {
        "admet_ai": _admet_capability,
        "ctoxpred2": _ctoxpred2_capability,
        "dta": _dta_capability,
        "pubchem_bioassay": _assay_capability,
    }
    capabilities = []
    for binding in sorted(bindings, key=lambda item: (item.tool_id, item.version)):
        factory = factories.get(binding.tool_id)
        if factory is not None:
            capabilities.append(factory(binding))
    return tuple(capabilities)


class CapabilityMatch(ContractModel):
    """한 capability에 대한 match 또는 고정된 단일 제외 사유."""

    gap_id: UUID
    capability_id: str
    tool_id: str
    tool_version: str
    endpoint_id: str | None = None
    matched: bool
    reason_code: CapabilityMatchReason
    missing_inputs: tuple[str, ...] = ()
    expected_fields: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_reason(self) -> Self:
        if self.matched != (self.reason_code is CapabilityMatchReason.CAPABILITY_MATCH):
            raise ValueError("matched flag must agree with reason code")
        if self.missing_inputs and (
            self.reason_code is not CapabilityMatchReason.MISSING_REQUIRED_INPUT
        ):
            raise ValueError("missing inputs require missing_required_input reason")
        return self


def match_capabilities(
    gap: EvidenceGap,
    capabilities: tuple[ToolCapability, ...],
    *,
    agent: AnalysisStageName,
    available_inputs: frozenset[str],
) -> tuple[CapabilityMatch, ...]:
    """입력 순서와 무관하게 모든 capability의 match 또는 제외 사유를 반환한다."""

    ordered = sorted(
        capabilities,
        key=lambda item: (item.tool_id, item.tool_version, item.capability_id),
    )
    return tuple(
        _match_capability(gap, capability, agent, available_inputs) for capability in ordered
    )


def _match_capability(
    gap: EvidenceGap,
    capability: ToolCapability,
    agent: AnalysisStageName,
    available_inputs: frozenset[str],
) -> CapabilityMatch:
    reason = _exclusion_reason(gap, capability, agent, available_inputs)
    endpoint = _select_endpoint(gap, capability)
    expected_fields = () if endpoint is None else endpoint.expected_fields
    missing_inputs = tuple(sorted(set(capability.required_inputs) - available_inputs))
    return CapabilityMatch(
        gap_id=gap.gap_id,
        capability_id=capability.capability_id,
        tool_id=capability.tool_id,
        tool_version=capability.tool_version,
        endpoint_id=gap.endpoint_id,
        matched=reason is CapabilityMatchReason.CAPABILITY_MATCH,
        reason_code=reason,
        missing_inputs=(
            missing_inputs if reason is CapabilityMatchReason.MISSING_REQUIRED_INPUT else ()
        ),
        expected_fields=expected_fields,
    )


def _exclusion_reason(
    gap: EvidenceGap,
    capability: ToolCapability,
    agent: AnalysisStageName,
    available_inputs: frozenset[str],
) -> CapabilityMatchReason:
    if agent not in capability.agents:
        return CapabilityMatchReason.AGENT_NOT_ALLOWED
    if gap.kind in capability.does_not_answer:
        return CapabilityMatchReason.EXPLICITLY_OUT_OF_SCOPE
    if gap.kind not in capability.answers:
        return CapabilityMatchReason.UNSUPPORTED_GAP_KIND
    if set(capability.required_inputs) - available_inputs:
        return CapabilityMatchReason.MISSING_REQUIRED_INPUT
    endpoint = _select_endpoint(gap, capability)
    if endpoint is None:
        return CapabilityMatchReason.UNSUPPORTED_ENDPOINT
    if endpoint.evidence_kind is not gap.required_evidence_kind:
        return CapabilityMatchReason.UNSUPPORTED_EVIDENCE_KIND
    if gap.expected_field is not None and gap.expected_field not in endpoint.expected_fields:
        return CapabilityMatchReason.UNSUPPORTED_FIELD
    if gap.unit is not None and endpoint.unit != gap.unit:
        return CapabilityMatchReason.UNIT_MISMATCH
    return CapabilityMatchReason.CAPABILITY_MATCH


def _select_endpoint(gap: EvidenceGap, capability: ToolCapability) -> CapabilityEndpoint | None:
    if gap.endpoint_id is None:
        return capability.endpoints[0] if len(capability.endpoints) == 1 else None
    exact = next(
        (endpoint for endpoint in capability.endpoints if endpoint.endpoint_id == gap.endpoint_id),
        None,
    )
    if exact is not None:
        return exact
    return next(
        (endpoint for endpoint in capability.endpoints if endpoint.endpoint_id == "*"),
        None,
    )


def trajectory_candidate_from_match(
    gap: EvidenceGap,
    capability: ToolCapability,
    match: CapabilityMatch,
) -> TrajectoryActionCandidate:
    """검증된 match만 학습 가능한 도구 행동 후보로 투영한다."""

    if not match.matched or match.capability_id != capability.capability_id:
        raise ValueError("only the matching capability can become an action candidate")
    if match.gap_id != gap.gap_id:
        raise ValueError("match belongs to a different evidence gap")
    return TrajectoryActionCandidate(
        action_id=f"call:{capability.capability_id}:{gap.gap_id}",
        kind=TrajectoryActionKind.CALL_TOOL,
        reason_code=match.reason_code.value,
        objective=gap.objective,
        expected_fields=match.expected_fields,
        tool_id=capability.tool_id,
        target_agent=None,
    )


def _admet_capability(binding: "ToolBinding") -> ToolCapability:
    return ToolCapability(
        capability_id=f"admet_profile:{binding.version}",
        tool_id=binding.tool_id,
        tool_version=binding.version,
        agents=binding.agents,
        answers=(EvidenceGapKind.ADMET_PROFILE, EvidenceGapKind.TOXICITY_RISK),
        does_not_answer=(EvidenceGapKind.CLINICAL_CARDIAC_SAFETY,),
        required_inputs=("canonical_smiles",),
        endpoints=(
            CapabilityEndpoint(
                endpoint_id="*",
                evidence_kind=EvidenceKind.MODEL_PREDICTION,
                expected_fields=("value", "drugbank_approved_percentile"),
            ),
        ),
        applicability=("ADMET-AI가 고정 manifest에서 제공하는 endpoint",),
        interpretation_limits=("예측값과 승인 약물 기준 percentile은 임상 안전성 판정이 아니다.",),
        provider="ADMET-AI",
        model_provenance="binding version과 저장된 model manifest hash로 고정",
        data_provenance="저장된 endpoint metadata와 DrugBank approved reference hash로 고정",
        execution_location=ExecutionLocation.LOCAL,
        cost_class=ResourceClass.MEDIUM,
        latency_class=ResourceClass.MEDIUM,
        makes_external_request=False,
    )


def _ctoxpred2_capability(binding: "ToolBinding") -> ToolCapability:
    return ToolCapability(
        capability_id=f"cardiac_ion_channels:{binding.version}",
        tool_id=binding.tool_id,
        tool_version=binding.version,
        agents=binding.agents,
        answers=(EvidenceGapKind.CARDIAC_ION_CHANNEL,),
        does_not_answer=(EvidenceGapKind.CLINICAL_CARDIAC_SAFETY,),
        required_inputs=("canonical_smiles",),
        endpoints=tuple(
            CapabilityEndpoint(
                endpoint_id=channel.value,
                evidence_kind=EvidenceKind.MODEL_PREDICTION,
                expected_fields=("label", "class_probability"),
            )
            for channel in CtoxChannel
        ),
        applicability=("hERG, Nav1.5, Cav1.2 RF-SSL 분류 panel",),
        interpretation_limits=(
            "이온통로 liability이며 QT 연장, 부정맥, 심근병증의 임상 판정이 아니다.",
            "음성 예측은 심장 안전성을 확정하지 않는다.",
        ),
        provider="CToxPred2",
        model_provenance="고정 upstream commit, RF-SSL artifact hash와 package set",
        data_provenance="upstream CToxPred2 학습 데이터; 개별 원자료 snapshot은 제공되지 않음",
        execution_location=ExecutionLocation.LOCAL,
        cost_class=ResourceClass.MEDIUM,
        latency_class=ResourceClass.HIGH,
        makes_external_request=False,
    )


def _dta_capability(binding: "ToolBinding") -> ToolCapability:
    endpoints = tuple(
        CapabilityEndpoint(
            endpoint_id=score_type.value,
            evidence_kind=EvidenceKind.MODEL_PREDICTION,
            expected_fields=("value", "score_type", "unit"),
            unit=SCORE_UNITS[score_type],
        )
        for score_type in DtaScoreType
    )
    return ToolCapability(
        capability_id=f"target_binding:{binding.version}",
        tool_id=binding.tool_id,
        tool_version=binding.version,
        agents=binding.agents,
        answers=(EvidenceGapKind.TARGET_BINDING,),
        does_not_answer=(EvidenceGapKind.DISEASE_CAUSALITY, EvidenceGapKind.TARGET_MATURITY),
        required_inputs=("canonical_smiles", "target_sequence"),
        endpoints=endpoints,
        applicability=("등록 provider가 지원하는 분자-단백질 서열 쌍",),
        interpretation_limits=(
            "서로 다른 score type과 단위는 직접 평균하거나 동등한 affinity로 취급하지 않는다.",
        ),
        provider="binding-pinned DTA provider",
        model_provenance="provider/model/version/artifact identity를 binding version hash로 고정",
        data_provenance="선택된 provider가 선언한 학습 데이터 provenance",
        execution_location=ExecutionLocation.LOCAL,
        cost_class=ResourceClass.HIGH,
        latency_class=ResourceClass.HIGH,
        makes_external_request=False,
    )


def _assay_capability(binding: "ToolBinding") -> ToolCapability:
    """동일 compound-target의 정량·정성 실험근거 조회 capability."""

    return ToolCapability(
        capability_id=f"target_binding_experiment:{binding.tool_id}:{binding.version}",
        tool_id=binding.tool_id,
        tool_version=binding.version,
        agents=binding.agents,
        answers=(EvidenceGapKind.TARGET_BINDING,),
        does_not_answer=(EvidenceGapKind.DISEASE_CAUSALITY, EvidenceGapKind.TARGET_MATURITY),
        required_inputs=("canonical_smiles", "uniprot_accession"),
        endpoints=(
            CapabilityEndpoint(
                endpoint_id="*",
                evidence_kind=EvidenceKind.EXPERIMENTAL_MEASUREMENT,
                expected_fields=("endpoint", "value", "unit", "source_record_id"),
            ),
        ),
        applicability=("canonical structure와 UniProt target이 정확히 일치하는 공개 assay",),
        interpretation_limits=(
            "서로 다른 endpoint와 정성 결과를 정량 affinity처럼 비교하지 않는다.",
            "공개 DB 수록 여부는 결합 부재나 임상 효능을 뜻하지 않는다.",
        ),
        provider=binding.tool_id,
        model_provenance="예측 모델 없음",
        data_provenance="provider 공개 API의 조회 시점 데이터",
        execution_location=ExecutionLocation.EXTERNAL,
        cost_class=ResourceClass.LOW,
        latency_class=ResourceClass.MEDIUM,
        makes_external_request=True,
    )
