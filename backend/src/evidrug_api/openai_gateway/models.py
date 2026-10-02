"""OpenAI 응답에서 서비스가 실제로 사용하는 값만 정의한다."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ResponseUsage:
    """한 번의 모델 호출이 사용한 토큰 수."""

    input_tokens: int
    output_tokens: int
    total_tokens: int


@dataclass(frozen=True, slots=True)
class DaconQuota:
    """Dacon 게이트웨이가 응답 헤더로 알려주는 잔여 사용량."""

    remaining_quota_tokens: int | None
    tokens_consumed: int | None
    remaining_tokens: int | None
    remaining_requests: int | None


@dataclass(frozen=True, slots=True)
class GeneratedText:
    """상위 에이전트가 모델 응답을 사용하는 데 필요한 결과."""

    response_id: str
    model: str
    text: str
    usage: ResponseUsage | None
    quota: DaconQuota
    status: str = "completed"
    refused: bool = False
    incomplete_reason: str | None = None
