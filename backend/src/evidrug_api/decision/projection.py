"""검증된 전문 Agent 결과에서 Decision에 전달하는 공통 순수 projection."""

import hashlib
import json
import math
from collections.abc import Sequence
from dataclasses import dataclass
from statistics import median
from uuid import UUID

from evidrug_api.admet.agent import AdmetAgentResult
from evidrug_api.admet.toxicity import calculate_toxicity_axes
from evidrug_api.analysis_input.models import (
    AnalysisInputResponse,
    PotencyCriterion,
    PotencyEndpoint,
)
from evidrug_api.analysis_jobs.models import AnalysisStageName
from evidrug_api.ctoxpred2.contracts import CtoxChannelPrediction
from evidrug_api.decision.dta_lineage import AssayEvidenceLineage
from evidrug_api.decision.policy import calculate_metadata, reference_boundary
from evidrug_api.dta.agent import DtaAgentResult, DtaModelRun
from evidrug_api.dta.contracts import DtaScoreType
from evidrug_api.dta.evidence import (
    AssayEvidence,
    AssaySource,
    assay_deduplication_key,
)
from evidrug_api.execution_contracts.agent import AgentOutput, AgentOutputStatus
from evidrug_api.execution_contracts.common import ExecutionLimits
from evidrug_api.orchestration.upstream import InvalidUpstream
from evidrug_api.target_hypothesis.contracts import TargetHypothesisResult

DTA_DECISION_MAX_KEY_RECORDS = 15
_MOLAR_FACTORS = {"pM": 1e-12, "nM": 1e-9, "uM": 1e-6, "mM": 1e-3, "M": 1.0}


@dataclass(frozen=True)
class _AssayProjectionItem:
    evidence: AssayEvidence
    original_position: int
    criterion_value: float | None
    supports_criterion: bool | None


def target_evidence(
    output: AgentOutput[TargetHypothesisResult],
) -> tuple[dict[str, object], bool]:
    result = output.result
    assert result is not None
    restricted = output.status != AgentOutputStatus.COMPLETED
    evidence: dict[str, object] = {}
    for candidate in (result.primary, *result.alternatives):
        causal = candidate.causal_support
        evidence["target:" + candidate.ensembl_id] = {
            "run_id": str(output.run_id),
            "symbol": candidate.approved_symbol,
            "association": candidate.association_score,
            "eligibility": candidate.eligibility.value,
            "causal_status": causal.status.value,
            "policy_direction": causal.therapeutic_direction.value,
            "reason_codes": causal.reason_codes,
            "llm_direction": candidate.modulation_action.value,
            "source_version": result.source_version,
            "execution_status": output.status.value,
            "execution_error_code": output.error.code if output.error else None,
        }
    restricted |= not any(
        item.causal_support.status == "supported"
        and item.causal_support.therapeutic_direction in ("inhibit", "activate")
        for item in (result.primary, *result.alternatives)
    )
    return evidence, restricted


def admet_evidence(output: AgentOutput[AdmetAgentResult]) -> tuple[dict[str, object], bool]:
    result = output.result
    assert result is not None
    if result.context.source_run_id != output.run_id:
        raise InvalidUpstream("decision_admet_source_mismatch")
    projection = result.model_dump(mode="json")
    projection["context"].pop("is_partial")
    for field in ("interpretation", "adme", "toxicity"):
        projection.pop(field, None)
    computed = calculate_toxicity_axes(result.context, result.missing_endpoints)
    if result.toxicity_axes is not None and computed != result.toxicity_axes:
        raise InvalidUpstream("decision_toxicity_axes_mismatch")
    projection["toxicity_axes"] = computed.model_dump(mode="json")
    projection["execution_status"] = output.status.value
    projection["execution_error_code"] = output.error.code if output.error else None
    projection["selection_coverage"] = {
        "selected_endpoint_count": len(result.context.rows),
        "catalog_endpoint_count": result.context.catalog_endpoint_count,
        "is_subset": result.context.is_partial,
    }
    return {"admet:context": projection}, output.status != AgentOutputStatus.COMPLETED


