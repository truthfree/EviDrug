"""v5.7 판정·숫자·표시 계약의 결정적 회귀 테스트."""

import hashlib
import json
from typing import Any
from uuid import uuid4

import pytest
from decision_v57_support import case_fixture
from pydantic import ValidationError

from evidrug_api.admet.agent import AdmetAgentResult
from evidrug_api.admet.context import AdmetContext
from evidrug_api.admet.toxicity import calculate_toxicity_axes
from evidrug_api.analysis_jobs.results import project_decision
from evidrug_api.decision.agent import (
    DECISION_MAX_INPUT_BYTES,
    DECISION_MAX_OUTPUT_TOKENS,
    INSTRUCTIONS,
    DecisionAssessment,
    DecisionAssessmentGeneration,
    DecisionResult,
    InvalidDecisionOutput,
    LegacyDecisionAssessment,
    decision_input_size,
    normalize_reader_numbers,
    remove_inline_evidence_markers,
    validate_assessment,
)
from evidrug_api.decision.policy import calculate_metadata
from evidrug_api.decision.projection import admet_evidence
from evidrug_api.decision.validation import PolicyViolation, allowed_values, validate_policy
from evidrug_api.dta.agent import DtaModelRun
from evidrug_api.dta.contracts import SCORE_UNITS, DtaModel, DtaObservation, DtaScoreType


def test_decision_input_limit_is_64_kib_including_fixed_margin() -> None:
    assert DECISION_MAX_OUTPUT_TOKENS == 8192
    assert decision_input_size("x" * (DECISION_MAX_INPUT_BYTES - 512), "") == (
        DECISION_MAX_INPUT_BYTES
    )
    assert decision_input_size("x" * (DECISION_MAX_INPUT_BYTES - 511), "") > (
        DECISION_MAX_INPUT_BYTES
    )


def test_correction_generation_schema_requires_every_object_property() -> None:
    schema = DecisionAssessmentGeneration.model_json_schema()
    objects = [schema, *schema["$defs"].values()]
    for definition in objects:
        if definition.get("type") == "object":
            assert set(definition["required"]) == set(definition["properties"])

    def contains_default(value: object) -> bool:
        if isinstance(value, dict):
            return "default" in value or any(contains_default(item) for item in value.values())
        if isinstance(value, list):
            return any(contains_default(item) for item in value)
        return False

    assert not contains_default(schema)


def validate(source: dict[str, Any], output: dict[str, Any]) -> DecisionAssessment:
    return validate_assessment(
        json.dumps(output),
        "completed",
        source["evidence"],
        False,
        metadata=calculate_metadata(source["evidence"]),
        boundary=source["dta_reference_boundary"],
    )


@pytest.mark.parametrize("case,symbol", [(1, "CDK4"), (2, "BRCA1")])
def test_attached_results_pass_actual_projection_contract(case: int, symbol: str) -> None:
    source, output = case_fixture(case)
    meta = calculate_metadata(source["evidence"])
    assert meta.lead_candidate is not None
    assert meta.lead_candidate.symbol == symbol
    assert not meta.go_restrictions
    assert validate(source, output).verdict == "conditional_go"
    assert not validate_policy(output, source["evidence"], meta, source["dta_reference_boundary"])
    result = DecisionResult(
        assessment=validate(source, output),
        source_run_ids=(uuid4(),),
        missing_stages=(),
        server_metadata=meta,
    )
    public = project_decision(DecisionResult.model_validate_json(result.model_dump_json()))
    assert public is not None
    assert public.headline == output["headline"] and public.server_metadata == meta


def test_prompt_is_exact_original_after_lf_normalization() -> None:
    assert hashlib.sha256(INSTRUCTIONS.encode()).hexdigest()[:12] == "d5bd73424fca"


@pytest.mark.parametrize("score_type", list(DtaScoreType))
def test_real_dta_json_contract_only_admits_predicted_pkd(score_type: DtaScoreType) -> None:
    """운영 provider의 typed JSON이 Decision 검증에 도달하는 계약 경계를 검사한다."""
    value = 0.81 if score_type is DtaScoreType.BINDING_PROBABILITY else 4.811026573181152
    run = DtaModelRun(
        tool_id="dta",
        tool_call_id=uuid4(),
        status="succeeded",
        model=DtaModel(provider="deeppurpose", model_id="CNN_CNN_BindingDB", version="0.1.5"),
        observations=(
            DtaObservation(score_type=score_type, value=value, unit=SCORE_UNITS[score_type]),
        ),
    )
    serialized = json.loads(run.model_dump_json())
    assert serialized["observations"][0]["score_type"] == score_type.value
    values = allowed_values(
        {"dta:ENSG00000135446": {"approved_symbol": "CDK4", "model_runs": [serialized]}}, None
    )["dta"]
    if score_type is DtaScoreType.PREDICTED_PKD:
        assert any(v.value == value and v.unit is None and v.metric == "cnn" for v in values)
        assert any(v.unit == "nM" and v.value == 10 ** (-value) / 1e-9 for v in values)
    else:
        assert values == []


