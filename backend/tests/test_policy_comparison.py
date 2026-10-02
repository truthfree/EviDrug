"""동일 원본 관측의 세 정책 비교가 실제 provider를 재호출하지 않는지 검증한다."""

from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_admet_repository import CountingProvider
from test_admet_trajectory_replay import live_call
from test_admet_trajectory_replay import policy as admet_policy
from test_admet_trajectory_replay import request as admet_request
from test_ctoxpred2_replay import Provider as CtoxProvider
from test_ctoxpred2_replay import policy as ctox_policy
from test_ctoxpred2_replay import request as ctox_request
from test_tool_execution import database as database_fixture

from evidrug_api.admet.adapter import AdmetToolAdapter
from evidrug_api.admet.tables import AdmetPredictionRecord
from evidrug_api.analysis_jobs.tables import AnalysisRecord
from evidrug_api.ctoxpred2.adapter import CtoxToolAdapter
from evidrug_api.execution_contracts.common import ExecutionLimits
from evidrug_api.tool_admission.bindings import admet_binding, ctoxpred2_binding
from evidrug_api.tool_admission.executor import AdmittedToolExecutor
from evidrug_api.tool_admission.registry import ToolRegistry
from evidrug_api.tool_admission.tables import ToolAdmissionRecord
from evidrug_api.trajectory.admet_snapshot import AdmetSnapshotRepository
from evidrug_api.trajectory.comparison_contracts import PolicyComparisonManifest
from evidrug_api.trajectory.contracts import TrajectoryStep
from evidrug_api.trajectory.ctox_snapshot import CtoxSnapshotRepository
from evidrug_api.trajectory.policy_comparison import (
    PolicyComparisonError,
    PolicyComparisonReport,
    PolicyComparisonRunner,
    aggregate_reports,
)
from evidrug_api.trajectory.tables import (
    AdmetObservationSnapshotRecord,
    TrajectoryEpisodeRecord,
    TrajectoryStepRecord,
)

Database = tuple[async_sessionmaker[AsyncSession], UUID]
database = database_fixture


async def snapshots(
    factory: async_sessionmaker[AsyncSession], analysis_id: UUID, *, admet_failure: bool = False
) -> tuple[UUID, UUID, CountingProvider, CtoxProvider]:
    admet_provider = CountingProvider()
    admet_provider.fail = admet_failure
    ctox_provider = CtoxProvider()
    admet = admet_binding(AdmetToolAdapter(admet_provider))
    ctox = ctoxpred2_binding(CtoxToolAdapter(ctox_provider))
    admet_config = admet_policy(analysis_id, admet)
    ctox_config = ctox_policy(analysis_id, ctox)
    admet_call, ctox_call = uuid4(), uuid4()
    await live_call(factory, admet_config, admet, admet_request(admet_config, admet), admet_call)
    async with factory() as session:
        executor = AdmittedToolExecutor(ToolRegistry((ctox,)), session)
        await executor.start_run(ctox_config)
        await executor.execute(
            ctox_config.context,
            tool_call_id=ctox_call,
            request=ctox_request(ctox_config, ctox),
        )
        admet_snapshot = await AdmetSnapshotRepository(session).create(
            analysis_id=analysis_id,
            snapshot_version="comparison-admet-v1",
            source_tool_call_ids=(admet_call,),
        )
        ctox_snapshot = await CtoxSnapshotRepository(session).create(
            analysis_id=analysis_id,
            snapshot_version="comparison-ctox-v1",
            source_tool_call_ids=(ctox_call,),
        )
    return admet_snapshot.snapshot_id, ctox_snapshot.snapshot_id, admet_provider, ctox_provider


def manifest(analysis_id: UUID, admet_id: UUID, ctox_id: UUID) -> PolicyComparisonManifest:
    return PolicyComparisonManifest(
        analysis_id=analysis_id,
        admet_snapshot_id=admet_id,
        ctox_snapshot_id=ctox_id,
        policy_version="comparison-pilot-v1",
        limits=ExecutionLimits(timeout_seconds=60, max_tool_calls=2, max_recall_depth=1),
    )


