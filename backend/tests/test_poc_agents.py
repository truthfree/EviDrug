"""실제 admission/runner/SQL을 fake 모델과 연결해 PoC 경계를 검증한다."""

import asyncio
import json
from copy import deepcopy
from typing import cast

import pytest
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_admet_adapter import provider_report
from test_admet_repository import CountingProvider
from test_dta_agent import CandidateProvider, FakeReasoner
from test_orchestration import orchestration_database as database_fixture
from test_target_hypothesis import agent_input as target_input
from test_target_hypothesis import create_analysis, target_agent

from evidrug_api.admet.adapter import AdmetToolAdapter
from evidrug_api.admet.agent import ADME_ENDPOINTS, POC_ENDPOINTS, AdmetAgent, AdmetAgentResult
from evidrug_api.admet.toxicity import calculate_toxicity_axes
from evidrug_api.analysis_jobs.models import AnalysisStageName
from evidrug_api.analysis_jobs.results import project_admet
from evidrug_api.decision.agent import (
    DECISION_PROMPT_VERSION,
    DecisionAgent,
    DecisionAssessmentGeneration,
)
from evidrug_api.decision.policy import calculate_metadata
from evidrug_api.dta.adapter import DtaToolAdapter
from evidrug_api.dta.agent import DtaAgent
from evidrug_api.execution_contracts.agent import AgentInput
from evidrug_api.execution_contracts.common import ExecutionLimits, TokenUsage
from evidrug_api.openai_gateway.client import DaconOpenAIClient
from evidrug_api.openai_gateway.models import DaconQuota, GeneratedText, ResponseUsage
from evidrug_api.orchestration.contracts import AgentExecutionResult, ExecutionProfile
from evidrug_api.orchestration.executor import RoutedAgentExecutor
from evidrug_api.orchestration.poc_runtime import ExclusiveModelAgent
from evidrug_api.orchestration.reasoning import (
    CitationDiagnostic,
    Interpretation,
    JsonParseDiagnostic,
    ReasoningOutcome,
)
from evidrug_api.orchestration.service import AnalysisOrchestrator
from evidrug_api.orchestration.tables import AgentRunRecord
from evidrug_api.tool_admission.bindings import admet_binding, dta_binding

orchestration_database = database_fixture


def test_adme_projection_covers_exposure_without_weak_volume_or_half_life_models() -> None:
    assert ADME_ENDPOINTS == (
        "Solubility_AqSolDB",
        "Caco2_Wang",
        "HIA_Hou",
        "Bioavailability_Ma",
        "logP",
        "tpsa",
        "molecular_weight",
        "PPBR_AZ",
        "Clearance_Hepatocyte_AZ",
        "Clearance_Microsome_AZ",
        "CYP3A4_Veith",
        "CYP2D6_Veith",
    )
    assert "VDss_Lombardo" not in POC_ENDPOINTS
    assert "Half_Life_Obach" not in POC_ENDPOINTS


