from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from openai import AsyncOpenAI
from openai.types.responses import Response, ResponseFunctionToolCall
from openai.types.responses.response_usage import ResponseUsage as SDKResponseUsage

from evidrug_api.openai_gateway.compatibility import (
    PROBE_INPUT,
    PROBE_INSTRUCTIONS,
    PROBE_MARKER,
    PROBE_TOOL,
    PROBE_TOOL_NAME,
    ToolCallingCompatibilityError,
    run_tool_calling_compatibility_probe,
)


def _response(
    *,
    response_id: str,
    output: list[ResponseFunctionToolCall],
    output_text: str,
) -> Response:
    response = MagicMock(spec=Response)
    response.id = response_id
    response.model = "gpt-5.6-luna"
    response.output = output
    response.output_text = output_text
    response.usage = SDKResponseUsage.model_construct(
        input_tokens=11,
        output_tokens=7,
        total_tokens=18,
    )
    return cast(Response, response)


def _raw_response(response: Response) -> MagicMock:
    raw_response = MagicMock()
    raw_response.parse.return_value = response
    raw_response.headers = {
        "x-team-remaining-quota-tokens": "29999982",
        "x-team-tokens-consumed": "18",
        "x-team-remaining-tokens": "499982",
        "x-team-remaining-requests": "299",
    }
    return raw_response


@pytest.mark.asyncio
async def test_probe_uses_strict_tool_and_continues_with_bound_output() -> None:
    function_call = ResponseFunctionToolCall.model_construct(
        arguments='{"value":7,"label":"strict-schema"}',
        call_id="call_probe",
        name=PROBE_TOOL_NAME,
        type="function_call",
        status="completed",
    )
    first = _response(response_id="resp_first", output=[function_call], output_text="")
    second = _response(
        response_id="resp_second",
        output=[],
        output_text=f"검증 완료: {PROBE_MARKER}",
    )
    sdk = MagicMock(spec=AsyncOpenAI)
    create = AsyncMock(side_effect=[_raw_response(first), _raw_response(second)])
    sdk.responses.with_raw_response.create = create

    report = await run_tool_calling_compatibility_probe(
        cast(AsyncOpenAI, sdk),
        "gpt-5.6-luna",
    )

    first_request = create.await_args_list[0].kwargs
    assert first_request == {
        "model": "gpt-5.6-luna",
        "input": PROBE_INPUT,
        "instructions": PROBE_INSTRUCTIONS,
        "max_output_tokens": 128,
        "parallel_tool_calls": False,
        "stream": False,
        "tool_choice": {"type": "function", "name": PROBE_TOOL_NAME},
        "tools": [PROBE_TOOL],
    }
    second_request = create.await_args_list[1].kwargs
    assert second_request["previous_response_id"] == "resp_first"
    assert second_request["instructions"] == PROBE_INSTRUCTIONS
    assert second_request["tool_choice"] == "none"
    assert second_request["input"] == [
        {
            "type": "function_call_output",
            "call_id": "call_probe",
            "output": '{"probe_value":7,"marker":"probe-complete:7"}',
        }
    ]
    assert report.status == "passed"
    assert report.arguments.value == 7
    assert report.first_usage.total_tokens == 18
    assert report.second_quota.remaining_requests == 299
    assert report.model_dump(mode="json")["first_usage"]["total_tokens"] == 18


@pytest.mark.asyncio
async def test_probe_rejects_arguments_outside_strict_schema() -> None:
    function_call = ResponseFunctionToolCall.model_construct(
        arguments='{"value":8,"label":"strict-schema","extra":true}',
        call_id="call_probe",
        name=PROBE_TOOL_NAME,
        type="function_call",
        status="completed",
    )
    first = _response(response_id="resp_first", output=[function_call], output_text="")
    sdk = MagicMock(spec=AsyncOpenAI)
    create = AsyncMock(return_value=_raw_response(first))
    sdk.responses.with_raw_response.create = create

    with pytest.raises(ToolCallingCompatibilityError, match="strict schema"):
        await run_tool_calling_compatibility_probe(
            cast(AsyncOpenAI, sdk),
            "gpt-5.6-luna",
        )

    create.assert_awaited_once()


@pytest.mark.asyncio
async def test_probe_rejects_missing_quota_headers() -> None:
    function_call = ResponseFunctionToolCall.model_construct(
        arguments='{"value":7,"label":"strict-schema"}',
        call_id="call_probe",
        name=PROBE_TOOL_NAME,
        type="function_call",
        status="completed",
    )
    first = _response(response_id="resp_first", output=[function_call], output_text="")
    raw_response = _raw_response(first)
    raw_response.headers = {}
    sdk = MagicMock(spec=AsyncOpenAI)
    create = AsyncMock(return_value=raw_response)
    sdk.responses.with_raw_response.create = create

    with pytest.raises(ToolCallingCompatibilityError, match="quota headers"):
        await run_tool_calling_compatibility_probe(
            cast(AsyncOpenAI, sdk),
            "gpt-5.6-luna",
        )