@pytest.mark.asyncio
async def test_three_policies_use_same_verified_snapshots_without_live_calls(
    database: Database,
) -> None:
    factory, analysis_id = database
    admet_id, ctox_id, admet_provider, ctox_provider = await snapshots(factory, analysis_id)
    async with factory() as session:
        report = await PolicyComparisonRunner(session).run(manifest(analysis_id, admet_id, ctox_id))
        rows = tuple(await session.scalars(select(TrajectoryStepRecord)))
    assert [item.strategy for item in report.outcomes] == [
        "baseline",
        "all_tools",
        "decision_recall",
    ]
    assert report.case_fingerprint != "a" * 64
    assert [item.selected_tool_ids for item in report.outcomes] == [
        ("admet_ai",),
        ("admet_ai", "ctoxpred2"),
        ("admet_ai", "ctoxpred2"),
    ]
    assert [item.recall_requests for item in report.outcomes] == [0, 0, 1]
    assert [item.attempted_observations for item in report.outcomes] == [1, 2, 2]
    assert [item.covered_gap_count for item in report.outcomes] == [0, 1, 1]
    assert [item.unresolved_gap_count for item in report.outcomes] == [1, 0, 0]
    assert [item.policy_excluded_tool_ids for item in report.outcomes] == [("ctoxpred2",), (), ()]
    assert [
        tuple(item.call_number for item in outcome.decision_inputs) for outcome in report.outcomes
    ] == [(1,), (1,), (1, 2)]
    assert [item.evidence_ids for item in report.outcomes[2].decision_inputs] == [
        ("admet:context",),
        ("admet:context", "admet:cardiac_ion_channels"),
    ]
    assert [
        item.observation_refs[0].tool_call_id
        for outcome in report.outcomes
        for item in outcome.decision_inputs[:1]
    ] == [report.outcomes[0].decision_inputs[0].observation_refs[0].tool_call_id] * 3
    assert (
        report.outcomes[1].decision_inputs[0].observation_refs[1].artifact
        == report.outcomes[2].decision_inputs[1].observation_refs[1].artifact
    )
    assert all(
        item.live_tool_calls == 0 and item.external_requests == 0 for item in report.outcomes
    )
    assert all(item.validation_verdict == "accepted" for item in report.outcomes)
    assert admet_provider.calls == 1 and ctox_provider.calls == 1
    first_by_episode = {
        row.episode_id: TrajectoryStep.model_validate_json(row.step_json)
        for row in rows
        if row.sequence_number == 1
    }
    assert [
        tuple(action.action_id for action in first_by_episode[item.episode_id].available_actions)
        for item in report.outcomes
    ] == [
        ("tool:admet_ai", "stop:comparison"),
        ("tool:admet_ai", "tool:ctoxpred2", "stop:comparison"),
        ("tool:admet_ai", "stop:comparison"),
    ]
    assert [
        tuple(
            (item.tool_id, item.reason_code)
            for item in first_by_episode[outcome.episode_id].excluded_capabilities
        )
        for outcome in report.outcomes
    ] == [
        (("ctoxpred2", "policy_disabled"),),
        (),
        (("ctoxpred2", "awaiting_decision_request"),),
    ]
    aggregates = aggregate_reports((report,))
    assert [item.case_count for item in aggregates] == [1, 1, 1]
    assert [item.accepted_episodes for item in aggregates] == [1, 1, 1]
    assert [item.attempted_observations for item in aggregates] == [1, 2, 2]
    assert [item.policy_excluded_cases for item in aggregates] == [1, 0, 0]
    assert [item.validator_pass_rate for item in aggregates] == [1.0, 1.0, 1.0]
    assert [item.failure_rate for item in aggregates] == [0.0, 0.0, 0.0]
    assert all(item.live_duration_ms is None for item in aggregates)
    with pytest.raises(ValidationError, match="all strategies"):
        PolicyComparisonReport.model_validate(
            report.model_dump() | {"outcomes": report.outcomes[:2]}
        )
    recall_steps = sorted(
        (
            TrajectoryStep.model_validate_json(row.step_json)
            for row in rows
            if row.episode_id == report.outcomes[2].episode_id
        ),
        key=lambda item: item.sequence_number,
    )
    assert [(item.agent_name, item.agent_call_number) for item in recall_steps] == [
        ("admet", 1),
        ("decision", 1),
        ("admet", 2),
        ("decision", 2),
    ]
    assert recall_steps[2].triggering_step_id == recall_steps[1].step_id
    assert recall_steps[3].triggering_step_id == recall_steps[2].step_id


@pytest.mark.asyncio
async def test_failed_source_remains_in_comparison_denominator(database: Database) -> None:
    factory, analysis_id = database
    admet_id, ctox_id, admet_provider, ctox_provider = await snapshots(
        factory, analysis_id, admet_failure=True
    )
    async with factory() as session:
        report = await PolicyComparisonRunner(session).run(manifest(analysis_id, admet_id, ctox_id))
    assert [item.failed_observations for item in report.outcomes] == [1, 1, 1]
    assert [item.failure_rate for item in aggregate_reports((report,))] == [1.0, 0.5, 0.5]
    assert all(item.validation_verdict == "accepted" for item in report.outcomes)
    assert admet_provider.calls == 1 and ctox_provider.calls == 1


