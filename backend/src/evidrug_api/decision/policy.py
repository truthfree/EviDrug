"""대표 후보와 영역 상태를 원 관측에서 한 곳에서 계산한다."""

import math
from typing import Literal, cast

from evidrug_api.admet.agent import ADME_ENDPOINTS
from evidrug_api.execution_contracts.common import ContractModel

AreaStatus = Literal["supported", "unresolved", "unavailable", "informative"]


def mapping(value: object) -> dict[str, object]:
    return cast(dict[str, object], value) if isinstance(value, dict) else {}


def items(value: object) -> list[object]:
    return list(value) if isinstance(value, (list, tuple)) else []


class LeadCandidate(ContractModel):
    ensembl_id: str
    symbol: str
    rule: str = "most-supported-gates-input-order-v1"


class CandidateGates(ContractModel):
    ensembl_id: str
    symbol: str
    target_supported: bool
    dta_supported: bool


class GoRestriction(ContractModel):
    stage: str
    cause: str


class DecisionMetadata(ContractModel):
    lead_candidate: LeadCandidate | None
    candidate_gates: tuple[CandidateGates, ...]
    confirm_channels: tuple[str, ...]
    expected_area_statuses: dict[str, AreaStatus]
    go_restrictions: tuple[GoRestriction, ...]
    safety_gate_baseline: AreaStatus
    safety_gate_final: AreaStatus
    prompt_version: str = "decision-gated-synthesis-v5.7"
    toxicity_policy_version: str = "toxicity-priority-policy-v1"
    assay_policy_version: str = "pubchem-recall-v1"


def admet_rows(evidence: dict[str, object]) -> list[dict[str, object]]:
    """실제 context 열/배열 계약에서 원 endpoint 관측을 읽는다."""
    context = mapping(mapping(evidence.get("admet:context")).get("context"))
    columns = [str(v) for v in items(context.get("columns"))]
    return [dict(zip(columns, items(row), strict=True)) for row in items(context.get("rows"))]


def calculate_metadata(
    evidence: dict[str, object],
    missing: tuple[str, ...] = (),
) -> DecisionMetadata:
    """대표 후보와 검증용 기대값은 동일한 계산 결과를 입력·저장·검증에 재사용한다."""
    gates = []
    restrictions = [GoRestriction(stage=s, cause=f"stage_missing:{s}") for s in missing]
    seen_stages: set[str] = set()
    for key, raw in evidence.items():
        row = mapping(raw)
        stage = key.split(":", 1)[0]
        stage = "target_hypothesis" if stage == "target" else stage
        if row.get("execution_status", "completed") != "completed" and stage not in seen_stages:
            seen_stages.add(stage)
            cause = row.get("execution_error_code") or "stage_incomplete"
            restrictions.append(GoRestriction(stage=stage, cause=f"stage_partial:{stage}:{cause}"))
        if not key.startswith("target:"):
            continue
        identity = key.split(":", 1)[1]
        dta = mapping(evidence.get("dta:" + identity))
        gates.append(
            CandidateGates(
                ensembl_id=identity,
                symbol=str(row.get("symbol", identity)),
                target_supported=row.get("causal_status") == "supported"
                and row.get("policy_direction") in ("inhibit", "activate"),
                dta_supported=dta.get("region_status") == "same_region_meets_reference"
                or dta.get("experimental_binding_support") is True,
            )
        )
    lead_gate = max(
        gates, key=lambda g: int(g.target_supported) + int(g.dta_supported), default=None
    )
    lead = (
        LeadCandidate(ensembl_id=lead_gate.ensembl_id, symbol=lead_gate.symbol)
        if lead_gate
        else None
    )
    if not any(g.target_supported for g in gates):
        restrictions.append(GoRestriction(stage="target_hypothesis", cause="no_supported_target"))
    if "admet:recall_failure" in evidence:
        restrictions.append(GoRestriction(stage="admet", cause="recall_failed"))
    axes = items(mapping(mapping(evidence.get("admet:context")).get("toxicity_axes")).get("axes"))
    rows = admet_rows(evidence)
    baseline: AreaStatus = (
        "unavailable"
        if not any(r.get("endpoint_id") in ("DILI", "hERG", "AMES") for r in rows)
        else "supported"
        if len(axes) == 3 and all(mapping(a).get("priority_status") == "not_priority" for a in axes)
        else "unresolved"
    )
    predictions = items(mapping(evidence.get("admet:cardiac_ion_channels")).get("predictions"))
    confirm = tuple(
        str(mapping(p)["channel"]) for p in predictions if mapping(p).get("label") == "positive"
    )
    final: AreaStatus = "unresolved" if confirm and baseline != "unavailable" else baseline
    dta = mapping(evidence.get("dta:" + lead.ensembl_id)) if lead else {}
    has_models = any(
        items(mapping(run).get("observations")) for run in items(dta.get("model_runs"))
    )
    expected: dict[str, AreaStatus] = {
        "target": "unavailable"
        if lead_gate is None
        else "supported"
        if lead_gate.target_supported
        else "unresolved",
        "dta": "unavailable"
        if not has_models
        else "supported"
        if lead_gate and lead_gate.dta_supported
        else "unresolved",
        "adme": "informative"
        if any(r.get("endpoint_id") in ADME_ENDPOINTS for r in rows)
        else "unavailable",
        "safety": final,
    }
    return DecisionMetadata(
        lead_candidate=lead,
        candidate_gates=tuple(gates),
        confirm_channels=confirm,
        expected_area_statuses=expected,
        go_restrictions=tuple(restrictions),
        safety_gate_baseline=baseline,
        safety_gate_final=final,
    )


def reference_boundary(criterion: object) -> dict[str, object] | None:
    """사용자 입력 기준을 표시용 계약으로 변환하며 원 모델을 바꾸지 않는다."""
    if criterion is None:
        return None
    from evidrug_api.analysis_input.models import PotencyCriterion

    assert isinstance(criterion, PotencyCriterion)
    molar = (
        criterion.maximum_value
        * {"pM": 1e-12, "nM": 1e-9, "uM": 1e-6, "mM": 1e-3, "M": 1}[criterion.unit]
    )
    return {
        "endpoint": criterion.endpoint.value,
        "value": criterion.maximum_value,
        "unit": criterion.unit,
        "pkd": -math.log10(molar) if criterion.endpoint == "Kd" else None,
        "role": "comparison_boundary",
    }