def test_failed_local_analysis_pkd_values_pass_strict_decision_validation() -> None:
    """0fa29fe8 실패 사례의 원 관측을 이용한다. 실제 모델을 다시 호출하지 않는다."""
    source, output = case_fixture()
    dta = source["evidence"]["dta:ENSG00000135446"]
    for run, value in zip(dta["model_runs"], (4.811026573181152, 6.590145587921143), strict=True):
        observation = DtaObservation(
            score_type=DtaScoreType.PREDICTED_PKD,
            value=value,
            unit=SCORE_UNITS[DtaScoreType.PREDICTED_PKD],
        )
        run["observations"] = [json.loads(observation.model_dump_json())]
    output["assessment"]["dta"]["key_values"] = [
        {"label": "CNN-CNN 예측 pKd", "value": 4.81, "unit": None},
        {"label": "MPNN-CNN 예측 pKd", "value": 6.59, "unit": None},
    ]
    assert validate(source, output).verdict == "conditional_go"


@pytest.mark.parametrize(
    "value,percentile,expected",
    [(0.5, 90, "priority_check"), (0.4999, 90, "not_priority"), (0.5, 89.99, "not_priority")],
)
def test_toxicity_priorities_use_unrounded_values(
    value: float, percentile: float, expected: str
) -> None:
    source, _ = case_fixture()
    context = AdmetContext.model_validate(source["evidence"]["admet:context"]["context"])
    rows = tuple(
        row._replace(value=value, drugbank_approved_percentile=percentile)
        if row.endpoint_id == "hERG"
        else row
        for row in context.rows
    )
    context = context.model_copy(update={"rows": rows})
    axes = calculate_toxicity_axes(context)
    assert axes.axes[1].priority_status == expected
    assert len(axes.axes) == 3
    assert calculate_toxicity_axes(context, ("hERG",)).axes[1].priority_status == "missing"
    assert all(a.priority_status == "missing" for a in calculate_toxicity_axes(None).axes)


@pytest.mark.parametrize(
    "field",
    [
        "decision_phase",
        "verdict",
        "headline",
        "assessment",
        "key_strengths",
        "key_concerns",
        "rationale",
        "used_evidence_ids",
        "conflicts",
        "gaps",
        "next_actions",
        "recall_request",
    ],
)
def test_each_new_generation_field_is_required(field: str) -> None:
    source, output = case_fixture()
    output.pop(field)
    with pytest.raises(InvalidDecisionOutput, match="decision_schema_invalid"):
        validate(source, output)


@pytest.mark.parametrize("area", ["target", "dta", "safety", "adme"])
def test_wrong_area_status_is_rejected(area: str) -> None:
    source, output = case_fixture()
    output["assessment"][area]["status"] = "unavailable"
    with pytest.raises(InvalidDecisionOutput, match="decision_area_status_mismatch"):
        validate(source, output)


@pytest.mark.parametrize("area", ["target", "dta", "safety", "adme"])
def test_fabricated_numbers_are_rejected(area: str) -> None:
    source, output = case_fixture()
    output["assessment"][area]["key_values"] = [{"label": "값", "value": 123456789, "unit": None}]
    with pytest.raises(InvalidDecisionOutput, match="decision_key_value_out_of_range"):
        validate(source, output)


@pytest.mark.parametrize(
    "label,value,area",
    [("hERG 지지값", 0.98, "safety"), ("CNN-CNN pKd", 6.59, "dta"), ("MPNN-CNN pKd", 4.81, "dta")],
)
def test_label_cannot_borrow_another_metric(label: str, value: float, area: str) -> None:
    source, output = case_fixture()
    output["assessment"][area]["key_values"] = [{"label": label, "value": value, "unit": None}]
    with pytest.raises(InvalidDecisionOutput, match="decision_key_value_label_mismatch"):
        validate(source, output)


