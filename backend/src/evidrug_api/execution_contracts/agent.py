"""production Agent와 baseline adapter가 공유하는 입출력 계약."""

from enum import StrEnum
from typing import Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, Field, model_validator

from evidrug_api.analysis_input.models import AnalysisInputResponse
from evidrug_api.analysis_jobs.models import AnalysisStageName
from evidrug_api.execution_contracts.common import (
    ArtifactReference,
    ContractModel,
    ExecutionError,
    ExecutionLimits,
    ExecutionMetadata,
    ProviderConfidence,
)
from evidrug_api.execution_contracts.recall import DecisionRecallRequest


class AgentOutputStatus(StrEnum):
    """한 Agent run의 최종 상태."""

    COMPLETED = "completed"
    PARTIAL_FAILURE = "partial_failure"
    FAILED = "failed"
    SKIPPED = "skipped"


class EvidenceDirection(StrEnum):
    """주장이 최종 판단에 미치는 방향."""

    SUPPORTS = "supports"
    OPPOSES = "opposes"
    UNCERTAIN = "uncertain"


class EvidenceClaim(ContractModel):
    """원본 출처까지 추적 가능한 하나의 판단 근거."""

    claim_id: UUID
    claim_type: str = Field(min_length=1, max_length=120)
    statement: str = Field(min_length=1, max_length=1000)
    direction: EvidenceDirection
    value: float | None = Field(default=None, allow_inf_nan=False)
    unit: str | None = Field(default=None, max_length=80)
    confidence: ProviderConfidence | None = None
    source: str = Field(min_length=1, max_length=200)
    source_record_id: str | None = Field(default=None, max_length=300)
    retrieved_at: AwareDatetime
    producer_run_id: UUID


class EvidenceGap(ContractModel):
    """현재 판단에 필요한데 아직 확보하지 못한 정보."""

    gap_id: UUID
    description: str = Field(min_length=1, max_length=1000)
    required_fields: tuple[str, ...] = ()


class AgentWarning(ContractModel):
    """결과를 폐기하지는 않지만 해석 시 보존해야 하는 경고."""

    code: str = Field(min_length=1, max_length=120)
    message: str = Field(min_length=1, max_length=500)
    related_claim_ids: tuple[UUID, ...] = ()


class FollowupRequest(ContractModel):
    """특정 도구가 아닌 조사 목적을 표현하는 선언적 재분석 요청."""

    request_id: UUID
    target_agent: AnalysisStageName
    objective: str = Field(min_length=1, max_length=1000)
    required_fields: tuple[str, ...] = Field(min_length=1)
    related_claim_ids: tuple[UUID, ...] = ()
    related_gap_ids: tuple[UUID, ...] = ()

    @model_validator(mode="after")
    def reject_decision_as_target(self) -> Self:
        """Decision의 선언적 요청이 Decision 자신을 재호출하지 못하게 한다."""

        if self.target_agent is AnalysisStageName.DECISION:
            raise ValueError("followup target must be a specialist agent")
        return self


class UpstreamOutputReference(ContractModel):
    """현재 Agent가 실제로 의존하는 이전 run 결과."""

    run_id: UUID
    agent_name: AnalysisStageName
    output: ArtifactReference


class RecallFeedback(ContractModel):
    """Decision 재판단에 전달할 한 번의 전문 Agent recall 결과."""

    run_id: UUID
    request: DecisionRecallRequest
    status: Literal["succeeded", "failed"]
    reason_code: str | None = Field(default=None, max_length=120)

    @model_validator(mode="after")
    def validate_outcome(self) -> Self:
        if (self.status == "failed") != (self.reason_code is not None):
            raise ValueError("failed recall requires a reason code")
        return self


