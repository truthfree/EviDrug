"""원격 오류 본문을 저장하지 않는 모델 실패 분류."""

from typing import Any, cast

import httpx
from openai import APIConnectionError, APITimeoutError, InternalServerError, RateLimitError

from evidrug_api.openai_gateway.diagnostics import model_call_diagnostic


def test_model_call_diagnostics_expose_only_stable_categories() -> None:
    request = httpx.Request("POST", "https://secret.invalid/responses")
    assert (
        model_call_diagnostic(APITimeoutError(request=cast(Any, request))) == "model_call_timeout"
    )
    assert (
        model_call_diagnostic(APIConnectionError(request=cast(Any, request)))
        == "model_connection_failed"
    )
    rate_limit = RateLimitError(
        "private server response",
        response=cast(Any, httpx.Response(429, request=request)),
        body=None,
    )
    assert model_call_diagnostic(rate_limit) == "model_rate_limited"
    server_error = InternalServerError(
        "private server response",
        response=cast(Any, httpx.Response(500, request=request)),
        body=None,
    )
    assert model_call_diagnostic(server_error) == "model_server_error"
    assert model_call_diagnostic(RuntimeError("private error")) == "model_call_unknown"
