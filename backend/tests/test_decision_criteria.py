"""운영 3분류를 바꾸지 않는 평가 기록 일관성 테스트."""

import pytest
from pydantic import ValidationError

from evidrug_api.evaluation.decision_criteria import DecisionCriteriaReview


def _review(**overrides: object) -> DecisionCriteriaReview:
    data: dict[str, object] = {
        "case_id": "DA-01",
        "compound_structure": "C1CCCCC1",
        "compound_role": "parent",
        "target_gene": "CDK4",
        "target_uniprot": "P11802",
        "intended_action": "inhibit",
        "disease": "breast cancer",
        "subtype": "HR-positive HER2-negative",
        "biomarker": "HR-positive",
        "next_experiment": "cellular CDK4 target engagement assay",
        "target_disease": {
            "status": "supported",
            "evidence_ids": ["study-a", "study-b"],
            "rationale": "reviewed",
        },
        "compound_target": {
            "status": "supported",
            "evidence_ids": ["assay-a"],
            "rationale": "reviewed",
        },
        "mechanism": {
            "status": "supported",
            "evidence_ids": ["control-a"],
            "rationale": "reviewed",
        },
        "experiment_feasibility": {
            "status": "supported",
            "evidence_ids": ["solubility-a"],
            "rationale": "reviewed",
        },
        "verdict": "go",
        "verdict_rationale": "all gates reviewed",
    }
    data.update(overrides)
    return DecisionCriteriaReview.model_validate(data)


def test_go_requires_all_gates_and_complete_context() -> None:
    assert _review().verdict == "go"
    with pytest.raises(ValidationError, match="go_requires_complete_context"):
        _review(subtype=None)
    with pytest.raises(ValidationError, match="go_requires_complete_context"):
        _review(compound_target={"status": "unresolved", "rationale": "prediction only"})


def test_conditional_go_requires_positive_basis_and_reassessment() -> None:
    gap = {"status": "unresolved", "rationale": "binding unmeasured"}
    with pytest.raises(ValidationError, match="conditional_go_requires_context_and_positive_basis"):
        _review(verdict="conditional_go", compound_target=gap)
    with pytest.raises(ValidationError, match="conditional_go_requires_actionable_condition"):
        _review(
            verdict="conditional_go",
            compound_target=gap,
            remaining_potential_evidence_ids=["study-a"],
        )
    result = _review(
        verdict="conditional_go",
        compound_target=gap,
        remaining_potential_evidence_ids=["study-a"],
        condition="confirm binding",
        condition_test="SPR binding assay",
        reevaluation_rule="measured binding supports Go; no binding supports No-Go",
    )
    assert result.verdict == "conditional_go"


def test_missing_evidence_is_not_no_go() -> None:
    gap = {"status": "unresolved", "rationale": "not tested"}
    with pytest.raises(ValidationError, match="no_go_requires_decisive_negative_evidence"):
        _review(verdict="no_go", compound_target=gap)
    with pytest.raises(ValidationError, match="no_go_requires_decisive_negative_evidence"):
        _review(verdict="no_go", compound_target=gap, decisive_negative_evidence_ids=["score-low"])
    assert _review(verdict="defer", compound_target=gap).verdict == "defer"


def test_no_go_needs_contradiction_and_decisive_reference() -> None:
    contradicted = {
        "status": "contradicted",
        "evidence_ids": ["assay-negative"],
        "rationale": "reviewed",
    }
    result = _review(
        verdict="no_go",
        compound_target=contradicted,
        decisive_negative_evidence_ids=["assay-negative"],
    )
    assert result.verdict == "no_go"
    with pytest.raises(ValidationError, match="decisive_evidence_must_reference"):
        _review(
            verdict="no_go",
            compound_target=contradicted,
            decisive_negative_evidence_ids=["unrelated"],
        )
    with pytest.raises(ValidationError, match="no_go_requires_complete_context"):
        _review(
            verdict="no_go",
            compound_target=contradicted,
            decisive_negative_evidence_ids=["assay-negative"],
            subtype=None,
        )


def test_resolved_gate_requires_evidence_reference() -> None:
    with pytest.raises(ValidationError, match="resolved_gate_requires_evidence"):
        _review(compound_target={"status": "supported", "rationale": "model prediction"})