def dta_evidence(
    output: AgentOutput[DtaAgentResult],
    target: AgentOutput[TargetHypothesisResult] | None,
    assay_lineage: AssayEvidenceLineage | None = None,
) -> tuple[dict[str, object], bool]:
    result = output.result
    assert result is not None
    if target is None or result.source_target_run_id != target.run_id:
        raise InvalidUpstream("decision_dta_target_lineage_mismatch")
    if result.assay_policy_version != "pubchem-recall-v1" or any(
        item.source is not AssaySource.PUBCHEM
        for candidate in result.candidates
        for item in candidate.experimental_evidence
    ):
        raise InvalidUpstream("decision_dta_assay_policy_mismatch")
    target_result = target.result
    assert target_result is not None
    allowed = {
        item.ensembl_id: item for item in (target_result.primary, *target_result.alternatives)
    }
    evidence: dict[str, object] = {}
    per_candidate_record_limit = max(
        len(AssaySource), DTA_DECISION_MAX_KEY_RECORDS // len(result.candidates)
    )
    for candidate in result.candidates:
        source = allowed.get(candidate.ensembl_id)
        if source is None or source.target_sequence_sha256 != candidate.target_sequence_sha256:
            raise InvalidUpstream("decision_dta_candidate_mismatch")
        assessment = candidate.evidence_assessment
        criterion = assessment.criterion if assessment else None
        projected_items = _deduplicate_assay_evidence(candidate.experimental_evidence, criterion)
        selected = _select_assay_records(
            projected_items,
            candidate.model_runs,
            criterion,
            limit=per_candidate_record_limit,
        )
        evidence["dta:" + candidate.ensembl_id] = {
            "approved_symbol": candidate.approved_symbol,
            "uniprot_accession": candidate.uniprot_accession,
            "status": candidate.status,
            "execution_status": output.status.value,
            "execution_error_code": output.error.code if output.error else None,
            "error_code": candidate.error_code,
            "duration_ms": candidate.duration_ms,
            "model_runs": [
                {
                    "tool_id": run.tool_id,
                    "tool_call_id": str(run.tool_call_id) if run.tool_call_id else None,
                    "status": run.status,
                    "model": run.model.model_dump(mode="json") if run.model else None,
                    "observations": [item.model_dump(mode="json") for item in run.observations],
                    "error_code": run.error_code,
                }
                for run in candidate.model_runs
            ],
            "assay_runs": [run.model_dump(mode="json") for run in candidate.assay_runs],
            "experimental_evidence_summary": {
                **_assay_distribution_summary(
                    candidate.experimental_evidence, projected_items, criterion
                ),
                "included_count": len(selected),
                "omitted_deduplicated_count": len(projected_items) - len(selected),
                "key_records": [
                    _compact_assay_record(
                        candidate.ensembl_id, item.evidence, assay_lineage, reasons
                    )
                    for item, reasons in selected
                ],
            },
            "region_status": assessment.region_status
            if assessment
            else "insufficient_model_results",
            "experimental_binding_support": assessment.experimental_binding_support
            if assessment
            else False,
            "pubchem_requested": assessment.pubchem_requested if assessment else False,
            "pubchem_executed": assessment.pubchem_executed if assessment else False,
            "pubchem_execution_issue": assessment.pubchem_execution_issue if assessment else None,
            "evidence_errors": candidate.evidence_errors,
        }
    evidence["dta:interpretation"] = {
        "summary": result.interpretation.model_dump(mode="json") if result.interpretation else None,
        "limitations": result.interpretation_note,
    }
    return evidence, output.status != AgentOutputStatus.COMPLETED


def _criterion_value(item: AssayEvidence, criterion: PotencyCriterion | None) -> float | None:
    if (
        criterion is None
        or item.endpoint is not criterion.endpoint
        or item.value is None
        or item.unit is None
    ):
        return None
    converted = item.value * _MOLAR_FACTORS[item.unit] / _MOLAR_FACTORS[criterion.unit]
    return float(format(converted, ".12g"))


def _deduplication_key(item: AssayEvidence) -> tuple[object, ...]:
    return assay_deduplication_key(item)


def _deduplicate_assay_evidence(
    evidence: tuple[AssayEvidence, ...], criterion: PotencyCriterion | None
) -> tuple[_AssayProjectionItem, ...]:
    seen: set[tuple[object, ...]] = set()
    result: list[_AssayProjectionItem] = []
    threshold = criterion.maximum_value if criterion else None
    for position, item in enumerate(evidence):
        key = _deduplication_key(item)
        if key in seen:
            continue
        seen.add(key)
        value = _criterion_value(item, criterion)
        result.append(
            _AssayProjectionItem(
                evidence=item,
                original_position=position,
                criterion_value=value,
                supports_criterion=value <= threshold
                if value is not None and threshold is not None
                else None,
            )
        )
    return tuple(result)


def _distance_from_criterion(
    item: _AssayProjectionItem, criterion: PotencyCriterion | None
) -> float:
    if item.criterion_value is None or criterion is None:
        return math.inf
    return abs(math.log10(item.criterion_value / criterion.maximum_value))