@pytest.mark.parametrize("value", [257, 257.0, 257.04, 260])
def test_model_kd_conversion_allows_documented_display_rounding(value: float) -> None:
    source, output = case_fixture()
    import math

    dta = source["evidence"]["dta:ENSG00000135446"]
    dta["model_runs"][0]["observations"][0]["value"] = -math.log10(257.04e-9)
    output["assessment"]["dta"]["key_values"] = [
        {"label": "CNN-CNN Kd", "value": value, "unit": "nM"}
    ]
    validate(source, output)


def test_prediction_only_no_go_and_unresolved_go_are_rejected() -> None:
    source, output = case_fixture()
    output["verdict"] = "no_go"
    with pytest.raises(InvalidDecisionOutput, match="decision_no_go_experimental_evidence_missing"):
        validate(source, output)
    output["verdict"] = "go"
    with pytest.raises(InvalidDecisionOutput, match="decision_go_gate_unresolved"):
        validate(source, output)


def test_go_requires_same_candidate_support_and_not_priority_axes() -> None:
    source, output = case_fixture(2)
    evidence = source["evidence"]
    axes = evidence["admet:context"]["toxicity_axes"]["axes"]
    for axis in axes:
        axis["priority_status"] = "not_priority"
    # 표적은 BRCA1, 결합은 CHEK2만 지지한다.
    dta_keys = [k for k in evidence if k.startswith("dta:") and k != "dta:interpretation"]
    evidence[dta_keys[0]]["region_status"] = "same_region_meets_reference"
    output["verdict"] = "go"
    with pytest.raises(InvalidDecisionOutput, match="decision_go_gate_unresolved"):
        validate(source, output)
    lead_id = next(
        k.split(":")[1]
        for k, v in evidence.items()
        if k.startswith("target:") and v["symbol"] == "BRCA1"
    )
    evidence["dta:" + lead_id]["region_status"] = "same_region_meets_reference"
    output["assessment"]["dta"]["status"] = "supported"
    output["assessment"]["safety"]["status"] = "supported"
    validate(source, output)


def test_recall_adds_channels_without_changing_axis_state() -> None:
    source, _ = case_fixture()
    evidence = source["evidence"]
    axes = evidence["admet:context"]["toxicity_axes"]["axes"]
    for axis in axes:
        axis["priority_status"] = "not_priority"
    evidence["admet:cardiac_ion_channels"] = {
        "predictions": [{"channel": "nav1_5", "label": "positive", "class_probability": 0.91}]
    }
    meta = calculate_metadata(evidence)
    assert meta.safety_gate_baseline == "supported" and meta.safety_gate_final == "unresolved"
    assert meta.confirm_channels == ("nav1_5",)
    evidence.pop("admet:cardiac_ion_channels")
    evidence["admet:recall_failure"] = {}
    meta = calculate_metadata(evidence)
    assert meta.safety_gate_final == meta.safety_gate_baseline
    assert any(r.cause == "recall_failed" for r in meta.go_restrictions)


def test_internal_terms_and_missing_lead_name_warn_without_rejection() -> None:
    source, output = case_fixture(2)
    output["headline"] = "priority_check와 algorithm을 검토합니다."
    output["assessment"]["target"]["summary"] = "인과 근거를 검토했습니다."
    warnings = validate_policy(
        output,
        source["evidence"],
        calculate_metadata(source["evidence"]),
        source["dta_reference_boundary"],
    )
    assert "decision_internal_terms_in_prose" in warnings
    assert "decision_lead_candidate_summary_missing" in warnings
    output["headline"] = "algorithm을 검토합니다."
    assert "decision_internal_terms_in_prose" not in validate_policy(
        output,
        source["evidence"],
        calculate_metadata(source["evidence"]),
        source["dta_reference_boundary"],
    )


def test_legacy_read_contract_is_separate_and_new_prose_is_cleaned() -> None:
    source, output = case_fixture()
    legacy = {
        k: v
        for k, v in output.items()
        if k not in ("headline", "assessment", "key_strengths", "key_concerns")
    }
    result = DecisionResult(
        assessment=LegacyDecisionAssessment.model_validate(legacy),
        source_run_ids=(),
        missing_stages=(),
    )
    public = project_decision(DecisionResult.model_validate_json(result.model_dump_json()))
    assert public is not None and public.headline is None
    with pytest.raises(ValidationError):
        DecisionAssessment.model_validate(legacy)
    output["headline"] = "값 4.811026 [target:ENSG00000135446]입니다."
    output["assessment"]["target"]["summary"] += " [target:ENSG00000135446]"
    assessment = DecisionAssessment.model_validate(output)
    cleaned, removed = remove_inline_evidence_markers(assessment)
    cleaned, changed = normalize_reader_numbers(cleaned)
    assert removed == 2 and changed == 1 and "4.81" in cleaned.headline
    assert "[target:" not in cleaned.assessment.target.summary


