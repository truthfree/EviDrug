"""Agent와 도구 계약이 공유하는 값 객체."""

from typing import Self
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator


class ContractModel(BaseModel):
    """모든 실행 계약에 적용하는 폐쇄형 불변 모델."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class ArtifactReference(ContractModel):
    """본문을 반복 전달하지 않고 저장된 원본 또는 결과를 가리킨다."""

    artifact_id: UUID
    schema_name: str = Field(min_length=1, max_length=120)
    schema_version: str = Field(min_length=1, max_length=40)
    media_type: str = Field(min_length=1, max_length=120)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ExecutionError(ContractModel):
    """stack trace나 비밀정보를 포함하지 않는 공개 가능한 실행 오류."""

    code: str = Field(min_length=1, max_length=120)
    message: str = Field(min_length=1, max_length=500)
    retryable: bool


class TokenUsage(ContractModel):
    """모델 제공자가 보고한 token 사용량."""

    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_total(self) -> Self:
        """총량이 알려진 입출력 token보다 작아지는 기록을 차단한다."""

        if self.total_tokens < self.input_tokens + self.output_tokens:
            raise ValueError("total_tokens must include input_tokens and output_tokens")
        return self


class ExecutionUsage(ContractModel):
    """Agent와 도구 종류에 무관하게 비교할 수 있는 사용량."""

    token_usage: TokenUsage | None = None
    tool_calls: int = Field(default=0, ge=0)
    external_requests: int = Field(default=0, ge=0)


class ComponentVersion(ContractModel):
    """재현에 필요한 prompt, model, tool 또는 package 버전."""

    component: str = Field(min_length=1, max_length=120)
    version: str = Field(min_length=1, max_length=200)


class ExecutionMetadata(ContractModel):
    """실행 시간, 구현 버전과 자원 사용량."""

    started_at: AwareDatetime
    finished_at: AwareDatetime
    duration_ms: int = Field(ge=0)
    implementation_version: str = Field(min_length=1, max_length=200)
    components: tuple[ComponentVersion, ...] = ()
    usage: ExecutionUsage = Field(default_factory=ExecutionUsage)

    @model_validator(mode="after")
    def validate_time_range(self) -> Self:
        """서로 비교 가능한 UTC offset과 순서가 있는 시간을 요구한다."""

        if self.finished_at < self.started_at:
            raise ValueError("finished_at must not precede started_at")
        return self


class ExecutionLimits(ContractModel):
    """Agent가 스스로 늘릴 수 없는 한 run의 실행 한도."""

    timeout_seconds: int = Field(gt=0)
    max_tool_calls: int = Field(ge=0)
    max_recall_depth: int = Field(ge=0)
    max_total_tokens: int | None = Field(default=None, gt=0)
    reserved_finalization_tokens: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_reserved_tokens(self) -> Self:
        """정리용 token 예약량이 전체 예산을 넘지 않게 한다."""

        if (
            self.max_total_tokens is not None
            and self.reserved_finalization_tokens >= self.max_total_tokens
        ):
            raise ValueError("reserved_finalization_tokens must be less than max_total_tokens")
        return self


class ProviderConfidence(ContractModel):
    """공통 확률로 오해하지 않도록 값의 의미와 산출법을 함께 보존한다."""

    value: float = Field(allow_inf_nan=False)
    scale: str = Field(min_length=1, max_length=120)
    method: str = Field(min_length=1, max_length=200)