def _select_assay_records(
    items: tuple[_AssayProjectionItem, ...],
    model_runs: Sequence[DtaModelRun],
    criterion: PotencyCriterion | None,
    *,
    limit: int,
) -> tuple[tuple[_AssayProjectionItem, tuple[str, ...]], ...]:
    selected: list[_AssayProjectionItem] = []
    reasons: dict[int, list[str]] = {}

    def choose(item: _AssayProjectionItem | None, reason: str) -> None:
        if item is None:
            return
        key = item.original_position
        if item not in selected and len(selected) < limit:
            selected.append(item)
        if item in selected and reason not in reasons.setdefault(key, []):
            reasons[key].append(reason)

    def best(pool: list[_AssayProjectionItem]) -> _AssayProjectionItem | None:
        return min(
            pool,
            key=lambda item: (
                _distance_from_criterion(item, criterion),
                item.evidence.kind.value != "quantitative",
                item.original_position,
            ),
            default=None,
        )

    # 모든 출처는 전체 통계뿐 아니라 최소 한 대표 레코드로도 설명한다.
    for source in AssaySource:
        choose(best([item for item in items if item.evidence.source is source]), "source_coverage")

    for side, reason in (
        (True, "criterion_support_boundary"),
        (False, "criterion_oppose_boundary"),
    ):
        choose(best([item for item in items if item.supports_criterion is side]), reason)

    source_sides = {
        source: {
            bool(item.supports_criterion)
            for item in items
            if item.evidence.source is source and item.supports_criterion is not None
        }
        for source in AssaySource
    }
    support_sources = {source for source, sides in source_sides.items() if True in sides}
    oppose_sources = {source for source, sides in source_sides.items() if False in sides}
    if any(support != oppose for support in support_sources for oppose in oppose_sources):
        for source, sides in source_sides.items():
            for side in sides:
                choose(
                    best(
                        [
                            item
                            for item in items
                            if item.evidence.source is source and item.supports_criterion is side
                        ]
                    ),
                    "cross_source_conflict",
                )

    prediction_sides = _model_prediction_sides(model_runs, criterion)
    if prediction_sides and len(set(prediction_sides)) == 1:
        opposing = not prediction_sides[0]
        choose(
            best([item for item in items if item.supports_criterion is opposing]),
            "prediction_experiment_conflict",
        )

    comparable = [item for item in items if item.criterion_value is not None]
    if comparable:
        choose(min(comparable, key=lambda item: item.criterion_value or math.inf), "range_min")
        choose(max(comparable, key=lambda item: item.criterion_value or -math.inf), "range_max")

    for item in sorted(
        items,
        key=lambda candidate: (
            candidate.criterion_value is None,
            _distance_from_criterion(candidate, criterion),
            candidate.evidence.kind.value != "quantitative",
            candidate.original_position,
        ),
    ):
        reason = "nearest_comparable_fill" if item.criterion_value is not None else "fallback_fill"
        choose(item, reason)
        if len(selected) >= limit:
            break
    return tuple((item, tuple(reasons[item.original_position])) for item in selected)


def _model_prediction_sides(
    model_runs: Sequence[DtaModelRun], criterion: PotencyCriterion | None
) -> tuple[bool, ...]:
    if criterion is None or criterion.endpoint is not PotencyEndpoint.KD:
        return ()
    threshold_molar = criterion.maximum_value * _MOLAR_FACTORS[criterion.unit]
    threshold_pkd = -math.log10(threshold_molar)
    return tuple(
        observation.value >= threshold_pkd
        for run in model_runs
        if run.status == "succeeded"
        for observation in run.observations
        if observation.score_type is DtaScoreType.PREDICTED_PKD
    )