@pytest.mark.asyncio
@pytest.mark.parametrize("diagnostic_kind", ["citation", "json"])
async def test_admet_preserves_safe_citation_diagnostic_with_model_observations(
    orchestration_database: async_sessionmaker[AsyncSession],
    diagnostic_kind: str,
) -> None:
    class RejectedReasoner:
        calls: list[str] = []

        async def interpret(
            self, task: str, evidence: dict[str, object], limits: ExecutionLimits
        ) -> ReasoningOutcome:
            self.calls.append(task)
            assert task in {"ADME", "TOXICITY"} and "model_context" in evidence
            model_context = cast(dict[str, object], evidence["model_context"])
            assert ("toxicity_axes" in model_context) == (task == "TOXICITY")
            assert not (
                set(evidence)
                & set(POC_ENDPOINTS)
                - set(POC_ENDPOINTS[4:] if task == "ADME" else POC_ENDPOINTS[:4])
            )
            if diagnostic_kind == "json":
                return ReasoningOutcome(
                    None,
                    TokenUsage(input_tokens=20, output_tokens=10, total_tokens=30),
                    "reasoning_invalid_output",
                    1,
                    "test-model",
                    diagnostic_code="reasoning_json_invalid",
                    json_parse_diagnostic=JsonParseDiagnostic(100, 70, 10, 2, 3, "plain"),
                )
            return ReasoningOutcome(
                None,
                TokenUsage(input_tokens=20, output_tokens=10, total_tokens=30),
                "reasoning_invalid_output",
                1,
                "test-model",
                "reasoning_citation_unknown",
                CitationDiagnostic(len(evidence), len(evidence) - 1, 1),
            )

    factory = orchestration_database
    analysis_id = await create_analysis(factory)
    provider = FullAdmetProvider()
    reasoner = RejectedReasoner()
    await AnalysisOrchestrator(
        factory,
        RoutedAgentExecutor(
            {
                AnalysisStageName.TARGET_HYPOTHESIS: target_agent(),
                AnalysisStageName.ADMET: AdmetAgent(
                    factory, admet_binding(AdmetToolAdapter(provider)), reasoner
                ),
            }
        ),
    ).run(analysis_id)
    async with factory() as session:
        row = await session.scalar(
            select(AgentRunRecord).where(
                AgentRunRecord.analysis_id == analysis_id,
                AgentRunRecord.agent_name == AnalysisStageName.ADMET,
            )
        )
        assert row is not None and row.output_json is not None
        output = json.loads(row.output_json)
        assert row.status == "partial_failure"
        assert output["error"]["code"] == "reasoning_invalid_output"
        assert output["result"]["context"]["rows"]
        stored = AdmetAgentResult.model_validate(output["result"])
        assert stored.toxicity_axes == calculate_toxicity_axes(stored.context)
        assert project_admet(stored).toxicity_axes == stored.toxicity_axes
        components = {
            item["component"]: item["version"]
            for item in output["execution_metadata"]["components"]
        }
        assert components["adme_prompt"] == "adme-interpretation-v4"
        assert components["toxicity_prompt"] == "toxicity-interpretation-v3"
        assert output["result"]["interpretation"] is None
        assert output["result"]["adme"]["error_code"] == "reasoning_invalid_output"
        assert output["result"]["toxicity"]["error_code"] == "reasoning_invalid_output"
        assert reasoner.calls == ["ADME", "TOXICITY"]
        assert output["warnings"][0]["code"] == (
            "reasoning_citation_unknown"
            if diagnostic_kind == "citation"
            else "reasoning_json_invalid"
        )
        assert "ADME: " in output["warnings"][0]["message"]
        assert (
            "미허용 인용 ID 1개" if diagnostic_kind == "citation" else "100바이트/70자"
        ) in output["warnings"][0]["message"]
        assert output["execution_metadata"]["usage"]["token_usage"]["total_tokens"] == 60
        assert output["execution_metadata"]["usage"]["external_requests"] == 2


@pytest.mark.asyncio
async def test_admet_records_single_json_fence_recovery_without_an_extra_request(
    orchestration_database: async_sessionmaker[AsyncSession],
) -> None:
    class WrappedReasoner:
        calls: list[str] = []

        async def interpret(
            self, task: str, evidence: dict[str, object], limits: ExecutionLimits
        ) -> ReasoningOutcome:
            self.calls.append(task)
            evidence_id = next(key for key in evidence if key != "model_context")
            return ReasoningOutcome(
                Interpretation(
                    summary="합성 관측을 해석했습니다.",
                    used_evidence_ids=(evidence_id,),
                    limitations=("실험적 확인이 필요합니다.",),
                ),
                TokenUsage(input_tokens=20, output_tokens=10, total_tokens=30),
                external_requests=1,
                model="test-model",
                json_fence_removed=task == "ADME",
            )

    factory = orchestration_database
    analysis_id = await create_analysis(factory)
    reasoner = WrappedReasoner()
    await AnalysisOrchestrator(
        factory,
        RoutedAgentExecutor(
            {
                AnalysisStageName.TARGET_HYPOTHESIS: target_agent(),
                AnalysisStageName.ADMET: AdmetAgent(
                    factory, admet_binding(AdmetToolAdapter(FullAdmetProvider())), reasoner
                ),
            }
        ),
    ).run(analysis_id)
    async with factory() as session:
        row = await session.scalar(
            select(AgentRunRecord).where(
                AgentRunRecord.analysis_id == analysis_id,
                AgentRunRecord.agent_name == AnalysisStageName.ADMET,
            )
        )
        assert row is not None and row.output_json is not None
        output = json.loads(row.output_json)
        assert row.status == "completed"
        assert reasoner.calls == ["ADME", "TOXICITY"]
        assert output["warnings"] == [
            {
                "code": "reasoning_json_fence_removed",
                "message": (
                    "ADME: 단일 JSON 코드블록 포장을 제거한 뒤 내용·근거 ID 검증을 통과했습니다."
                ),
                "related_claim_ids": [],
            }
        ]
        assert output["execution_metadata"]["usage"]["external_requests"] == 2


