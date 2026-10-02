"""조회 근거와 결정적 정책 안에서만 shortlist를 만드는 Dacon LLM 경계."""

import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Protocol

from pydantic import ValidationError

from evidrug_api.analysis_input.models import AnalysisInputResponse
from evidrug_api.openai_gateway import DaconOpenAIClient, GeneratedText
from evidrug_api.reader_numbers import (
    READER_NUMBER_INSTRUCTIONS,
    reader_facing_numbers_are_concise,
)
from evidrug_api.target_hypothesis.causal_policy import CausalSupportPolicy
from evidrug_api.target_hypothesis.contracts import (
    AssessedTargetCandidate,
    CausalEvidenceAxis,
    DiseaseEvidenceScope,
    ModulationAction,
    TargetCausalEvidence,
    TargetEligibility,
    TargetRanking,
)

PROMPT_VERSION = "target-causal-support-v7"
MAX_OUTPUT_TOKENS = 4096
MAX_PROMPT_EVIDENCE_PER_CANDIDATE = 8
INSTRUCTIONS = (
    "You are the evidence-constrained target prioritisation component of EviDrug.\n"
    "Rank exactly the requested number of supplied candidates for a small-molecule DTA "
    "shortlist.\n"
    "Use only supplied fields. Association score is disease relevance, not druggability or "
    "probability.\n"
    "Biological causal support, clinical validation, tractability, and association are separate "
    "axes. Clinical validation must not be described as biological causality.\n"
    "Tractability false means evidence was not found by that assessment, not proof of "
    "impossibility.\n"
    "Never exclude a target merely because the disease is genetic.\n"
    "GoF/risk and LoF/protective BOTH imply inhibit; LoF/risk and GoF/protective "
    "BOTH imply activate under the supplied heuristic. Different raw effects are not "
    "necessarily conflicting therapeutic directions. Use action_counts and each evidence's "
    "implied_action. For conflicts, cite at least one inhibit and one activate evidence "
    "and explain the actual opposing combinations. Counts are retrieved records, not "
    "independent experiments or probabilities. Direction is a hypothesis, not proof that "
    "a drug can restore lost function.\n"
    "Use exactly the supplied therapeutic_direction as modulation_action. Select only supplied "
    "causal evidence IDs in causal_evidence_ids. Follow citation_requirements for EACH candidate: "
    "when minimum_ids is 1, include at least one representative evidence ID; when "
    "required_action_groups are present, include at least one ID from EACH action group. "
    "If no representative evidence exists, use an empty list; do not invent citations. "
    "Never put raw evidence IDs in rationale or "
    "causal_rationale; use the supplied evidence_label and describe its disease, scope, axis, "
    "and directions instead. Explain whether evidence is direct-disease or subtype evidence. "
    "Do not invent identifiers, "
    "binding sites, ligands, experiments, publications, clinical claims, or tractability "
    "evidence.\n"
    "Write every reader-facing Korean explanation in a consistent polite formal style "
    "(합니다체), using complete sentences such as '가능성이 있습니다' or '근거가 부족합니다'. "
    "Apply this to rationale and causal_rationale; do not use plain '-다' endings or "
    "noun-ending fragments. "
    f"{READER_NUMBER_INSTRUCTIONS}\n"
    "Return one JSON object and no markdown with exactly this shape:\n"
    '{"ranked_candidates":[{"ensembl_id":"one supplied ENSG identifier",'
    '"modulation_action":"inhibit|activate|stabilize|unknown",'
    '"rationale":"concise Korean prioritisation explanation grounded in supplied fields",'
    '"causal_evidence_ids":["IDs satisfying citation_requirements"],'
    '"causal_rationale":"concise Korean causal interpretation, explicitly separating '
    'clinical validation"}]}'
)


class TargetReasoningUnavailable(RuntimeError):
    """Dacon 모델 호출이 완료되지 않았다."""


class InvalidTargetSelection(RuntimeError):
    """모델 출력이 계약, 개수 또는 허용 후보 범위를 벗어났다."""

    def __init__(
        self,
        message: str,
        *,
        diagnostic_code: str = "target_output_invalid",
        generated: GeneratedText | None = None,
    ) -> None:
        super().__init__(message)
        self.diagnostic_code = diagnostic_code
        self.generated = generated


