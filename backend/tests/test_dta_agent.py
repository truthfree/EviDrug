"""실제 admission/runner/SQL을 fake 모델과 연결해 PoC 경계를 검증한다."""

import json
from dataclasses import replace
from uuid import uuid4

import pytest
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_dta import MODEL, OBSERVATION
from test_orchestration import orchestration_database as database_fixture
from test_target_hypothesis import create_analysis, target_agent

from evidrug_api.analysis_input.models import PotencyEndpoint, TargetMode
from evidrug_api.analysis_jobs.models import AnalysisStageName
from evidrug_api.analysis_jobs.results import project_dta
from evidrug_api.analysis_jobs.tables import AnalysisRecord
from evidrug_api.dta.adapter import DtaProviderUnavailable, DtaToolAdapter
from evidrug_api.dta.agent import DtaAgent, DtaAgentResult
from evidrug_api.dta.contracts import DtaArguments, DtaObservation, DtaScoreType
from evidrug_api.dta.deeppurpose import MPNN_CNN_BINDINGDB_MODEL
from evidrug_api.dta.evidence import (
    AssayEvidence,
    AssayEvidenceKind,
    AssaySource,
    DtaAssayArguments,
    DtaAssayResult,
)
from evidrug_api.dta.tables import DtaExecutionRecord
from evidrug_api.execution_contracts.agent import AgentInput, AgentOutput
from evidrug_api.execution_contracts.common import ExecutionLimits, TokenUsage
from evidrug_api.orchestration.contracts import ExecutionProfile
from evidrug_api.orchestration.executor import RoutedAgentExecutor
from evidrug_api.orchestration.reasoning import (
    CitationDiagnostic,
    Interpretation,
    ReasoningOutcome,
)
from evidrug_api.orchestration.service import AnalysisOrchestrator
from evidrug_api.orchestration.tables import AgentRunRecord
from evidrug_api.orchestration.upstream import InvalidUpstream, load_upstream
from evidrug_api.target_hypothesis.contracts import TargetHypothesisResult
from evidrug_api.tool_admission.bindings import dta_binding
from evidrug_api.tool_admission.registry import Invocation, ToolBinding
from evidrug_api.tool_admission.tables import ToolAdmissionRecord

orchestration_database = database_fixture


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["analysis", "hash", "artifact", "case"])
async def test_stored_target_integrity_is_checked_before_new_dta_calls(
    orchestration_database: async_sessionmaker[AsyncSession],
    mutation: str,
) -> None:
    factory = orchestration_database
    analysis_id = await create_analysis(factory)
    await AnalysisOrchestrator(
        factory, RoutedAgentExecutor({AnalysisStageName.TARGET_HYPOTHESIS: target_agent()})
    ).run(analysis_id)
    async with factory() as session:
        row = await session.scalar(
            select(AgentRunRecord).where(
                AgentRunRecord.analysis_id == analysis_id,
                AgentRunRecord.agent_name == AnalysisStageName.DTA,
            )
        )
        assert row is not None and row.input_json is not None
        incoming = AgentInput.model_validate_json(row.input_json)
    reference = incoming.upstream_outputs[0]
    if mutation == "analysis":
        incoming = incoming.model_copy(update={"analysis_id": uuid4()})
    elif mutation == "case":
        incoming = incoming.model_copy(
            update={
                "case_input": incoming.case_input.model_copy(update={"canonical_smiles": "CCO"})
            }
        )
    else:
        changes = {"sha256": "0" * 64} if mutation == "hash" else {"artifact_id": uuid4()}
        reference = reference.model_copy(
            update={"output": reference.output.model_copy(update=changes)}
        )
    with pytest.raises(InvalidUpstream):
        await load_upstream(factory, incoming, reference, AgentOutput[TargetHypothesisResult])


