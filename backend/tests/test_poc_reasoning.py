"""LLM을 호출하지 않고 비용 가드·실패 사용량·응답 상태를 검증한다."""

import json
from dataclasses import replace
from typing import cast

import pytest

from evidrug_api.execution_contracts.common import ExecutionLimits
from evidrug_api.openai_gateway.client import DaconOpenAIClient
from evidrug_api.openai_gateway.models import DaconQuota, GeneratedText, ResponseUsage
from evidrug_api.orchestration.reasoning import DaconSpecialistReasoner, Interpretation


class Client:
    def __init__(self, mode: str) -> None:
        self.mode, self.calls = mode, 0

    async def generate_text(
        self,
        prompt: str,
        *,
        instructions: str,
        max_output_tokens: int,
        output_schema: type[Interpretation] | None = None,
    ) -> GeneratedText:
        self.calls += 1
        assert max_output_tokens == 4096
        payload = json.loads(prompt)
        assert (output_schema is Interpretation) == (payload["task"] in ("ADME", "TOXICITY"))
        assert payload["allowed_evidence_ids"] == list(payload["evidence"])
        assert payload["observation_evidence_ids"] == [
            key for key in payload["evidence"] if key != "model_context"
        ]
        assert "Copy used_evidence_ids exactly from allowed_evidence_ids" in instructions
        if self.mode == "network":
            raise RuntimeError("synthetic network failure")
        output = {
            "summary": "합성 관측 해석",
            "used_evidence_ids": ["AMES"],
            "limitations": ["실험 근거 아님"],
        }
        if self.mode == "citation":
            output["used_evidence_ids"] = ["unknown"]
        if self.mode == "metadata_only":
            output["used_evidence_ids"] = ["model_context"]
        if self.mode == "schema":
            output["summary"] = ""
        if self.mode == "precision":
            output["summary"] = "합성 관측값은 0.8158712983131409입니다."
        return GeneratedText(
            "test",
            "gpt-5.6-luna",
            "not json" if self.mode == "malformed" else json.dumps(output),
            ResponseUsage(20, 10, 30),
            DaconQuota(None, None, None, None),
            status="incomplete" if self.mode == "incomplete" else "completed",
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode",
    [
        "success",
        "citation",
        "malformed",
        "schema",
        "precision",
        "incomplete",
        "metadata_only",
        "network",
    ],
)
async def test_reasoner_calls_once_and_preserves_received_usage_on_failure(mode: str) -> None:
    client = Client(mode)
    reasoner = DaconSpecialistReasoner(cast(DaconOpenAIClient, client))
    outcome = await reasoner.interpret(
        "ADMET",
        {"AMES": 0.2, "model_context": "synthetic"},
        ExecutionLimits(
            timeout_seconds=30,
            max_tool_calls=1,
            max_recall_depth=0,
        ),
    )
    assert client.calls == 1 and outcome.external_requests == 1
    assert (outcome.interpretation is not None) == (mode == "success")
    expected_diagnostic = {
        "citation": "reasoning_citation_unknown",
        "malformed": "reasoning_json_invalid",
        "schema": "reasoning_schema_invalid",
        "precision": "reasoning_number_precision_invalid",
        "incomplete": "reasoning_response_incomplete",
        "metadata_only": "reasoning_observation_citation_missing",
    }.get(mode)
    assert outcome.diagnostic_code == (
        "model_call_unknown" if mode == "network" else expected_diagnostic
    )
    assert outcome.components[0].version == "poc-specialist-v5"
    assert outcome.error_code == (
        "reasoning_invalid_output"
        if expected_diagnostic
        else "reasoning_unavailable"
        if mode == "network"
        else None
    )
    if mode == "network":
        assert outcome.usage is None
    else:
        assert outcome.usage is not None and outcome.usage.total_tokens == 30
    if mode == "citation":
        assert outcome.citation_diagnostic is not None
        assert outcome.citation_diagnostic.allowed_id_count == 2
        assert outcome.citation_diagnostic.unknown_id_count == 1
        assert "unknown" not in outcome.diagnostic_message
    if mode == "metadata_only":
        assert outcome.citation_diagnostic is not None
        assert outcome.citation_diagnostic.observation_id_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "wrapper,expected_diagnostic,expected_recovery",
    [
        ("```json\n{document}\n```", None, True),
        ("```\n{document}\n```", None, True),
        ("Here is JSON:\n{document}", "reasoning_json_invalid", False),
        ("```json\n{document}\n```\nextra", "reasoning_json_invalid", False),
        ("```json\n{document}\n```\n```json\n{{}}\n```", "reasoning_json_invalid", False),
    ],
)
async def test_reasoner_only_unwraps_a_single_json_fence(
    wrapper: str, expected_diagnostic: str | None, expected_recovery: bool
) -> None:
    class WrappedClient(Client):
        async def generate_text(
            self,
            prompt: str,
            *,
            instructions: str,
            max_output_tokens: int,
            output_schema: type[Interpretation] | None = None,
        ) -> GeneratedText:
            result = await super().generate_text(
                prompt,
                instructions=instructions,
                max_output_tokens=max_output_tokens,
                output_schema=output_schema,
            )
            return replace(result, text=wrapper.format(document=result.text))

    client = WrappedClient("success")
    outcome = await DaconSpecialistReasoner(cast(DaconOpenAIClient, client)).interpret(
        "ADME",
        {"AMES": 0.2, "model_context": "synthetic"},
        ExecutionLimits(timeout_seconds=30, max_tool_calls=1, max_recall_depth=0),
    )
    assert client.calls == outcome.external_requests == 1
    assert outcome.diagnostic_code == expected_diagnostic
    assert outcome.json_fence_removed is expected_recovery
    assert (outcome.interpretation is not None) is (expected_diagnostic is None)
    assert outcome.usage is not None and outcome.usage.total_tokens == 30


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode,expected_diagnostic",
    [
        ("citation", "reasoning_citation_unknown"),
        ("schema", "reasoning_schema_invalid"),
        ("malformed", "reasoning_json_invalid"),
        ("incomplete", "reasoning_response_incomplete"),
    ],
)
async def test_unwrapped_json_still_requires_schema_and_allowed_citations(
    mode: str, expected_diagnostic: str
) -> None:
    class WrappedClient(Client):
        async def generate_text(
            self,
            prompt: str,
            *,
            instructions: str,
            max_output_tokens: int,
            output_schema: type[Interpretation] | None = None,
        ) -> GeneratedText:
            result = await super().generate_text(
                prompt,
                instructions=instructions,
                max_output_tokens=max_output_tokens,
                output_schema=output_schema,
            )
            return replace(result, text=f"```json\n{result.text}\n```")

    client = WrappedClient(mode)
    outcome = await DaconSpecialistReasoner(cast(DaconOpenAIClient, client)).interpret(
        "ADME",
        {"AMES": 0.2, "model_context": "synthetic"},
        ExecutionLimits(timeout_seconds=30, max_tool_calls=1, max_recall_depth=0),
    )
    assert client.calls == outcome.external_requests == 1
    assert outcome.diagnostic_code == expected_diagnostic
    assert outcome.interpretation is None
    assert not outcome.json_fence_removed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "candidate_ids,citations,expected_diagnostic,unknown_count",
    [
        (("ENSG00000135446",), ("ENSG00000135446",), None, 0),
        (
            ("ENSG00000135446", "ENSG00000105810"),
            ("ENSG00000135446", "ENSG00000105810"),
            None,
            0,
        ),
        (("ENSG00000135446",), ("CDK4",), "reasoning_citation_unknown", 1),
        (
            ("ENSG00000135446", "ENSG00000105810"),
            ("ENSG00000135446", "predicted_pkd"),
            "reasoning_citation_unknown",
            1,
        ),
    ],
)
async def test_dta_citations_use_top_level_candidate_ids_only(
    candidate_ids: tuple[str, ...],
    citations: tuple[str, ...],
    expected_diagnostic: str | None,
    unknown_count: int,
) -> None:
    class DtaClient:
        calls = 0

        async def generate_text(
            self,
            prompt: str,
            *,
            instructions: str,
            max_output_tokens: int,
            output_schema: type[Interpretation] | None = None,
        ) -> GeneratedText:
            self.calls += 1
            payload = json.loads(prompt)
            assert payload["task"] == "DTA"
            assert payload["allowed_evidence_ids"] == list(candidate_ids)
            assert payload["observation_evidence_ids"] == list(candidate_ids)
            assert payload["evidence"][candidate_ids[0]]["approved_symbol"] == "CDK4"
            assert "Never cite nested ensembl_id" in instructions
            return GeneratedText(
                "test",
                "gpt-5.6-luna",
                json.dumps(
                    {
                        "summary": "합성 결합 관측 해석",
                        "used_evidence_ids": citations,
                        "limitations": ["실험 근거 아님"],
                    }
                ),
                ResponseUsage(20, 10, 30),
                DaconQuota(None, None, None, None),
            )

    evidence: dict[str, object] = {
        candidate_id: {
            "ensembl_id": candidate_id,
            "approved_symbol": "CDK4",
            "observations": [{"score_type": "predicted_pkd", "value": 4.81}],
        }
        for candidate_id in candidate_ids
    }
    client = DtaClient()
    outcome = await DaconSpecialistReasoner(cast(DaconOpenAIClient, client)).interpret(
        "DTA", evidence, ExecutionLimits(timeout_seconds=30, max_tool_calls=2, max_recall_depth=0)
    )
    assert client.calls == 1
    assert outcome.diagnostic_code == expected_diagnostic
    assert (outcome.interpretation is not None) == (expected_diagnostic is None)
    assert outcome.usage is not None and outcome.usage.total_tokens == 30
    assert outcome.external_requests == 1
    if expected_diagnostic:
        assert outcome.citation_diagnostic is not None
        assert outcome.citation_diagnostic.allowed_id_count == len(candidate_ids)
        assert outcome.citation_diagnostic.unknown_id_count == unknown_count
        assert "CDK4" not in outcome.diagnostic_message
        assert "predicted_pkd" not in outcome.diagnostic_message


