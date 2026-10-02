"""Dacon Responses API의 tool calling 호환성을 독립적으로 검증한다."""

import json
from typing import Literal

from openai import AsyncOpenAI
from openai.types.responses import FunctionToolParam, Response, ResponseFunctionToolCall
from pydantic import BaseModel, ConfigDict

from evidrug_api.openai_gateway.client import DaconModel, _read_quota, _read_usage
from evidrug_api.openai_gateway.models import DaconQuota, ResponseUsage

PROBE_TOOL_NAME = "record_compatibility_probe"
PROBE_MARKER = "probe-complete:7"
PROBE_INSTRUCTIONS = (
    "호환성 검증 요청이다. 첫 응답에서는 record_compatibility_probe를 정확히 한 번 호출한다. "
    "도구 결과를 받은 다음 응답에는 probe-complete:7을 포함한 짧은 문장만 반환한다."
)
PROBE_INPUT = "정수 7과 label strict-schema를 사용해 호환성 검증을 수행해 주세요."
PROBE_TOOL: FunctionToolParam = {
    "type": "function",
    "name": PROBE_TOOL_NAME,
    "description": "Responses API의 strict function calling 지원 여부를 검증한다.",
    "parameters": {
        "type": "object",
        "properties": {
            "value": {
                "type": "integer",
                "enum": [7],
                "description": "검증에 사용하는 고정 정수",
            },
            "label": {
                "type": "string",
                "enum": ["strict-schema"],
                "description": "strict schema 검증용 고정 표식",
            },
        },
        "required": ["value", "label"],
        "additionalProperties": False,
    },
    "strict": True,
}


class ToolCallingCompatibilityError(RuntimeError):
    """응답이 검증 계약을 충족하지 못했을 때 발생한다."""


class ProbeArguments(BaseModel):
    """strict schema가 강제해야 하는 고정 도구 인자."""

    model_config = ConfigDict(extra="forbid")

    value: Literal[7]
    label: Literal["strict-schema"]


class ToolCallingCompatibilityReport(BaseModel):
    """비밀값과 원본 헤더를 제외한 호환성 검증 결과."""

    status: Literal["passed"] = "passed"
    model: str
    function_call_name: str
    arguments: ProbeArguments
    final_text: str
    first_usage: ResponseUsage
    second_usage: ResponseUsage
    first_quota: DaconQuota
    second_quota: DaconQuota


async def run_tool_calling_compatibility_probe(
    sdk: AsyncOpenAI,
    model: DaconModel,
) -> ToolCallingCompatibilityReport:
    """strict function call을 실행하고 같은 응답 흐름에서 결과 해석까지 확인한다."""

    first_raw = await sdk.responses.with_raw_response.create(
        model=model,
        input=PROBE_INPUT,
        instructions=PROBE_INSTRUCTIONS,
        max_output_tokens=128,
        parallel_tool_calls=False,
        stream=False,
        tool_choice={"type": "function", "name": PROBE_TOOL_NAME},
        tools=[PROBE_TOOL],
    )
    first = first_raw.parse()
    function_call, arguments = _extract_probe_call(first)
    first_usage = _require_usage(first, "first")
    first_quota = _require_quota(_read_quota(first_raw.headers), "first")

    tool_output = json.dumps(
        {"probe_value": arguments.value, "marker": PROBE_MARKER},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    second_raw = await sdk.responses.with_raw_response.create(
        model=model,
        input=[
            {
                "type": "function_call_output",
                "call_id": function_call.call_id,
                "output": tool_output,
            }
        ],
        instructions=PROBE_INSTRUCTIONS,
        max_output_tokens=128,
        parallel_tool_calls=False,
        previous_response_id=first.id,
        stream=False,
        tool_choice="none",
        tools=[PROBE_TOOL],
    )
    second = second_raw.parse()
    final_text = second.output_text.strip()
    if PROBE_MARKER not in final_text:
        raise ToolCallingCompatibilityError("continuation response omitted the probe marker")

    return ToolCallingCompatibilityReport(
        model=second.model,
        function_call_name=function_call.name,
        arguments=arguments,
        final_text=final_text,
        first_usage=first_usage,
        second_usage=_require_usage(second, "second"),
        first_quota=first_quota,
        second_quota=_require_quota(_read_quota(second_raw.headers), "second"),
    )


def _extract_probe_call(response: Response) -> tuple[ResponseFunctionToolCall, ProbeArguments]:
    calls = [item for item in response.output if isinstance(item, ResponseFunctionToolCall)]
    if len(calls) != 1:
        raise ToolCallingCompatibilityError("first response must contain exactly one function call")
    call = calls[0]
    if call.name != PROBE_TOOL_NAME:
        raise ToolCallingCompatibilityError("first response called an unexpected function")
    try:
        arguments = ProbeArguments.model_validate_json(call.arguments)
    except ValueError as error:
        raise ToolCallingCompatibilityError(
            "function arguments violated the strict schema"
        ) from error
    return call, arguments


def _require_usage(response: Response, turn: str) -> ResponseUsage:
    usage = _read_usage(response)
    if usage is None:
        raise ToolCallingCompatibilityError(f"{turn} response omitted token usage")
    return usage


def _require_quota(quota: DaconQuota, turn: str) -> DaconQuota:
    if all(
        value is None
        for value in (
            quota.remaining_quota_tokens,
            quota.tokens_consumed,
            quota.remaining_tokens,
            quota.remaining_requests,
        )
    ):
        raise ToolCallingCompatibilityError(f"{turn} response omitted Dacon quota headers")
    return quota