class FakeReasoner:
    async def interpret(
        self, task: str, evidence: dict[str, object], limits: ExecutionLimits
    ) -> ReasoningOutcome:
        return ReasoningOutcome(
            Interpretation(
                summary="합성 관측 해석",
                used_evidence_ids=(next(iter(evidence)),),
                limitations=("실험 근거 아님",),
            )
        )


class CandidateProvider:
    model = MODEL

    def __init__(self, failures: tuple[int, ...] = ()) -> None:
        self.calls: list[DtaArguments] = []
        self.failures = failures

    async def predict(self, arguments: DtaArguments) -> tuple[DtaObservation, ...]:
        self.calls.append(arguments)
        if len(self.calls) in self.failures:
            raise DtaProviderUnavailable("synthetic unavailable")
        return (OBSERVATION,)


def assay_binding(source: AssaySource, calls: list[AssaySource]) -> ToolBinding:
    tool_id = {
        AssaySource.BINDINGDB: "bindingdb",
        AssaySource.CHEMBL: "chembl",
        AssaySource.PUBCHEM: "pubchem_bioassay",
    }[source]

    async def invoke(call: Invocation, arguments: BaseModel) -> BaseModel:
        parsed = DtaAssayArguments.model_validate(arguments.model_dump())
        assert parsed.uniprot_accession
        calls.append(source)
        evidence = (
            AssayEvidence(
                source=source,
                source_record_id=f"{tool_id}:{len(calls)}",
                kind=AssayEvidenceKind.QUANTITATIVE,
                endpoint=PotencyEndpoint.KD,
                value=50,
                unit="nM",
            ),
        )
        return DtaAssayResult(
            source=source,
            status="succeeded",
            evidence=evidence,
            external_requests=1,
        )

    return ToolBinding(
        tool_id,
        "test-v1",
        "동일 compound-target assay 조회",
        (AnalysisStageName.DTA,),
        DtaAssayArguments,
        DtaAssayResult,
        invoke,
        30,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["success", "no_records", "failed", "disabled", "budget"])
