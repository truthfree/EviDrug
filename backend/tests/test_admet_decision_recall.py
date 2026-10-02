"""Decision 요청이 저장된 ADMET/CToxPred2 관측을 거쳐 재판단되는 통합 경로."""

import json
from typing import cast
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_ctoxpred2_adapter import provider_report
from test_dta_agent import CandidateProvider, FakeReasoner
from test_orchestration import orchestration_database as database_fixture
from test_poc_agents import FullAdmetProvider
from test_target_hypothesis import create_analysis, target_agent

from evidrug_api.admet.adapter import AdmetToolAdapter
from evidrug_api.admet.agent import AdmetAgent, AdmetAgentResult
from evidrug_api.admet.recall import AdmetRecallAgent, AdmetRecallResult
from evidrug_api.analysis_jobs.models import AnalysisStageName, AnalysisStatus
from evidrug_api.analysis_jobs.repository import AnalysisRepository
from evidrug_api.analysis_jobs.result_models import AgentCallSummary, CardiacRecallSummary
from evidrug_api.analysis_jobs.results import (
    project_admet,
    project_agent_calls,
    project_cardiac_recall,
    project_decision,
    project_run,
)
from evidrug_api.ctoxpred2.adapter import CtoxToolAdapter
from evidrug_api.ctoxpred2.contracts import CtoxToolArguments
from evidrug_api.decision.agent import DecisionAgent, DecisionResult
from evidrug_api.decision.policy import calculate_metadata
from evidrug_api.decision.projection import decision_payload_sha256
from evidrug_api.dta.adapter import DtaToolAdapter
from evidrug_api.dta.agent import DtaAgent
from evidrug_api.evaluation.baseline import extract_baseline_snapshot
from evidrug_api.execution_contracts.agent import AgentInput, AgentOutput
from evidrug_api.execution_contracts.common import ExecutionLimits
from evidrug_api.openai_gateway.client import DaconOpenAIClient
from evidrug_api.openai_gateway.models import DaconQuota, GeneratedText, ResponseUsage
from evidrug_api.orchestration.contracts import ExecutionProfile
from evidrug_api.orchestration.executor import RoutedAgentExecutor
from evidrug_api.orchestration.fixed_tools import FixedToolRun
from evidrug_api.orchestration.repository import OrchestrationRepository
from evidrug_api.orchestration.service import AnalysisOrchestrator
from evidrug_api.orchestration.tables import AgentRunRecord
from evidrug_api.tool_admission.bindings import (
    admet_binding,
    ctoxpred2_binding,
    dta_binding,
)
from evidrug_api.tool_execution.tables import ToolExecutionRecord
from evidrug_api.trajectory.admet_snapshot import AdmetSnapshotRepository
from evidrug_api.trajectory.comparison_contracts import PolicyComparisonManifest
from evidrug_api.trajectory.contracts import TrajectoryProfile, TrajectoryStep
from evidrug_api.trajectory.ctox_snapshot import CtoxSnapshotRepository
from evidrug_api.trajectory.decision_sources import (
    DecisionSourceError,
    DecisionSourceManifest,
    DecisionSourceReference,
    verify_decision_sources,
)
from evidrug_api.trajectory.policy_comparison import (
    PolicyComparisonReport,
    PolicyComparisonRunner,
)
from evidrug_api.trajectory.tables import TrajectoryEpisodeRecord, TrajectoryStepRecord
from evidrug_api.trajectory.validator import validate_stored_episode

orchestration_database = database_fixture


class CtoxProvider:
    def __init__(self, fail: bool = False) -> None:
        self.calls = 0
        self.fail = fail

    async def predict(self, canonical_smiles: str) -> dict[str, object]:
        self.calls += 1
        if self.fail:
            raise RuntimeError("synthetic provider failure")
        report = provider_report()
        report["canonical_smiles"] = canonical_smiles
        return report


