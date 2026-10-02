"""전문 해석의 strict 요청·안전한 진단·단일 호출 계약을 검증한다."""

import json
from typing import cast

import httpx2 as httpx
import pytest
from openai import BadRequestError

from evidrug_api.execution_contracts.common import ExecutionLimits
from evidrug_api.openai_gateway.client import DaconOpenAIClient
from evidrug_api.openai_gateway.models import DaconQuota, GeneratedText, ResponseUsage
from evidrug_api.orchestration.reasoning import (
    ADMET_OUTPUT_CONTRACT_VERSION,
    DaconSpecialistReasoner,
    Interpretation,
)


class SchemaClient:
    def __init__(self, text: str, *, refused: bool = False, incomplete: bool = False) -> None:
        self.text, self.refused, self.incomplete = text, refused, incomplete
        self.calls = 0
        self.schema: type[Interpretation] | None = None

    async def generate_text(
        self,
        prompt: str,
        *,
        instructions: str,
        max_output_tokens: int,
        output_schema: type[Interpretation] | None = None,
    ) -> GeneratedText:
        self.calls += 1
        self.schema = output_schema
        return GeneratedText(
            "test",
            "gpt-5.6-sol",
            self.text,
            ResponseUsage(30, 40, 70),
            DaconQuota(None, None, None, None),
            status="incomplete" if self.incomplete else "completed",
            refused=self.refused,
            incomplete_reason="max_output_tokens" if self.incomplete else None,
        )


LIMITS = ExecutionLimits(timeout_seconds=30, max_tool_calls=1, max_recall_depth=0)
DOCUMENT = json.dumps(
    {
        "summary": "원 관측의 해석입니다.",
        "used_evidence_ids": ["DILI"],
        "limitations": ["실험 근거가 아닙니다."],
    },
    ensure_ascii=False,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("task", ["ADME", "TOXICITY", "DTA"])
async def test_only_admet_domains_request_strict_schema(task: str) -> None:
    client = SchemaClient(DOCUMENT)
    outcome = await DaconSpecialistReasoner(cast(DaconOpenAIClient, client)).interpret(
        task,
        {"DILI": 0.98},
        LIMITS,
    )
    assert outcome.interpretation is not None
    assert client.calls == outcome.external_requests == 1
    assert (client.schema is Interpretation) == (task in ("ADME", "TOXICITY"))
    assert outcome.output_contract_version == (
        ADMET_OUTPUT_CONTRACT_VERSION if task in ("ADME", "TOXICITY") else None
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    [
        "",
        "sensitive-synthetic-not-json",
        '{"summary": "민감 합성 문자열",}',
        DOCUMENT + DOCUMENT,
        "설명\n" + DOCUMENT,
        '\x60\x60\x60json\n{"summary": }\n\x60\x60\x60',
    ],
)
async def test_invalid_json_saves_only_lengths_and_positions_without_retry(text: str) -> None:
    client = SchemaClient(text)
    outcome = await DaconSpecialistReasoner(cast(DaconOpenAIClient, client)).interpret(
        "ADME",
        {"DILI": 0.98},
        LIMITS,
    )
    assert outcome.diagnostic_code == "reasoning_json_invalid"
    assert outcome.interpretation is None
    assert client.calls == outcome.external_requests == 1
    assert outcome.usage is not None and outcome.usage.total_tokens == 70
    detail = outcome.json_parse_diagnostic
    assert detail is not None and detail.byte_length == len(text.encode())
    assert detail.character_count == len(text) and detail.line >= 1 and detail.column >= 1
    assert "민감 합성 문자열" not in outcome.diagnostic_message
    assert "sensitive-synthetic-not-json" not in outcome.diagnostic_message
    assert "응답 원문은 저장하지 않습니다" in outcome.diagnostic_message


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "refused,incomplete,expected",
    [
        (True, False, "reasoning_response_refused"),
        (False, True, "reasoning_response_incomplete"),
    ],
)
async def test_refusal_and_token_limit_are_not_misreported_as_json_error(
    refused: bool,
    incomplete: bool,
    expected: str,
) -> None:
    client = SchemaClient("", refused=refused, incomplete=incomplete)
    outcome = await DaconSpecialistReasoner(cast(DaconOpenAIClient, client)).interpret(
        "TOXICITY",
        {"DILI": 0.98},
        LIMITS,
    )
    assert outcome.diagnostic_code == expected and outcome.json_parse_diagnostic is None
    assert client.calls == outcome.external_requests == 1
    assert outcome.usage is not None and outcome.usage.total_tokens == 70
    if incomplete:
        assert "max_output_tokens" in outcome.diagnostic_message


@pytest.mark.asyncio
async def test_unsupported_gateway_has_no_hidden_fallback_or_second_call() -> None:
    class UnsupportedClient(SchemaClient):
        async def generate_text(
            self,
            prompt: str,
            *,
            instructions: str,
            max_output_tokens: int,
            output_schema: type[Interpretation] | None = None,
        ) -> GeneratedText:
            self.calls += 1
            assert output_schema is Interpretation
            raise BadRequestError(
                "synthetic unsupported text.format",
                response=httpx.Response(
                    400, request=httpx.Request("POST", "https://example.invalid")
                ),
                body=None,
            )

    client = UnsupportedClient("")
    outcome = await DaconSpecialistReasoner(cast(DaconOpenAIClient, client)).interpret(
        "ADME",
        {"DILI": 0.98},
        LIMITS,
    )
    assert client.calls == outcome.external_requests == 1
    assert outcome.error_code == "reasoning_unavailable"
    assert outcome.diagnostic_code == "model_request_rejected"
    assert outcome.output_contract_version == ADMET_OUTPUT_CONTRACT_VERSION