class AgentInput(ContractModel):
    """모든 Agent 구현에 전달되는 공통 입력 envelope."""

    schema_version: Literal["1"] = "1"
    analysis_id: UUID
    run_id: UUID
    agent_name: AnalysisStageName
    attempt: int = Field(ge=1)
    case_input: AnalysisInputResponse
    upstream_outputs: tuple[UpstreamOutputReference, ...] = ()
    execution_limits: ExecutionLimits
    recall_request: DecisionRecallRequest | None = None
    recall_feedback: RecallFeedback | None = None

    @model_validator(mode="after")
    def reject_self_reference(self) -> Self:
        """현재 run을 자신의 선행 결과로 참조하지 못하게 한다."""

        if any(reference.run_id == self.run_id for reference in self.upstream_outputs):
            raise ValueError("upstream_outputs must not reference the current run")
        if self.recall_request is not None and self.agent_name is not AnalysisStageName.ADMET:
            raise ValueError("recall request must target the ADMET agent")
        if self.recall_feedback is not None and self.agent_name is not AnalysisStageName.DECISION:
            raise ValueError("recall feedback must target the Decision agent")
        if (
            self.recall_request is not None or self.recall_feedback is not None
        ) and self.attempt != 2:
            raise ValueError("recall input must be the second agent call")
        return self


class AgentOutput[AgentResultT: BaseModel](ContractModel):
    """Agent별 결과를 공통 근거 및 실행 정보와 함께 반환한다."""

    schema_version: Literal["1"] = "1"
    analysis_id: UUID
    run_id: UUID
    agent_name: AnalysisStageName
    status: AgentOutputStatus
    result: AgentResultT | None = None
    evidence_claims: tuple[EvidenceClaim, ...] = ()
    gaps: tuple[EvidenceGap, ...] = ()
    warnings: tuple[AgentWarning, ...] = ()
    requested_followups: tuple[FollowupRequest, ...] = ()
    error: ExecutionError | None = None
    raw_result: ArtifactReference | None = None
    execution_metadata: ExecutionMetadata

    @model_validator(mode="after")
    def validate_status_and_references(self) -> Self:
        """상태별 payload와 내부 식별자 참조의 무결성을 검사한다."""

        if (
            self.status
            in {
                AgentOutputStatus.COMPLETED,
                AgentOutputStatus.PARTIAL_FAILURE,
            }
            and self.result is None
        ):
            raise ValueError("completed and partial_failure outputs require a result")
        if self.status is AgentOutputStatus.COMPLETED and self.error is not None:
            raise ValueError("completed output must not include an error")
        if self.status is AgentOutputStatus.PARTIAL_FAILURE and self.error is None:
            raise ValueError("partial_failure output requires an error")
        if self.status in {AgentOutputStatus.FAILED, AgentOutputStatus.SKIPPED}:
            if self.result is not None:
                raise ValueError("failed and skipped outputs must not include a result")
            if self.error is None:
                raise ValueError("failed and skipped outputs require an error")

        claim_ids = {claim.claim_id for claim in self.evidence_claims}
        gap_ids = {gap.gap_id for gap in self.gaps}
        if len(claim_ids) != len(self.evidence_claims):
            raise ValueError("evidence_claims must have unique claim_id values")
        if len(gap_ids) != len(self.gaps):
            raise ValueError("gaps must have unique gap_id values")
        if any(claim.producer_run_id != self.run_id for claim in self.evidence_claims):
            raise ValueError("evidence claim producer_run_id must match output run_id")

        referenced_claim_ids = {
            claim_id for warning in self.warnings for claim_id in warning.related_claim_ids
        }
        referenced_claim_ids.update(
            claim_id
            for request in self.requested_followups
            for claim_id in request.related_claim_ids
        )
        referenced_gap_ids = {
            gap_id for request in self.requested_followups for gap_id in request.related_gap_ids
        }
        if not referenced_claim_ids <= claim_ids:
            raise ValueError("warnings and followups must reference included evidence claims")
        if not referenced_gap_ids <= gap_ids:
            raise ValueError("followups must reference included evidence gaps")
        return self