@pytest.mark.parametrize("case", [1, 2])
def test_specified_and_discover_go_when_all_lead_gates_supported(case: int) -> None:
    source, output = case_fixture(case)
    evidence = source["evidence"]
    lead = calculate_metadata(evidence).lead_candidate
    assert lead is not None
    evidence["dta:" + lead.ensembl_id]["region_status"] = "same_region_meets_reference"
    for axis in evidence["admet:context"]["toxicity_axes"]["axes"]:
        axis["priority_status"] = "not_priority"
    output["verdict"] = "go"
    output["gaps"] = []
    output["assessment"]["dta"]["status"] = "supported"
    output["assessment"]["safety"]["status"] = "supported"
    validate(source, output)


def test_quantitative_assay_allows_no_go_without_using_predictions_as_refutation() -> None:
    source, output = case_fixture()
    source["evidence"]["dta:ENSG00000135446"]["experimental_evidence_summary"]["key_records"] = [
        {"kind": "quantitative", "endpoint": "Kd", "value": 500, "unit": "nM"}
    ]
    output["verdict"] = "no_go"
    validate(source, output)
    # 비교 기준이 Kd가 아닌 경우 이 기록은 현재 가설의 비교 가능한 근거가 아니다.
    source["dta_reference_boundary"]["endpoint"] = "IC50"
    with pytest.raises(InvalidDecisionOutput, match="decision_no_go_experimental_evidence_missing"):
        validate(source, output)


def test_all_priority_axes_allow_six_values_but_reject_supported_safety() -> None:
    source, output = case_fixture()
    for axis in source["evidence"]["admet:context"]["toxicity_axes"]["axes"]:
        axis["priority_status"] = "priority_check"
    rows = source["evidence"]["admet:context"]["context"]["rows"]
    output["assessment"]["safety"]["key_values"] = [
        {"label": row[0], "value": round(row[index], 2), "unit": None}
        for row in rows
        if row[0] in ("DILI", "hERG", "AMES")
        for index in (4, 6)
    ]
    validate(source, output)
    output["assessment"]["safety"]["status"] = "supported"
    with pytest.raises(InvalidDecisionOutput, match="decision_area_status_mismatch"):
        validate(source, output)


def test_ctox_herg_only_value_warns_but_coincident_admet_value_does_not() -> None:
    source, output = case_fixture()
    source["evidence"]["admet:cardiac_ion_channels"] = {
        "predictions": [{"channel": "herg", "label": "negative", "class_probability": 0.9}]
    }
    output["used_evidence_ids"].append("admet:cardiac_ion_channels")
    output["assessment"]["safety"]["key_values"] = [
        {"label": "hERG 분류 확률", "value": 0.9, "unit": None}
    ]
    meta = calculate_metadata(source["evidence"])
    assert "decision_ctox_herg_value_ambiguous" in validate_policy(
        output, source["evidence"], meta, source["dta_reference_boundary"]
    )
    source["evidence"]["admet:cardiac_ion_channels"]["predictions"][0]["class_probability"] = 0.82
    output["assessment"]["safety"]["key_values"][0]["value"] = 0.82
    assert "decision_ctox_herg_value_ambiguous" not in validate_policy(
        output, source["evidence"], meta, source["dta_reference_boundary"]
    )


def test_non_lead_candidate_value_without_a_name_warns() -> None:
    source, output = case_fixture(2)
    candidate = source["evidence"]["dta:ENSG00000183765"]
    value = round(candidate["model_runs"][0]["observations"][0]["value"], 2)
    output["assessment"]["dta"]["key_values"] = [
        {"label": "결합 예측", "value": value, "unit": None}
    ]
    warnings = validate_policy(
        output,
        source["evidence"],
        calculate_metadata(source["evidence"]),
        source["dta_reference_boundary"],
    )
    assert "decision_non_lead_value_unlabelled" in warnings
    output["assessment"]["dta"]["key_values"][0]["label"] = "CHEK2 CNN-CNN 예측 pKd"
    assert "decision_non_lead_value_unlabelled" not in validate_policy(
        output,
        source["evidence"],
        calculate_metadata(source["evidence"]),
        source["dta_reference_boundary"],
    )