def _assay_distribution_summary(
    raw: tuple[AssayEvidence, ...],
    items: tuple[_AssayProjectionItem, ...],
    criterion: PotencyCriterion | None,
) -> dict[str, object]:
    comparable = [item for item in items if item.criterion_value is not None]
    values = sorted(item.criterion_value for item in comparable if item.criterion_value is not None)

    def source_summary(source: AssaySource) -> dict[str, object]:
        source_items = [item for item in items if item.evidence.source is source]
        sides = {item.supports_criterion for item in source_items} - {None}
        direction = (
            "mixed"
            if len(sides) > 1
            else "supports"
            if sides == {True}
            else "opposes"
            if sides == {False}
            else "not_comparable"
        )
        return {
            "raw_count": sum(item.source is source for item in raw),
            "deduplicated_count": len(source_items),
            "quantitative_count": sum(
                item.evidence.kind.value == "quantitative" for item in source_items
            ),
            "qualitative_count": sum(
                item.evidence.kind.value == "qualitative" for item in source_items
            ),
            "comparable_count": sum(item.criterion_value is not None for item in source_items),
            "supports_criterion_count": sum(
                item.supports_criterion is True for item in source_items
            ),
            "opposes_criterion_count": sum(
                item.supports_criterion is False for item in source_items
            ),
            "criterion_direction": direction,
        }

    return {
        "raw_count": len(raw),
        "deduplicated_count": len(items),
        "duplicate_count": len(raw) - len(items),
        "comparable_count": len(comparable),
        "noncomparable_count": len(items) - len(comparable),
        "supports_criterion_count": sum(item.supports_criterion is True for item in items),
        "opposes_criterion_count": sum(item.supports_criterion is False for item in items),
        "criterion_value_distribution": {
            "unit": criterion.unit if criterion and values else None,
            "minimum": values[0] if values else None,
            "median": median(values) if values else None,
            "maximum": values[-1] if values else None,
        },
        "by_source": {
            source.value: source_summary(source)
            for source in AssaySource
            if any(item.source is source for item in raw)
        },
        "by_endpoint": {
            endpoint.value: sum(item.evidence.endpoint is endpoint for item in items)
            for endpoint in PotencyEndpoint
            if any(item.evidence.endpoint is endpoint for item in items)
        },
    }


def _compact_assay_record(
    ensembl_id: str,
    item: AssayEvidence,
    assay_lineage: AssayEvidenceLineage | None,
    selection_reasons: tuple[str, ...],
) -> dict[str, object]:
    projection: dict[str, object] = {
        "source": item.source.value,
        "source_record_id": item.source_record_id,
        "kind": item.kind.value,
        "endpoint": item.endpoint.value if item.endpoint else None,
        "value": item.value,
        "unit": item.unit,
        "qualitative_outcome": item.qualitative_outcome,
        "doi": item.doi,
        "pmid": item.pmid,
        "selection_reasons": selection_reasons,
    }
    reference = next(
        (
            reference
            for reference in assay_lineage or ()
            if reference.ensembl_id == ensembl_id
            and reference.source is item.source
            and reference.source_record_id == item.source_record_id
        ),
        None,
    )
    if reference is not None:
        projection["source_tool_call_id"] = str(reference.source_tool_call_id)
    return projection


def cardiac_evidence(
    predictions: tuple[CtoxChannelPrediction, ...],
    limitations: tuple[str, ...],
    *,
    source_tool_call_id: UUID,
    tool_version: str,
    agent_run_id: UUID | None = None,
) -> dict[str, object]:
    """실제 source tool call을 유지하고 시뮬레이션 Agent run은 만들지 않는다."""
    projection: dict[str, object] = {
        "source_tool_call_id": str(source_tool_call_id),
        "tool_version": tool_version,
        "predictions": [item.model_dump(mode="json") for item in predictions],
        "limitations": limitations,
    }
    if agent_run_id is not None:
        projection = {"run_id": str(agent_run_id), **projection}
    return {"admet:cardiac_ion_channels": projection}


def decision_payload(
    case_input: AnalysisInputResponse,
    attempt: int,
    limits: ExecutionLimits,
    evidence: dict[str, object],
    missing: tuple[AnalysisStageName, ...],
    restricted: bool,
    *,
    recall_feedback: dict[str, object] | None = None,
) -> dict[str, object]:
    """live prompt와 replay 기록이 동일한 필드·정렬·정책을 사용한다."""
    allow_recall = (
        limits.max_recall_depth > 0 and attempt == 1 and AnalysisStageName.ADMET not in missing
    )
    metadata = calculate_metadata(evidence, tuple(stage.value for stage in missing))
    restricted = restricted or bool(metadata.go_restrictions)
    return {
        "target_mode": case_input.target_mode.value,
        "dta_reference_boundary": reference_boundary(case_input.potency_criterion),
        "lead_candidate": metadata.lead_candidate.model_dump(mode="json")
        if metadata.lead_candidate
        else None,
        "disease": case_input.disease_name,
        "missing_stages": [item.value for item in missing],
        "allowed_evidence_ids": list(evidence),
        "required_citation_prefixes": sorted({key.split(":", 1)[0] for key in evidence}),
        "allowed_verdicts": ["conditional_go", "no_go"]
        if restricted
        else ["go", "conditional_go", "no_go"],
        "evidence": evidence,
        "allow_recall": allow_recall,
        "recall_feedback": recall_feedback,
    }


def decision_payload_json(payload: dict[str, object]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def decision_payload_sha256(payload: dict[str, object]) -> str:
    return hashlib.sha256(decision_payload_json(payload).encode()).hexdigest()
