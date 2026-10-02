"""외부 근거를 변경하지 않고 small-molecule 후보 자격을 판정하는 정책."""

from evidrug_api.target_hypothesis.contracts import (
    AssessedTargetCandidate,
    TargetCandidate,
    TargetEligibility,
    TherapeuticModality,
)

CLINICAL_PRECEDENCE_LABELS = {
    "Approved Drug": "approved_small_molecule",
    "Advanced Clinical": "advanced_clinical_small_molecule",
    "Phase 1 Clinical": "phase_1_clinical_small_molecule",
}
TRACTABILITY_LABELS = {
    "Structure with Ligand": "structure_with_ligand",
    "High-Quality Ligand": "high_quality_ligand",
    "High-Quality Pocket": "high_quality_pocket",
    "Med-Quality Pocket": "medium_quality_pocket",
    "Druggable Family": "druggable_family",
}


class SmallMoleculePrioritizationPolicy:
    """긍정적 tractability 근거와 근거 부족을 명시적으로 구분한다."""

    modality = TherapeuticModality.SMALL_MOLECULE

    def assess(self, candidate: TargetCandidate) -> AssessedTargetCandidate:
        positive_labels = {
            assessment.label
            for assessment in candidate.tractability_assessments
            if assessment.value
        }
        reason_codes = tuple(
            reason
            for label, reason in (
                *CLINICAL_PRECEDENCE_LABELS.items(),
                *TRACTABILITY_LABELS.items(),
            )
            if label in positive_labels
        )
        if reason_codes:
            eligibility = TargetEligibility.ELIGIBLE
        else:
            eligibility = TargetEligibility.EXPLORATORY
            reason_codes = ("small_molecule_tractability_evidence_missing",)
        return AssessedTargetCandidate(
            candidate=candidate,
            modality=self.modality,
            eligibility=eligibility,
            reason_codes=reason_codes,
        )

    def assess_all(
        self, candidates: tuple[TargetCandidate, ...]
    ) -> tuple[AssessedTargetCandidate, ...]:
        """provider 순서를 보존해 모든 후보에 동일 정책을 적용한다."""
        return tuple(self.assess(candidate) for candidate in candidates)