@pytest.mark.parametrize("area", ["target", "dta", "adme", "safety"])
def test_missing_raw_observations_only_allow_unavailable(area: str) -> None:
    source, output = case_fixture()
    evidence = source["evidence"]
    if area == "target":
        evidence.pop("target:ENSG00000135446")
        assert calculate_metadata(evidence).lead_candidate is None
    elif area == "dta":
        evidence.pop("dta:ENSG00000135446")
    else:
        context = evidence["admet:context"]["context"]
        context["rows"] = [
            row
            for row in context["rows"]
            if (
                row[0] not in ("DILI", "hERG", "AMES")
                if area == "safety"
                else row[0] in ("DILI", "hERG", "AMES", "LD50_Zhu")
            )
        ]
    metadata = calculate_metadata(evidence)
    assert metadata.expected_area_statuses[area] == "unavailable"
    # 기대 상태 계산뿐 아니라 실제 C-3 검증의 허용/거부 경계까지 검사한다.
    for name, status in metadata.expected_area_statuses.items():
        output["assessment"][name]["status"] = status
        output["assessment"][name]["key_values"] = []
    assert not validate_policy(output, evidence, metadata, source["dta_reference_boundary"])
    output["assessment"][area]["status"] = "unresolved"
    with pytest.raises(PolicyViolation, match="decision_area_status_mismatch"):
        validate_policy(output, evidence, metadata, source["dta_reference_boundary"])


def test_partial_interpretation_keeps_observation_states_but_restricts_go() -> None:
    source, _ = case_fixture()
    evidence = source["evidence"]
    evidence["admet:context"].update(
        execution_status="partial_failure", execution_error_code="reasoning_invalid_output"
    )
    meta = calculate_metadata(evidence)
    assert meta.expected_area_statuses["adme"] == "informative"
    assert meta.expected_area_statuses["safety"] == "unresolved"
    assert any(
        r.cause == "stage_partial:admet:reasoning_invalid_output" for r in meta.go_restrictions
    )


def test_stabilize_and_no_candidates_do_not_create_supported_target() -> None:
    source, _ = case_fixture()
    source["evidence"]["target:ENSG00000135446"]["policy_direction"] = "stabilize"
    assert calculate_metadata(source["evidence"]).expected_area_statuses["target"] == "unresolved"
    meta = calculate_metadata({})
    assert meta.lead_candidate is None
    assert (
        meta.expected_area_statuses["target"] == meta.expected_area_statuses["dta"] == "unavailable"
    )


def test_admet_projection_rejects_priority_status_tampering() -> None:
    from types import SimpleNamespace
    from typing import cast

    from evidrug_api.execution_contracts.agent import AgentOutput, AgentOutputStatus
    from evidrug_api.orchestration.upstream import InvalidUpstream

    source, _ = case_fixture()
    context = AdmetContext.model_validate(source["evidence"]["admet:context"]["context"])
    axes = calculate_toxicity_axes(context)
    altered = axes.model_copy(
        update={
            "axes": (
                axes.axes[0].model_copy(update={"priority_status": "not_priority"}),
                *axes.axes[1:],
            )
        }
    )
    result = AdmetAgentResult(context=context, interpretation=None, toxicity_axes=altered)
    output = cast(
        AgentOutput[AdmetAgentResult],
        SimpleNamespace(
            result=result,
            run_id=context.source_run_id,
            status=AgentOutputStatus.COMPLETED,
            error=None,
        ),
    )
    with pytest.raises(InvalidUpstream, match="decision_toxicity_axes_mismatch"):
        admet_evidence(output)


@pytest.mark.parametrize(
    "mutation",
    ["extra", "headline", "rationale", "strengths", "concerns", "values", "status", "string_value"],
)
def test_new_schema_limits_and_closed_fields(mutation: str) -> None:
    source, output = case_fixture()
    if mutation == "extra":
        output["lead_candidate"] = {}
    elif mutation == "headline":
        output["headline"] = "x" * 151
    elif mutation == "rationale":
        output["rationale"] = "x" * 301
    elif mutation == "strengths":
        output["key_strengths"] = ["x"] * 4
    elif mutation == "concerns":
        output["key_concerns"] = ["x"] * 4
    elif mutation == "values":
        output["assessment"]["dta"]["key_values"] *= 3
    elif mutation == "status":
        output["assessment"]["dta"]["status"] = "concerning"
    else:
        output["assessment"]["dta"]["key_values"][0]["value"] = "4.81"
    with pytest.raises(InvalidDecisionOutput, match="decision_schema_invalid"):
        validate(source, output)
