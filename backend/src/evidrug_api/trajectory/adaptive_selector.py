"""고정 capability·권한·자원 상한에서 evidence gap의 다음 도구를 결정한다."""

from enum import StrEnum
from typing import Literal, Self

from pydantic import Field, field_validator, model_validator

from evidrug_api.analysis_jobs.models import AnalysisStageName
from evidrug_api.execution_contracts.common import ContractModel
from evidrug_api.tool_admission.capabilities import (
    EvidenceGap,
    ResourceClass,
    ToolCapability,
    match_capabilities,
    trajectory_candidate_from_match,
)
from evidrug_api.tool_admission.contracts import ToolPin
from evidrug_api.trajectory.contracts import TrajectoryActionCandidate


class SelectionReason(StrEnum):
    SELECTED = "selected"
    LOWER_RANKED = "lower_ranked"
    NOT_ALLOWED = "not_allowed"
    VERSION_NOT_PINNED = "version_not_pinned"
    COST_LIMIT_EXCEEDED = "cost_limit_exceeded"
    LATENCY_LIMIT_EXCEEDED = "latency_limit_exceeded"
    TOOL_BUDGET_EXHAUSTED = "tool_budget_exhausted"
    NO_MATCH = "no_match"


_RESOURCE_RANK = {
    ResourceClass.LOW: 0,
    ResourceClass.MEDIUM: 1,
    ResourceClass.HIGH: 2,
}


class AdaptiveSelectorInput(ContractModel):
    schema_version: Literal["1"] = "1"
    policy_version: str = Field(min_length=1, max_length=120)
    gap: EvidenceGap
    agent: AnalysisStageName
    capabilities: tuple[ToolCapability, ...]
    allowed_tools: tuple[ToolPin, ...]
    available_inputs: tuple[str, ...]
    remaining_tool_calls: int = Field(ge=0)
    max_cost_class: ResourceClass
    max_latency_class: ResourceClass

    @field_validator("capabilities")
    @classmethod
    def canonical_capabilities(
        cls, values: tuple[ToolCapability, ...]
    ) -> tuple[ToolCapability, ...]:
        return tuple(
            sorted(values, key=lambda item: (item.tool_id, item.tool_version, item.capability_id))
        )

    @field_validator("allowed_tools")
    @classmethod
    def canonical_pins(cls, values: tuple[ToolPin, ...]) -> tuple[ToolPin, ...]:
        return tuple(sorted(values, key=lambda item: (item.tool_id, item.version)))

    @field_validator("available_inputs")
    @classmethod
    def canonical_inputs(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)):
            raise ValueError("selector available input names must be unique")
        return tuple(sorted(values))

    @model_validator(mode="after")
    def unique_catalog_and_pins(self) -> Self:
        if len({item.capability_id for item in self.capabilities}) != len(self.capabilities):
            raise ValueError("selector capability IDs must be unique")
        if len({item.tool_id for item in self.allowed_tools}) != len(self.allowed_tools):
            raise ValueError("selector must pin one version per tool")
        return self


class AdaptiveCandidate(ContractModel):
    capability_id: str
    tool_id: str
    tool_version: str
    endpoint_id: str | None
    reason_code: str
    selected: bool
    cost_class: ResourceClass
    latency_class: ResourceClass


class AdaptiveSelection(ContractModel):
    schema_version: Literal["1"] = "1"
    policy_version: str
    gap_id: str
    selected_action: TrajectoryActionCandidate | None
    reason_code: SelectionReason
    candidates: tuple[AdaptiveCandidate, ...]


def select_next_tool(config: AdaptiveSelectorInput) -> AdaptiveSelection:
    """matcher의 과학적 제외 사유를 보존하고 비용·latency·ID 순으로 tie-break한다."""
    catalog = tuple(
        sorted(
            config.capabilities,
            key=lambda item: (item.tool_id, item.tool_version, item.capability_id),
        )
    )
    matches = match_capabilities(
        config.gap,
        catalog,
        agent=config.agent,
        available_inputs=frozenset(config.available_inputs),
    )
    pins = {item.tool_id: item.version for item in config.allowed_tools}
    reasons: dict[str, str] = {}
    eligible: list[ToolCapability] = []
    for capability, match in zip(catalog, matches, strict=True):
        reason = match.reason_code.value if not match.matched else None
        if reason is None and capability.tool_id not in pins:
            reason = SelectionReason.NOT_ALLOWED.value
        if reason is None and capability.tool_version != pins[capability.tool_id]:
            reason = SelectionReason.VERSION_NOT_PINNED.value
        if reason is None and config.remaining_tool_calls == 0:
            reason = SelectionReason.TOOL_BUDGET_EXHAUSTED.value
        if (
            reason is None
            and _RESOURCE_RANK[capability.cost_class] > _RESOURCE_RANK[config.max_cost_class]
        ):
            reason = SelectionReason.COST_LIMIT_EXCEEDED.value
        if (
            reason is None
            and _RESOURCE_RANK[capability.latency_class] > _RESOURCE_RANK[config.max_latency_class]
        ):
            reason = SelectionReason.LATENCY_LIMIT_EXCEEDED.value
        if reason is None:
            eligible.append(capability)
        reasons[capability.capability_id] = reason or SelectionReason.LOWER_RANKED.value
    eligible.sort(
        key=lambda item: (
            _RESOURCE_RANK[item.cost_class],
            _RESOURCE_RANK[item.latency_class],
            item.tool_id,
            item.tool_version,
            item.capability_id,
        )
    )
    chosen = eligible[0] if eligible else None
    if chosen is not None:
        reasons[chosen.capability_id] = SelectionReason.SELECTED.value
    candidates = tuple(
        AdaptiveCandidate(
            capability_id=capability.capability_id,
            tool_id=capability.tool_id,
            tool_version=capability.tool_version,
            endpoint_id=match.endpoint_id,
            reason_code=reasons[capability.capability_id],
            selected=capability is chosen,
            cost_class=capability.cost_class,
            latency_class=capability.latency_class,
        )
        for capability, match in zip(catalog, matches, strict=True)
    )
    selected_match = next(
        (
            match
            for match in matches
            if chosen is not None and match.capability_id == chosen.capability_id
        ),
        None,
    )
    return AdaptiveSelection(
        policy_version=config.policy_version,
        gap_id=str(config.gap.gap_id),
        selected_action=trajectory_candidate_from_match(config.gap, chosen, selected_match)
        if chosen is not None and selected_match is not None
        else None,
        reason_code=SelectionReason.SELECTED if chosen else SelectionReason.NO_MATCH,
        candidates=candidates,
    )