@pytest.mark.asyncio
async def test_changed_case_input_records_replay_miss_without_live_fallback(
    database: Database,
) -> None:
    factory, analysis_id = database
    admet_id, ctox_id, admet_provider, ctox_provider = await snapshots(factory, analysis_id)
    async with factory() as session:
        original = await PolicyComparisonRunner(session).run(
            manifest(analysis_id, admet_id, ctox_id)
        )
        analysis = await session.get(AnalysisRecord, analysis_id)
        assert analysis is not None
        analysis.canonical_smiles = "CCC"
        await session.commit()
        report = await PolicyComparisonRunner(session).run(manifest(analysis_id, admet_id, ctox_id))
    assert [item.replay_misses for item in report.outcomes] == [1, 2, 2]
    assert [item.failed_observations for item in report.outcomes] == [1, 2, 2]
    assert [item.unresolved_gap_count for item in report.outcomes] == [1, 1, 1]
    assert all(item.validation_verdict == "accepted" for item in report.outcomes)
    assert report.case_fingerprint != original.case_fingerprint
    assert admet_provider.calls == 1 and ctox_provider.calls == 1


@pytest.mark.asyncio
async def test_missing_or_tampered_snapshot_aborts_before_episode(database: Database) -> None:
    factory, analysis_id = database
    admet_id, ctox_id, _, _ = await snapshots(factory, analysis_id)
    async with factory() as session:
        with pytest.raises(PolicyComparisonError, match="comparison_snapshot_missing"):
            await PolicyComparisonRunner(session).run(manifest(analysis_id, uuid4(), ctox_id))
        row = await session.get(AdmetObservationSnapshotRecord, admet_id)
        assert row is not None
        row.manifest_sha256 = "0" * 64
        await session.commit()
        with pytest.raises(Exception, match="hash mismatch"):
            await PolicyComparisonRunner(session).run(manifest(analysis_id, admet_id, ctox_id))


@pytest.mark.asyncio
async def test_changed_source_result_aborts_before_new_episode(database: Database) -> None:
    factory, analysis_id = database
    admet_id, ctox_id, _, _ = await snapshots(factory, analysis_id)
    async with factory() as session:
        prediction = await session.scalar(select(AdmetPredictionRecord).limit(1))
        assert prediction is not None
        prediction.value += 0.01
        await session.commit()
        with pytest.raises(Exception, match="snapshot source changed"):
            await PolicyComparisonRunner(session).run(manifest(analysis_id, admet_id, ctox_id))
        count = await session.scalar(select(func.count(TrajectoryEpisodeRecord.episode_id)))
    assert count == 0


@pytest.mark.asyncio
async def test_rejected_source_admission_cannot_enter_comparison(database: Database) -> None:
    factory, analysis_id = database
    admet_id, ctox_id, _, _ = await snapshots(factory, analysis_id)
    async with factory() as session:
        snapshot = await AdmetSnapshotRepository(session).load(admet_id)
        assert snapshot is not None
        admission = await session.scalar(
            select(ToolAdmissionRecord).where(
                ToolAdmissionRecord.tool_call_id == snapshot.entries[0].source_tool_call_id
            )
        )
        assert admission is not None
        admission.decision = "rejected"
        admission.reason_code = "fixture_denial"
        await session.commit()
        with pytest.raises(Exception, match="not approved"):
            await PolicyComparisonRunner(session).run(manifest(analysis_id, admet_id, ctox_id))
        count = await session.scalar(select(func.count(TrajectoryEpisodeRecord.episode_id)))
    assert count == 0


@pytest.mark.asyncio
async def test_repeated_policy_run_has_same_steps_except_generated_ids(database: Database) -> None:
    factory, analysis_id = database
    admet_id, ctox_id, _, _ = await snapshots(factory, analysis_id)
    async with factory() as session:
        runner = PolicyComparisonRunner(session)
        first = await runner.run(manifest(analysis_id, admet_id, ctox_id))
        second = await runner.run(manifest(analysis_id, admet_id, ctox_id))
        rows = tuple(await session.scalars(select(TrajectoryStepRecord)))

    def normalized(episode_id: UUID) -> tuple[dict[str, object], ...]:
        steps = sorted(
            (
                TrajectoryStep.model_validate_json(row.step_json)
                for row in rows
                if row.episode_id == episode_id
            ),
            key=lambda item: item.sequence_number,
        )
        return tuple(
            step.model_dump(exclude={"episode_id", "step_id", "triggering_step_id"})
            for step in steps
        )

    assert [normalized(item.episode_id) for item in first.outcomes] == [
        normalized(item.episode_id) for item in second.outcomes
    ]
