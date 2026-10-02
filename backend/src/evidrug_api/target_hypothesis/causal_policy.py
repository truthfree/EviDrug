"""개별 Open Targets evidence를 인과성과 치료 방향으로 결정적으로 요약한다."""

from evidrug_api.target_hypothesis.contracts import (
    CausalEvidenceAxis,
    CausalSupportStatus,
    DiseaseEvidenceScope,
    ModulationAction,
    TargetCausalEvidence,
    TargetCausalSupport,
    TargetEffectDirection,
    TraitEffectDirection,
)

BIOLOGICAL_AXES = frozenset(
    {
        CausalEvidenceAxis.STATISTICAL_GENETICS,
        CausalEvidenceAxis.CLINICAL_GENETICS,
        CausalEvidenceAxis.SOMATIC,
        CausalEvidenceAxis.FUNCTIONAL,
    }
)

AXIS_REASON_CODES = {
    CausalEvidenceAxis.STATISTICAL_GENETICS: "statistical_genetic_evidence_present",
    CausalEvidenceAxis.CLINICAL_GENETICS: "clinical_genetic_evidence_present",
    CausalEvidenceAxis.SOMATIC: "somatic_driver_evidence_present",
    CausalEvidenceAxis.FUNCTIONAL: "functional_perturbation_evidence_present",
    CausalEvidenceAxis.CLINICAL_VALIDATION: "clinical_validation_present",
}


class CausalSupportPolicy:
    """임상 선행 근거를 생물학적 인과 근거와 섞지 않고 요약한다."""

    @staticmethod
    def assess(evidence: tuple[TargetCausalEvidence, ...]) -> TargetCausalSupport:
        biological = tuple(item for item in evidence if item.axis in BIOLOGICAL_AXES)
        clinical = tuple(
            item for item in evidence if item.axis is CausalEvidenceAxis.CLINICAL_VALIDATION
        )
        therapeutic_direction_votes = {
            action
            for item in evidence
            if (action := CausalSupportPolicy.therapeutic_action(item))
            is not ModulationAction.UNKNOWN
        }

        status = CausalSupportStatus.SUPPORTED if biological else CausalSupportStatus.UNKNOWN

        if len(therapeutic_direction_votes) > 1:
            therapeutic_direction = ModulationAction.UNKNOWN
        else:
            therapeutic_direction = next(
                iter(therapeutic_direction_votes), ModulationAction.UNKNOWN
            )

        reason_codes = [
            code
            for axis, code in AXIS_REASON_CODES.items()
            if any(item.axis is axis for item in evidence)
        ]
        if any(item.disease_scope is DiseaseEvidenceScope.DIRECT for item in evidence):
            reason_codes.append("direct_disease_evidence_present")
        if any(item.disease_scope is DiseaseEvidenceScope.SUBTYPE for item in evidence):
            reason_codes.append("subtype_evidence_present")
        if not biological:
            reason_codes.append("causal_evidence_missing")
        if len(therapeutic_direction_votes) > 1:
            reason_codes.append("therapeutic_direction_conflicting")
        elif not therapeutic_direction_votes:
            reason_codes.append("therapeutic_direction_unknown")

        return TargetCausalSupport(
            status=status,
            reason_codes=tuple(reason_codes),
            biological_evidence_count=len(biological),
            clinical_validation_count=len(clinical),
            therapeutic_direction=therapeutic_direction,
            evidence=evidence,
        )

    @staticmethod
    def therapeutic_action(evidence: TargetCausalEvidence) -> ModulationAction:
        target = evidence.direction_on_target
        trait = evidence.direction_on_trait
        if (
            target is TargetEffectDirection.GAIN_OF_FUNCTION and trait is TraitEffectDirection.RISK
        ) or (
            target is TargetEffectDirection.LOSS_OF_FUNCTION
            and trait is TraitEffectDirection.PROTECTIVE
        ):
            return ModulationAction.INHIBIT
        if (
            target is TargetEffectDirection.LOSS_OF_FUNCTION and trait is TraitEffectDirection.RISK
        ) or (
            target is TargetEffectDirection.GAIN_OF_FUNCTION
            and trait is TraitEffectDirection.PROTECTIVE
        ):
            return ModulationAction.ACTIVATE
        return ModulationAction.UNKNOWN
