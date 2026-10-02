from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from openai import AsyncOpenAI
from openai.types.responses import Response
from openai.types.responses.response_usage import ResponseUsage as SDKResponseUsage
from pydantic import SecretStr

from evidrug_api.config import Settings
from evidrug_api.openai_gateway.client import (
    DaconOpenAIClient,
    OpenAIConfigurationError,
    _read_quota,
    build_dacon_openai_client,
)
from evidrug_api.orchestration.reasoning import Interpretation


def test_client_requires_an_api_key() -> None:
    with pytest.raises(OpenAIConfigurationError, match="EVIDRUG_OPENAI_API_KEY"):
        build_dacon_openai_client(Settings(openai_api_key=None))


def test_sol_is_the_default_model() -> None:
    assert Settings().openai_model == "gpt-5.6-sol"


@pytest.mark.asyncio
async def test_sdk_client_uses_dacon_url_and_api_key_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for variable_name in (
        "ALL_PROXY",
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "all_proxy",
        "https_proxy",
        "http_proxy",
    ):
        monkeypatch.delenv(variable_name, raising=False)

    client = build_dacon_openai_client(Settings(openai_api_key=SecretStr("test-api-key")))

    assert str(client._sdk.base_url) == (
        "https://dacon-apim-hackathon-0903.azure-api.net/hackathon/openai/v1/"
    )
    assert client._sdk.default_headers["api-key"] == "test-api-key"
    await client.close()


@pytest.mark.asyncio
async def test_generate_text_returns_usage_and_quota() -> None:
    response = MagicMock(spec=Response)
    response.id = "resp_test"
    response.status = "completed"
    response.output = []
    response.incomplete_details = None
    response.model = "gpt-5.6-luna"
    response.output_text = "후보 질환입니다."
    response.usage = SDKResponseUsage.model_construct(
        input_tokens=11,
        output_tokens=7,
        total_tokens=18,
    )

    raw_response = MagicMock()
    raw_response.parse.return_value = response
    raw_response.headers = {
        "x-team-remaining-quota-tokens": "29999982",
        "x-team-tokens-consumed": "18",
        "x-team-remaining-tokens": "499982",
        "x-team-remaining-requests": "299",
    }

    sdk = MagicMock(spec=AsyncOpenAI)
    create = AsyncMock(return_value=raw_response)
    sdk.responses.with_raw_response.create = create
    client = DaconOpenAIClient(
        sdk=cast(AsyncOpenAI, sdk),
        default_model="gpt-5.6-luna",
    )

    result = await client.generate_text(
        "입력 질환을 해석해 주세요.",
        instructions="한국어로 답하세요.",
        max_output_tokens=300,
    )

    create.assert_awaited_once_with(
        model="gpt-5.6-luna",
        input="입력 질환을 해석해 주세요.",
        instructions="한국어로 답하세요.",
        max_output_tokens=300,
        stream=False,
    )
    assert result.response_id == "resp_test"
    assert result.status == "completed"
    assert result.text == "후보 질환입니다."
    assert result.usage is not None
    assert result.usage.total_tokens == 18
    assert result.quota.remaining_quota_tokens == 29_999_982
    assert result.quota.remaining_requests == 299


def test_missing_or_invalid_quota_headers_are_safe() -> None:
    quota = _read_quota(
        {
            "x-team-tokens-consumed": "not-a-number",
            "x-team-remaining-requests": "12",
        }
    )

    assert quota.remaining_quota_tokens is None
    assert quota.tokens_consumed is None
    assert quota.remaining_tokens is None
    assert quota.remaining_requests == 12


@pytest.mark.asyncio
async def test_generate_text_rejects_invalid_input_before_request() -> None:
    sdk = MagicMock(spec=AsyncOpenAI)
    create = AsyncMock()
    sdk.responses.with_raw_response.create = create
    client = DaconOpenAIClient(
        sdk=cast(AsyncOpenAI, sdk),
        default_model="gpt-5.6-luna",
    )

    with pytest.raises(ValueError, match="must not be empty"):
        await client.generate_text("   ")

    create.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("refused", [False, True])
async def test_strict_schema_request_uses_actual_responses_sdk_contract(refused: bool) -> None:
    response = Response.model_construct(
        id="resp_schema",
        model="gpt-5.6-sol",
        status="completed",
        output=[],
        usage=None,
        incomplete_details=None,
    )
    if refused:
        from openai.types.responses import ResponseOutputMessage
        from openai.types.responses.response_output_refusal import ResponseOutputRefusal

        response.output = [
            ResponseOutputMessage(
                id="msg_test",
                role="assistant",
                status="completed",
                type="message",
                content=[ResponseOutputRefusal(type="refusal", refusal="synthetic refusal")],
            )
        ]
    raw = MagicMock()
    raw.parse.return_value = response
    raw.headers = {}
    sdk = MagicMock(spec=AsyncOpenAI)
    create = AsyncMock(return_value=raw)
    sdk.responses.with_raw_response.create = create
    client = DaconOpenAIClient(cast(AsyncOpenAI, sdk), "gpt-5.6-sol")
    result = await client.generate_text("synthetic evidence", output_schema=Interpretation)
    create.assert_awaited_once()
    format_config = create.call_args.kwargs["text"]["format"]
    assert format_config["type"] == "json_schema" and format_config["strict"] is True
    assert format_config["schema"] == Interpretation.model_json_schema()
    assert format_config["schema"]["additionalProperties"] is False
    assert set(format_config["schema"]["required"]) == {
        "summary",
        "used_evidence_ids",
        "limitations",
    }
    assert result.refused is refused
    assert result.incomplete_reason is None