@pytest.mark.asyncio
async def test_admet_keeps_successful_domain_when_other_interpretation_fails(
    orchestration_database: async_sessionmaker[AsyncSession],
) -> None:
    class MixedReasoner:
        async def interpret(
            self, task: str, evidence: dict[str, object], limits: ExecutionLimits
        ) -> ReasoningOutcome:
            usage = TokenUsage(input_tokens=20, output_tokens=10, total_tokens=30)
            if task == "TOXICITY":
                return ReasoningOutcome(None, usage, "reasoning_invalid_output", 1)
            return ReasoningOutcome(
                Interpretation(
                    summary="용해도와 청소율을 함께 고려해야 합니다.",
                    used_evidence_ids=(next(iter(evidence)),),
                    limitations=("노출 농도는 계산하지 않았습니다.",),
                ),
                usage,
                external_requests=1,
            )

    factory = orchestration_database
    analysis_id = await create_analysis(factory)
    await AnalysisOrchestrator(
        factory,
        RoutedAgentExecutor(
            {
                AnalysisStageName.TARGET_HYPOTHESIS: target_agent(),
                AnalysisStageName.ADMET: AdmetAgent(
                    factory,
                    admet_binding(AdmetToolAdapter(FullAdmetProvider())),
                    MixedReasoner(),
                ),
            }
        ),
    ).run(analysis_id)
    async with factory() as session:
        row = await session.scalar(
            select(AgentRunRecord).where(
                AgentRunRecord.analysis_id == analysis_id,
                AgentRunRecord.agent_name == AnalysisStageName.ADMET,
            )
        )
        assert row is not None and row.output_json is not None
        output = json.loads(row.output_json)
        assert row.status == "partial_failure"
        assert output["result"]["adme"]["interpretation"]["summary"]
        assert output["result"]["toxicity"]["interpretation"] is None
        assert output["result"]["toxicity"]["error_code"] == "reasoning_invalid_output"
        assert output["execution_metadata"]["usage"]["token_usage"]["total_tokens"] == 60


@pytest.mark.asyncio
async def test_admet_second_domain_respects_shared_token_budget(
    orchestration_database: async_sessionmaker[AsyncSession],
) -> None:
    class BudgetReasoner:
        calls = 0

        async def interpret(
            self, task: str, evidence: dict[str, object], limits: ExecutionLimits
        ) -> ReasoningOutcome:
            self.calls += 1
            assert task == "ADME" and limits.max_total_tokens == 30
            return ReasoningOutcome(
                Interpretation(
                    summary="합성 ADME 해석",
                    used_evidence_ids=(next(iter(evidence)),),
                    limitations=("합성 결과",),
                ),
                TokenUsage(input_tokens=20, output_tokens=10, total_tokens=30),
                external_requests=1,
            )

    factory = orchestration_database
    analysis_id = await create_analysis(factory)
    reasoner = BudgetReasoner()
    await AnalysisOrchestrator(
        factory,
        RoutedAgentExecutor(
            {
                AnalysisStageName.TARGET_HYPOTHESIS: target_agent(),
                AnalysisStageName.ADMET: AdmetAgent(
                    factory,
                    admet_binding(AdmetToolAdapter(FullAdmetProvider())),
                    reasoner,
                ),
            }
        ),
        profile=ExecutionProfile(
            stage_limits=ExecutionLimits(
                timeout_seconds=30,
                max_tool_calls=1,
                max_recall_depth=0,
                max_total_tokens=30,
            )
        ),
    ).run(analysis_id)
    assert reasoner.calls == 1
    async with factory() as session:
        row = await session.scalar(
            select(AgentRunRecord).where(
                AgentRunRecord.analysis_id == analysis_id,
                AgentRunRecord.agent_name == AnalysisStageName.ADMET,
            )
        )
        assert row is not None and row.output_json is not None
        output = json.loads(row.output_json)
        assert output["result"]["toxicity"]["error_code"] == "reasoning_input_budget_exceeded"
        assert output["execution_metadata"]["usage"]["external_requests"] == 1


