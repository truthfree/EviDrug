from uuid import uuid4

import pytest
from pydantic import BaseModel

from evidrug_api.analysis_jobs.models import AnalysisStageName
from evidrug_api.ctoxpred2.contracts import CTOX_TOOL_VERSION
from evidrug_api.tool_admission.capabilities import (
    CapabilityMatchReason,
    EvidenceGap,
    EvidenceGapKind,
    EvidenceKind,
    capabilities_for_bindings,
    match_capabilities,
    trajectory_candidate_from_match,
    validate_capabilities,
)
from evidrug_api.tool_admission.registry import Invocation, ToolBinding, ToolRegistry
from evidrug_api.trajectory.contracts import TrajectoryActionKind


class Arguments(BaseModel):
    canonical_smiles: str


class Result(BaseModel):
    value: float


async def invoke(_: Invocation, __: BaseModel) -> BaseModel:
    return Result(value=1.0)


def binding(tool_id: str, version: str, agent: AnalysisStageName) -> ToolBinding:
    return ToolBinding(
        tool_id=tool_id,
        version=version,
        description="test binding",
        agents=(agent,),
        arguments_type=Arguments,
        result_type=Result,
        invoke=invoke,
        max_timeout_seconds=10,
    )


def gap(
    kind: EvidenceGapKind,
    *,
    endpoint_id: str | None = None,
    expected_field: str | None = None,
    unit: str | None = None,
) -> EvidenceGap:
    return EvidenceGap(
        gap_id=uuid4(),
        kind=kind,
        objective="missing evidence",
        target_context="candidate molecule",
        required_evidence_kind=EvidenceKind.MODEL_PREDICTION,
        endpoint_id=endpoint_id,
        expected_field=expected_field,
        unit=unit,
        priority=3,
    )


def test_catalog_matches_registry_and_is_stably_ordered() -> None:
    bindings = (
        binding("dta", "model-test", AnalysisStageName.DTA),
        binding("ctoxpred2", CTOX_TOOL_VERSION, AnalysisStageName.ADMET),
        binding("admet_ai", "1.4.0", AnalysisStageName.ADMET),
    )
    capabilities = capabilities_for_bindings(bindings)

    assert [item.tool_id for item in capabilities] == ["admet_ai", "ctoxpred2", "dta"]
    validate_capabilities(capabilities, ToolRegistry(bindings))


def test_ctox_gap_matches_and_projects_to_trajectory_candidate() -> None:
    ctox = binding("ctoxpred2", CTOX_TOOL_VERSION, AnalysisStageName.ADMET)
    capability = capabilities_for_bindings((ctox,))[0]
    evidence_gap = gap(
        EvidenceGapKind.CARDIAC_ION_CHANNEL,
        endpoint_id="herg",
        expected_field="class_probability",
    )

    match = match_capabilities(
        evidence_gap,
        (capability,),
        agent=AnalysisStageName.ADMET,
        available_inputs=frozenset({"canonical_smiles"}),
    )[0]
    candidate = trajectory_candidate_from_match(evidence_gap, capability, match)

    assert match.reason_code is CapabilityMatchReason.CAPABILITY_MATCH
    assert candidate.kind is TrajectoryActionKind.CALL_TOOL
    assert candidate.tool_id == "ctoxpred2"
    assert candidate.expected_fields == ("label", "class_probability")


def test_matcher_is_input_order_independent_and_returns_all_exclusions() -> None:
    bindings = (
        binding("dta", "model-test", AnalysisStageName.DTA),
        binding("admet_ai", "1.4.0", AnalysisStageName.ADMET),
        binding("ctoxpred2", CTOX_TOOL_VERSION, AnalysisStageName.ADMET),
    )
    capabilities = capabilities_for_bindings(bindings)
    evidence_gap = gap(EvidenceGapKind.TARGET_BINDING, endpoint_id="predicted_pkd")

    first = match_capabilities(
        evidence_gap,
        capabilities,
        agent=AnalysisStageName.DTA,
        available_inputs=frozenset({"canonical_smiles"}),
    )
    second = match_capabilities(
        evidence_gap,
        tuple(reversed(capabilities)),
        agent=AnalysisStageName.DTA,
        available_inputs=frozenset({"canonical_smiles"}),
    )

    assert first == second
    assert [item.tool_id for item in first] == ["admet_ai", "ctoxpred2", "dta"]
    assert first[-1].reason_code is CapabilityMatchReason.MISSING_REQUIRED_INPUT
    assert first[-1].missing_inputs == ("target_sequence",)


@pytest.mark.parametrize(
    ("kind", "agent", "endpoint", "field", "unit", "reason"),
    [
        (
            EvidenceGapKind.CLINICAL_CARDIAC_SAFETY,
            AnalysisStageName.ADMET,
            "herg",
            None,
            None,
            CapabilityMatchReason.EXPLICITLY_OUT_OF_SCOPE,
        ),
        (
            EvidenceGapKind.CARDIAC_ION_CHANNEL,
            AnalysisStageName.DTA,
            "herg",
            None,
            None,
            CapabilityMatchReason.AGENT_NOT_ALLOWED,
        ),
        (
            EvidenceGapKind.CARDIAC_ION_CHANNEL,
            AnalysisStageName.ADMET,
            "unknown",
            None,
            None,
            CapabilityMatchReason.UNSUPPORTED_ENDPOINT,
        ),
        (
            EvidenceGapKind.CARDIAC_ION_CHANNEL,
            AnalysisStageName.ADMET,
            "herg",
            "unknown",
            None,
            CapabilityMatchReason.UNSUPPORTED_FIELD,
        ),
    ],
)
def test_ctox_exclusion_reason_codes(
    kind: EvidenceGapKind,
    agent: AnalysisStageName,
    endpoint: str,
    field: str | None,
    unit: str | None,
    reason: CapabilityMatchReason,
) -> None:
    ctox = binding("ctoxpred2", CTOX_TOOL_VERSION, AnalysisStageName.ADMET)
    capability = capabilities_for_bindings((ctox,))[0]

    result = match_capabilities(
        gap(kind, endpoint_id=endpoint, expected_field=field, unit=unit),
        (capability,),
        agent=agent,
        available_inputs=frozenset({"canonical_smiles"}),
    )[0]

    assert result.reason_code is reason
    assert result.matched is False


def test_dta_unit_mismatch_is_explicit() -> None:
    dta = binding("dta", "model-test", AnalysisStageName.DTA)
    capability = capabilities_for_bindings((dta,))[0]
    result = match_capabilities(
        gap(
            EvidenceGapKind.TARGET_BINDING,
            endpoint_id="predicted_pkd",
            unit="probability",
        ),
        (capability,),
        agent=AnalysisStageName.DTA,
        available_inputs=frozenset({"canonical_smiles", "target_sequence"}),
    )[0]

    assert result.reason_code is CapabilityMatchReason.UNIT_MISMATCH


def test_registry_validation_rejects_version_and_agent_drift() -> None:
    registered = binding("admet_ai", "1.4.0", AnalysisStageName.ADMET)
    capability = capabilities_for_bindings((registered,))[0]

    with pytest.raises(ValueError, match="unregistered tool version"):
        validate_capabilities(
            (capability.model_copy(update={"tool_version": "1.5.0"}),),
            ToolRegistry((registered,)),
        )
    with pytest.raises(ValueError, match="agents must exactly match"):
        validate_capabilities(
            (capability.model_copy(update={"agents": (AnalysisStageName.DTA,)}),),
            ToolRegistry((registered,)),
        )