@pytest.mark.asyncio
@pytest.mark.parametrize("oversized", [True, False])
async def test_input_guard_rejects_before_paid_call(oversized: bool) -> None:
    client = Client("success")
    outcome = await DaconSpecialistReasoner(cast(DaconOpenAIClient, client)).interpret(
        "ADMET",
        {"AMES": "x" * 25000 if oversized else 0.2},
        ExecutionLimits(
            timeout_seconds=30,
            max_tool_calls=1,
            max_recall_depth=0,
            max_total_tokens=None if oversized else 100,
        ),
    )
    assert client.calls == 0 and outcome.external_requests == 0
    assert outcome.error_code == "reasoning_input_budget_exceeded"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "task,evidence_id,required,forbidden",
    [
        ("ADME", "Caco2_Wang", "conditional oral-development", "Do not discuss absorption"),
        ("TOXICITY", "DILI", "clinical adverse event", "Microsomal clearance"),
    ],
)
async def test_admet_domain_prompts_are_separate(
    task: str, evidence_id: str, required: str, forbidden: str
) -> None:
    class DomainClient:
        async def generate_text(
            self,
            prompt: str,
            *,
            instructions: str,
            max_output_tokens: int,
            output_schema: type[Interpretation] | None = None,
        ) -> GeneratedText:
            payload = json.loads(prompt)
            assert payload["task"] == task
            assert max_output_tokens == 4096
            assert payload["observation_evidence_ids"] == [evidence_id]
            assert required in instructions
            assert forbidden not in instructions
            return GeneratedText(
                "test",
                "gpt-5.6-luna",
                json.dumps(
                    {
                        "summary": "영역별 해석",
                        "used_evidence_ids": [evidence_id],
                        "limitations": ["합성 근거"],
                    }
                ),
                ResponseUsage(20, 10, 30),
                DaconQuota(None, None, None, None),
            )

    result = await DaconSpecialistReasoner(cast(DaconOpenAIClient, DomainClient())).interpret(
        task,
        {evidence_id: 0.2, "model_context": "synthetic"},
        ExecutionLimits(timeout_seconds=30, max_tool_calls=1, max_recall_depth=0),
    )
    assert result.interpretation is not None