@dataclass(frozen=True, slots=True)
class ReasonedTargetRanking:
    ranking: TargetRanking
    generated: GeneratedText


class TargetReasoner(Protocol):
    async def rank(
        self,
        case_input: AnalysisInputResponse,
        candidates: tuple[AssessedTargetCandidate, ...],
        *,
        shortlist_limit: int,
    ) -> ReasonedTargetRanking: ...


class DaconTargetReasoner:
    """모델에게 근거 해석만 맡기고 순위 식별자를 allowlist로 재검증한다."""

    def __init__(self, client: DaconOpenAIClient) -> None:
        self._client = client

    async def rank(
        self,
        case_input: AnalysisInputResponse,
        candidates: tuple[AssessedTargetCandidate, ...],
        *,
        shortlist_limit: int,
    ) -> ReasonedTargetRanking:
        actionable = tuple(
            candidate
            for candidate in candidates
            if candidate.eligibility is not TargetEligibility.INELIGIBLE
        )
        requested_count = min(shortlist_limit, len(actionable))
        prompt_evidence = {
            candidate.candidate.ensembl_id: self._representative_evidence(
                candidate.causal_support.evidence
            )
            for candidate in actionable
        }
        prompt = json.dumps(
            {
                "disease": {
                    "id": case_input.disease_id,
                    "name": case_input.disease_name,
                },
                "target_mode": case_input.target_mode.value,
                "requested_target": case_input.target_name,
                "modality": "small_molecule",
                "requested_candidate_count": requested_count,
                "candidates": [
                    self._prompt_candidate(
                        candidate,
                        prompt_evidence[candidate.candidate.ensembl_id],
                    )
                    for candidate in actionable
                ],
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        try:
            generated = await self._client.generate_text(
                prompt,
                instructions=INSTRUCTIONS,
                max_output_tokens=MAX_OUTPUT_TOKENS,
            )
        except Exception as error:
            raise TargetReasoningUnavailable("target ranking model is unavailable") from error

        if generated.status != "completed":
            raise InvalidTargetSelection(
                "target ranking response is incomplete",
                diagnostic_code="target_response_incomplete",
                generated=generated,
            )

        try:
            ranking = TargetRanking.model_validate_json(generated.text)
        except (ValidationError, ValueError) as error:
            raise InvalidTargetSelection(
                "target ranking output is invalid",
                diagnostic_code="target_schema_invalid",
                generated=generated,
            ) from error

        selected_ids = {candidate.ensembl_id for candidate in ranking.ranked_candidates}
        allowed_ids = {candidate.candidate.ensembl_id for candidate in actionable}
        if not selected_ids <= allowed_ids:
            raise InvalidTargetSelection(
                "target ranking is outside the candidate allowlist",
                diagnostic_code="target_candidate_not_allowed",
                generated=generated,
            )
        if len(ranking.ranked_candidates) != requested_count:
            raise InvalidTargetSelection(
                "target ranking has an unexpected candidate count",
                diagnostic_code="target_count_mismatch",
                generated=generated,
            )
        assessed_by_id = {candidate.candidate.ensembl_id: candidate for candidate in actionable}
        normalized_candidates = []
        for selected in ranking.ranked_candidates:
            assessed = assessed_by_id[selected.ensembl_id]
            representative = prompt_evidence[selected.ensembl_id]
            evidence_ids = {item.evidence_id for item in representative}
            if not set(selected.causal_evidence_ids) <= evidence_ids:
                raise InvalidTargetSelection(
                    "target causal rationale is outside the evidence allowlist",
                    diagnostic_code="target_citation_not_allowed",
                    generated=generated,
                )
            if evidence_ids and not selected.causal_evidence_ids:
                raise InvalidTargetSelection(
                    "target causal rationale must reference supplied evidence",
                    diagnostic_code="target_citation_missing",
                    generated=generated,
                )
            if selected.modulation_action is not assessed.causal_support.therapeutic_direction:
                raise InvalidTargetSelection(
                    "target modulation action conflicts with supplied evidence direction",
                    diagnostic_code="target_direction_mismatch",
                    generated=generated,
                )
            if "therapeutic_direction_conflicting" in assessed.causal_support.reason_codes:
                cited_actions = {
                    CausalSupportPolicy.therapeutic_action(item)
                    for item in prompt_evidence[selected.ensembl_id]
                    if item.evidence_id in selected.causal_evidence_ids
                }
                if not {ModulationAction.INHIBIT, ModulationAction.ACTIVATE} <= cited_actions:
                    raise InvalidTargetSelection(
                        "conflict explanation must cite both directions",
                        diagnostic_code="target_conflict_citation_missing",
                        generated=generated,
                    )
            labels = self._evidence_labels(representative)
            if not reader_facing_numbers_are_concise(selected.rationale, selected.causal_rationale):
                raise InvalidTargetSelection(
                    "target rationale uses excessive decimal precision",
                    diagnostic_code="target_number_precision_invalid",
                    generated=generated,
                )
            normalized_candidates.append(
                selected.model_copy(
                    update={
                        "rationale": self._replace_evidence_ids(selected.rationale, labels),
                        "causal_rationale": self._replace_evidence_ids(
                            selected.causal_rationale, labels
                        ),
                    }
                )
            )
        normalized_ranking = ranking.model_copy(
            update={"ranked_candidates": tuple(normalized_candidates)}
        )
        return ReasonedTargetRanking(ranking=normalized_ranking, generated=generated)

    @staticmethod
    def _prompt_candidate(
        candidate: AssessedTargetCandidate,
        representative_evidence: tuple[TargetCausalEvidence, ...],
    ) -> dict[str, object]:
        source = candidate.candidate
        evidence_labels = DaconTargetReasoner._evidence_labels(representative_evidence)
        datasource_counts = Counter(
            item.datasource_id for item in candidate.causal_support.evidence
        )
        scope_counts = Counter(
            item.disease_scope.value for item in candidate.causal_support.evidence
        )
        direction_counts = Counter(
            f"{item.direction_on_target.value}/{item.direction_on_trait.value}"
            f" -> {CausalSupportPolicy.therapeutic_action(item).value}"
            for item in candidate.causal_support.evidence
        )
        citation_requirements: dict[str, object] = {
            "minimum_ids": 1 if representative_evidence else 0,
            "required_action_groups": {},
        }
        if "therapeutic_direction_conflicting" in candidate.causal_support.reason_codes:
            citation_requirements["required_action_groups"] = {
                action.value: [
                    item.evidence_id
                    for item in representative_evidence
                    if CausalSupportPolicy.therapeutic_action(item) is action
                ]
                for action in (ModulationAction.INHIBIT, ModulationAction.ACTIVATE)
            }
        return {
            "ensembl_id": source.ensembl_id,
            "approved_symbol": source.approved_symbol,
            "approved_name": source.approved_name,
            "biotype": source.biotype,
            "function_descriptions": source.function_descriptions,
            "association_score": source.association_score,
            "data_type_scores": [
                score.model_dump(mode="json") for score in source.data_type_scores
            ],
            "small_molecule_tractability": [
                assessment.model_dump(mode="json") for assessment in source.tractability_assessments
            ],
            "eligibility": candidate.eligibility.value,
            "eligibility_reason_codes": candidate.reason_codes,
            "target_maturity": (
                source.pharos_evidence.model_dump(mode="json")
                if source.pharos_evidence is not None
                else None
            ),
            "citation_requirements": citation_requirements,
            "causal_support": {
                "status": candidate.causal_support.status.value,
                "reason_codes": candidate.causal_support.reason_codes,
                "biological_evidence_count": (candidate.causal_support.biological_evidence_count),
                "clinical_validation_count": (candidate.causal_support.clinical_validation_count),
                "therapeutic_direction": (candidate.causal_support.therapeutic_direction.value),
                "evidence_total_count": len(candidate.causal_support.evidence),
                "datasource_counts": dict(sorted(datasource_counts.items())),
                "scope_counts": dict(sorted(scope_counts.items())),
                "direction_counts": dict(sorted(direction_counts.items())),
                "action_counts": dict(
                    Counter(
                        CausalSupportPolicy.therapeutic_action(item).value
                        for item in candidate.causal_support.evidence
                    )
                ),
                "representative_evidence": [
                    {
                        "evidence_id": item.evidence_id,
                        "evidence_label": evidence_labels[item.evidence_id],
                        "datasource_id": item.datasource_id,
                        "axis": item.axis.value,
                        "score": item.score,
                        "disease_id": item.disease_id,
                        "disease_name": item.disease_name,
                        "disease_scope": item.disease_scope.value,
                        "direction_on_target": item.direction_on_target.value,
                        "direction_on_trait": item.direction_on_trait.value,
                        "implied_action": CausalSupportPolicy.therapeutic_action(item).value,
                        "target_role": item.target_role,
                        "confidence": item.confidence,
                        "significant_driver_methods": item.significant_driver_methods,
                    }
                    for item in representative_evidence
                ],
            },
        }

    @staticmethod
    def _evidence_labels(
        evidence: tuple[TargetCausalEvidence, ...],
    ) -> dict[str, str]:
        """원시 record ID 대신 설명문에서 사용할 안정적인 한국어 표식을 만든다."""
        counts: Counter[tuple[DiseaseEvidenceScope, CausalEvidenceAxis]] = Counter()
        labels: dict[str, str] = {}
        scope_names = {
            DiseaseEvidenceScope.DIRECT: "직접 질환",
            DiseaseEvidenceScope.SUBTYPE: "하위 유형",
        }
        axis_names = {
            CausalEvidenceAxis.STATISTICAL_GENETICS: "통계 유전학",
            CausalEvidenceAxis.CLINICAL_GENETICS: "임상 유전학",
            CausalEvidenceAxis.SOMATIC: "체세포 변이",
            CausalEvidenceAxis.FUNCTIONAL: "기능 교란",
            CausalEvidenceAxis.CLINICAL_VALIDATION: "임상 검증",
        }
        for item in evidence:
            key = (item.disease_scope, item.axis)
            counts[key] += 1
            labels[item.evidence_id] = (
                f"{scope_names[item.disease_scope]} {axis_names[item.axis]} 근거 {counts[key]}"
            )
        return labels

    @staticmethod
    def _replace_evidence_ids(text: str, labels: dict[str, str]) -> str:
        """모델이 설명문에 복사한 불투명 ID를 사람이 읽을 수 있는 표식으로 치환한다."""
        for evidence_id in sorted(labels, key=len, reverse=True):
            text = text.replace(evidence_id, labels[evidence_id])
        return text

    @staticmethod
    def _representative_evidence(
        evidence: tuple[TargetCausalEvidence, ...],
    ) -> tuple[TargetCausalEvidence, ...]:
        """축·범위·방향을 보존하면서 LLM prompt용 근거를 결정적으로 제한한다."""
        grouped: dict[CausalEvidenceAxis, list[TargetCausalEvidence]] = defaultdict(list)
        seen: dict[CausalEvidenceAxis, set[tuple[object, ...]]] = defaultdict(set)
        ordered = sorted(
            evidence,
            key=lambda item: (
                0 if item.disease_scope is DiseaseEvidenceScope.DIRECT else 1,
                -item.score,
                item.datasource_id,
                item.evidence_id,
            ),
        )
        for item in ordered:
            group_key = (
                item.disease_scope,
                item.direction_on_target,
                item.direction_on_trait,
            )
            if group_key in seen[item.axis]:
                continue
            seen[item.axis].add(group_key)
            grouped[item.axis].append(item)

        selected: list[TargetCausalEvidence] = []
        # Reserve witnesses for both directions before filling the remaining axis slots.
        for action in (ModulationAction.INHIBIT, ModulationAction.ACTIVATE):
            witness = next(
                (
                    item
                    for item in ordered
                    if CausalSupportPolicy.therapeutic_action(item) is action
                ),
                None,
            )
            if witness is not None:
                selected.append(witness)
        position = 0
        axes = tuple(axis for axis in CausalEvidenceAxis if grouped[axis])
        while len(selected) < MAX_PROMPT_EVIDENCE_PER_CANDIDATE:
            added = False
            for axis in axes:
                if position < len(grouped[axis]):
                    added = True
                    if grouped[axis][position] in selected:
                        continue
                    selected.append(grouped[axis][position])
                    if len(selected) == MAX_PROMPT_EVIDENCE_PER_CANDIDATE:
                        break
            if not added:
                break
            position += 1
        return tuple(selected)
