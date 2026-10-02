import json

import pytest
from decision_v57_support import add_new_fields, case_fixture
from pydantic import ValidationError

from evidrug_api.analysis_jobs.models import AnalysisStageName
from evidrug_api.ctoxpred2.contracts import CtoxChannel
from evidrug_api.decision.agent import (
    DecisionNextAction,
    InvalidDecisionOutput,
    normalize_reader_numbers,
    remove_inline_evidence_markers,
    validate_assessment,
)
from evidrug_api.decision.agent import (
    LegacyDecisionAssessment as DecisionAssessment,
)
from evidrug_api.decision.recall import DecisionRecallRequest, EvidenceGapKind, route_recall


def request() -> DecisionRecallRequest:
    return DecisionRecallRequest(
        gap_kind=EvidenceGapKind.CARDIAC_ION_CHANNEL,
        objective="기본 ADMET 이후 심장 이온통로 근거 공백을 보완한다.",
        reason_code="cardiac_evidence_gap",
        required_endpoints=tuple(CtoxChannel),
    )


def test_decision_requests_evidence_gap_and_server_routes_one_recall() -> None:
    route = route_recall(request(), current_depth=0)
    assert route.target_agent is AnalysisStageName.ADMET
    assert route.max_tool_calls == route.max_recall_depth == 1


def test_recall_rejects_recursive_or_partial_panel() -> None:
    with pytest.raises(ValueError, match="recall_depth_exceeded"):
        route_recall(request(), current_depth=1)
    with pytest.raises(ValidationError):
        DecisionRecallRequest(
            gap_kind=EvidenceGapKind.CARDIAC_ION_CHANNEL,
            objective="partial",
            reason_code="cardiac_evidence_gap",
            required_endpoints=(CtoxChannel.HERG,),
        )


def test_decision_recall_is_rejected_when_depth_budget_is_unavailable() -> None:
    assessment = {
        "decision_phase": "request_recall",
        "verdict": "conditional_go",
        "rationale": "심장 이온통로 근거가 더 필요하다.",
        "used_evidence_ids": ["admet:context"],
        "conflicts": [],
        "gaps": ["cardiac ion channel evidence"],
        "next_actions": [
            {
                "status": "proposed",
                "action": "심장 이온통로 근거를 확인합니다.",
                "rationale": "현재 판정을 제한하는 공백입니다.",
                "decision_impact": "우려가 확인되면 우선순위를 낮춥니다.",
            }
        ],
        "recall_request": request().model_dump(mode="json"),
    }
    with pytest.raises(InvalidDecisionOutput, match="decision_recall_not_allowed"):
        validate_assessment(
            json.dumps(add_new_fields(assessment, {"admet:context": {}})),
            "completed",
            {"admet:context": {}},
            False,
            allow_recall=False,
        )
    assert (
        validate_assessment(
            json.dumps(assessment),
            "completed",
            {"admet:context": {}},
            False,
            allow_recall=True,
        ).recall_request
        is not None
    )


def test_final_go_does_not_require_a_generic_gap() -> None:
    assessment = {
        "decision_phase": "final",
        "verdict": "go",
        "rationale": "제공된 근거에서 추가 연구 우선순위가 높다.",
        "used_evidence_ids": ["target:ENSG00000135446"],
        "conflicts": [],
        "gaps": [],
        "next_actions": [
            {
                "status": "proposed",
                "action": "후속 기능 검증을 진행합니다.",
                "rationale": "연구 우선순위를 확인하기 위해 필요합니다.",
                "decision_impact": "기능 효과가 재현되지 않으면 우선순위를 낮춥니다.",
            }
        ],
        "recall_request": None,
    }
    source, _ = case_fixture()
    evidence = source["evidence"]
    evidence["dta:ENSG00000135446"]["region_status"] = "same_region_meets_reference"
    for axis in evidence["admet:context"]["toxicity_axes"]["axes"]:
        axis["priority_status"] = "not_priority"
    assessment["used_evidence_ids"] = list(evidence)
    add_new_fields(assessment, evidence)
    assert not validate_assessment(json.dumps(assessment), "completed", evidence, False).gaps
    assessment["verdict"] = "conditional_go"
    with pytest.raises(InvalidDecisionOutput, match="decision_schema_invalid"):
        validate_assessment(
            json.dumps(assessment), "completed", {"target:ENSG00000135446": {}}, False
        )


