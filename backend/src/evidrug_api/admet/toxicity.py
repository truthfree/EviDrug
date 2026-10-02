"""확인시험 우선순위를 원 관측으로 한 번 계산하는 공통 정책."""

from typing import Literal, cast
from uuid import UUID

from evidrug_api.admet.context import AdmetContext
from evidrug_api.execution_contracts.common import ContractModel

TOXICITY_POLICY_VERSION = "toxicity-priority-policy-v1"
AXIS_ENDPOINTS = (
    ("liver_injury", "DILI"),
    ("cardiac_ion_channel", "hERG"),
    ("mutagenicity", "AMES"),
)


class ToxicityAxis(ContractModel):
    axis: Literal["liver_injury", "cardiac_ion_channel", "mutagenicity"]
    endpoint_id: str
    value: float | None
    drugbank_approved_percentile: float | None
    priority_status: Literal["priority_check", "not_priority", "missing"]
    source_tool_call_id: UUID | None


class ToxicityAxes(ContractModel):
    policy_version: Literal["toxicity-priority-policy-v1"] = "toxicity-priority-policy-v1"
    axes: tuple[ToxicityAxis, ...]


def calculate_toxicity_axes(
    context: AdmetContext | None,
    missing_endpoints: tuple[str, ...] = (),
) -> ToxicityAxes:
    """반올림 전 값과 승인약 집단 백분위로 세 축의 확인 우선순위를 정한다."""
    rows = {row.endpoint_id: row for row in context.rows} if context else {}
    axes = []
    for axis, endpoint in AXIS_ENDPOINTS:
        row = rows.get(endpoint) if endpoint not in missing_endpoints else None
        axes.append(
            ToxicityAxis(
                axis=cast(Literal["liver_injury", "cardiac_ion_channel", "mutagenicity"], axis),
                endpoint_id=endpoint,
                value=row.value if row else None,
                drugbank_approved_percentile=row.drugbank_approved_percentile if row else None,
                source_tool_call_id=context.source_tool_call_id if context and row else None,
                priority_status=(
                    "missing"
                    if row is None
                    else "priority_check"
                    if row.value >= 0.5 and row.drugbank_approved_percentile >= 90
                    else "not_priority"
                ),
            )
        )
    return ToxicityAxes(axes=tuple(axes))
