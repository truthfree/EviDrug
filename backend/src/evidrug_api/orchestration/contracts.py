"""고정 DAG orchestration이 사용하는 실행 계약."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, Field

from evidrug_api.execution_contracts.agent import AgentInput, AgentOutput
from evidrug_api.execution_contracts.common import ContractModel, ExecutionLimits
from evidrug_api.tool_admission.capabilities import CapabilityMatch


class OrchestrationStatus(StrEnum):
    """한 분석 orchestration 실행의 영속 상태."""

    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class TraceEventType(StrEnum):
    """평가와 장애 분석을 위해 추가만 하는 실행 event."""

    ANALYSIS_STARTED = "analysis_started"
    STAGE_STARTED = "stage_started"
    STAGE_COMPLETED = "stage_completed"
    STAGE_PARTIAL_FAILURE = "stage_partial_failure"
    STAGE_FAILED = "stage_failed"
    STAGE_SKIPPED = "stage_skipped"
    ANALYSIS_COMPLETED = "analysis_completed"
    ANALYSIS_PARTIAL_FAILURE = "analysis_partial_failure"
    ANALYSIS_FAILED = "analysis_failed"
    LEASE_EXPIRED = "lease_expired"


class ExecutionProfile(ContractModel):
    """MVP 실행 시작 시 고정하는 orchestration 정책 snapshot."""

    schema_version: Literal["1"] = "1"
    profile_id: str = Field(default="orchestration-mvp", min_length=1, max_length=120)
    policy_version: str = Field(default="fixed-dag-v1", min_length=1, max_length=120)
    stage_limits: ExecutionLimits = Field(
        default_factory=lambda: ExecutionLimits(
            timeout_seconds=300,
            max_tool_calls=1,
            max_recall_depth=0,
        )
    )


@dataclass(frozen=True)
class AgentExecutionResult:
    """공통 AgentOutput과 DTA 입력 준비 여부를 executor에서 전달한다."""

    output: AgentOutput[BaseModel]
    provides_dta_input: bool = False
    selection_matches: tuple[CapabilityMatch, ...] = ()
    selection_tool_call_id: UUID | None = None
    selection_replay_snapshot_id: UUID | None = None


class AgentExecutor(Protocol):
    """실제 Agent와 scripted test double이 공유하는 주입 경계."""

    async def execute(self, agent_input: AgentInput) -> AgentExecutionResult:
        """한 Agent run을 수행하되 분석 상태를 직접 변경하지 않는다."""
        ...


class AgentExecutorUnavailable(RuntimeError):
    """실제 Agent adapter가 아직 worker에 구성되지 않았다."""


class UnavailableAgentExecutor:
    """미구성 상태를 가짜 성공으로 만들지 않는 운영 기본 executor."""

    async def execute(self, agent_input: AgentInput) -> AgentExecutionResult:
        raise AgentExecutorUnavailable(f"{agent_input.agent_name} executor is not configured")
