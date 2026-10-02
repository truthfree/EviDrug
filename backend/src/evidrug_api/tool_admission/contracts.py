"""LLM 요청과 분리하여 서버가 생성하는 실행 설정."""

from typing import Self
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from evidrug_api.analysis_jobs.models import AnalysisStageName
from evidrug_api.execution_contracts.common import ContractModel


class ToolPin(ContractModel):
    tool_id: str = Field(min_length=1, max_length=120)
    version: str = Field(min_length=1, max_length=200)


class RunContext(ContractModel):
    """인증된 orchestration이 전달한다. LLM 인수에서 만들지 않는다."""

    analysis_id: UUID
    run_id: UUID
    agent: AnalysisStageName


class ToolRunPolicy(ContractModel):
    """한 run에서 불변인 도구 권한과 예산 snapshot. 전체 ExecutionProfile은 아니다."""

    context: RunContext
    policy_version: str = Field(min_length=1, max_length=120)
    allowed_tools: tuple[ToolPin, ...] = ()
    max_tool_calls: int = Field(ge=0)
    deadline: AwareDatetime
    call_timeout_seconds: float = Field(gt=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def unique_tools(self) -> Self:
        if len({pin.tool_id for pin in self.allowed_tools}) != len(self.allowed_tools):
            raise ValueError("each tool must have exactly one pinned version")
        return self
