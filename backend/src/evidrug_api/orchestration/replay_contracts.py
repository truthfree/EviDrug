"""단독 실행 계획과 비용 보고서의 폐쇄형 계약."""

from typing import Literal, Self
from uuid import UUID

from pydantic import Field, model_validator

from evidrug_api.analysis_input.models import AnalysisInputResponse
from evidrug_api.analysis_jobs.models import AnalysisStageName, AnalysisStatus
from evidrug_api.execution_contracts.agent import AgentOutputStatus
from evidrug_api.execution_contracts.common import (
    ComponentVersion,
    ContractModel,
    ExecutionMetadata,
    ExecutionUsage,
)
from evidrug_api.orchestration.contracts import ExecutionProfile

ReplayMode = Literal["live_agent", "reuse_output", "target_from_snapshot", "continue_dta_decision"]


class ReplayError(ValueError):
    """외부 호출 없이 종료해야 하는 replay 검증 오류."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class ReplayRequest(ContractModel):
    """원본과 선행 run을 자동 선택하지 않는 명시적 요청."""

    base_analysis_id: UUID
    source_run_id: UUID
    agent_name: AnalysisStageName
    mode: ReplayMode
    upstream_run_ids: tuple[UUID, ...] = ()

    @model_validator(mode="after")
    def unique_runs(self) -> Self:
        if self.mode == "continue_dta_decision" and self.agent_name != AnalysisStageName.DECISION:
            raise ValueError("continuation must select decision")
        if len(set(self.upstream_run_ids)) != len(self.upstream_run_ids):
            raise ValueError("upstream runs must be unique")
        if self.source_run_id in self.upstream_run_ids:
            raise ValueError("source run must not be an upstream run")
        return self


class StoredRunIdentity(ContractModel):
    """재사용 출처를 hash와 원래 비용·버전으로 고정한다."""

    run_id: UUID
    agent_name: AnalysisStageName
    input_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: AgentOutputStatus
    error_code: str | None = None
    output_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    metadata: ExecutionMetadata | None = None


class ReplayManifest(ContractModel):
    """준비 시 저장하고 실행 직전에 재검증하는 불변 계획."""

    schema_version: Literal["1"] = "1"
    request: ReplayRequest
    case_input: AnalysisInputResponse
    case_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source: StoredRunIdentity
    upstream: tuple[StoredRunIdentity, ...] = ()
    runtime_versions: tuple[ComponentVersion, ...] = ()
    profile: ExecutionProfile = Field(
        default_factory=lambda: ExecutionProfile(
            profile_id="agent-replay", policy_version="single-agent-v1"
        )
    )


class ReplayReport(ContractModel):
    """새 사용량과 참조 원본의 과거 사용량을 합산하지 않는다."""

    analysis_id: UUID
    status: AnalysisStatus
    mode: ReplayMode
    agent_name: AnalysisStageName
    base_analysis_id: UUID
    source_run_id: UUID
    new_run_id: UUID | None = None
    error_code: str | None = None
    new_usage: ExecutionUsage | None = None
    original_source_usage: ExecutionUsage | None = None
    reused_runs: tuple[StoredRunIdentity, ...] = ()
    uncalled_agents: tuple[AnalysisStageName, ...] = ()


class ContinuationStageReport(ContractModel):
    agent_name: AnalysisStageName
    run_id: UUID
    status: AgentOutputStatus | Literal["running"]
    error_code: str | None = None
    new_usage: ExecutionUsage | None = None


class ContinuationReport(ContractModel):
    analysis_id: UUID
    base_analysis_id: UUID
    status: AnalysisStatus
    mode: Literal["continue_dta_decision"] = "continue_dta_decision"
    new_runs: tuple[ContinuationStageReport, ...]
    reused_runs: tuple[StoredRunIdentity, ...]
    uncalled_agents: tuple[AnalysisStageName, ...] = (
        AnalysisStageName.TARGET_HYPOTHESIS,
        AnalysisStageName.ADMET,
    )