async def test_dta_queries_pubchem_once_per_candidate_only_after_disagreement(
    orchestration_database: async_sessionmaker[AsyncSession],
    mode: str,
) -> None:
    factory = orchestration_database
    analysis_id = await create_analysis(factory)
    async with factory() as session:
        analysis = await session.get(AnalysisRecord, analysis_id)
        assert analysis is not None
        analysis.target_mode = TargetMode.DISCOVER
        analysis.target_name = None
        analysis.potency_endpoint = "Kd"
        analysis.potency_maximum_value = 100
        analysis.potency_unit = "nM"
        await session.commit()

    low = CandidateProvider(())
    high = CandidateProvider(())
    high.model = MPNN_CNN_BINDINGDB_MODEL

    async def high_predict(arguments: DtaArguments) -> tuple[DtaObservation, ...]:
        high.calls.append(arguments)
        return (
            DtaObservation(
                score_type=DtaScoreType.PREDICTED_PKD,
                value=8.0,
                unit="-log10(Kd [M])",
            ),
        )

    high.predict = high_predict  # type: ignore[method-assign]
    assay_calls: list[AssaySource] = []
    binding = assay_binding(AssaySource.PUBCHEM, assay_calls)
    original_invoke = binding.invoke

    async def invoke(call: Invocation, arguments: BaseModel) -> BaseModel:
        if mode == "failed":
            assay_calls.append(AssaySource.PUBCHEM)
            raise RuntimeError("synthetic provider failure")
        if mode == "no_records":
            assay_calls.append(AssaySource.PUBCHEM)
            return DtaAssayResult(
                source=AssaySource.PUBCHEM, status="no_records", external_requests=1
            )
        return await original_invoke(call, arguments)

    binding = replace(binding, invoke=invoke)
    agent = DtaAgent(
        factory,
        (
            dta_binding(DtaToolAdapter(low)),
            dta_binding(DtaToolAdapter(high), tool_id="dta_mpnn_cnn_bindingdb"),
        ),
        FakeReasoner(),
        assay_bindings=() if mode == "disabled" else (binding,),
    )
    await AnalysisOrchestrator(
        factory,
        RoutedAgentExecutor(
            {
                AnalysisStageName.TARGET_HYPOTHESIS: target_agent(),
                AnalysisStageName.DTA: agent,
            }
        ),
        profile=ExecutionProfile(
            stage_limits=ExecutionLimits(
                timeout_seconds=30, max_tool_calls=2 if mode == "budget" else 10, max_recall_depth=0
            )
        ),
    ).run(analysis_id)

    assert assay_calls.count(AssaySource.PUBCHEM) == (0 if mode in ("disabled", "budget") else 2)
    async with factory() as session:
        row = await session.scalar(
            select(AgentRunRecord).where(
                AgentRunRecord.analysis_id == analysis_id,
                AgentRunRecord.agent_name == AnalysisStageName.DTA,
            )
        )
        assert row is not None and row.output_json is not None
        output = AgentOutput[DtaAgentResult].model_validate_json(row.output_json)
        assert output.result is not None
        public = project_dta(output.result)
        assert output.result.assay_policy_version == "pubchem-recall-v1"
        for candidate in output.result.candidates:
            assert candidate.evidence_assessment is not None
            if candidate.status != "succeeded":
                assert not candidate.evidence_assessment.pubchem_requested
                continue
            assert candidate.evidence_assessment.pubchem_requested is True
            assert candidate.evidence_assessment.region_status == "split_across_reference"
            assert candidate.evidence_assessment.pubchem_executed == (
                mode not in ("disabled", "budget")
            )
            assert (
                candidate.evidence_assessment.pubchem_execution_issue
                == {
                    "success": None,
                    "no_records": None,
                    "failed": "execution_failed",
                    "disabled": "provider_disabled",
                    "budget": "tool_budget_exhausted",
                }[mode]
            )
            assert candidate.evidence_assessment.experimental_binding_support == (mode == "success")
        assert all(
            len(c.experimental_evidence) == (1 if mode == "success" else 0)
            for c in public.candidates
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failures,budget,expected,calls",
    [
        ((), 3, "completed", 2),
        ((1,), 3, "partial_failure", 2),
        ((1, 2), 3, "failed", 2),
        ((), 1, "partial_failure", 1),
        ((), 0, "failed", 0),
    ],
)
async def test_dta_shortlist_uses_ledger_and_preserves_partial_failure(
    orchestration_database: async_sessionmaker[AsyncSession],
    failures: tuple[int, ...],
    budget: int,
    expected: str,
    calls: int,
) -> None:
    factory = orchestration_database
    analysis_id = await create_analysis(factory)
    async with factory() as session:
        analysis = await session.get(AnalysisRecord, analysis_id)
        assert analysis is not None
        analysis.target_mode = TargetMode.DISCOVER
        analysis.target_name = None
        await session.commit()
    provider = CandidateProvider(failures)
    executor = RoutedAgentExecutor(
        {
            AnalysisStageName.TARGET_HYPOTHESIS: target_agent(),
            AnalysisStageName.DTA: DtaAgent(
                factory, dta_binding(DtaToolAdapter(provider)), FakeReasoner()
            ),
        }
    )
    orchestrator = AnalysisOrchestrator(
        factory,
        executor,
        profile=ExecutionProfile(
            stage_limits=ExecutionLimits(
                timeout_seconds=30, max_tool_calls=budget, max_recall_depth=0
            ),
        ),
    )
    await orchestrator.run(analysis_id)
    assert len(provider.calls) == calls

    async with factory() as session:
        row = await session.scalar(
            select(AgentRunRecord).where(
                AgentRunRecord.analysis_id == analysis_id,
                AgentRunRecord.agent_name == AnalysisStageName.DTA,
            )
        )
        assert row is not None and row.status == expected
        assert row.output_json is not None
        output = json.loads(row.output_json)
        assert output["execution_metadata"]["usage"]["tool_calls"] == calls
        assert await session.scalar(select(func.count()).select_from(DtaExecutionRecord)) == calls
        assert await session.scalar(select(func.count()).select_from(ToolAdmissionRecord)) == calls
        if expected != "failed":
            candidates = output["result"]["candidates"]
            assert len(candidates) == 2
            assert "target_sequence" not in candidates[0]
            for candidate in candidates:
                if candidate["status"] != "succeeded":
                    assert candidate["observations"] == []
                    assert candidate["error_code"] is not None
    await orchestrator.run(analysis_id)
    assert len(provider.calls) == calls


