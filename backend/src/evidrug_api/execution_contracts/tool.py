"""전문 Agent와 공통 도구 실행 계층 사이의 계약."""

from enum import StrEnum
from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, Field, SerializeAsAny, model_validator

from evidrug_api.execution_contracts.common import (
    ArtifactReference,
    ContractModel,
    ExecutionError,
    ExecutionMetadata,
)


class ToolAdmissionDecision(StrEnum):
    """실행 계층의 기계적 요청 승인 결과."""

    APPROVED = "approved"
    REJECTED = "rejected"


class ToolObservationStatus(StrEnum):
    """승인 이후 도구 실행을 포함한 최종 관측 상태."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    REJECTED = "rejected"


class ToolAdmission(ContractModel):
    """권한, 예산, 중복과 입력 schema 검사 결과."""

    decision: ToolAdmissionDecision
    policy_version: str = Field(min_length=1, max_length=120)
    reason_code: str | None = Field(default=None, max_length=120)

    @model_validator(mode="after")
    def validate_reason(self) -> Self:
        """거부에는 고정 reason code를 요구한다."""

        if self.decision is ToolAdmissionDecision.REJECTED and self.reason_code is None:
            raise ValueError("rejected admission requires reason_code")
        if self.decision is ToolAdmissionDecision.APPROVED and self.reason_code is not None:
            raise ValueError("approved admission must not include reason_code")
        return self


class ToolRequest[ToolArgumentsT: BaseModel](ContractModel):
    """Agent가 선택한 도구 실행 목적과 타입 지정 인수."""

    schema_version: Literal["1"] = "1"
    request_id: UUID
    run_id: UUID
    tool_id: str = Field(min_length=1, max_length=120)
    tool_version: str = Field(min_length=1, max_length=200)
    arguments: ToolArgumentsT
    objective: str = Field(min_length=1, max_length=1000)
    related_claim_ids: tuple[UUID, ...] = ()
    related_gap_ids: tuple[UUID, ...] = ()


class ToolObservation[ToolResultT: BaseModel](ContractModel):
    """실행 계층이 승인 여부와 도구 관측을 Agent에 반환한다."""

    schema_version: Literal["1"] = "1"
    tool_call_id: UUID
    request_id: UUID
    run_id: UUID
    status: ToolObservationStatus
    admission: ToolAdmission
    result: SerializeAsAny[ToolResultT] | None = None
    raw_result: ArtifactReference | None = None
    error: ExecutionError | None = None
    execution_metadata: ExecutionMetadata

    @model_validator(mode="after")
    def validate_outcome(self) -> Self:
        """승인 결정과 실행 결과 조합을 일관되게 유지한다."""

        if self.status is ToolObservationStatus.SUCCEEDED:
            if self.admission.decision is not ToolAdmissionDecision.APPROVED:
                raise ValueError("succeeded observation requires approved admission")
            if self.result is None or self.error is not None:
                raise ValueError("succeeded observation requires result and no error")
        elif self.status is ToolObservationStatus.FAILED:
            if self.admission.decision is not ToolAdmissionDecision.APPROVED:
                raise ValueError("failed observation requires approved admission")
            if self.result is not None or self.error is None:
                raise ValueError("failed observation requires error and no result")
        else:
            if self.admission.decision is not ToolAdmissionDecision.REJECTED:
                raise ValueError("rejected observation requires rejected admission")
            if self.result is not None or self.error is None:
                raise ValueError("rejected observation requires error and no result")
        return self