def test_inline_evidence_markers_are_removed_without_changing_structured_citations() -> None:
    assessment = DecisionAssessment(
        decision_phase="final",
        verdict="conditional_go",
        rationale="CDK4 표적 근거 [target:ENSG00000135446]. 비교값 [12]은 유지한다.",
        used_evidence_ids=("target:ENSG00000135446",),
        conflicts=("상충 근거 [target:ENSG00000135446]",),
        gaps=("기능 검증 [dta:ENSG00000135446]",),
        recall_request=None,
    )
    cleaned, count = remove_inline_evidence_markers(assessment)
    assert count == 3
    assert cleaned.rationale == "CDK4 표적 근거. 비교값 [12]은 유지한다."
    assert cleaned.used_evidence_ids == assessment.used_evidence_ids
    assert cleaned.conflicts == ("상충 근거",)
    assert cleaned.gaps == ("기능 검증",)


def test_citation_cleanup_does_not_turn_an_empty_report_into_success() -> None:
    assessment = DecisionAssessment(
        decision_phase="final",
        verdict="conditional_go",
        rationale="[target:ENSG00000135446]",
        used_evidence_ids=("target:ENSG00000135446",),
        conflicts=(),
        gaps=("CDK4 기능 검증",),
        recall_request=None,
    )
    with pytest.raises(InvalidDecisionOutput, match="decision_prose_empty_after_citation_cleanup"):
        remove_inline_evidence_markers(assessment)


def test_reader_numbers_are_rounded_without_turning_small_values_into_zero() -> None:
    assessment = DecisionAssessment(
        decision_phase="final",
        verdict="conditional_go",
        rationale="예측값은 4.811026이고 작은 값은 0.00004811026입니다.",
        used_evidence_ids=("dta:CDK4",),
        conflicts=("두 값은 12.3456과 9.8765입니다.",),
        gaps=("0.0049의 의미를 확인합니다.",),
        next_actions=(
            DecisionNextAction(
                status="proposed",
                action="4.811026 예측을 확인합니다.",
                rationale="0.00004811026 신호가 작기 때문입니다.",
                decision_impact="12.3456이 재현되지 않으면 우선순위를 낮춥니다.",
            ),
        ),
        recall_request=None,
    )
    normalized, count = normalize_reader_numbers(assessment)
    assert count == 8
    assert normalized.rationale == "예측값은 4.81이고 작은 값은 4.8e-5입니다."
    assert normalized.conflicts == ("두 값은 12.35과 9.88입니다.",)
    assert normalized.gaps == ("4.9e-3의 의미를 확인합니다.",)
    assert normalized.next_actions[0].action == "4.81 예측을 확인합니다."


def test_legacy_stored_assessment_without_next_actions_remains_readable() -> None:
    legacy = DecisionAssessment.model_validate(
        {
            "decision_phase": "final",
            "verdict": "go",
            "rationale": "과거 저장 결과입니다.",
            "used_evidence_ids": ["target:CDK4"],
            "conflicts": [],
            "gaps": [],
            "recall_request": None,
        }
    )
    assert legacy.next_actions == ()


def test_new_generated_assessment_requires_next_actions() -> None:
    payload = {
        "decision_phase": "final",
        "verdict": "go",
        "rationale": "새 생성 결과입니다.",
        "used_evidence_ids": ["target:CDK4"],
        "conflicts": [],
        "gaps": [],
        "recall_request": None,
    }
    with pytest.raises(InvalidDecisionOutput, match="decision_schema_invalid"):
        validate_assessment(json.dumps(payload), "completed", {"target:CDK4": {}}, False)
