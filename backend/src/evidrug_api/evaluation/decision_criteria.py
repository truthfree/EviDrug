"""평가자 작성 4분류 검토 기록의 구조적 일관성 검사 (운영 판정과 분리)."""

from typing import Literal

from pydantic import BaseModel, Field, model_validator

Verdict = Literal["go", "conditional_go", "no_go", "defer"]
GateStatus = Literal["supported", "contradicted", "unresolved"]


class GateReview(BaseModel):
    status: GateStatus
    evidence_ids: list[str] = Field(default_factory=list)
    rationale: str = Field(min_length=1)

    @model_validator(mode="after")
    def require_evidence_for_resolved_status(self) -> "GateReview":
        if self.status != "unresolved" and not self.evidence_ids:
            raise ValueError("resolved_gate_requires_evidence")
        return self


class DecisionCriteriaReview(BaseModel):
    """사람이 근거를 평가한 후 쓰는 기록. 관측의 진실성은 검증하지 않는다."""

    criteria_version: Literal["1"] = "1"
    case_id: str = Field(min_length=1)
    compound_structure: str | None = None
    compound_role: Literal["parent", "active_metabolite"] | None = None
    target_gene: str | None = None
    target_uniprot: str | None = None
    intended_action: Literal["inhibit", "activate", "degrade", "other"] | None = None
    disease: str | None = None
    subtype: str | None = None
    subtype_not_applicable_reason: str | None = None
    biomarker: str | None = None
    biomarker_not_applicable_reason: str | None = None
    next_experiment: str | None = None
    target_disease: GateReview
    compound_target: GateReview
    mechanism: GateReview
    experiment_feasibility: GateReview
    verdict: Verdict
    verdict_rationale: str = Field(min_length=1)
    decisive_negative_evidence_ids: list[str] = Field(default_factory=list)
    remaining_potential_evidence_ids: list[str] = Field(default_factory=list)
    condition: str | None = None
    condition_test: str | None = None
    reevaluation_rule: str | None = None

    @model_validator(mode="after")
    def check_decision_consistency(self) -> "DecisionCriteriaReview":
        gates = (
            self.target_disease,
            self.compound_target,
            self.mechanism,
            self.experiment_feasibility,
        )
        required = (
            self.compound_structure,
            self.compound_role,
            self.target_gene,
            self.target_uniprot,
            self.intended_action,
            self.disease,
            self.next_experiment,
        )
        context_complete = (
            all(required)
            and bool(self.subtype or self.subtype_not_applicable_reason)
            and bool(self.biomarker or self.biomarker_not_applicable_reason)
        )
        if self.verdict == "go" and (
            not context_complete or any(gate.status != "supported" for gate in gates)
        ):
            raise ValueError("go_requires_complete_context_and_supported_gates")
        if self.verdict == "conditional_go":
            if not context_complete or not self.remaining_potential_evidence_ids:
                raise ValueError("conditional_go_requires_context_and_positive_basis")
            if not all((self.condition, self.condition_test, self.reevaluation_rule)):
                raise ValueError("conditional_go_requires_actionable_condition")
            if all(gate.status == "supported" for gate in gates):
                raise ValueError("conditional_go_requires_unresolved_or_contradicted_gate")
        if self.verdict == "no_go":
            if not context_complete:
                raise ValueError("no_go_requires_complete_context")
            if not self.decisive_negative_evidence_ids or not any(
                gate.status == "contradicted" for gate in gates
            ):
                raise ValueError("no_go_requires_decisive_negative_evidence")
            contradicted_ids = {
                evidence_id
                for gate in gates
                if gate.status == "contradicted"
                for evidence_id in gate.evidence_ids
            }
            if not set(self.decisive_negative_evidence_ids) <= contradicted_ids:
                raise ValueError("decisive_evidence_must_reference_contradicted_gate")
        if self.verdict == "defer" and self.decisive_negative_evidence_ids:
            raise ValueError("defer_cannot_claim_decisive_negative_evidence")
        return self