@pytest.mark.asyncio
async def test_dta_shortlist_preserves_two_model_runs_without_averaging(
    orchestration_database: async_sessionmaker[AsyncSession],
) -> None:
    factory = orchestration_database
    analysis_id = await create_analysis(factory)
    async with factory() as session:
        analysis = await session.get(AnalysisRecord, analysis_id)
        assert analysis is not None
        analysis.target_mode = TargetMode.DISCOVER
        analysis.target_name = None
        await session.commit()

    cnn = CandidateProvider(())
    mpnn = CandidateProvider((2,))
    mpnn.model = MPNN_CNN_BINDINGDB_MODEL
    orchestrator = AnalysisOrchestrator(
        factory,
        RoutedAgentExecutor(
            {
                AnalysisStageName.TARGET_HYPOTHESIS: target_agent(),
                AnalysisStageName.DTA: DtaAgent(
                    factory,
                    (
                        dta_binding(DtaToolAdapter(cnn)),
                        dta_binding(DtaToolAdapter(mpnn), tool_id="dta_mpnn_cnn_bindingdb"),
                    ),
                    FakeReasoner(),
                ),
            }
        ),
        profile=ExecutionProfile(
            stage_limits=ExecutionLimits(timeout_seconds=30, max_tool_calls=4, max_recall_depth=0),
        ),
    )

    await orchestrator.run(analysis_id)

    async with factory() as session:
        row = await session.scalar(
            select(AgentRunRecord).where(
                AgentRunRecord.analysis_id == analysis_id,
                AgentRunRecord.agent_name == AnalysisStageName.DTA,
            )
        )
        assert row is not None and row.status == "partial_failure"
        assert row.output_json is not None
        output = json.loads(row.output_json)
        candidates = output["result"]["candidates"]
        assert output["execution_metadata"]["usage"]["tool_calls"] == 4
        assert await session.scalar(select(func.count()).select_from(DtaExecutionRecord)) == 4
        assert [run["model"]["model_id"] for run in candidates[0]["model_runs"]] == [
            MODEL.model_id,
            MPNN_CNN_BINDINGDB_MODEL.model_id,
        ]
        assert [run["status"] for run in candidates[0]["model_runs"]] == [
            "succeeded",
            "succeeded",
        ]
        assert [run["status"] for run in candidates[1]["model_runs"]] == [
            "succeeded",
            "failed",
        ]
        assert len(candidates[0]["observations"]) == 1
        assert "average" not in candidates[0]
        saved_result = AgentOutput[DtaAgentResult].model_validate_json(row.output_json).result
        assert saved_result is not None
        public = project_dta(saved_result)
        first, second = public.candidates
        assert [run.model.model_id for run in first.model_runs if run.model] == [
            MODEL.model_id,
            MPNN_CNN_BINDINGDB_MODEL.model_id,
        ]
        assert first.model_runs[0].tool_call_id != first.model_runs[1].tool_call_id
        assert first.model_runs[0].observations == first.observations
        assert [run.status for run in second.model_runs] == ["succeeded", "failed"]
        assert second.model_runs[1].observations == ()
        assert second.model_runs[1].error_code is not None
        assert "target_sequence_sha256" not in public.model_dump_json()
        assert "artifact_sha256" not in public.model_dump_json()
        legacy_output = json.loads(row.output_json)
        for item in legacy_output["result"]["candidates"]:
            item.pop("model_runs")
        legacy_result = AgentOutput[DtaAgentResult].model_validate(legacy_output).result
        assert legacy_result is not None
        legacy_public = project_dta(legacy_result)
        assert legacy_public.candidates[0].model_runs == ()
        assert legacy_public.candidates[0].observations == public.candidates[0].observations

    await orchestrator.run(analysis_id)
    assert len(cnn.calls) == 2
    assert len(mpnn.calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("failures", [(), (1,), (1, 2)])
async def test_dta_interpretation_failure_preserves_predictions_and_safe_diagnostic(
    orchestration_database: async_sessionmaker[AsyncSession],
    failures: tuple[int, ...],
) -> None:
    class RejectedReasoner:
        calls = 0

        async def interpret(
            self, task: str, evidence: dict[str, object], limits: ExecutionLimits
        ) -> ReasoningOutcome:
            self.calls += 1
            return ReasoningOutcome(
                None,
                TokenUsage(input_tokens=20, output_tokens=10, total_tokens=30),
                "reasoning_invalid_output",
                1,
                "test-model",
                "reasoning_citation_unknown",
                CitationDiagnostic(2, 2, 1),
            )

    factory = orchestration_database
    analysis_id = await create_analysis(factory)
    async with factory() as session:
        analysis = await session.get(AnalysisRecord, analysis_id)
        assert analysis is not None
        analysis.target_mode = TargetMode.DISCOVER
        analysis.target_name = None
        await session.commit()
    provider = CandidateProvider(failures)
    reasoner = RejectedReasoner()
    orchestrator = AnalysisOrchestrator(
        factory,
        RoutedAgentExecutor(
            {
                AnalysisStageName.TARGET_HYPOTHESIS: target_agent(),
                AnalysisStageName.DTA: DtaAgent(
                    factory, dta_binding(DtaToolAdapter(provider)), reasoner
                ),
            }
        ),
        profile=ExecutionProfile(
            stage_limits=ExecutionLimits(timeout_seconds=30, max_tool_calls=3, max_recall_depth=0),
        ),
    )
    await orchestrator.run(analysis_id)
    async with factory() as session:
        row = await session.scalar(
            select(AgentRunRecord).where(
                AgentRunRecord.analysis_id == analysis_id,
                AgentRunRecord.agent_name == AnalysisStageName.DTA,
            )
        )
        assert row is not None and row.output_json is not None
        output = json.loads(row.output_json)
        assert output["raw_result"] is None
        if len(failures) == 2:
            assert row.status == "failed"
            assert reasoner.calls == 0
            assert output["error"]["code"] == "dta_candidates_incomplete"
            assert output["warnings"] == []
        else:
            assert row.status == "partial_failure"
            assert reasoner.calls == 1
            assert output["error"]["code"] == "reasoning_invalid_output"
            assert output["warnings"][0]["code"] == "reasoning_citation_unknown"
            assert "허용 근거 ID 2개, 미허용 인용 ID 1개" in output["warnings"][0]["message"]
            assert {
                "component": "specialist_prompt",
                "version": "poc-specialist-v5",
            } in output["execution_metadata"]["components"]
            assert output["result"]["interpretation"] is None
            assert output["execution_metadata"]["usage"]["token_usage"]["total_tokens"] == 30
            candidates = output["result"]["candidates"]
            assert sum(c["status"] == "succeeded" for c in candidates) == 2 - len(failures)
            for candidate in candidates:
                if candidate["status"] == "succeeded":
                    assert candidate["observations"] == [OBSERVATION.model_dump(mode="json")]
            if failures:
                assert "일부 후보" in output["error"]["message"]
            else:
                assert "결합 예측은 완료했으나 LLM 해석" in output["error"]["message"]
    await orchestrator.run(analysis_id)
    assert len(provider.calls) == 2
    assert reasoner.calls == (0 if len(failures) == 2 else 1)