@pytest.mark.asyncio
async def test_model_stage_closes_on_cancellation_before_releasing_shared_lock() -> None:
    lock = asyncio.Lock()
    started = asyncio.Event()
    closed = asyncio.Event()

    class WaitingAgent:
        async def execute(self, incoming: AgentInput) -> AgentExecutionResult:
            started.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    async def close() -> None:
        assert lock.locked()
        closed.set()

    task = asyncio.create_task(
        ExclusiveModelAgent(WaitingAgent(), lock, close).execute(target_input())
    )
    await asyncio.wait_for(started.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed.is_set() and not lock.locked()


class FullAdmetProvider(CountingProvider):
    async def predict(self, canonical_smiles: str) -> dict[str, object]:
        await super().predict(canonical_smiles)
        report = provider_report()
        report["smiles"] = canonical_smiles
        base = report["endpoint_metadata"][0]
        metadata = []
        predictions = {}
        for key in (*POC_ENDPOINTS, "unused_endpoint"):
            item = deepcopy(base)
            item["id"] = key
            item["name"] = "Synthetic " + key
            metadata.append(item)
            predictions[key] = 0.2
            predictions[key + "_drugbank_approved_percentile"] = 40.0
        report["endpoint_metadata"] = metadata
        report["predictions"] = predictions
        return cast(dict[str, object], report)


class DecisionClient:
    def __init__(self, mode: str = "normal") -> None:
        self.calls: list[str] = []
        self.instructions: list[str] = []
        self.output_schemas: list[type[BaseModel] | None] = []
        self.mode = mode

    async def generate_text(
        self,
        prompt: str,
        *,
        instructions: str,
        max_output_tokens: int,
        output_schema: type[BaseModel] | None = None,
    ) -> GeneratedText:
        self.calls.append(prompt)
        self.instructions.append(instructions)
        self.output_schemas.append(output_schema)
        assert max_output_tokens == 4096
        payload = json.loads(prompt)
        assert payload["allowed_evidence_ids"] == list(payload["evidence"])
        assert payload["required_citation_prefixes"] == sorted(
            {key.split(":", 1)[0] for key in payload["evidence"]}
        )
        if payload["missing_stages"]:
            assert "go" not in payload["allowed_verdicts"]
        if self.mode == "network":
            raise RuntimeError("private upstream error")
        output: dict[str, object] = {
            "headline": "합성 근거의 추가 확인이 필요합니다.",
            "assessment": {
                area: {"status": state, "summary": "합성 관측을 검토했습니다.", "key_values": []}
                for area, state in calculate_metadata(
                    payload["evidence"]
                ).expected_area_statuses.items()
            },
            "key_strengths": [],
            "key_concerns": [],
            "recall_request": None,
            "decision_phase": "final",
            "verdict": "conditional_go",
            "rationale": "합성 관측 기반 예측값은 4.811026입니다.",
            "used_evidence_ids": list(payload["evidence"]),
            "conflicts": [],
            "gaps": ["두 결합 모델의 예측 차이를 결합 실험으로 확인해야 합니다."],
            "next_actions": [
                {
                    "status": "proposed",
                    "action": "결합 모델 간 차이를 독립 결합 분석으로 확인합니다.",
                    "rationale": "두 예측의 차이가 현재 판정의 핵심 불확실성이기 때문입니다.",
                    "decision_impact": (
                        "일관된 결합이 확인되면 연구 우선순위를 높이고, 그렇지 않으면 낮춥니다."
                    ),
                }
            ],
        }
        if self.mode == "unknown_citation":
            output["used_evidence_ids"] = ["fabricated"]
        if self.mode == "unqualified_go":
            output["verdict"] = "go"
        if self.mode == "missing_citation":
            output["used_evidence_ids"] = ["admet:context"]
        if self.mode == "schema":
            output["gaps"] = []
        if self.mode == "bad_value_once" and len(self.calls) == 1:
            assessment = cast(dict[str, object], output["assessment"])
            target = cast(dict[str, object], assessment["target"])
            target["key_values"] = [{"label": "질환 연관성 점수", "value": 999.0, "unit": None}]
        if self.mode == "bad_value_label_once" and len(self.calls) == 1:
            assessment = cast(dict[str, object], output["assessment"])
            target = cast(dict[str, object], assessment["target"])
            target_evidence = next(
                cast(dict[str, object], value)
                for key, value in cast(dict[str, object], payload["evidence"]).items()
                if key.startswith("target:")
            )
            target["key_values"] = [
                {
                    "label": "hERG 분류 지지값",
                    "value": target_evidence["association"],
                    "unit": None,
                }
            ]
        if self.mode == "inline_citations":
            output["rationale"] = (
                f"표적 근거가 확인됐다 [{list(payload['evidence'])[0]}]. "
                f"후보 검증이 다음 단계다 [{list(payload['evidence'])[1]}]."
            )
        if self.mode == "nested_citation":
            output["used_evidence_ids"] = ["AMES"]
        return GeneratedText(
            "synthetic",
            "gpt-5.6-luna",
            "not json"
            if self.mode in {"malformed", "malformed_once"}
            and (self.mode == "malformed" or len(self.calls) == 1)
            else json.dumps(output),
            ResponseUsage(10, 4096, 4106)
            if self.mode == "incomplete"
            else ResponseUsage(10, 10, 20),
            DaconQuota(None, None, None, None),
            status="incomplete" if self.mode == "incomplete" else "completed",
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "admet_failure,dta_failures,decision_mode,expected",
    [
        (False, (), "normal", "completed"),
        (False, (), "inline_citations", "completed"),
        (True, (), "normal", "partial_failure"),
        (False, (1,), "normal", "partial_failure"),
        (False, (), "unknown_citation", "failed"),
        (False, (), "malformed", "failed"),
        (False, (), "malformed_once", "completed"),
        (False, (), "bad_value_once", "completed"),
        (False, (), "bad_value_label_once", "completed"),
        (False, (), "schema", "failed"),
        (False, (), "missing_citation", "failed"),
        (False, (), "nested_citation", "failed"),
        (False, (), "incomplete", "failed"),
        (False, (), "network", "failed"),
        (True, (), "unqualified_go", "failed"),
    ],
)
async def test_full_poc_dag_persists_real_contracts_without_network(
    orchestration_database: async_sessionmaker[AsyncSession],
    admet_failure: bool,
    dta_failures: tuple[int, ...],
    decision_mode: str,
    expected: str,
) -> None:
    factory = orchestration_database
    analysis_id = await create_analysis(factory)
    admet = FullAdmetProvider()
    admet.fail = admet_failure
    dta = CandidateProvider(dta_failures)
    client = DecisionClient(decision_mode)
    executor = RoutedAgentExecutor(
        {
            AnalysisStageName.TARGET_HYPOTHESIS: target_agent(),
            AnalysisStageName.ADMET: AdmetAgent(
                factory, admet_binding(AdmetToolAdapter(admet)), FakeReasoner()
            ),
            AnalysisStageName.DTA: DtaAgent(
                factory, dta_binding(DtaToolAdapter(dta)), FakeReasoner()
            ),
            AnalysisStageName.DECISION: DecisionAgent(factory, cast(DaconOpenAIClient, client)),
        }
    )
    orchestrator = AnalysisOrchestrator(factory, executor)
    assert await orchestrator.run(analysis_id) == expected
    corrected = decision_mode in {
        "malformed",
        "malformed_once",
        "bad_value_once",
        "bad_value_label_once",
    }
    expected_calls = 2 if corrected else 1
    assert len(client.calls) == expected_calls
    assert len(client.instructions) == expected_calls
    assert client.output_schemas == ([None, DecisionAssessmentGeneration] if corrected else [None])
    assert "All twelve output fields are required" in client.instructions[0]
    assert "experimental_binding_support" in client.instructions[0]
    if corrected:
        reason = (
            "decision_key_value_label_mismatch"
            if decision_mode == "bad_value_label_once"
            else "decision_key_value_out_of_range"
            if decision_mode == "bad_value_once"
            else "decision_json_invalid"
        )
        assert reason in client.instructions[1]
    prompt = client.calls[0]
    if not admet_failure:
        projected = json.loads(prompt)["evidence"]["admet:context"]
        assert projected["execution_status"] == "completed"
        assert projected["selection_coverage"]["is_subset"] is True
        assert projected["selection_coverage"]["selected_endpoint_count"] == len(POC_ENDPOINTS)
        assert "is_partial" not in projected["context"]
        assert projected["missing_endpoints"] == []
        assert not {"interpretation", "adme", "toxicity"} & projected.keys()
        assert projected["toxicity_axes"]["policy_version"] == "toxicity-priority-policy-v1"
        assert {axis["endpoint_id"] for axis in projected["toxicity_axes"]["axes"]} == {
            "DILI",
            "hERG",
            "AMES",
        }
    for excluded in ("target_sequence", "unused_endpoint", "model_artifacts", "canonical_smiles"):
        assert excluded not in prompt
    async with factory() as session:
        rows = list(
            await session.scalars(
                select(AgentRunRecord).where(AgentRunRecord.analysis_id == analysis_id)
            )
        )
        assert len(rows) == 4
        if not admet_failure:
            stored = next(row for row in rows if row.agent_name == AnalysisStageName.ADMET)
            assert stored.output_json is not None
            assert json.loads(stored.output_json)["result"]["context"]["is_partial"] is True
            assert json.loads(stored.output_json)["result"]["adme"]["interpretation"]
            assert json.loads(stored.output_json)["result"]["toxicity"]["interpretation"]
        decision = next(row for row in rows if row.agent_name == AnalysisStageName.DECISION)
        assert decision.output_json is not None
        output = json.loads(decision.output_json)
        if decision_mode == "inline_citations":
            assert "[target:" not in output["result"]["assessment"]["rationale"]
            assert "[admet:" not in output["result"]["assessment"]["rationale"]
            assert output["result"]["assessment"]["used_evidence_ids"]
            assert output["warnings"][0]["code"] == "decision_inline_citations_removed"
        assert {
            (item["component"], item["version"])
            for item in output["execution_metadata"]["components"]
        } >= {("decision_prompt", DECISION_PROMPT_VERSION)}
        if decision_mode == "network":
            assert output["execution_metadata"]["usage"]["token_usage"] is None
        else:
            expected_tokens = 40 if corrected else 4106 if decision_mode == "incomplete" else 20
            assert output["execution_metadata"]["usage"]["token_usage"]["total_tokens"] == (
                expected_tokens
            )
        if expected == "failed":
            assert output["error"]["code"] == (
                "decision_model_unavailable"
                if decision_mode == "network"
                else "decision_invalid_output"
            )
            reason = {
                "unknown_citation": "decision_citation_unknown",
                "nested_citation": "decision_citation_unknown",
                "malformed": "decision_json_invalid",
                "schema": "decision_schema_invalid",
                "missing_citation": "decision_specialist_citation_missing",
                "incomplete": "decision_response_incomplete",
                "unqualified_go": "decision_go_restricted",
                "network": "model_call_unknown",
            }[decision_mode]
            assert output["warnings"][0]["code"] == reason
            assert "private upstream error" not in decision.output_json
            assert output["raw_result"] is None
            assert output["result"] is None
        else:
            assert output["result"]["assessment"]["verdict"] == "conditional_go"
            if decision_mode == "normal":
                rationale = output["result"]["assessment"]["rationale"]
                assert "4.81" in rationale
                assert "4.811026" not in rationale
                assert output["warnings"][0]["code"] == "decision_numeric_display_normalized"
            if corrected:
                assert output["execution_metadata"]["usage"]["external_requests"] == 2
                assert any(
                    item["code"] == "decision_output_correction_applied"
                    for item in output["warnings"]
                )
    await orchestrator.run(analysis_id)
    assert len(client.calls) == expected_calls and admet.calls == 1 and len(dta.calls) == 1
