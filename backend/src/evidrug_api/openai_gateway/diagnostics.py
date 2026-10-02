"""모델 호출 실패를 원문·URL·키 없이 분류하는 저장용 진단 코드."""

from typing import Literal

from openai import APIConnectionError, APIStatusError, APITimeoutError, RateLimitError

ModelCallDiagnostic = Literal[
    "model_call_timeout",
    "model_rate_limited",
    "model_connection_failed",
    "model_server_error",
    "model_request_rejected",
    "model_call_unknown",
]


def model_call_diagnostic(error: BaseException) -> ModelCallDiagnostic:
    """재시도 정책이 아니라 실패 관측 분류만 제공한다."""
    if isinstance(error, APITimeoutError):
        return "model_call_timeout"
    if isinstance(error, RateLimitError):
        return "model_rate_limited"
    if isinstance(error, APIConnectionError):
        return "model_connection_failed"
    if isinstance(error, APIStatusError):
        if error.status_code >= 500:
            return "model_server_error"
        return "model_request_rejected"
    return "model_call_unknown"
