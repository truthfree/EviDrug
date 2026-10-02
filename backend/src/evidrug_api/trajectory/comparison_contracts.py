"""동일 관측 위에서 ADMET 실행 정책을 비교하기 위한 고정 입력 계약."""

from enum import StrEnum
from typing import Literal, Self
from uuid import UUID

from pydantic import Field, model_validator

from evidrug_api.execution_contracts.common import ContractModel, ExecutionLimits
from evidrug_api.trajectory.decision_sources import DecisionSourceManifest


class ComparisonStrategy(StrEnum):
    BASELINE = "baseline"
    ALL_TOOLS = "all_tools"
    DECISION_RECALL = "decision_recall"
    ADAPTIVE = "adaptive"


COMPARISON_ORDER = (
    ComparisonStrategy.BASELINE,
    ComparisonStrategy.ALL_TOOLS,
    ComparisonStrategy.DECISION_RECALL,
)


class PolicyComparisonManifest(ContractModel):
    """하나의 case·두 불변 snapshot·공통 예산에서 세 정책을 비교한다."""

    schema_version: Literal["1"] = "1"
    analysis_id: UUID
    admet_snapshot_id: UUID
    ctox_snapshot_id: UUID
    decision_sources: DecisionSourceManifest | None = None
    policy_version: str = Field(min_length=1, max_length=120)
    limits: ExecutionLimits
    strategies: tuple[ComparisonStrategy, ...] = COMPARISON_ORDER

    @model_validator(mode="after")
    def validate_comparison(self) -> Self:
        if self.admet_snapshot_id == self.ctox_snapshot_id:
            raise ValueError("comparison snapshots must have distinct identities")
        if (
            self.decision_sources is not None
            and self.decision_sources.analysis_id != self.analysis_id
        ):
            raise ValueError("decision sources must belong to comparison analysis")
        if len(self.strategies) != len(COMPARISON_ORDER) or set(self.strategies) != set(
            COMPARISON_ORDER
        ):
            raise ValueError("comparison requires baseline, all_tools and decision_recall")
        if self.limits.max_tool_calls < 2 or self.limits.max_recall_depth < 1:
            raise ValueError("comparison budget must permit both tools and one recall")
        return self

    def ordered_strategies(self) -> tuple[ComparisonStrategy, ...]:
        """입력 tuple 순서와 무관한 실행 순서를 반환한다."""
        return COMPARISON_ORDER
