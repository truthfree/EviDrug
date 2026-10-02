"""Decision 입력의 대표 후보·제한·공통 상태와 관측 투영을 검증한다."""

from types import SimpleNamespace
from typing import Any, cast

import pytest
from test_target_hypothesis import agent_input
from test_toxicity_priority import context

from evidrug_api.admet.agent import AdmetAgentResult
from evidrug_api.admet.toxicity import calculate_toxicity_axes
from evidrug_api.analysis_input.models import PotencyCriterion, PotencyEndpoint
from evidrug_api.analysis_jobs.models import AnalysisStageName
from evidrug_api.decision.policy import calculate_metadata, reference_boundary
from evidrug_api.decision.projection import admet_evidence, decision_payload
from evidrug_api.execution_contracts.agent import AgentOutput, AgentOutputStatus
from evidrug_api.execution_contracts.common import ExecutionLimits
from evidrug_api.orchestration.reasoning import Interpretation
from evidrug_api.orchestration.upstream import InvalidUpstream


def evidence() -> dict[str, Any]:
    source = context(0.2, 50.0)
    return {
        "target:A": {"symbol": "CDK4", "causal_status": "supported", "policy_direction": "inhibit"},
        "target:B": {
            "symbol": "BRCA1",
            "causal_status": "unresolved",
            "policy_direction": "unknown",
        },
        "dta:A": {
            "region_status": "same_region_below_reference",
            "model_runs": [{"observations": [7]}],
        },
        "dta:B": {
            "region_status": "same_region_meets_reference",
            "model_runs": [{"observations": [7]}],
        },
        "admet:context": {
            "context": source.model_dump(mode="json"),
            "toxicity_axes": calculate_toxicity_axes(source).model_dump(mode="json"),
        },
    }


def test_lead_uses_supported_gate_count_then_original_order_without_cross_candidate_union() -> None:
    rows = evidence()
    meta = calculate_metadata(rows)
    assert meta.lead_candidate is not None and meta.lead_candidate.symbol == "CDK4"
    assert meta.expected_area_statuses["target"] == "supported"
    assert meta.expected_area_statuses["dta"] == "unresolved"
    assert not meta.go_restrictions  # 미확정 대안은 전체 실행 제한이 아니다.
    rows["target:B"].update(causal_status="supported", policy_direction="activate")
    assert calculate_metadata(rows).lead_candidate.symbol == "BRCA1"  # type: ignore[union-attr]
    rows["dta:A"]["experimental_binding_support"] = True
    assert calculate_metadata(rows).lead_candidate.symbol == "CDK4"  # type: ignore[union-attr]


def test_no_candidate_and_stabilize_do_not_become_supported_targets() -> None:
    assert calculate_metadata({}).lead_candidate is None
    rows = evidence()
    rows["target:A"]["policy_direction"] = "stabilize"
    meta = calculate_metadata(rows)
    assert not any(g.target_supported for g in meta.candidate_gates)
    assert any(r.cause == "no_supported_target" for r in meta.go_restrictions)


def test_partial_interpretation_preserves_observation_states_and_original_error() -> None:
    rows = evidence()
    rows["admet:context"].update(
        execution_status="partial_failure", execution_error_code="reasoning_invalid_output"
    )
    meta = calculate_metadata(rows, ("dta",))
    assert meta.expected_area_statuses["safety"] == "supported"
    assert any(
        r.cause == "stage_partial:admet:reasoning_invalid_output" for r in meta.go_restrictions
    )
    assert any(r.cause == "stage_missing:dta" for r in meta.go_restrictions)


@pytest.mark.parametrize("label", ["positive", "negative"])
def test_recall_adds_positive_channels_but_does_not_clear_priority_axes(label: str) -> None:
    rows = evidence()
    rows["admet:cardiac_ion_channels"] = {"predictions": [{"channel": "herg", "label": label}]}
    meta = calculate_metadata(rows)
    assert meta.confirm_channels == (("herg",) if label == "positive" else ())
    assert meta.safety_gate_baseline == "supported"
    assert meta.safety_gate_final == ("unresolved" if label == "positive" else "supported")
    rows["admet:context"]["toxicity_axes"]["axes"][0]["priority_status"] = "priority_check"
    assert calculate_metadata(rows).safety_gate_final == "unresolved"


def test_recall_failure_preserves_baseline_and_adds_execution_restriction() -> None:
    rows = evidence()
    rows["admet:recall_failure"] = {}
    meta = calculate_metadata(rows)
    assert meta.safety_gate_final == meta.safety_gate_baseline
    assert any(r.cause == "recall_failed" for r in meta.go_restrictions)


@pytest.mark.parametrize("endpoint", [PotencyEndpoint.KD, PotencyEndpoint.IC50])
def test_boundary_is_input_comparison_not_a_new_potency_contract(endpoint: PotencyEndpoint) -> None:
    criterion = PotencyCriterion(endpoint=endpoint, maximum_value=100, unit="nM")
    boundary = reference_boundary(criterion)
    assert boundary is not None and boundary["role"] == "comparison_boundary"
    assert boundary["pkd"] == (7.0 if endpoint is PotencyEndpoint.KD else None)
    assert reference_boundary(None) is None


def test_payload_uses_shared_lead_and_restricts_missing_or_partial_execution() -> None:
    incoming = agent_input()
    rows = evidence()
    payload = decision_payload(
        incoming.case_input,
        1,
        ExecutionLimits(timeout_seconds=30, max_tool_calls=2, max_recall_depth=1),
        rows,
        (),
        False,
    )
    assert payload["target_mode"] == incoming.case_input.target_mode.value
    assert cast(dict[str, object], payload["lead_candidate"])["symbol"] == "CDK4"
    assert "go" in cast(list[str], payload["allowed_verdicts"])
    payload = decision_payload(
        incoming.case_input, 1, incoming.execution_limits, rows, (AnalysisStageName.ADMET,), False
    )
    assert "go" not in cast(list[str], payload["allowed_verdicts"])


def test_admet_projection_excludes_prose_and_rejects_axis_tampering() -> None:
    source = context()
    axes = calculate_toxicity_axes(source)
    result = AdmetAgentResult(
        context=source,
        interpretation=Interpretation(
            summary="해석 문장", used_evidence_ids=("DILI",), limitations=("합성 관측",)
        ),
        toxicity_axes=axes,
    )
    output = cast(
        AgentOutput[AdmetAgentResult],
        SimpleNamespace(
            result=result,
            run_id=source.source_run_id,
            status=AgentOutputStatus.COMPLETED,
            error=None,
        ),
    )
    projected, _ = admet_evidence(output)
    body = cast(dict[str, object], projected["admet:context"])
    assert not {"interpretation", "adme", "toxicity"} & body.keys()
    assert body["toxicity_axes"] == axes.model_dump(mode="json")
    altered = axes.model_copy(
        update={
            "axes": (
                axes.axes[0].model_copy(update={"priority_status": "not_priority"}),
                *axes.axes[1:],
            )
        }
    )
    output.result = result.model_copy(update={"toxicity_axes": altered})
    with pytest.raises(InvalidUpstream, match="decision_toxicity_axes_mismatch"):
        admet_evidence(output)