class RecallDecisionClient:
    def __init__(self, expect_failure: bool = False) -> None:
        self.prompts: list[dict[str, object]] = []
        self.expect_failure = expect_failure

    async def generate_text(
        self, prompt: str, *, instructions: str, max_output_tokens: int
    ) -> GeneratedText:
        payload = json.loads(prompt)
        self.prompts.append(payload)
        first = len(self.prompts) == 1
        evidence = payload["evidence"]
        assert isinstance(evidence, dict)
        if not first:
            key = "admet:recall_failure" if self.expect_failure else "admet:cardiac_ion_channels"
            assert key in evidence
            assert payload["recall_feedback"]["status"] == (
                "failed" if self.expect_failure else "succeeded"
            )
        output: dict[str, object] = {
            "headline": "추가 근거를 검토합니다.",
            "assessment": {
                area: {"status": state, "summary": "합성 근거를 검토했습니다.", "key_values": []}
                for area, state in calculate_metadata(evidence).expected_area_statuses.items()
            },
            "key_strengths": [],
            "key_concerns": [],
            "decision_phase": "request_recall" if first else "final",
            "verdict": "conditional_go",
            "rationale": "심장 이온통로 근거를 검토한다.",
            "used_evidence_ids": list(evidence),
            "conflicts": [],
            "gaps": ["심장 안전성은 임상 검증이 필요하다."],
            "next_actions": [
                {
                    "status": "proposed",
                    "action": "심장 이온통로 신호를 추가 확인합니다.",
                    "rationale": "현재 판정을 제한하는 사례별 안전성 공백이기 때문입니다.",
                    "decision_impact": (
                        "우려가 재현되면 우선순위를 낮추고, 해소되면 상향을 검토합니다."
                    ),
                }
            ],
            "recall_request": {
                "gap_kind": "cardiac_ion_channel_evidence",
                "objective": "hERG, Nav1.5, Cav1.2 근거 보완",
                "reason_code": "cardiac_evidence_gap",
            }
            if first
            else None,
        }
        return GeneratedText(
            "synthetic",
            "gpt-5.6-luna",
            json.dumps(output),
            ResponseUsage(10, 10, 20),
            DaconQuota(None, None, None, None),
            status="completed",
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("recall_mode", ["success", "provider_failure", "no_match", "replay_miss"])
async def test_admet_recall_executes_ctox_and_returns_verified_evidence_to_decision(
    orchestration_database: async_sessionmaker[AsyncSession],
    recall_mode: str,
) -> None:
    factory = orchestration_database
    analysis_id = await create_analysis(factory)
    admet_binding_value = admet_binding(AdmetToolAdapter(FullAdmetProvider()))
    fail_recall = recall_mode != "success"
    ctox_provider = CtoxProvider(recall_mode == "provider_failure")
    ctox_binding_value = ctoxpred2_binding(CtoxToolAdapter(ctox_provider))
    client = RecallDecisionClient(fail_recall)
    recall_agent = AdmetRecallAgent(
        factory,
        admet_binding_value,
        ctox_binding_value,
        replay_snapshot_id=uuid4() if recall_mode == "replay_miss" else None,
    )
    if recall_mode == "no_match":
        recall_agent.capabilities = tuple(
            item for item in recall_agent.capabilities if item.tool_id != "ctoxpred2"
        )
    agent = AdmetAgent(
        factory,
        admet_binding_value,
        FakeReasoner(),
        recall_agent=recall_agent,
    )
    executor = RoutedAgentExecutor(
        {
            AnalysisStageName.TARGET_HYPOTHESIS: target_agent(),
            AnalysisStageName.ADMET: agent,
            AnalysisStageName.DTA: DtaAgent(
                factory, dta_binding(DtaToolAdapter(CandidateProvider(()))), FakeReasoner()
            ),
            AnalysisStageName.DECISION: DecisionAgent(factory, cast(DaconOpenAIClient, client)),
        }
    )
    profile = ExecutionProfile(
        stage_limits=ExecutionLimits(timeout_seconds=60, max_tool_calls=1, max_recall_depth=1)
    )

    status = await AnalysisOrchestrator(factory, executor, profile=profile).run(analysis_id)

    assert status is (AnalysisStatus.PARTIAL_FAILURE if fail_recall else AnalysisStatus.COMPLETED)
    assert ctox_provider.calls == (0 if recall_mode in {"no_match", "replay_miss"} else 1)
    assert len(client.prompts) == 2
    async with factory() as session:
        repository = AnalysisRepository(session)
        baseline_row = await repository.get_run_attempt(analysis_id, AnalysisStageName.ADMET, 1)
        recall_row = await repository.get_run_attempt(analysis_id, AnalysisStageName.ADMET, 2)
        baseline_public = project_run(baseline_row, AgentOutput[AdmetAgentResult], project_admet)
        recall_public = project_run(
            recall_row, AgentOutput[AdmetRecallResult], project_cardiac_recall
        )
        first_decision_row = await repository.get_run_attempt(
            analysis_id, AnalysisStageName.DECISION, 1
        )
        first_decision_public = project_run(
            first_decision_row, AgentOutput[DecisionResult], project_decision
        )
        baseline_snapshot = await extract_baseline_snapshot(repository, analysis_id)
        runs = list(
            await session.scalars(
                select(AgentRunRecord).where(AgentRunRecord.analysis_id == analysis_id)
            )
        )
        source_rows = {
            row.agent_name: row
            for row in runs
            if row.attempt == 1
            and row.agent_name
            in {
                AnalysisStageName.TARGET_HYPOTHESIS,
                AnalysisStageName.ADMET,
                AnalysisStageName.DTA,
            }
        }
        assert all(
            row.output_json is not None and row.output_sha256 is not None
            for row in source_rows.values()
        )
        assert baseline_row is not None and baseline_row.output_json is not None
        baseline_output = AgentOutput[AdmetAgentResult].model_validate_json(
            baseline_row.output_json
        )
        assert baseline_output.result is not None
        admet_observation = await AdmetSnapshotRepository(session).create(
            analysis_id=analysis_id,
            snapshot_version="decision-source-test-v1",
            source_tool_call_ids=(baseline_output.result.context.source_tool_call_id,),
        )
        source_manifest = DecisionSourceManifest(
            analysis_id=analysis_id,
            target=DecisionSourceReference(
                run_id=source_rows[AnalysisStageName.TARGET_HYPOTHESIS].run_id,
                agent_name=AnalysisStageName.TARGET_HYPOTHESIS,
                input_sha256=source_rows[AnalysisStageName.TARGET_HYPOTHESIS].input_sha256,
                output_sha256=cast(
                    str, source_rows[AnalysisStageName.TARGET_HYPOTHESIS].output_sha256
                ),
                implementation_version=AgentOutput.model_validate_json(
                    cast(str, source_rows[AnalysisStageName.TARGET_HYPOTHESIS].output_json)
                ).execution_metadata.implementation_version,
            ),
            admet=DecisionSourceReference(
                run_id=baseline_row.run_id,
                agent_name=AnalysisStageName.ADMET,
                input_sha256=baseline_row.input_sha256,
                output_sha256=cast(str, baseline_row.output_sha256),
                implementation_version=baseline_output.execution_metadata.implementation_version,
            ),
            dta=DecisionSourceReference(
                run_id=source_rows[AnalysisStageName.DTA].run_id,
                agent_name=AnalysisStageName.DTA,
                input_sha256=source_rows[AnalysisStageName.DTA].input_sha256,
                output_sha256=cast(str, source_rows[AnalysisStageName.DTA].output_sha256),
                implementation_version=AgentOutput.model_validate_json(
                    cast(str, source_rows[AnalysisStageName.DTA].output_json)
                ).execution_metadata.implementation_version,
            ),
        )
        verified_sources = await verify_decision_sources(
            session, source_manifest, admet_observation
        )
        assert verified_sources.admet.run_id == baseline_row.run_id
        with pytest.raises(DecisionSourceError, match="decision_source_hash_mismatch:admet"):
            await verify_decision_sources(
                session,
                source_manifest.model_copy(
                    update={
                        "admet": source_manifest.admet.model_copy(
                            update={"output_sha256": "0" * 64}
                        )
                    }
                ),
                admet_observation,
            )
        with pytest.raises(DecisionSourceError, match="decision_source_version_mismatch:admet"):
            await verify_decision_sources(
                session,
                source_manifest.model_copy(
                    update={
                        "admet": source_manifest.admet.model_copy(
                            update={"implementation_version": "unsupported-version"}
                        )
                    }
                ),
                admet_observation,
            )
        with pytest.raises(DecisionSourceError, match="decision_source_missing:dta"):
            await verify_decision_sources(
                session,
                source_manifest.model_copy(
                    update={"dta": source_manifest.dta.model_copy(update={"run_id": uuid4()})}
                ),
                admet_observation,
            )
        if recall_mode == "success":
            assert recall_row is not None and recall_row.output_json is not None
            recall_output = AgentOutput[AdmetRecallResult].model_validate_json(
                recall_row.output_json
            )
            assert recall_output.result is not None
            ctox_observation = await CtoxSnapshotRepository(session).create(
                analysis_id=analysis_id,
                snapshot_version="decision-source-test-v1",
                source_tool_call_ids=(recall_output.result.source_tool_call_id,),
            )
            comparison = await PolicyComparisonRunner(session).run(
                PolicyComparisonManifest(
                    analysis_id=analysis_id,
                    admet_snapshot_id=admet_observation.snapshot_id,
                    ctox_snapshot_id=ctox_observation.snapshot_id,
                    decision_sources=source_manifest,
                    policy_version="decision-source-test-v1",
                    limits=ExecutionLimits(
                        timeout_seconds=60, max_tool_calls=2, max_recall_depth=1
                    ),
                )
            )
            assert (
                PolicyComparisonReport.model_validate_json(comparison.model_dump_json())
                == comparison
            )
            assert comparison.decision_sources == source_manifest
            count_before_rejection = await session.scalar(
                select(func.count(TrajectoryEpisodeRecord.episode_id)).where(
                    TrajectoryEpisodeRecord.analysis_id == analysis_id
                )
            )
            with pytest.raises(DecisionSourceError, match="decision_source_hash_mismatch:admet"):
                await PolicyComparisonRunner(session).run(
                    PolicyComparisonManifest(
                        analysis_id=analysis_id,
                        admet_snapshot_id=admet_observation.snapshot_id,
                        ctox_snapshot_id=ctox_observation.snapshot_id,
                        decision_sources=source_manifest.model_copy(
                            update={
                                "admet": source_manifest.admet.model_copy(
                                    update={"output_sha256": "0" * 64}
                                )
                            }
                        ),
                        policy_version="decision-source-test-v1",
                        limits=ExecutionLimits(
                            timeout_seconds=60, max_tool_calls=2, max_recall_depth=1
                        ),
                    )
                )
            assert (
                await session.scalar(
                    select(func.count(TrajectoryEpisodeRecord.episode_id)).where(
                        TrajectoryEpisodeRecord.analysis_id == analysis_id
                    )
                )
                == count_before_rejection
            )
            first_recall_payload = comparison.outcomes[2].decision_inputs[0].full_payload_json
            assert first_recall_payload is not None
            assert json.loads(first_recall_payload) == client.prompts[0]
            assert comparison.outcomes[2].decision_inputs[
                0
            ].full_payload_sha256 == decision_payload_sha256(client.prompts[0])
            baseline_payload = comparison.outcomes[0].decision_inputs[0].full_payload_json
            assert baseline_payload is not None
            assert json.loads(baseline_payload)["allow_recall"] is False
            assert comparison.outcomes[0].decision_inputs[0].source_run_ids == (
                source_manifest.target.run_id,
                source_manifest.admet.run_id,
                source_manifest.dta.run_id,
            )
            assert comparison.outcomes[2].decision_inputs[1].full_payload_sha256 is not None
            second_payload_json = comparison.outcomes[2].decision_inputs[1].full_payload_json
            assert second_payload_json is not None
            second_payload = json.loads(second_payload_json)
            live_second_evidence = json.loads(json.dumps(client.prompts[1]["evidence"]))
            live_second_evidence["admet:cardiac_ion_channels"].pop("run_id")
            assert second_payload["evidence"] == live_second_evidence
            assert second_payload["recall_feedback"]["kind"] == "simulated_replay_feedback"
            assert "run_id" not in second_payload["recall_feedback"]
            repeated = await PolicyComparisonRunner(session).run(
                PolicyComparisonManifest(
                    analysis_id=analysis_id,
                    admet_snapshot_id=admet_observation.snapshot_id,
                    ctox_snapshot_id=ctox_observation.snapshot_id,
                    decision_sources=source_manifest,
                    policy_version="decision-source-test-v1",
                    limits=ExecutionLimits(
                        timeout_seconds=60, max_tool_calls=2, max_recall_depth=1
                    ),
                )
            )
            assert [
                [item.full_payload_sha256 for item in outcome.decision_inputs]
                for outcome in comparison.outcomes
            ] == [
                [item.full_payload_sha256 for item in outcome.decision_inputs]
                for outcome in repeated.outcomes
            ]
            assert ctox_provider.calls == 1
        elif recall_mode == "provider_failure":
            assert recall_row is not None
            failed_tool_call = await session.scalar(
                select(ToolExecutionRecord).where(ToolExecutionRecord.run_id == recall_row.run_id)
            )
            assert failed_tool_call is not None and failed_tool_call.status == "failed"
            failed_ctox_snapshot = await CtoxSnapshotRepository(session).create(
                analysis_id=analysis_id,
                snapshot_version="decision-source-failure-test-v1",
                source_tool_call_ids=(failed_tool_call.tool_call_id,),
            )
            failed_comparison = await PolicyComparisonRunner(session).run(
                PolicyComparisonManifest(
                    analysis_id=analysis_id,
                    admet_snapshot_id=admet_observation.snapshot_id,
                    ctox_snapshot_id=failed_ctox_snapshot.snapshot_id,
                    decision_sources=source_manifest,
                    policy_version="decision-source-failure-test-v1",
                    limits=ExecutionLimits(
                        timeout_seconds=60, max_tool_calls=2, max_recall_depth=1
                    ),
                )
            )
            failed_payload = failed_comparison.outcomes[2].decision_inputs[1].full_payload_json
            assert failed_payload is not None
            assert "admet:recall_failure" in json.loads(failed_payload)["evidence"]
            assert failed_comparison.outcomes[2].failed_observations == 1
            assert ctox_provider.calls == 1
        episodes = [
            row
            for row in await session.scalars(
                select(TrajectoryEpisodeRecord).where(
                    TrajectoryEpisodeRecord.analysis_id == analysis_id
                )
            )
            if not TrajectoryProfile.model_validate_json(row.profile_json).profile_id.startswith(
                "comparison:"
            )
        ]
        steps = list(
            await session.scalars(
                select(TrajectoryStepRecord)
                .where(TrajectoryStepRecord.episode_id == episodes[0].episode_id)
                .order_by(TrajectoryStepRecord.sequence_number)
            )
        )
        validation = await validate_stored_episode(session, episodes[0].episode_id)
    assert baseline_public.projection_status == "available"
    assert first_decision_public.projection_status == "unavailable"
    assert baseline_snapshot.admet.projection_status == "available"
    assert baseline_snapshot.admet.attempt == 1
    assert recall_public.projection_status == ("unavailable" if fail_recall else "available")
    recall = next(
        row for row in runs if row.agent_name == AnalysisStageName.ADMET and row.attempt == 2
    )
    final = next(
        row for row in runs if row.agent_name == AnalysisStageName.DECISION and row.attempt == 2
    )
    calls = project_agent_calls(tuple(runs))
    admet_calls = [item for item in calls if item.agent_name == AnalysisStageName.ADMET]
    decision_calls = [item for item in calls if item.agent_name == AnalysisStageName.DECISION]
    assert [item.call_number for item in admet_calls] == [1, 2]
    assert [item.call_number for item in decision_calls] == [1, 2]
    assert admet_calls[0].purpose == "initial"
    assert admet_calls[1].purpose == "evidence_followup"
    assert admet_calls[1].request is not None
    assert admet_calls[1].request.requesting_run_id == decision_calls[0].run_id
    assert admet_calls[1].request.objective
    assert admet_calls[1].responds_to_run_id == decision_calls[0].run_id
    assert decision_calls[0].response_run_id == admet_calls[1].run_id
    assert decision_calls[1].purpose == "reassessment"
    assert decision_calls[1].responds_to_run_id == admet_calls[1].run_id
    assert decision_calls[1].request == admet_calls[1].request
    assert admet_calls[1].projection_status == ("unavailable" if fail_recall else "available")
    if not fail_recall:
        round_trip = AgentCallSummary.model_validate_json(admet_calls[1].model_dump_json())
        assert round_trip.result_kind == "cardiac_ion_channel"
        assert isinstance(round_trip.result, CardiacRecallSummary)
        assert len(round_trip.result.predictions) == 3
    original_input = recall.input_json
    recall.input_json = "tampered"
    unverified = project_agent_calls(tuple(runs))
    unverified_admet = next(
        item
        for item in unverified
        if item.agent_name == AnalysisStageName.ADMET and item.call_number == 2
    )
    assert unverified_admet.purpose == "unverified"
    assert unverified_admet.request is None
    recall.input_json = original_input
    assert first_decision_row is not None
    original_decision_output = first_decision_row.output_json
    first_decision_row.output_json = "tampered"
    unverified_request = project_agent_calls(tuple(runs))
    assert (
        next(
            item
            for item in unverified_request
            if item.agent_name == AnalysisStageName.DECISION and item.call_number == 1
        ).response_run_id
        is None
    )
    first_decision_row.output_json = original_decision_output
    assert recall.status == ("failed" if fail_recall else "completed")
    assert final.status == "completed"
    assert len(episodes) == 1 and len(steps) == 3
    assert validation.verdict == "accepted"
    trajectory = [TrajectoryStep.model_validate_json(item.step_json) for item in steps]
    assert trajectory[0].selected_action_id == "request:admet:cardiac_ion_channels"
    assert trajectory[1].source_run_id == recall.run_id
    assert trajectory[1].triggering_run_id == trajectory[0].source_run_id
    assert trajectory[2].source_run_id == final.run_id
    assert trajectory[2].triggering_run_id == recall.run_id
    assert trajectory[1].available_actions[0].tool_id == (
        None if recall_mode == "no_match" else "ctoxpred2"
    )
    if recall_mode == "no_match":
        assert trajectory[1].observations == ()
        assert trajectory[1].available_actions[0].reason_code == "recall_capability_unavailable"
        assert trajectory[1].excluded_capabilities
        assert {item.reason_code for item in trajectory[1].excluded_capabilities} == {
            "unsupported_gap_kind"
        }
    else:
        assert trajectory[1].observations[0].outcome == (
            "replay_miss"
            if recall_mode == "replay_miss"
            else "failed"
            if fail_recall
            else "succeeded"
        )
    assert trajectory[1].usage.tool_calls == (
        0 if recall_mode in {"no_match", "replay_miss"} else 1
    )
    if not fail_recall:
        assert recall.output_json is not None
        payload = json.loads(recall.output_json)["result"]
        assert payload["tool_version"] == ctox_binding_value.version
        assert len(payload["predictions"]) == 3
        assert {item["tool_id"] for item in payload["capability_matches"] if item["matched"]} == {
            "ctoxpred2"
        }


@pytest.mark.asyncio
async def test_admet_recall_replays_same_analysis_snapshot_without_provider_call(
    orchestration_database: async_sessionmaker[AsyncSession],
) -> None:
    factory = orchestration_database
    analysis_id = await create_analysis(factory)
    ctox_provider = CtoxProvider()
    ctox_tool = ctoxpred2_binding(CtoxToolAdapter(ctox_provider))
    async with factory() as session:
        analysis = await OrchestrationRepository(session)._analysis(analysis_id)
        assert analysis is not None
        case_input = OrchestrationRepository._case_input(analysis)
    seed_input = AgentInput(
        analysis_id=analysis_id,
        run_id=uuid4(),
        agent_name=AnalysisStageName.ADMET,
        attempt=1,
        case_input=case_input,
        execution_limits=ExecutionLimits(timeout_seconds=60, max_tool_calls=1, max_recall_depth=0),
    )
    tools = FixedToolRun(factory, ctox_tool, seed_input)
    await tools.start()
    source = await tools.execute(
        "seed",
        CtoxToolArguments(canonical_smiles=case_input.canonical_smiles),
        "snapshot seed",
    )
    assert source.result is not None and ctox_provider.calls == 1
    async with factory() as session:
        snapshot = await CtoxSnapshotRepository(session).create(
            analysis_id=analysis_id,
            snapshot_version="cardiac-panel-v1",
            source_tool_call_ids=(source.tool_call_id,),
        )
    admet_tool = admet_binding(AdmetToolAdapter(FullAdmetProvider()))
    client = RecallDecisionClient()
    executor = RoutedAgentExecutor(
        {
            AnalysisStageName.TARGET_HYPOTHESIS: target_agent(),
            AnalysisStageName.ADMET: AdmetAgent(
                factory,
                admet_tool,
                FakeReasoner(),
                recall_agent=AdmetRecallAgent(
                    factory,
                    admet_tool,
                    ctox_tool,
                    replay_snapshot_id=snapshot.snapshot_id,
                ),
            ),
            AnalysisStageName.DTA: DtaAgent(
                factory, dta_binding(DtaToolAdapter(CandidateProvider(()))), FakeReasoner()
            ),
            AnalysisStageName.DECISION: DecisionAgent(factory, cast(DaconOpenAIClient, client)),
        }
    )
    profile = ExecutionProfile(
        stage_limits=ExecutionLimits(timeout_seconds=60, max_tool_calls=1, max_recall_depth=1)
    )

    status = await AnalysisOrchestrator(factory, executor, profile=profile).run(analysis_id)

    assert status is AnalysisStatus.COMPLETED
    assert ctox_provider.calls == 1
    async with factory() as session:
        episode = await session.scalar(
            select(TrajectoryEpisodeRecord).where(
                TrajectoryEpisodeRecord.analysis_id == analysis_id
            )
        )
        assert episode is not None
        steps = list(
            await session.scalars(
                select(TrajectoryStepRecord)
                .where(TrajectoryStepRecord.episode_id == episode.episode_id)
                .order_by(TrajectoryStepRecord.sequence_number)
            )
        )
    saved_profile = json.loads(episode.profile_json)
    assert saved_profile["execution_mode"] == "replay"
    assert saved_profile["observation_snapshot_version"] == "cardiac-panel-v1"
    selection = TrajectoryStep.model_validate_json(steps[1].step_json)
    assert selection.available_actions[0].tool_id == "ctoxpred2"
    assert selection.observations[0].outcome == "succeeded"
    assert selection.usage.tool_calls == 0
