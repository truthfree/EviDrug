"""전문 Agent로 전달하는 Decision evidence-gap 요청 계약."""

from enum import StrEnum
from typing import Literal, Self

from pydantic import Field, model_validator

from evidrug_api.execution_contracts.common import ContractModel


class RecallEvidenceGapKind(StrEnum):
    CARDIAC_ION_CHANNEL = "cardiac_ion_channel_evidence"


class RecallPriority(StrEnum):
    NORMAL = "normal"
    HIGH = "high"


class DecisionRecallRequest(ContractModel):
    """Decision이 특정 도구 없이 요청하는 폐쇄형 추가 근거 계약."""

    schema_version: Literal["1"] = "1"
    gap_kind: Literal[RecallEvidenceGapKind.CARDIAC_ION_CHANNEL]
    objective: str = Field(min_length=1, max_length=500)
    reason_code: Literal[
        "baseline_herg_signal", "baseline_herg_uncertainty", "cardiac_evidence_gap"
    ]
    required_endpoints: tuple[str, ...] = ("herg", "nav1_5", "cav1_2")
    priority: RecallPriority = RecallPriority.NORMAL

    @model_validator(mode="after")
    def exact_supported_panel(self) -> Self:
        if (
            set(self.required_endpoints) != {"herg", "nav1_5", "cav1_2"}
            or len(self.required_endpoints) != 3
        ):
            raise ValueError("cardiac recall requires the complete channel panel")
        return self
